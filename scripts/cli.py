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

# 压制内部库的杂音告警，用户只看我们显式 print 的内容
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
logging.basicConfig(level=logging.ERROR, format="%(message)s")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.models import FundInfo, FundDataResult
from core.fetcher import FundFetcher as CoreFetcher
from core.sources.eastmoney import EastMoneySource
from core.sources.howbuy import HowbuySource
from core.sources.csrc import CSRCSource
from core.sources.base import SourceError
from core.validate import validate_data, print_report
from adapters import get_adapter, list_adapters


def _build_purchase_info(status: str, limit: str, effectively_closed: bool) -> str:
    if effectively_closed or status == "\u6682\u505c":
        return "\u6682\u505c\u7533\u8d2d"
    if status == "\u9650\u5c0f\u989d":
        m = re.search(r"([\d.]+)\s*(\u4e07|\u5143)?", str(limit or ""))
        if m:
            return f"\u9650\u5c0f\u989d {limit}"
        return f"\u9650\u5c0f\u989d {limit}" if limit else "\u9650\u5c0f\u989d\uff08\u8bf7\u4ee5\u5e73\u53f0\u4e3a\u51c6\uff09"
    if status == "\u9650\u5927\u989d":
        return f"\u9650\u5927\u989d\uff08{limit}\uff09" if limit else "\u9650\u5927\u989d"
    if status in ("\u5f00\u653e", "\u5f00\u653e\u7533\u8d2d"):
        return "\u5f00\u653e\u7533\u8d2d\uff08\u65e0\u9650\u989d\uff09"
    return f"{status}\uff08{limit}\uff09" if limit else status


CONFIG_DIR = os.path.expanduser("~/.fund-scout")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def _load_config(path: str | None = None) -> dict:
    p = path or CONFIG_PATH
    if not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _ensure_config_dir() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)


class FundFetcher:
    def __init__(self):
        self.em = EastMoneySource()
        self.howbuy = HowbuySource()
        self.csrc = CSRCSource()

    def fetch_detail(self, code: str, holdings: bool = False, csrc: bool = False) -> FundInfo:
        try:
            info = self.em.fetch_detail(code)
        except (SourceError, Exception) as e:
            print(f"  ! 主源(eastmoney)获取 {code} 失败: {e}，尝试备用源(howbuy)")
            try:
                info = self.howbuy.fetch_detail(code)
            except (SourceError, Exception) as e2:
                print(f"  ! 备用源(howbuy)也失败: {e2}")
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

    def fetch_batch(self, codes: list[str]) -> list[FundInfo]:
        results = self.em.fetch_batch(codes)
        for info in results:
            if not info.data_unavailable:
                info._purchase_info = _build_purchase_info(
                    info.purchase_status, info.purchase_limit, info.effectively_closed
                )
                info.market_top3 = CoreFetcher._compute_market_top3(None, info)
        # 并行获取 CSRC 季报数据（仅主动基金需要）
        from concurrent.futures import ThreadPoolExecutor, as_completed
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
        return results

    def search(self, keyword: str, fund_type: str = "") -> list[dict]:
        return self.em.search_funds(keyword, fund_type)


def _format_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _format_csv(data: dict) -> str:
    funds = data.get("funds", [])
    if not funds:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(funds[0].keys()))
    writer.writeheader()
    for f in funds:
        row = {}
        for k, v in f.items():
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
        safe_hide = {"purchase_status", "purchase_limit", "data_unavailable", "_purchase_info"}
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
    ("return_1y", "近1年", 8, 10),
    ("drawdown_1y", "近一年回撤", 12, 14),
    ("scale", "规模", 10, 12),
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
    cols = []
    for key, label, min_w, max_w in TABLE_COLS:
        vals = [_fmt_val(f, key) for f in funds] + [label]
        data_w = max(_dw(v) for v in vals)
        cols.append(min(max(data_w + 2, min_w), max_w))

    top_s = _sep_line(cols, "┌", "┬", "┐")
    sep_s = _sep_line(cols, "├", "┼", "┤")
    bot_s = _sep_line(cols, "└", "┴", "┘")

    hdr = "│" + "│".join(" " + _pad(label, cols[i] - 2) + " " for i, (_, label, _, _) in enumerate(TABLE_COLS)) + "│"

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
    funds = fetcher.fetch_batch(codes)
    result = _build_result(funds)
    data = result.to_dict()

    profile = config.get("defaults", {}).get("profile", "compare")
    validation = validate_data(data, profile=profile)
    data["_validation"] = validation.to_dict()
    data["_warnings"] = validation.warnings
    print_report(validation)

    fmt = args.format
    style = args.style
    content = _format_output(data, fmt, style=style)
    print(content)

    push_targets = []
    if args.push:
        push_targets = [p.strip() for p in args.push.split(",")]
    else:
        push_cfg = config.get("push", {})
        if push_cfg.get("feishu_webhook"):
            push_targets.append("feishu")
        if push_cfg.get("wechat_webhook"):
            push_targets.append("wechat")

    for target in push_targets:
        _push(result, target)


def cmd_search(args: argparse.Namespace) -> None:
    fetcher = FundFetcher()
    results = fetcher.search(args.keyword, fund_type=args.type or "")

    if args.cls:
        results = [r for r in results if args.cls in r.get("name", "")]

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

    if args.push:
        _push(content, args.push)


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
    _add_common_args(p_search)
    p_search.set_defaults(func=cmd_search)

    p_validate = sub.add_parser("validate", help="校验已有数据文件")
    p_validate.add_argument("file", help="数据文件路径")
    p_validate.add_argument("--profile", choices=["quick", "compare", "detail", "qdii"], default="compare")
    p_validate.set_defaults(func=cmd_validate)

    p_test = sub.add_parser("test", help="测试适配器连接")
    p_test.add_argument("adapter", choices=["feishu", "wechat"], help="适配器名称")
    p_test.set_defaults(func=cmd_test)

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
