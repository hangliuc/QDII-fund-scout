#!/usr/bin/env python3
"""QDII-fund-scout 本地 Web UI 后端服务"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import warnings
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

warnings.filterwarnings("ignore", message=".*OpenSSL.*")
logging.basicConfig(level=logging.ERROR, format="%(message)s")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
SCRIPTS_DIR = os.path.join(PROJECT_DIR, "scripts")
CONFIG_DIR = os.path.expanduser("~/.fund-scout")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

sys.path.insert(0, SCRIPTS_DIR)

PORT = int(os.environ.get("FUND_UI_PORT", "8765"))

_last_result = None
_last_codes: set = set()
_state_lock = threading.Lock()


def _load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {"my_funds": [], "push": {"feishu_webhook": "", "wechat_webhook": ""}, "defaults": {}}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _fund_to_row(fund) -> dict:
    name = fund.short_name or fund.name or "-"
    return {
        "code": fund.code,
        "name": name,
        "nav": fund.nav,
        "nav_date": fund.nav_date,
        "return_1y": fund.return_1y,
        "return_3y": fund.return_3y,
        "purchase_info": fund._purchase_info,
        "effectively_closed": fund.effectively_closed,
        "total_fee": fund.total_fee,
        "scale": fund.scale,
        "drawdown_1y": fund.drawdown_1y,
        "manager_name": fund.manager_name,
        "market_top3": fund.market_top3 or "",
        "t1_prediction": fund._t1_prediction or {},
    }


def _run_query(codes: list[str], include_prediction: bool = False) -> dict:
    global _last_result, _last_codes
    from core.fetcher import FundFetcher
    fetcher = FundFetcher(rate_limit=0.3)
    result = fetcher.compare(
        codes=codes, cross_validate=True, include_csrc=True,
        include_prediction=include_prediction,
    )
    with _state_lock:
        _last_result = result
        _last_codes = set(codes)
    rows = [_fund_to_row(f) for f in result.funds]
    return {"funds": rows, "warnings": result._warnings or [],
            "update_date": result.update_date, "count": result.count}


def _run_prediction_only(codes: list[str]) -> dict:
    """单独跑 T-1 预测。返回 {code: t1_prediction} 字典。"""
    try:
        from core.predict_inline import predict_t1_batch
    except ImportError:
        return {}

    # 拿到基金的 main_code/short_name 信息（从最近 query 结果）
    targets: list[dict] = []
    with _state_lock:
        cached_result = _last_result
        cached_codes = set(_last_codes)
    if cached_result is not None and cached_codes == set(codes):
        for f in cached_result.funds:
            if f.data_unavailable:
                continue
            type_str = (f.type or "") + (f.name or "")
            if not any(kw in type_str for kw in ("QDII", "美元", "全球", "海外", "纳斯达克", "标普", "新兴市场")):
                continue
            targets.append({
                "code": f.code,
                "main_code": f.code,  # 这里没法从 BulkSnapshot 拿到主代码，用 C 码兜底
                "short_name": f.short_name or f.name,
            })
    else:
        targets = [{"code": c, "main_code": c, "short_name": ""} for c in codes]

    if not targets:
        return {}

    return predict_t1_batch(targets)


def _do_push(target: str, codes: list[str]) -> dict:
    try:
        cfg = _load_config()
        push_cfg = cfg.get("push", {})
        # 支持 "feishu,wechat" 多目标，逐个推送，全部成功才算成功
        targets = [t.strip() for t in target.split(",") if t.strip()]
        if not targets:
            return {"ok": False, "error": "未指定推送目标"}

        # 先校验每个目标都已配置 webhook
        for t in targets:
            url = push_cfg.get(f"{t}_webhook", "")
            if not url:
                return {"ok": False, "error": f"未配置 {t} Webhook，请先在输入框中填写并保存"}

        with _state_lock:
            cached_result = _last_result
            cached_codes = set(_last_codes)
        if cached_result is not None and cached_codes == set(codes):
            result = cached_result
        else:
            from core.fetcher import FundFetcher
            fetcher = FundFetcher(rate_limit=0.3)
            # 推送场景: include_prediction=True 让卡片带"最新涨跌"
            result = fetcher.compare(
                codes=codes, cross_validate=False,
                include_csrc=True, include_prediction=True,
            )

        # 逐个推送，记录每个目标结果
        from adapters.feishu import FeishuAdapter
        from adapters.wechat import WechatAdapter
        results: dict[str, bool] = {}
        for t in targets:
            url = push_cfg.get(f"{t}_webhook", "")
            try:
                if t == "feishu":
                    adapter = FeishuAdapter(webhook_url=url)
                elif t == "wechat":
                    adapter = WechatAdapter(webhook_url=url)
                else:
                    results[t] = False
                    continue
                results[t] = adapter.send(result)
            except Exception:
                results[t] = False

        ok_targets = [t for t, ok in results.items() if ok]
        fail_targets = [t for t, ok in results.items() if not ok]
        if not fail_targets:
            return {"ok": True, "error": "", "ok_targets": ok_targets}
        return {
            "ok": False,
            "ok_targets": ok_targets,
            "fail_targets": fail_targets,
            "error": f"推送失败：{', '.join(fail_targets)}（请检查 Webhook 地址）",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


SCHEDULE_SCRIPT_PATH = os.path.join(CONFIG_DIR, "scheduled_push.sh")
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.fundscout.push.plist")


def _get_schedule_status() -> dict:
    from schedule_setup import status
    info = status()
    if info.get("unsupported"):
        return {"active": False, "unsupported": True}
    return info


def _setup_schedule(times_str: str, weekdays: str) -> dict:
    from schedule_setup import setup
    weekdays_only = (weekdays == "1-5")
    return setup(times_str, weekdays_only=weekdays_only, scripts_dir=SCRIPTS_DIR)


def _remove_schedule() -> dict:
    from schedule_setup import remove
    return remove()


def _cache_stats() -> dict:
    """季报本地缓存统计"""
    from core.sources.csrc_cache import CSRCCache
    cache = CSRCCache()
    return cache.cache_stats()


def _cache_action(action: str) -> dict:
    """季报缓存诊断操作"""
    try:
        if action == "refresh":
            from holdings_refresh import cmd_refresh, load_user_funds
            funds = load_user_funds()
            stats = cmd_refresh(funds, force=False)
            return {"ok": True, "stats": stats}
        if action == "force_refresh":
            from holdings_refresh import cmd_refresh, load_user_funds
            funds = load_user_funds()
            stats = cmd_refresh(funds, force=True)
            return {"ok": True, "stats": stats}
        if action == "clear_index":
            from core.sources.csrc_cache import CSRCCache
            n = CSRCCache().invalidate_all_indexes()
            return {"ok": True, "cleared": n}
        return {"ok": False, "error": f"未知操作: {action}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 基金清单 / 反查 / 搜索：动态读 references/qdii_fund_list.json，避免 HTML 硬编码
# ---------------------------------------------------------------------------

_FUND_LIST_PATH = os.path.join(PROJECT_DIR, "references", "qdii_fund_list.json")
_fund_list_cache: dict | None = None
_fund_list_lock = threading.Lock()


def _load_fund_list() -> dict:
    global _fund_list_cache
    with _fund_list_lock:
        if _fund_list_cache is not None:
            return _fund_list_cache
        if not os.path.exists(_FUND_LIST_PATH):
            _fund_list_cache = {"funds": [], "types": [], "count": 0, "update_date": ""}
            return _fund_list_cache
        try:
            with open(_FUND_LIST_PATH, "r", encoding="utf-8") as f:
                _fund_list_cache = json.load(f)
        except Exception:
            _fund_list_cache = {"funds": [], "types": [], "count": 0, "update_date": ""}
        return _fund_list_cache


def _fund_lookup_meta(code: str) -> dict | None:
    fl = _load_fund_list()
    for f in fl.get("funds", []):
        if f.get("code") == code:
            return f
    return None


def _categorize(name: str, ftype: str) -> list[str]:
    """给基金打分类标签（前端按 tag 筛选用）"""
    tags: list[str] = []
    n_upper = (name or "").upper()
    if "纳斯达克" in name or "NASDAQ" in n_upper:
        tags.append("纳指")
    if "标普" in name or "S&P" in n_upper:
        tags.append("标普")
    if any(k in name for k in ("信息技术", "信息科技", "科技", "互联网", "智能")):
        tags.append("科技")
    if "全球" in name or "海外" in name:
        tags.append("全球")
    if any(k in name for k in ("新兴市场", "印度", "越南", "亚太", "东南亚")):
        tags.append("新兴市场")
    if "恒生" in name or "港股" in name:
        tags.append("港股")
    if any(k in name for k in ("黄金", "原油", "石油", "REIT")) or "REIT" in n_upper:
        tags.append("商品/REIT")
    if "ETF" in n_upper or "联接" in name or ("指数" in name and "增强" not in name):
        tags.append("被动")
    elif "QDII" in (ftype or "") and "REIT" not in (ftype or ""):
        tags.append("主动")
    # 份额类别
    if name.endswith("C"):
        tags.append("C 类")
    elif name.endswith("A"):
        tags.append("A 类")
    return tags


def _public_fund_entry(f: dict) -> dict:
    return {
        "code": f.get("code", ""),
        "name": f.get("name", ""),
        "abbr": f.get("abbr", ""),
        "pinyin": f.get("pinyin", ""),
        "type": f.get("type", ""),
        "tags": _categorize(f.get("name", ""), f.get("type", "")),
    }


def _list_all_funds() -> dict:
    fl = _load_fund_list()
    funds = [_public_fund_entry(f) for f in fl.get("funds", [])]
    tag_set: set[str] = set()
    for f in funds:
        tag_set.update(f["tags"])
    funds.sort(key=lambda x: x["code"])
    return {
        "count": len(funds),
        "update_date": fl.get("update_date", ""),
        "types": fl.get("types", []),
        "tags": sorted(tag_set, key=lambda t: ("C 类" in t, "A 类" in t, t)),
        "funds": funds,
    }


def _lookup_fund(code: str) -> dict:
    """单只基金反查名称：先查本地 list，再 fallback 到天天基金全量清单"""
    code = (code or "").strip()
    if not code:
        return {"code": code, "name": "", "found": False}
    meta = _fund_lookup_meta(code)
    if meta:
        return {"code": code, "name": meta.get("name", ""),
                "type": meta.get("type", ""), "found": True}
    try:
        from core.sources.eastmoney import EastMoneySource
        em = EastMoneySource()
        all_funds = em.fetch_all_fund_codes()
        for c, abbr, name, ft, pinyin in all_funds:
            if c == code:
                return {"code": code, "name": name, "type": ft,
                        "found": True, "from": "eastmoney"}
    except Exception:
        pass
    return {"code": code, "name": "", "found": False}


def _search_funds(keyword: str, fund_type: str = "", limit: int = 50) -> dict:
    keyword = (keyword or "").strip()
    fund_type = (fund_type or "").strip()
    fl = _load_fund_list()
    results = []
    kw_upper = keyword.upper()
    for f in fl.get("funds", []):
        if fund_type and fund_type != f.get("type", ""):
            continue
        if keyword:
            name = f.get("name", "")
            code = f.get("code", "")
            pinyin = (f.get("pinyin", "") or "").upper()
            abbr = (f.get("abbr", "") or "").upper()
            tags = _categorize(name, f.get("type", ""))
            if (
                keyword not in name and keyword not in code
                and kw_upper not in pinyin and kw_upper not in abbr
                and not any(keyword in t for t in tags)
            ):
                continue
        results.append(_public_fund_entry(f))
        if len(results) >= limit:
            break
    return {"count": len(results), "funds": results}


class _Handler(BaseHTTPRequestHandler):

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._serve_index()
        elif path == "/api/config":
            self._send_json(_load_config())
        elif path == "/api/schedule":
            self._send_json(_get_schedule_status())
        elif path == "/api/cache":
            self._send_json(_cache_stats())
        elif path == "/api/funds/list":
            # 全部 QDII 基金清单（用于"添加基金"弹层）
            self._send_json(_list_all_funds())
        elif path == "/api/funds/lookup":
            # 单只反查名称：?code=xxxxxx
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("code", [""])[0] or "").strip()
            self._send_json(_lookup_fund(code))
        elif path == "/api/funds/search":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            keyword = (qs.get("q", [""])[0] or "").strip()
            fund_type = (qs.get("type", [""])[0] or "").strip()
            limit = int(qs.get("limit", ["50"])[0] or 50)
            self._send_json(_search_funds(keyword, fund_type, limit))
        elif path == "/app.js":
            self._serve_static("app.js", "application/javascript; charset=utf-8")
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/config":
            cfg = _load_config()
            if "my_funds" in body:
                cfg["my_funds"] = body["my_funds"]
            if "push" in body:
                push = cfg.setdefault("push", {})
                for k in ("feishu_webhook", "wechat_webhook"):
                    if k in body["push"]:
                        push[k] = body["push"][k]
            _save_config(cfg)
            self._send_json({"ok": True, "config": cfg})

        elif path == "/api/query":
            codes = body.get("codes", [])
            if not codes:
                cfg = _load_config()
                codes = [f["code"] for f in cfg.get("my_funds", []) if f.get("code")]
            if not codes:
                self._send_json({"error": "请先添加基金代码", "funds": [], "warnings": ["未配置基金列表"]})
                return
            try:
                # 默认不带预测：先把基础数据快速返回，前端再调 /api/predict 异步补预测
                with_pred = bool(body.get("with_prediction", False))
                result = _run_query(codes, include_prediction=with_pred)
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e), "funds": [], "warnings": [f"查询失败: {e}"]})

        elif path == "/api/predict":
            codes = body.get("codes", [])
            if not codes:
                cfg = _load_config()
                codes = [f["code"] for f in cfg.get("my_funds", []) if f.get("code")]
            if not codes:
                self._send_json({"predictions": {}})
                return
            # 长操作（拉 yfinance 行情）用线程池设超时
            result = [None]
            t = threading.Thread(
                target=lambda: result.__setitem__(0, _run_prediction_only(codes)),
                daemon=True,
            )
            t.start()
            t.join(timeout=60)
            if result[0] is not None:
                self._send_json({"predictions": result[0]})
            else:
                self._send_json({"predictions": {}, "error": "预测超时"})

        elif path == "/api/push":
            target = body.get("target", "")
            codes = body.get("codes", [])
            if not codes:
                cfg = _load_config()
                codes = [f["code"] for f in cfg.get("my_funds", []) if f.get("code")]
            if not codes:
                self._send_json({"ok": False, "error": "无基金代码"})
                return
            result = [None]
            t = threading.Thread(target=lambda: result.__setitem__(0, _do_push(target, codes)), daemon=True)
            t.start()
            t.join(timeout=30)
            if result[0] is not None:
                self._send_json(result[0])
            else:
                self._send_json({"ok": False, "error": "推送超时，请检查 Webhook 地址是否正确"})

        elif path == "/api/test-webhook":
            target = body.get("type", "")
            url = body.get("url", "")
            if target == "feishu":
                from adapters.feishu import FeishuAdapter
                a = FeishuAdapter(webhook_url=url)
                ok = a.test_connection()
            elif target == "wechat":
                from adapters.wechat import WechatAdapter
                a = WechatAdapter(webhook_url=url)
                ok = a.test_connection()
            else:
                self._send_json({"ok": False, "error": f"未知类型: {target}"})
                return
            self._send_json({"ok": ok, "error": "" if ok else "连接失败，请检查地址"})

        elif path == "/api/schedule":
            action = body.get("action", "")
            if action == "setup":
                times_str = body.get("times", "")
                weekdays = body.get("weekdays", "*")
                if not times_str:
                    self._send_json({"ok": False, "error": "缺少推送时间"})
                    return
                result = _setup_schedule(times_str, weekdays)
                self._send_json(result)
            elif action == "remove":
                result = _remove_schedule()
                self._send_json(result)
            else:
                self._send_json({"ok": False, "error": "未知操作"})

        elif path == "/api/cache":
            action = body.get("action", "")
            # 长操作（refresh / force_refresh）放到线程并设超时
            result = [None]
            t = threading.Thread(
                target=lambda: result.__setitem__(0, _cache_action(action)),
                daemon=True,
            )
            t.start()
            t.join(timeout=120)
            if result[0] is not None:
                self._send_json(result[0])
            else:
                self._send_json({"ok": False, "error": "操作超时（>120 秒），请检查网络后重试"})

        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_index(self) -> None:
        self._serve_static("index.html", "text/html; charset=utf-8")

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = os.path.join(SCRIPT_DIR, filename)
        if not os.path.exists(path):
            self._send_json({"error": f"{filename} not found"}, 404)
            return
        with open(path, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    import socket

    class ReuseHTTPServer(HTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True

    try:
        server = ReuseHTTPServer(("0.0.0.0", PORT), _Handler)
    except OSError as e:
        if "Address already in use" in str(e) or e.errno == 48:
            import subprocess
            try:
                result = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True, timeout=3)
                pid = result.stdout.strip().split("\n")[0] if result.stdout.strip() else ""
                if pid:
                    subprocess.run(["kill", pid], timeout=3)
                    import time; time.sleep(0.5)
                    server = ReuseHTTPServer(("0.0.0.0", PORT), _Handler)
                else:
                    raise
            except Exception:
                print(f"\n  端口 {PORT} 被占用，请先关闭占用进程：")
                print(f"  lsof -ti :{PORT} | xargs kill")
                print(f"  或换个端口：FUND_UI_PORT=8766 bash run.sh\n")
                return
        else:
            raise

    print(f"\n  QDII-fund-scout 本地配置页面")
    print(f"  打开浏览器访问：")
    print(f"  → http://localhost:{PORT}")
    print(f"\n  按 Ctrl+C 停止服务。\n")

    # 后台预热：① BulkSnapshot 全市场快照 ② CSRC 季报索引
    # 用户打开页面 / 第一次查询时这些数据通常已就绪。
    def _warm_up():
        try:
            from core.fetcher import FundFetcher
            FundFetcher.warm_up()  # 拉 JJJZ + RANKING 全市场快照
        except Exception:
            pass
        try:
            from holdings_refresh import refresh_stale_in_background
            refresh_stale_in_background()
        except Exception:
            pass
    threading.Thread(target=_warm_up, daemon=True, name="warm-up").start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  服务已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
