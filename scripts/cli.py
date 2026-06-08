# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

# 压制内部库的杂音告警，用户只看我们显式 print 的内容
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
logging.basicConfig(level=logging.ERROR, format="%(message)s")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.models import FundInfo, FundDataResult
from core.fetcher import FundFetcher as CoreFetcher, _build_purchase_info
from core.sources.eastmoney import EastMoneySource
from core.sources.eastmoney_bulk import BulkSnapshot
from core.sources.csrc import CSRCSource
from core.sources.base import SourceError
from core.validate import validate_data, print_report
from adapters import get_adapter, list_adapters


CONFIG_DIR = os.path.expanduser("~/.fund-scout")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def _load_config(path: str | None = None) -> dict:
    p = path or CONFIG_PATH
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(cfg: dict, path: str | None = None) -> None:
    p = path or CONFIG_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # 原子写：先写临时文件再 rename
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _ensure_config_dir() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# funds 子命令：读写 ~/.fund-scout/config.json 中的 my_funds 列表
# 设计目的：让 AI Agent 能用自然语言维护用户的基金列表，
# 用户和浏览器界面共享同一份持久化配置。
# ---------------------------------------------------------------------------

def _parse_fund_token(token: str) -> tuple[str, str]:
    """把 '012870' / '012870:易方达纳指100C' / '012870 易方达纳指100C' 解析成 (code, name)"""
    token = token.strip()
    if not token:
        return ("", "")
    # 支持 ':' 和空格作为分隔符
    for sep in (":", " ", "\t"):
        if sep in token:
            parts = token.split(sep, 1)
            return (parts[0].strip(), parts[1].strip())
    return (token, "")


def _validate_fund_code(code: str) -> str:
    """校验基金代码是否合法（6 位数字）"""
    code = code.strip()
    if not re.match(r"^\d{6}$", code):
        return f"基金代码必须是 6 位数字: {code!r}"
    return ""


def cmd_funds(args: argparse.Namespace) -> None:
    cfg = _load_config()
    funds = cfg.setdefault("my_funds", [])
    by_code = {f["code"]: f for f in funds if f.get("code")}

    action = args.action

    if action == "list":
        if args.format == "json":
            print(json.dumps(funds, ensure_ascii=False, indent=2))
        else:
            if not funds:
                print("（基金列表为空）")
                print(f"配置文件: {CONFIG_PATH}")
                return
            print(f"已保存 {len(funds)} 只基金:\n")
            for i, f in enumerate(funds, 1):
                print(f"  {i:>2}. {f.get('code', ''):<8s} {f.get('name', '')}")
            print(f"\n配置文件: {CONFIG_PATH}")
        return

    if action == "add":
        if not args.tokens:
            print("❌ 请提供至少一个基金代码")
            sys.exit(1)
        added: list[dict] = []
        skipped: list[str] = []
        invalid: list[str] = []
        for token in args.tokens:
            code, name = _parse_fund_token(token)
            err = _validate_fund_code(code)
            if err:
                invalid.append(err)
                continue
            if code in by_code:
                # 已存在：name 非空时更新名称
                if name and by_code[code].get("name") != name:
                    by_code[code]["name"] = name
                    added.append({"code": code, "name": name, "updated": True})
                else:
                    skipped.append(code)
                continue
            entry = {"code": code}
            if name:
                entry["name"] = name
            funds.append(entry)
            by_code[code] = entry
            added.append(entry)

        if added or skipped or invalid:
            _save_config(cfg)

        if args.format == "json":
            print(json.dumps({
                "added": added, "skipped": skipped, "invalid": invalid,
                "total": len(funds),
            }, ensure_ascii=False, indent=2))
        else:
            for item in added:
                tag = "更新" if item.get("updated") else "已添加"
                name_part = f" ({item['name']})" if item.get("name") else ""
                print(f"✓ {tag}: {item['code']}{name_part}")
            for code in skipped:
                print(f"· 已存在，跳过: {code}")
            for err in invalid:
                print(f"✗ {err}")
            print(f"\n当前共 {len(funds)} 只基金")
        return

    if action == "remove":
        if not args.tokens:
            print("❌ 请提供至少一个基金代码")
            sys.exit(1)
        removed: list[str] = []
        not_found: list[str] = []
        for token in args.tokens:
            code = token.strip()
            if code in by_code:
                funds[:] = [f for f in funds if f.get("code") != code]
                del by_code[code]
                removed.append(code)
            else:
                not_found.append(code)
        if removed:
            _save_config(cfg)
        if args.format == "json":
            print(json.dumps({
                "removed": removed, "not_found": not_found, "total": len(funds),
            }, ensure_ascii=False, indent=2))
        else:
            for code in removed:
                print(f"✓ 已移除: {code}")
            for code in not_found:
                print(f"· 不在列表中: {code}")
            print(f"\n当前共 {len(funds)} 只基金")
        return

    if action == "clear":
        if not args.yes:
            print("⚠ 此操作会清空所有基金。请加 --yes 确认。")
            sys.exit(1)
        n = len(funds)
        cfg["my_funds"] = []
        _save_config(cfg)
        if args.format == "json":
            print(json.dumps({"cleared": n}, ensure_ascii=False))
        else:
            print(f"✓ 已清空 {n} 只基金")
        return

    print(f"❌ 未知操作: {action}")
    sys.exit(1)


