# -*- coding: utf-8 -*-
"""QDII T-1 估值预测命令行工具

用法:
    # 单只基金预测今日 NAV 涨跌
    python3 scripts/predict_cli.py 012922

    # 显式指定主代码 + 简称（对 CSRC 搜不到的基金有用）
    python3 scripts/predict_cli.py 012922 --main 012920 --short '易方达全球成长精选'

    # 批量预测配置文件中的基金
    python3 scripts/predict_cli.py --config ~/.fund-scout/config.json

    # 选择模型
    python3 scripts/predict_cli.py 012922 --model calib_bias

    # 完整 JSON 输出
    python3 scripts/predict_cli.py 012922 --json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.predict.backtest import Backtester, fetch_nav_series


def _load_config(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def predict_one(code: str, main_code: str = "", short_name: str = "",
                model: str = "hybrid", calib_window: int = 10,
                target_date: str = "") -> dict:
    """预测单只基金 NAV 涨跌。

    target_date 默认为今天（中国交易日）。如果尚未公布 NAV，预测的就是
    "未公布的今日涨跌"；如果已公布，就显示预测 vs 实际。

    默认模型 hybrid：不需要历史 NAV 校准，单日独立可算，平均 MAE 0.56pp。
    可选 calib_* 系列：需要 8 天 warmup 累积历史预测，对持仓快速轮换的
    基金（如 008254、539002）有 0.1-0.2pp 改善。
    """
    bt = Backtester(target_quarter="auto",
                    calib_window=calib_window, calib_min_window=8)

    today = target_date or datetime.now().date().isoformat()
    start = "2026-04-22"

    # 把 backtest_end 设为 today（如果有 NAV 就含 today，否则不含）
    result = bt.run(
        fund_code=code,
        main_code=main_code or code,
        short_name=short_name,
        backtest_start=start,
        backtest_end=today,
    )

    # 检查 today 是否在已公布的 NAV 中
    nav_dates = result.nav_series["date"].tolist() if not result.nav_series.empty else []
    today_published = today in nav_dates

    if today_published and result.days:
        # backtest 已经覆盖了 today，直接用最后一个
        last = result.days[-1]
        if last.date != today:
            # 可能 today 不是交易日，取最后交易日
            last = result.days[-1]
        pred = last.predictions.get(model) or last.predictions["hybrid"]
        return {
            "code": code,
            "name": result.fund_name,
            "report_quarter": result.report_quarter,
            "target_date": last.date,
            "prev_date": last.prev_date,
            "model": pred.model_name,
            "predicted_pct": round(pred.predicted_pct * 100, 4),
            "actual_pct": round(last.actual_pct * 100, 4),
            "error_pp": round(pred.error, 4) if pred.error is not None else None,
            "metrics_full_window": result.metrics.get(model, {}),
            "components_top": [
                {"name": k, "pp": round(v * 100, 4)}
                for k, v in sorted(pred.components.items(), key=lambda x: -abs(x[1]))[:5]
                if abs(v) > 0.0005
            ],
            "exposure": {
                "top10_total_pct": result.exposure_summary["top10_total_pct"],
                "foreign_pct": result.exposure_summary["foreign_pct"],
            },
            "is_realtime_prediction": False,
        }

    # ---- 实时预测：today 还没公布 NAV，需要单独跑一次 ----
    # 取上一个 NAV 日作为 prev_date，从行情数据预测 today
    if not nav_dates:
        return {"code": code, "error": "no_nav_history"}

    prev_date = max(d for d in nav_dates if d < today)

    # 重建 exposure + prices（要包含 today 的行情）
    from core.predict.predictor import Predictor
    p = Predictor(target_quarter="auto")
    exposure = p.build_exposure(code, main_code or code, short_name)

    # 行情区间略宽
    price_start = (datetime.fromisoformat(start) - timedelta(days=15)).date().isoformat()
    price_end = (datetime.fromisoformat(today) + timedelta(days=2)).date().isoformat()
    prices = p.fetch_prices(exposure, price_start, price_end)

    # 历史校准：用回测里已经算过的 hybrid 预测 + 实际
    from core.predict.models.calibrated import CalibratedModel
    from core.predict.models import HybridModel
    calib_history = []
    for d in result.days:
        h_pred = d.predictions["hybrid"]
        calib_history.append((d.date, h_pred, d.actual_pct))

    # 当日 hybrid + calibrated 预测（actual 未知）
    variant_map = {
        "calib_bias": "bias", "calib_scale": "scale",
        "calib_split": "split", "calib_full": "full",
    }
    if model.startswith("calib_"):
        cm = CalibratedModel(window=calib_window, min_window=8,
                             variant=variant_map.get(model, "bias"))
        pred = cm.predict_with_history(
            exposure, prices, today, prev_date,
            history=calib_history, actual_pct=None,
        )
    else:
        from core.predict.models import Top10OnlyModel, RegionProxyModel
        m = {"hybrid": HybridModel, "top10_only": Top10OnlyModel,
             "region_proxy": RegionProxyModel}[model]()
        pred = m.predict(exposure, prices, today, prev_date, actual_pct=None)

    return {
        "code": code,
        "name": result.fund_name,
        "report_quarter": result.report_quarter,
        "target_date": today,
        "prev_date": prev_date,
        "model": pred.model_name,
        "predicted_pct": round(pred.predicted_pct * 100, 4),
        "actual_pct": None,  # 还未公布
        "error_pp": None,
        "metrics_full_window": result.metrics.get(model, {}),
        "components_top": [
            {"name": k, "pp": round(v * 100, 4)}
            for k, v in sorted(pred.components.items(), key=lambda x: -abs(x[1]))[:5]
            if abs(v) > 0.0005
        ],
        "exposure": {
            "top10_total_pct": result.exposure_summary["top10_total_pct"],
            "foreign_pct": result.exposure_summary["foreign_pct"],
        },
        "is_realtime_prediction": True,
    }


def main():
    parser = argparse.ArgumentParser(prog="predict-cli", description="QDII T-1 估值预测")
    parser.add_argument("code", nargs="?", help="基金代码")
    parser.add_argument("--main", default="", help="基金主代码（A 类）")
    parser.add_argument("--short", default="", help="基金简称")
    parser.add_argument("--model", default="hybrid",
                        choices=["top10_only", "region_proxy", "hybrid",
                                 "calib_bias", "calib_scale", "calib_split", "calib_full"])
    parser.add_argument("--config", default="", help="批量预测配置文件路径")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--calib-window", type=int, default=10)
    args = parser.parse_args()

    if args.config:
        config = _load_config(os.path.expanduser(args.config))
        funds = config.get("my_funds", [])
        if not funds:
            print("配置文件中没有 my_funds")
            sys.exit(1)
        results = []
        for f in funds:
            print(f"\n预测 {f['code']} ({f.get('name', '?')}) ...", file=sys.stderr)
            try:
                r = predict_one(
                    code=f["code"],
                    main_code=f.get("main_code", ""),
                    short_name=f.get("name", ""),
                    model=args.model,
                    calib_window=args.calib_window,
                )
                results.append(r)
            except Exception as e:
                results.append({"code": f["code"], "error": str(e)})

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            print(f"\n{'代码':<8s} {'名称':<22s} {'目标日期':<12s} {'预测':>9s} {'实际':>9s} {'误差':>9s}")
            print("-" * 80)
            for r in results:
                if "error" in r:
                    print(f"{r['code']:<8s} {'ERROR':<22s} {r['error']}")
                    continue
                pp = f"{r['predicted_pct']:+.3f}%" if r.get("predicted_pct") is not None else "-"
                ap = f"{r['actual_pct']:+.3f}%" if r.get("actual_pct") is not None else "未公布"
                err = f"{r['error_pp']:.3f}pp" if r.get("error_pp") is not None else "-"
                print(f"{r['code']:<8s} {r.get('name', '')[:20]:<22s} {r['target_date']:<12s} {pp:>9s} {ap:>9s} {err:>9s}")
        return

    if not args.code:
        parser.print_help()
        sys.exit(1)

    r = predict_one(
        code=args.code,
        main_code=args.main,
        short_name=args.short,
        model=args.model,
        calib_window=args.calib_window,
    )

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if "error" in r:
        print(f"❌ {r['code']}: {r['error']}")
        sys.exit(1)

    print(f"基金: {r['name']} ({r['code']})")
    print(f"季报: {r['report_quarter']}")
    print(f"目标日期: {r['target_date']} (基于 {r['prev_date']} 的 NAV 预测涨跌)")
    print(f"使用模型: {r['model']}")
    print()
    sign = "+" if r["predicted_pct"] >= 0 else ""
    print(f"  预测涨跌: {sign}{r['predicted_pct']:.3f}%")
    if r.get("actual_pct") is not None:
        sign2 = "+" if r["actual_pct"] >= 0 else ""
        print(f"  实际涨跌: {sign2}{r['actual_pct']:.3f}% (官方公布)")
        print(f"  误差    : {r['error_pp']:.3f}pp")
    print()
    print(f"  Top10 占基金净值: {r['exposure']['top10_total_pct']:.1f}%")
    print(f"  海外资产占比    : {r['exposure']['foreign_pct']:.1f}%")
    print()
    print("  主要贡献因子:")
    for c in r["components_top"]:
        sign = "+" if c["pp"] >= 0 else ""
        print(f"    {c['name'][:50]:<50s} {sign}{c['pp']:.3f}pp")
    print()
    print("  历史表现 (回测窗口):")
    m = r.get("metrics_full_window", {})
    if m:
        print(f"    n={m.get('n', '-')} 天")
        print(f"    MAE={m.get('mae_pp', '-')}pp, RMSE={m.get('rmse_pp', '-')}pp")
        print(f"    50bp 命中率={m.get('hit_rate_05pp', '-')}%, 100bp 命中率={m.get('hit_rate_10pp', '-')}%")


if __name__ == "__main__":
    main()
