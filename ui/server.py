#!/usr/bin/env python3
"""QDII-fund-scout 本地 Web UI 后端服务"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import subprocess
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
    }


def _run_query(codes: list[str]) -> dict:
    global _last_result, _last_codes
    from core.fetcher import FundFetcher
    fetcher = FundFetcher(rate_limit=0.3)
    result = fetcher.compare(codes=codes, cross_validate=True, include_csrc=True)
    _last_result = result
    _last_codes = set(codes)
    rows = [_fund_to_row(f) for f in result.funds]
    return {"funds": rows, "warnings": result._warnings or [], "update_date": result.update_date, "count": result.count}


def _do_push(target: str, codes: list[str]) -> dict:
    try:
        cfg = _load_config()
        push_cfg = cfg.get("push", {})
        url = push_cfg.get(f"{target}_webhook", "")
        if not url:
            return {"ok": False, "error": f"未配置 {target} Webhook，请先在输入框中填写并保存"}
        if _last_result and _last_codes == set(codes):
            result = _last_result
        else:
            from core.fetcher import FundFetcher
            fetcher = FundFetcher(rate_limit=0.3)
            result = fetcher.compare(codes=codes, cross_validate=False, include_csrc=False)
        if target == "feishu":
            from adapters.feishu import FeishuAdapter
            adapter = FeishuAdapter(webhook_url=url)
        else:
            from adapters.wechat import WechatAdapter
            adapter = WechatAdapter(webhook_url=url)
        ok = adapter.send(result)
        if not ok:
            return {"ok": False, "error": f"推送失败，请检查{target} Webhook地址是否正确"}
        return {"ok": ok, "error": ""}
    except Exception as e:
        return {"ok": False, "error": str(e)}


SCHEDULE_SCRIPT_PATH = os.path.join(CONFIG_DIR, "scheduled_push.sh")
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.fundscout.push.plist")


def _get_schedule_status() -> dict:
    is_mac = platform.system() == "Darwin"
    is_linux = platform.system() == "Linux"
    if is_mac:
        if not os.path.exists(PLIST_PATH):
            return {"active": False}
        try:
            r = subprocess.run(["launchctl", "list", "com.fundscout.push"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                import plistlib
                with open(PLIST_PATH, "rb") as f:
                    plist = plistlib.load(f)
                intervals = plist.get("StartCalendarInterval", [])
                times = []
                for iv in (intervals if isinstance(intervals, list) else [intervals]):
                    h = iv.get("Hour", 0)
                    m = iv.get("Minute", 0)
                    t = f"{h:02d}:{m:02d}"
                    if t not in times:
                        times.append(t)
                weekdays = any(iv.get("Weekday", 0) != 0 for iv in (intervals if isinstance(intervals, list) else [intervals]))
                return {"active": True, "times": times, "weekdays": weekdays}
            return {"active": False}
        except Exception:
            return {"active": False}
    elif is_linux:
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    if "scheduled_push.sh" in line:
                        parts = line.split()
                        if len(parts) >= 5:
                            return {"active": True, "cron": " ".join(parts[:5])}
            return {"active": False}
        except Exception:
            return {"active": False}
    return {"active": False, "unsupported": True}


def _setup_schedule(times_str: str, weekdays: str) -> dict:
    parsed_times = []
    for t in times_str.split(","):
        t = t.strip()
        m = __import__("re").match(r"^(\d{1,2}):(\d{2})$", t)
        if not m:
            return {"ok": False, "error": f"时间格式错误: {t}"}
        parsed_times.append((int(m.group(1)), int(m.group(2))))

    if not parsed_times:
        return {"ok": False, "error": "未提供推送时间"}

    label = ("工作日" if weekdays == "1-5" else "每天") + " " + "、".join(f"{h:02d}:{m:02d}" for h, m in parsed_times)

    os.makedirs(CONFIG_DIR, exist_ok=True)
    script_content = f"""#!/bin/bash