class FundFetcher:
    def __init__(self):
        self.em = EastMoneySource()
        self.csrc = CSRCSource()
        # 全市场快照（2 次 HTTP 搞定所有基金的核心数据）
        self._bulk = BulkSnapshot()

    def fetch_detail(self, code: str, holdings: bool = False, csrc: bool = False) -> FundInfo:
        try:
            info = self.em.fetch_detail(code)
        except (SourceError, Exception) as e:
            print(f"  ! 天天基金获取 {code} 失败: {e}")
            return FundInfo(code=code, data_source="unavailable", data_unavailable=True)

        info._purchase_info = _build_purchase_info(
            info.purchase_status, info.purchase_limit, info.effectively_closed
        )
        info.market_top3 = CoreFetcher._compute_market_top3(None, info)

        if holdings:
            year = time.localtime().tm_year
            try:
                quarters = self.em._fetch_holdings(code, year)
                if quarters:
                    info.top10_holdings = quarters[0].get("stocks", [])
            except Exception as e:
                print(f"  ! 获取持仓失败: {e}")
        if csrc:
            try:
                info.market_distribution = self.csrc.fetch_market_distribution(
                    code, info.short_name or info.name
                )
            except Exception as e:
                print(f"  ! 获取CSRC数据失败: {e}")
        return info

    def fetch_batch(self, codes: list[str], include_prediction: bool = False) -> list[FundInfo]:
        """批量获取基金数据。

        主路径: BulkSnapshot (2 次 HTTP 拉全市场快照)
        补充: 并行从档案页 + NAV API 拿 scale/fee/drawdown
        降级: 逐只 HTML 详情页（仅当 Bulk 全部失败时）
        """
        try:
            results = self._bulk.get_batch(codes)
        except Exception as e:
            print(f"  ! 快照接口失败({e})，降级到逐只抓取")
            results = self.em.fetch_batch(codes)

        # 兜底：快照中找不到的基金（新基金/退市等），逐只从 HTML 补
        missing = [f for f in results if f.data_unavailable]
        if missing:
            missing_codes = [f.code for f in missing]
            try:
                fallback = self.em.fetch_batch(missing_codes)
                fb_map = {f.code: f for f in fallback if not f.data_unavailable}
                results = [fb_map.get(f.code, f) if f.data_unavailable else f for f in results]
            except Exception:
                pass  # HTML 也失败就保持 unavailable

        # 并行补充 scale/fee/drawdown（每只 ~1s，并行后总耗时 ~1-2s）
        enrich_targets = [f for f in results if not f.data_unavailable]
        if enrich_targets:
            with ThreadPoolExecutor(max_workers=min(len(enrich_targets), 6)) as ex:
                futs = {ex.submit(BulkSnapshot.enrich_fund, f): f for f in enrich_targets}
                for fut in as_completed(futs):
                    try:
                        fut.result(timeout=15)
                    except Exception:
                        pass

        for info in results:
            if not info.data_unavailable:
                info._purchase_info = _build_purchase_info(
                    info.purchase_status, info.purchase_limit, info.effectively_closed
                )
                info.market_top3 = CoreFetcher._compute_market_top3(None, info)

        # CSRC 季报地区分布
        csrc_funds = [f for f in results if not f.data_unavailable and not f.market_top3]
        if csrc_funds:
            with ThreadPoolExecutor(max_workers=4) as ex:
                fut_map = {ex.submit(self.csrc.fetch_market_distribution, f.code, f.short_name or f.name): f for f in csrc_funds}
                for fut in as_completed(fut_map):
                    f = fut_map[fut]
                    try:
                        dist = fut.result()
                        f.market_distribution = dist
                        f.market_top3 = CoreFetcher._compute_market_top3(dist, f)
                    except Exception:
                        pass

        # T-1 估值预测
        if include_prediction:
            self._enrich_predictions(results)

        return results

    @staticmethod
    def _enrich_predictions(funds: list[FundInfo]) -> None:
        """并行为 QDII 基金计算最新涨跌"""
        try:
            from core.predict_inline import predict_t1_for_fund
        except ImportError:
            return

        targets = []
        for f in funds:
            if f.data_unavailable:
                continue
            type_str = (f.type or "") + (f.name or "")
            if any(k in type_str for k in ("QDII", "美元", "全球", "海外", "纳斯达克", "标普", "新兴市场", "高端制造")):
                targets.append(f)
        if not targets:
            return

        with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as ex:
            fut_map = {
                ex.submit(predict_t1_for_fund, f.code, f.code, f.short_name or f.name): f
                for f in targets
            }
            for fut in as_completed(fut_map):
                f = fut_map[fut]
                try:
                    f._t1_prediction = fut.result(timeout=30)
                except Exception:
                    f._t1_prediction = {}

    def search(self, keyword: str, fund_type: str = "") -> list[dict]:
        return self.em.search_funds(keyword, fund_type)


