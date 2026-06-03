# -*- coding: utf-8 -*-
"""批量回测所有 QDII 基金，输出 JSON 结果给报告生成器使用"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.predict.backtest import Backtester


# 18 只 QDII 基金（C 类） + 主代码 + 简称
# 主代码用于查 CSRC 季报；简称用于备用搜索
# 主代码已通过 fundcode_search.js 反查验证
FUNDS = [
    # (C类代码, 主代码, 简称, 显示名)
    ("017437", "017436", "华宝纳斯达克精选", "华宝纳斯达克精选"),
    ("014002", "014002", "浦银安盛全球智能科技", "浦银安盛全球智能科技"),
    ("021277", "021277", "广发全球精选", "广发全球精选"),
    ("017731", "017730", "嘉实全球产业升级", "嘉实全球产业升级"),
    ("000043", "000043", "嘉实美国成长", "嘉实美国成长"),
    ("161128", "161128", "易方达标普信息科技", "易方达标普信息科技"),
    ("012922", "012920", "易方达全球成长精选", "易方达全球成长精选"),
    ("021842", "021842", "国富全球科技互联", "国富全球科技"),
    ("539002", "539002", "建信新兴市场混合", "建信新兴市场混合"),
    ("015202", "015202", "汇添富全球移动互联", "汇添富全球移动互联"),
    ("024239", "024239", "华夏全球科技先锋", "华夏全球科技先锋"),
    ("016702", "016701", "银华海外数字经济", "银华海外数字经济"),
    ("018036", "018036", "长城全球新能源车", "长城全球新能源车"),
    ("017145", "017144", "华宝海外新能源汽车", "华宝海外新能源汽车"),
    ("017204", "017204", "华宝海外科技", "华宝海外科技"),
    ("008254", "008253", "华宝致远混合", "华宝致远混合"),
    ("016665", "016664", "天弘全球高端制造", "天弘全球高端制造"),
    ("017093", "017091", "景顺长城纳斯达克科技ETF联接", "景顺长城纳斯达克科技"),
]


def run_one(args):
    code, main_code, short_name, display = args
    try:
        bt = Backtester(target_quarter="auto",
                        calib_window=10, calib_min_window=8)
        result = bt.run(
            fund_code=code,
            main_code=main_code,
            short_name=short_name,
            backtest_start="2026-04-22",
            backtest_end="2026-06-01",
        )
        d = result.to_dict()
        d["display_name"] = display
        return code, d, None
    except Exception as e:
        import traceback
        return code, None, f"{e}\n{traceback.format_exc()}"


def main():
    out_dir = os.path.expanduser("~/.fund-scout/backtest_2026Q1")
    os.makedirs(out_dir, exist_ok=True)

    # 并行回测 (4 个 worker，避免 yfinance/CSRC 过载)
    results: dict[str, dict] = {}
    errors: dict[str, str] = {}

    t0 = time.time()
    # 单线程执行避免 yfinance / CSRC 跨基金请求互相干扰
    # （已发现 ThreadPoolExecutor + max_workers>1 会偶尔丢失 CSRC 响应）
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut_map = {ex.submit(run_one, f): f[0] for f in FUNDS}
        for i, fut in enumerate(as_completed(fut_map), start=1):
            code = fut_map[fut]
            try:
                c, data, err = fut.result()
                if err:
                    errors[c] = err
                    print(f"[{i}/{len(FUNDS)}] ✗ {c}: {err.splitlines()[0]}")
                else:
                    results[c] = data
                    m = data.get("metrics", {})
                    h = m.get("hybrid", {})
                    cb = m.get("calib_bias", {})
                    print(f"[{i}/{len(FUNDS)}] ✓ {c} ({data.get('display_name')}) "
                          f"hybrid_MAE={h.get('mae_pp', '-')} "
                          f"calib_bias_MAE={cb.get('mae_pp', '-')} "
                          f"days={data.get('n_days', 0)}")
            except Exception as e:
                errors[code] = str(e)
                print(f"[{i}/{len(FUNDS)}] ✗ {code}: {e}")

    print(f"\n总耗时: {time.time()-t0:.1f}s")

    # 写出聚合结果
    aggregate = {
        "backtest_start": "2026-04-22",
        "backtest_end": "2026-06-01",
        "report_quarter": "2026Q1",
        "n_funds": len(FUNDS),
        "n_success": len(results),
        "n_failed": len(errors),
        "results": results,
        "errors": errors,
    }
    out_path = os.path.join(out_dir, "all_funds.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(aggregate, f, ensure_ascii=False, indent=2)
    print(f"已保存: {out_path}")


if __name__ == "__main__":
    main()