# QDII-fund-scout 定时推送脚本（由 Web UI 自动生成）
CONFIG_FILE="{CONFIG_FILE}"
SCRIPT_DIR="{SCRIPTS_DIR}"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[$(date)] 配置文件不存在，跳过" >> "{CONFIG_DIR}/schedule.log"
    exit 1
fi

cd "$SCRIPT_DIR"
python3 cli.py compare --config "$CONFIG_FILE" --push feishu,wechat >> "{CONFIG_DIR}/schedule.log" 2>&1
echo "[$(date)] 推送完成" >> "{CONFIG_DIR}/schedule.log"
"""
    with open(SCHEDULE_SCRIPT_PATH, "w") as f:
        f.write(script_content)
    os.chmod(SCHEDULE_SCRIPT_PATH, 0o755)

    is_mac = platform.system() == "Darwin"
    is_linux = platform.system() == "Linux"

    if is_mac:
        import plistlib
        intervals = []
        for h, m in parsed_times:
            if weekdays == "*":
                intervals.append({"Hour": h, "Minute": m})
            else:
                for d in range(1, 6):
                    intervals.append({"Hour": h, "Minute": m, "Weekday": d})

        plist = {
            "Label": "com.fundscout.push",
            "ProgramArguments": ["/bin/bash", SCHEDULE_SCRIPT_PATH],
            "StartCalendarInterval": intervals,
            "StandardOutPath": os.path.join(CONFIG_DIR, "schedule.log"),
            "StandardErrorPath": os.path.join(CONFIG_DIR, "schedule.log"),
            "EnvironmentVariables": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
        }
        os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
        subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True, timeout=5)
        with open(PLIST_PATH, "wb") as f:
            plistlib.dump(plist, f)
        subprocess.run(["launchctl", "load", PLIST_PATH], capture_output=True, timeout=5)
        return {"ok": True, "label": label}

    elif is_linux:
        cron_parts = []
        for h, m in parsed_times:
            cron_parts.append(f"{m} {h}")
        cron_expr = ",".join(cron_parts) + f" * * {weekdays}"
        existing = ""
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                existing = "\n".join(l for l in r.stdout.splitlines() if "scheduled_push.sh" not in l)
        except Exception:
            pass
        new_cron = f"{existing}\n{cron_expr} bash {SCHEDULE_SCRIPT_PATH}\n"
        subprocess.run(["crontab", "-"], input=new_cron, text=True, capture_output=True, timeout=5)
        return {"ok": True, "label": label}

    return {"ok": False, "error": "当前系统不支持定时任务设置"}


def _remove_schedule() -> dict:
    is_mac = platform.system() == "Darwin"
    is_linux = platform.system() == "Linux"

    if is_mac:
        if os.path.exists(PLIST_PATH):
            subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True, timeout=5)
            os.remove(PLIST_PATH)
        if os.path.exists(SCHEDULE_SCRIPT_PATH):
            os.remove(SCHEDULE_SCRIPT_PATH)
        return {"ok": True}
    elif is_linux:
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                new_cron = "\n".join(l for l in r.stdout.splitlines() if "scheduled_push.sh" not in l)
                subprocess.run(["crontab", "-"], input=new_cron + "\n", text=True, capture_output=True, timeout=5)
        except Exception:
            pass
        if os.path.exists(SCHEDULE_SCRIPT_PATH):
            os.remove(SCHEDULE_SCRIPT_PATH)
        return {"ok": True}
    return {"ok": False, "error": "当前系统不支持定时任务设置"}


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
                result = _run_query(codes)
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e), "funds": [], "warnings": [f"查询失败: {e}"]})

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

        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_index(self) -> None:
        index_path = os.path.join(SCRIPT_DIR, "index.html")
        if not os.path.exists(index_path):
            self._send_json({"error": "index.html not found"}, 500)
            return
        with open(index_path, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), _Handler)
    print(f"\n  QDII-fund-scout 本地配置页面")
    print(f"  打开浏览器访问：")
    print(f"  → http://localhost:{PORT}")
    print(f"\n  按 Ctrl+C 停止服务。\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  服务已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