def _format_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _format_csv(data: dict) -> str:
    funds = data.get("funds", [])
    if not funds:
        return ""
    # 收集所有 fund 的 keys 并集，避免后续 fund 多出字段时 DictWriter 抛异常
    fieldnames = sorted({k for f in funds for k in f.keys()})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for f in funds:
        row = {}
        for k in fieldnames:
            v = f.get(k, "")
            if isinstance(v, (dict, list)):
                row[k] = json.dumps(v, ensure_ascii=False)
            else:
                row[k] = v
        writer.writerow(row)
    return buf.getvalue()



def _format_md(data: dict, style: str = "table") -> str:
    funds = data.get("funds", [])
    if not funds:
        return ""

    if style == "summary":
        lines = []
        for f in funds:
            name = f.get("name", "")
            code = f.get("code", "")
            ret = f.get("return_1y", "")
            purchase_info = f.get("purchase_info", "") or f.get("purchase_status", "")
            lines.append(f"- **{name}** ({code})  近1年: {ret}  申购: {purchase_info}")
        return "\n".join(lines)

    if style == "card":
        blocks = []
        safe_hide = {
            "purchase_status", "purchase_limit", "data_unavailable", "_purchase_info",
            # 隐藏内部字段, 包括 T-1 估值预测（控制台保持简洁，估算只在推送渠道展示）
            "_t1_prediction", "t1_prediction",
            "_cross_validation", "_cross_resolved", "_cross_validated",
        }
        for f in funds:
            lines = [f"### {f.get('name', '')} ({f.get('code', '')})"]
            for k, v in f.items():
                if k in ("name", "code") or v is None or v == "" or v == [] or v == {}:
                    continue
                if k in safe_hide:
                    continue
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                lines.append(f"- **{k}**: {v}")
            blocks.append("\n".join(lines))
        return "\n\n---\n\n".join(blocks)

    if style == "table":
        return _format_rich_table(funds)

    return ""


TABLE_COLS = [
    ("name", "名称", 20, 26),
    ("code", "代码", 6, 8),
    ("latest_change", "最新涨跌", 12, 16),
    ("return_1y", "近1年", 8, 10),
    ("drawdown_1y", "近一年回撤", 12, 14),
    ("scale", "规模(亿)", 8, 10),
    ("total_fee", "费率%", 7, 8),
    ("purchase_info", "申购状态", 18, 20),
    ("market_top3", "市场投资TOP3", 30, 40),
]


_RET_FIELDS = {"return_1y", "return_1m", "return_3m", "return_6m", "return_3y"}


def _fmt_pct(v) -> str:
    try:
        n = float(v)
        if n > 0:
            return f"+{n:.2f}%"
        return f"{n:.2f}%"
    except (ValueError, TypeError):
        return str(v) if v else "-"


def _dw(s: str) -> int:
    w = 0
    for ch in s:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF:
            w += 2
        else:
            w += 1
    return w


def _crop(s: str, limit: int) -> str:
    if _dw(s) <= limit:
        return s
    out = []
    w = 0
    for ch in s:
        cw = 2 if (0x4E00 <= ord(ch) <= 0x9FFF) else 1
        if w + cw > limit - 2:
            break
        out.append(ch)
        w += cw
    return "".join(out) + ".."


