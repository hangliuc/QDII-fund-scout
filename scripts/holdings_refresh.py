# -*- coding: utf-8 -*-
"""持仓数据刷新工具

QDII 基金每季度披露持仓数据（CSRC 季报 PDF），披露时间约：
  - Q1 季报：4 月下旬
  - Q2 季报：7 月下旬
  - Q3 季报：10 月下旬
  - Q4 季报：次年 1 月下旬

本工具自动检查所有配置基金是否有新季报，命中则重新解析并更新缓存。
配合 cron 每天定时跑一次即可全自动跟进新季报。

用法:
    # 检查并更新所有配置基金
    python3 scripts/holdings_refresh.py

    # 只检查特定基金
    python3 scripts/holdings_refresh.py --funds 012922,539002

    # 强制刷新所有索引（跳过 24h TTL，重新查 CSRC）
    python3 scripts/holdings_refresh.py --force

    # 查看缓存统计
    python3 scripts/holdings_refresh.py --stats

    # 配合 run_backtest 一起：先刷新持仓，再回测
    python3 scripts/holdings_refresh.py && python3 scripts/run_backtest.py

定时任务示例 (crontab -e):
    # 每天早上 8 点检查新季报
    0 8 * * * cd ~/QDII-fund-scout && /usr/bin/python3 scripts/holdings_refresh.py >> ~/.fund-scout/refresh.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.sources.csrc import CSRCSource
from core.sources.csrc_cache import CSRCCache


# 默认基金列表：仅作为 fallback 示例使用（用户没有配置文件 / 未传 --funds 时）。
# 生产用法应通过 ~/.fund-scout/config.json 自定义关注的基金。
# 此清单不代表"全部 QDII"，新基金或未列入的请在配置文件中明确添加。
# (C 代码, 主代码 / A 代码, 简称) - 主代码通过 fundcode_search.js 反查
DEFAULT_FUNDS = [
    ("017437", "017436", "华宝纳斯达克精选"),
    ("014002", "014002", "浦银安盛全球智能科技"),
    ("021277", "021277", "广发全球精选"),
    ("017731", "017730", "嘉实全球产业升级"),
    ("000043", "000043", "嘉实美国成长"),
    ("161128", "161128", "易方达标普信息科技"),
    ("012922", "012920", "易方达全球成长精选"),
    ("021842", "021842", "国富全球科技互联"),
    ("539002", "539002", "建信新兴市场混合"),
    ("015202", "015202", "汇添富全球移动互联"),
    ("024239", "024239", "华夏全球科技先锋"),
    ("016702", "016701", "银华海外数字经济"),
    ("018036", "018036", "长城全球新能源车"),
    ("017145", "017144", "华宝海外新能源汽车"),
    ("017204", "017204", "华宝海外科技"),
    ("008254", "008253", "华宝致远混合"),
    ("016665", "016664", "天弘全球高端制造"),
    ("017093", "017091", "景顺长城纳斯达克科技ETF联接"),
    # 常见 ETF 联接补充
    ("008971", "008970", "大成纳斯达克100ETF联接"),
    ("006479", "006479", "广发纳斯达克100ETF联接"),
    ("014978", "014977", "华安纳斯达克100ETF联接"),
    ("012870", "012869", "易方达纳斯达克100ETF联接"),
    ("015300", "015299", "华夏纳斯达克100ETF联接"),
    ("016453", "016452", "南方纳斯达克100"),
    ("160213", "160213", "国泰纳斯达克100"),
]


def load_user_funds(config_path: str = "") -> list[tuple[str, str, str]]:
    """从用户配置文件读取基金列表，回退到 DEFAULT_FUNDS。

    如果用户配置只有 code 没有 main_code，会从 DEFAULT_FUNDS 中匹配补全。
    """
    # 用 code 索引 DEFAULT_FUNDS，用于补全 main_code
    default_by_code = {c: (c, m, n) for c, m, n in DEFAULT_FUNDS}

    path = config_path or os.path.expanduser("~/.fund-scout/config.json")
    if not os.path.exists(path):
        return DEFAULT_FUNDS
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        funds = cfg.get("my_funds", [])
        if not funds:
            return DEFAULT_FUNDS
        result = []
        for f in funds:
            code = f.get("code", "")
            user_main = f.get("main_code", "")
            user_name = f.get("name", "") or f.get("display", "")
            # 补全：优先用用户给的 main_code，否则查 DEFAULT_FUNDS
            if code in default_by_code:
                _, default_main, default_name = default_by_code[code]
                main = user_main or default_main
                name = user_name or default_name
            else:
                main = user_main or code  # fallback: 用 code 当 main_code
                name = user_name
            if code:
                result.append((code, main, name))
        return result if result else DEFAULT_FUNDS
    except Exception as e:
        print(f"⚠ 配置文件读取失败 ({e}), 使用默认基金列表")
        return DEFAULT_FUNDS


def cmd_refresh(funds: list[tuple[str, str, str]], force: bool = False) -> dict:
    """检查并更新基金持仓缓存

    返回汇总: {n_total, n_new, n_unchanged, n_failed, details}
    """
    cache = CSRCCache()
    if force:
        n_invalidated = cache.invalidate_all_indexes()
        print(f"⏩ 强制刷新模式: 清掉 {n_invalidated} 个索引")

    csrc = CSRCSource(target_quarter="auto", cache=cache)

    stats = {"n_total": len(funds), "n_new": 0, "n_unchanged": 0, "n_failed": 0, "details": []}

    print(f"\n开始检查 {len(funds)} 只基金的最新季报...\n")
    print(f"{'代码':<8s} {'名称':<22s} {'状态':<8s} {'季报':<10s} {'instance_id':<12s} {'变化':<8s}")
    print("-" * 80)

    for code, main_code, short_name in funds:
        # 用 main_code 查 CSRC（A 类代码更稳定）
        # 先看现在缓存里 main_code 对应什么 instance_id
        old_idx = cache.get_cached_index(main_code) or {}
        old_iid = old_idx.get("instance_id", "")

        # 强制重新查（不靠缓存索引）
        if force:
            cache.invalidate_index(main_code)

        try:
            t0 = time.time()
            exp = csrc.fetch_exposure(main_code, short_name)
            iid = exp.get("instance_id", "")
            quarter = exp.get("report_quarter", "")
            elapsed = time.time() - t0

            if not iid:
                stats["n_failed"] += 1
                status = "❌ 失败"
                change = "搜不到"
            elif old_iid and iid != old_iid:
                stats["n_new"] += 1
                status = "🆕 更新"
                change = f"{old_iid}→{iid}"
            elif not old_iid:
                stats["n_new"] += 1
                status = "🆕 首次"
                change = "新增"
            else:
                stats["n_unchanged"] += 1
                status = "✓ 不变"
                change = "—"

            stats["details"].append({
                "code": code,
                "name": short_name,
                "quarter": quarter,
                "instance_id": iid,
                "old_instance_id": old_iid,
                "elapsed_sec": round(elapsed, 2),
                "status": status,
            })
            print(f"{code:<8s} {short_name[:20]:<22s} {status:<8s} {quarter:<10s} {iid:<12s} {change}")
        except Exception as e:
            stats["n_failed"] += 1
            stats["details"].append({
                "code": code, "name": short_name, "error": str(e),
            })
            print(f"{code:<8s} {short_name[:20]:<22s} ❌ 异常: {e}")

    print()
    print("=" * 80)
    print(f"总计: {stats['n_total']} 只基金")
    print(f"  🆕 新增/更新: {stats['n_new']} 只")
    print(f"  ✓ 不变: {stats['n_unchanged']} 只")
    print(f"  ❌ 失败: {stats['n_failed']} 只")

    cs = cache.cache_stats()
    print(f"\n本地缓存: {cs['n_pdfs']} 份 PDF + {cs['n_parsed']} 份解析数据，{cs['total_size_mb']} MB")
    print(f"路径: {cs['cache_dir']}")

    return stats


def cmd_stats() -> None:
    cache = CSRCCache()
    cs = cache.cache_stats()
    print(f"缓存目录: {cs['cache_dir']}")
    print(f"  - PDF 数量:     {cs['n_pdfs']}")
    print(f"  - 解析数据:     {cs['n_parsed']}")
    print(f"  - 已索引基金:   {cs['n_indexed_funds']}")
    print(f"  - 总大小:       {cs['total_size_mb']} MB")

    funds = cache.list_cached_funds()
    if funds:
        print(f"\n已缓存基金:")
        print(f"{'主代码':<10s} {'最新 instance_id':<18s} {'报告':<60s} {'检查时间'}")
        for f in funds:
            print(f"{f.get('main_code', ''):<10s} "
                  f"{f.get('instance_id', ''):<18s} "
                  f"{f.get('report_name', '')[:55]:<60s} "
                  f"{f.get('checked_at', '')}")


def cmd_clear(only_index: bool = False) -> None:
    cache = CSRCCache()
    if only_index:
        n = cache.invalidate_all_indexes()
        print(f"已清除 {n} 个索引（PDF 和解析数据保留）")
    else:
        # 危险操作：彻底清空
        import shutil
        if os.path.exists(cache.cache_dir):
            shutil.rmtree(cache.cache_dir)
            print(f"已彻底清除缓存目录: {cache.cache_dir}")
        else:
            print(f"缓存目录不存在: {cache.cache_dir}")


def refresh_stale_in_background(funds: list[tuple[str, str, str]] | None = None,
                                 stale_threshold_hours: int = 24) -> None:
    """在后台静默刷新 stale 的基金索引（用于 UI / run.sh 启动时预热）。

    与 cmd_refresh 的区别：
    - 不打印输出，不阻塞主流程
    - 只刷新 stale (索引过期或缺失) 的基金
    - 异常完全静默
    - 用 daemon 线程，主进程退出时跟着退出

    使用场景：用户打开 UI / 进入 run.sh 菜单时调用一次，无感跟进新季报。
    """
    import threading
    import time as _time
    import logging as _logging

    def _worker():
        try:
            cache = CSRCCache()
            now = _time.time()
            stale_threshold = stale_threshold_hours * 3600

            target_funds = funds or load_user_funds()
            stale_targets = []
            for code, main_code, short_name in target_funds:
                idx_path = cache._index_path(main_code)
                # 缺失或超过阈值都算 stale
                if not os.path.exists(idx_path):
                    stale_targets.append((code, main_code, short_name))
                    continue
                try:
                    age = now - os.path.getmtime(idx_path)
                    if age > stale_threshold:
                        stale_targets.append((code, main_code, short_name))
                except OSError:
                    stale_targets.append((code, main_code, short_name))

            if not stale_targets:
                return

            csrc = CSRCSource(target_quarter="auto", cache=cache)
            for code, main_code, short_name in stale_targets:
                try:
                    csrc.fetch_exposure(main_code, short_name)
                except Exception as e:
                    _logging.getLogger(__name__).debug(
                        "后台刷新 %s 失败: %s", code, e
                    )
        except Exception as e:
            _logging.getLogger(__name__).debug("后台刷新整体异常: %s", e)

    t = threading.Thread(target=_worker, daemon=True, name="csrc-refresh")
    t.start()


def main():
    parser = argparse.ArgumentParser(
        description="QDII 持仓数据刷新工具（自动检查 CSRC 新季报）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--funds", default="", help="逗号分隔的基金代码（默认读 ~/.fund-scout/config.json）")
    parser.add_argument("--force", action="store_true", help="强制刷新所有索引（绕过 24h TTL）")
    parser.add_argument("--stats", action="store_true", help="只查看缓存统计")
    parser.add_argument("--clear", action="store_true", help="清除整个缓存目录（危险）")
    parser.add_argument("--clear-index", action="store_true", help="只清除索引，保留 PDF 和解析数据")
    args = parser.parse_args()

    if args.stats:
        cmd_stats()
        return
    if args.clear:
        cmd_clear(only_index=False)
        return
    if args.clear_index:
        cmd_clear(only_index=True)
        return

    if args.funds:
        codes = [c.strip() for c in args.funds.split(",") if c.strip()]
        all_funds = load_user_funds()
        funds = [(c, m, n) for c, m, n in all_funds if c in codes]
        if not funds:
            # 用户给的代码不在配置里，假设直接是主代码
            funds = [(c, c, "") for c in codes]
    else:
        funds = load_user_funds()

    cmd_refresh(funds, force=args.force)


if __name__ == "__main__":
    main()