def _pad(s: str, width: int, align: str = "l") -> str:
    pad = width - _dw(s)
    if pad <= 0:
        return _crop(s, width)
    if align == "r":
        return " " * pad + s
    if align == "c":
        l = pad // 2
        r = pad - l
        return " " * l + s + " " * r
    return s + " " * pad


def _sep_line(cols: list[int], left: str, sep: str, right: str, fill: str = "─") -> str:
    return left + sep.join(fill * w for w in cols) + right


def _format_rich_table(funds: list[dict]) -> str:
    # 动态表头：根据数据判断"最新涨跌"是真值还是估算
    preds = [(f.get("t1_prediction") or {}) for f in funds]
    has_estimate = any(p.get("is_estimate") for p in preds if p)
    has_published = any(
        (p.get("value") is not None and not p.get("is_estimate"))
        for p in preds
    )
    if has_estimate and has_published:
        change_label = "最新涨跌(部分估算)"
    elif has_estimate:
        change_label = "最新涨跌(估算)"
    elif has_published:
        change_label = "最新涨跌(已公布)"
    else:
        change_label = "最新涨跌"

    cols_with_label = [
        (k, change_label if k == "latest_change" else lbl, mn, mx)
        for k, lbl, mn, mx in TABLE_COLS
    ]

    cols = []
    for key, label, min_w, max_w in cols_with_label:
        vals = [_fmt_val(f, key) for f in funds] + [label]
        data_w = max(_dw(v) for v in vals)
        cols.append(min(max(data_w + 2, min_w), max_w))

    top_s = _sep_line(cols, "┌", "┬", "┐")
    sep_s = _sep_line(cols, "├", "┼", "┤")
    bot_s = _sep_line(cols, "└", "┴", "┘")

    hdr = "│" + "│".join(" " + _pad(label, cols[i] - 2) + " " for i, (_, label, _, _) in enumerate(cols_with_label)) + "│"

    rows = [top_s, hdr, sep_s]
    for idx, f in enumerate(funds):
        cells = []
        for i, (key, _, _, _) in enumerate(TABLE_COLS):
            w = cols[i] - 2
            v = _fmt_val(f, key)
            if key == "name":
                v = _crop(v, w)
                cells.append(" " + _pad(v, w) + " ")
            else:
                v = _crop(v, w)
                cells.append(" " + _pad(v, w, "r") + " ")
        rows.append("│" + "│".join(cells) + "│")
        if idx < len(funds) - 1:
            rows.append(sep_s)
    rows.append(bot_s)
    return "\n".join(rows)


def _fmt_val(f: dict, key: str) -> str:
    if key in _RET_FIELDS:
        return _fmt_pct(f.get(key))
    if key == "latest_change":
        pred = f.get("t1_prediction") or f.get("_t1_prediction") or {}
        val = pred.get("value")
        nav_date = pred.get("date", "")
        is_est = pred.get("is_estimate", False)
        short_date = nav_date[5:] if len(nav_date) == 10 else nav_date
        if val is None:
            return "-"
        sign = "+" if val > 0 else ""
        suffix = "(估算)" if is_est else ""
        return f"{short_date} {sign}{val:.2f}%{suffix}"
    if key in ("drawdown_1y", "drawdown_3y"):
        v = f.get(key)
        if v is None:
            return "-"
        try:
            return f"{float(v)*100:.2f}%"
        except (ValueError, TypeError):
            return str(v)
    if key == "purchase_info":
        v = f.get("purchase_info", "") or f.get("purchase_status", "-")
    else:
        v = f.get(key, "")
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    if v is None or v == "None":
        v = "-"
    return str(v)


def _format_output(data: dict, fmt: str, style: str = "table") -> str:
    if fmt == "csv":
        return _format_csv(data)
    if fmt == "md":
        return _format_md(data, style=style)
    return _format_json(data)


def _write_output(content: str, output_dir: str, filename: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _push(result: FundDataResult, adapter_name: str) -> None:
    cls = get_adapter(adapter_name)
    adapter = cls()
    adapter.send(result)


def _build_result(funds: list[FundInfo]) -> FundDataResult:
    return FundDataResult(
        update_date=time.strftime("%Y-%m-%d"),
        count=len(funds),
        funds=funds,
    )


def cmd_detail(args: argparse.Namespace) -> None:
    fetcher = FundFetcher()
    info = fetcher.fetch_detail(args.code, holdings=args.holdings, csrc=args.csrc)
    # detail 输出 JSON 时也带上最新涨跌
    if args.format == "json":
        try:
            from core.predict_inline import predict_t1_for_fund
            info._t1_prediction = predict_t1_for_fund(
                info.code, info.code, info.short_name or info.name
            )
        except Exception:
            info._t1_prediction = {}
    result = _build_result([info])
    data = result.to_dict()

    validation = validate_data(data, profile="detail")
    data["_validation"] = validation.to_dict()
    data["_warnings"] = validation.warnings
    print_report(validation)

    content = _format_output(data, args.format)
    print(content)

    ext = {"json": "json", "csv": "csv", "md": "md"}[args.format]
    filename = f"{args.code}_detail.{ext}"
    path = _write_output(content, args.output, filename)
    print(f"\n已保存: {path}")

    if args.push:
        _push(result, args.push)


def cmd_compare(args: argparse.Namespace) -> None:
    config = {}
    if args.config:
        config = _load_config(args.config)
    elif not args.codes:
        config = _load_config()

    if config and not args.codes:
        my_funds = config.get("my_funds", [])
        codes = [f["code"] for f in my_funds if "code" in f]
        if not codes:
            print("❌ 配置文件中没有基金代码，请编辑 ~/.fund-scout/config.json")
            sys.exit(1)
    else:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    fetcher = FundFetcher()
    # 先决定是否要推送, 决定要不要计算预测
    push_targets = []
    if args.push:
        push_targets = [p.strip() for p in args.push.split(",")]
    else:
        push_cfg = config.get("push", {})
        if push_cfg.get("feishu_webhook"):
            push_targets.append("feishu")
        if push_cfg.get("wechat_webhook"):
            push_targets.append("wechat")

    # 何时计算 T-1 估值预测：
    # - 总是计算（CLI 表格也显示"最新涨跌"列）
    funds = fetcher.fetch_batch(codes, include_prediction=True)
    result = _build_result(funds)
    data = result.to_dict()

    # bulk 快照不含 scale/total_fee/drawdown，跳过校验（这些字段仅 detail 命令提供）
    data["_validation"] = {}
    data["_warnings"] = []

    fmt = args.format
    style = args.style
    content = _format_output(data, fmt, style=style)
    print(content)

    # push_targets 已在前面计算过了, 直接复用
    for target in push_targets:
        _push(result, target)


def cmd_search(args: argparse.Namespace) -> None:
    fetcher = FundFetcher()
    results = fetcher.search(args.keyword, fund_type=args.type or "")

    if args.cls:
        results = [r for r in results if args.cls in r.get("name", "")]

    # --with-limits / --details: 自动 follow up 调 compare 拿真实限额数据
    # 这样 agent 一条命令就能拿到完整字段，避免误把 search 清单当详情展示。
    if args.with_limits and results:
        codes = [r["code"] for r in results[:args.limit]]  # 不超过 limit 只
        try:
            funds = fetcher.fetch_batch(codes, include_prediction=False)
            # 用 compare 结果替换 search 清单（保留代码顺序）
            funds_dict = {f.code: f.to_dict() for f in funds}
            results = [funds_dict[c] for c in codes if c in funds_dict]
        except Exception as e:
            print(f"⚠ 拉取详情失败: {e}", file=sys.stderr)

    data = {
        "update_date": time.strftime("%Y-%m-%d"),
        "count": len(results),
        "funds": results,
    }

    content = _format_output(data, args.format)
    print(content)

    ext = {"json": "json", "csv": "csv", "md": "md"}[args.format]
    filename = f"search_{args.keyword}.{ext}"
    path = _write_output(content, args.output, filename)
    print(f"\n已保存: {path}")

    # search 只返回基金清单（不含详情字段），不支持推送
    if args.push:
        if not args.with_limits:
            print("⚠ search 命令默认不含详情，--push 不支持。先用 --with-limits 拉详情，或改用 compare。")
        else:
            print("⚠ search 命令不支持 --push，请用 compare 命令推送。")


def cmd_validate(args: argparse.Namespace) -> None:
    with open(args.file, "r", encoding="utf-8") as f:
        data = json.load(f)

    profile = args.profile or "compare"
    result = validate_data(data, profile=profile)
    print_report(result)

    if result.fatal_count > 0:
        sys.exit(1)


def cmd_test(args: argparse.Namespace) -> None:
    try:
        cls = get_adapter(args.adapter)
        adapter = cls()
        ok = adapter.test_connection()
        if ok:
            print(f"✅ {args.adapter} 连接正常")
        else:
            print(f"❌ {args.adapter} 连接失败")
            sys.exit(1)
    except KeyError as e:
        print(f"❌ {e}")
        sys.exit(1)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=["json", "csv", "md"], default="json")
    parser.add_argument("--push", default="", help="推送目标 (feishu/wechat/feishu,wechat)")
    parser.add_argument("--output", default=".")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fund-scout", description="基金数据获取与校验工具")
    sub = parser.add_subparsers(dest="command")

    p_detail = sub.add_parser("detail", help="单只基金详情")
    p_detail.add_argument("code", help="基金代码")
    p_detail.add_argument("--holdings", action="store_true", help="包含持仓")
    p_detail.add_argument("--csrc", action="store_true", help="包含证监会季报")
    _add_common_args(p_detail)
    p_detail.set_defaults(func=cmd_detail)

    p_compare = sub.add_parser("compare", help="批量对比")
    p_compare.add_argument("codes", nargs="?", default="", help="逗号分隔的基金代码")
    p_compare.add_argument("--config", default="", help="配置文件路径（默认 ~/.fund-scout/config.json）")
    p_compare.add_argument("--style", choices=["table", "card", "summary"], default="table", help="输出格式")
    p_compare.add_argument("--format", choices=["json", "csv", "md"], default="md")
    p_compare.add_argument("--push", default="", help="推送目标 (feishu/wechat/feishu,wechat)")
    p_compare.add_argument("--output", default=".")
    p_compare.set_defaults(func=cmd_compare)

    p_search = sub.add_parser("search", help="关键词搜索")
    p_search.add_argument("keyword", help="搜索关键词")
    p_search.add_argument("--type", default="", help="基金类型筛选")
    p_search.add_argument("--class", dest="cls", default="", help="份额类别筛选 (A/C)")
    p_search.add_argument("--with-limits", action="store_true",
                          help="自动跟进 compare 拿真实限额、收益率、费率（推荐 agent 用法）")
    p_search.add_argument("--limit", type=int, default=20,
                          help="--with-limits 时最多拉详情的基金数量（默认 20）")
    _add_common_args(p_search)
    p_search.set_defaults(func=cmd_search)

    p_validate = sub.add_parser("validate", help="校验已有数据文件")
    p_validate.add_argument("file", help="数据文件路径")
    p_validate.add_argument("--profile", choices=["quick", "compare", "detail", "qdii"], default="compare")
    p_validate.set_defaults(func=cmd_validate)

    p_test = sub.add_parser("test", help="测试适配器连接")
    p_test.add_argument("adapter", choices=["feishu", "wechat"], help="适配器名称")
    p_test.set_defaults(func=cmd_test)

    # ── funds: 维护用户基金列表（agent 友好）──────────────────
    p_funds = sub.add_parser(
        "funds",
        help="读写用户基金列表（持久化到 ~/.fund-scout/config.json）",
        description=(
            '维护用户的"我的基金"列表。\n'
            "Agent 调用约定：每次新对话开始时先 `funds list --format json` 读取持久化的基金，\n"
            "用户提到新基金时用 `funds add ...` 写回。这样跨对话状态保持一致。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    funds_sub = p_funds.add_subparsers(dest="action", required=True)

    p_list = funds_sub.add_parser("list", help="列出已保存的基金")
    p_list.add_argument("--format", choices=["text", "json"], default="text")

    p_add = funds_sub.add_parser(
        "add",
        help="添加基金（可批量）",
        description=(
            "支持的格式：\n"
            "  funds add 012870\n"
            "  funds add 012870:易方达纳指100C\n"
            "  funds add '012870:易方达纳指100C' '006479:广发纳指100C'\n"
            "  funds add 012870 006479 008971              # 不指定名字"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_add.add_argument("tokens", nargs="+", help="基金代码（可附加冒号 + 名称）")
    p_add.add_argument("--format", choices=["text", "json"], default="text")

    p_rm = funds_sub.add_parser("remove", help="移除基金")
    p_rm.add_argument("tokens", nargs="+", help="要移除的基金代码")
    p_rm.add_argument("--format", choices=["text", "json"], default="text")

    p_clear = funds_sub.add_parser("clear", help="清空所有基金")
    p_clear.add_argument("--yes", action="store_true", help="确认清空（必填）")
    p_clear.add_argument("--format", choices=["text", "json"], default="text")

    p_funds.set_defaults(func=cmd_funds)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
