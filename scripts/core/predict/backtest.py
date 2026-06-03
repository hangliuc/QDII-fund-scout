# -*- coding: utf-8 -*-
"""回测引擎：用真实 NAV 序列评估各模型误差"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests

from core.predict.models import (
    Top10OnlyModel,
    RegionProxyModel,
    HybridModel,
    CalibratedModel,
    PredictionResult,
)
from core.predict.predictor import Predictor
from core.predict.models.base import FundExposure

logger = logging.getLogger(__name__)


NAV_API_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://fund.eastmoney.com/",
}
NAV_API_URL = (
    "https://api.fund.eastmoney.com/f10/lsjz"
    "?callback=jQuery&fundCode={code}&pageIndex={page}"
    "&pageSize=20&startDate={start}&endDate={end}"
)


def fetch_nav_series(code: str, start: str, end: str) -> pd.DataFrame:
    """拉取真实 NAV 序列（带涨跌幅）

    返回 DataFrame，列: date, nav, change_pct
    change_pct 来自天天基金 API 原始字段，已经是基金会计的官方涨跌幅
    """
    rows: list[dict] = []
    for page in range(1, 30):  # 上限 30 页 = 600 条，覆盖 2 年多
        try:
            resp = requests.get(
                NAV_API_URL.format(code=code, page=page, start=start, end=end),
                headers=NAV_API_HEADERS,
                timeout=20,
            )
        except requests.RequestException as e:
            logger.warning("NAV API 请求失败 page=%d: %s", page, e)
            break
        text = resp.text.strip()
        if text.startswith("jQuery(") and text.endswith(")"):
            text = text[7:-1]
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning("NAV API JSON 解析失败: %s", e)
            break
        items = (data.get("Data", {}) or {}).get("LSJZList", [])
        if not items:
            break
        for it in items:
            try:
                nav = float(it.get("DWJZ", "0"))
                date = it.get("FSRQ", "")
                jz = it.get("JZZZL", "")
                change_pct = float(jz) / 100.0 if jz and jz not in ("", "None") else None
                rows.append({"date": date, "nav": nav, "change_pct": change_pct})
            except (ValueError, TypeError):
                continue
        # 如果不足一页，就到底了
        if len(items) < 20:
            break
    if not rows:
        return pd.DataFrame(columns=["date", "nav", "change_pct"])
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


@dataclass
class DayResult:
    """单日回测结果"""
    date: str
    prev_date: str
    actual_pct: float
    predictions: dict[str, PredictionResult]


@dataclass
class BacktestResult:
    """完整回测输出"""
    fund_code: str
    fund_name: str
    report_quarter: str
    backtest_start: str
    backtest_end: str
    nav_series: pd.DataFrame = field(default_factory=pd.DataFrame)
    days: list[DayResult] = field(default_factory=list)
    metrics: dict[str, dict] = field(default_factory=dict)
    exposure_summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "fund_code": self.fund_code,
            "fund_name": self.fund_name,
            "report_quarter": self.report_quarter,
            "backtest_start": self.backtest_start,
            "backtest_end": self.backtest_end,
            "n_days": len(self.days),
            "metrics": self.metrics,
            "exposure_summary": self.exposure_summary,
            "days": [
                {
                    "date": d.date,
                    "prev_date": d.prev_date,
                    "actual_pct": round(d.actual_pct * 100, 4),
                    "predictions": {
                        name: pred.to_dict() for name, pred in d.predictions.items()
                    },
                }
                for d in self.days
            ],
        }


def compute_metrics(errors_pp: list[float], signed_errors_pp: list[float]) -> dict:
    """指标：MAE / RMSE / 命中率(<0.5pp) / 命中率(<1pp) / 偏差"""
    if not errors_pp:
        return {}
    arr = np.array(errors_pp)
    sgn = np.array(signed_errors_pp)
    return {
        "n": len(arr),
        "mae_pp": round(float(np.mean(arr)), 4),
        "rmse_pp": round(float(np.sqrt(np.mean(arr ** 2))), 4),
        "max_error_pp": round(float(np.max(arr)), 4),
        "median_error_pp": round(float(np.median(arr)), 4),
        "p90_error_pp": round(float(np.percentile(arr, 90)), 4),
        "hit_rate_05pp": round(float(np.mean(arr <= 0.5)) * 100, 2),
        "hit_rate_10pp": round(float(np.mean(arr <= 1.0)) * 100, 2),
        "bias_pp": round(float(np.mean(sgn)), 4),
        "bias_std_pp": round(float(np.std(sgn)), 4),
    }


class Backtester:
    """回测调度器

    使用方式：
        bt = Backtester()
        result = bt.run(
            fund_code="012922",
            main_code="012920",
            short_name="易方达全球成长精选混合（QDII）",
            backtest_start="2026-04-22",
            backtest_end="2026-06-02",
        )
        print(result.metrics)
    """

    def __init__(
        self,
        report_year: str = "",          # 空 = 不限年份（auto 模式）
        target_quarter: str = "auto",   # auto = 用最新季报；显式可填"第1季度"等
        calib_window: int = 20,
        calib_min_window: int = 10,
    ):
        self.predictor = Predictor(report_year=report_year, target_quarter=target_quarter)
        self.calib_window = calib_window
        self.calib_min_window = calib_min_window

    def run(
        self,
        fund_code: str,
        main_code: str = "",
        short_name: str = "",
        backtest_start: str = "2026-04-22",
        backtest_end: Optional[str] = None,
        report_year_holdings: int | None = None,
    ) -> BacktestResult:
        if not backtest_end:
            backtest_end = datetime.now().date().isoformat()
        if report_year_holdings is None:
            # 默认根据 backtest_start 选年份（保证拿到回测期所属年份的最新季报）
            report_year_holdings = int(backtest_start[:4])

        logger.info("=" * 60)
        logger.info("回测 %s（%s ~ %s）", fund_code, backtest_start, backtest_end)

        # 1) 构建基金暴露画像（CSRC + Top10）
        exposure = self.predictor.build_exposure(
            fund_code=fund_code,
            main_code=main_code,
            short_name=short_name,
            report_year=report_year_holdings,
        )

        # 2) 拉取 NAV 真值序列（多拉一周用于覆盖 prev_date）
        nav_start = (datetime.fromisoformat(backtest_start) - timedelta(days=10)).date().isoformat()
        nav_df = fetch_nav_series(fund_code, nav_start, backtest_end)
        if nav_df.empty:
            raise ValueError(f"无法获取 {fund_code} 在 {nav_start}~{backtest_end} 的 NAV 数据")

        # 3) 拉取所有需要的 ticker 的行情
        # 价格区间略宽于 nav 区间，确保能算 prev_date 涨跌
        price_start = (datetime.fromisoformat(backtest_start) - timedelta(days=15)).date().isoformat()
        price_end = (datetime.fromisoformat(backtest_end) + timedelta(days=2)).date().isoformat()
        prices = self.predictor.fetch_prices(exposure, price_start, price_end)

        # 4) 逐日预测
        in_window = nav_df[
            (nav_df["date"] >= backtest_start) & (nav_df["date"] <= backtest_end)
        ].reset_index(drop=True)

        days: list[DayResult] = []
        # 校准模型的滚动历史
        calib_history: list[tuple[str, PredictionResult, float]] = []
        # 4 个变体并行跑
        calib_variants = {
            "calib_bias": CalibratedModel(window=self.calib_window, min_window=self.calib_min_window, variant="bias"),
            "calib_scale": CalibratedModel(window=self.calib_window, min_window=self.calib_min_window, variant="scale"),
            "calib_split": CalibratedModel(window=self.calib_window, min_window=self.calib_min_window, variant="split"),
            "calib_full": CalibratedModel(window=self.calib_window, min_window=self.calib_min_window, variant="full"),
            # EMA-加权变体（更重视最近样本）
            "calib_bias_ema": CalibratedModel(window=self.calib_window * 2, min_window=self.calib_min_window, variant="bias", ema_decay=0.1),
            "calib_scale_ema": CalibratedModel(window=self.calib_window * 2, min_window=self.calib_min_window, variant="scale", ema_decay=0.1),
            # blend 变体: 仅部分应用 bias 修正（避免 bias 不稳定时过度修正）
            "calib_blend": CalibratedModel(window=5, min_window=4, variant="blend", blend=0.3),
            # it_residual 变体: 在持续负偏差时加权 IT 行业残余
            "calib_it_resid": CalibratedModel(window=10, min_window=5, variant="it_residual", residual_mult=1.5, bias_threshold=-0.2),
        }

        # 对于 in_window 中每一天，找到它前一个 NAV 日期（在 nav_df 中）
        nav_dates = nav_df["date"].tolist()
        nav_change_map = dict(zip(nav_df["date"], nav_df["change_pct"]))

        for _, row in in_window.iterrows():
            target_date = row["date"]
            if target_date not in nav_dates:
                continue
            idx = nav_dates.index(target_date)
            if idx == 0:
                continue  # 第一天没有前序
            prev_date = nav_dates[idx - 1]
            actual_pct = row["change_pct"]
            if actual_pct is None or pd.isna(actual_pct):
                continue

            # 跑各模型
            preds: dict[str, PredictionResult] = {}

            # Top10 only
            preds["top10_only"] = Top10OnlyModel().predict(
                exposure, prices, target_date, prev_date, actual_pct
            )
            # Region proxy
            preds["region_proxy"] = RegionProxyModel().predict(
                exposure, prices, target_date, prev_date, actual_pct
            )
            # Hybrid
            hybrid_pred = HybridModel().predict(
                exposure, prices, target_date, prev_date, actual_pct
            )
            preds["hybrid"] = hybrid_pred

            # Calibrated（基于 hybrid 的因子分解 + 滚动 OLS） — 4 个变体
            for var_name, var_model in calib_variants.items():
                preds[var_name] = var_model.predict_with_history(
                    exposure, prices, target_date, prev_date,
                    history=calib_history, actual_pct=actual_pct,
                )

            # auto: 选择最近 lookback 天表现最好的子模型
            # 在 days 列表已有足够长度时启用
            auto_lookback = 8
            if len(days) >= auto_lookback:
                recent_days = days[-auto_lookback:]
                cands = ['hybrid', 'calib_bias', 'calib_scale', 'calib_blend']
                best_model_name = 'hybrid'
                best_recent_mae = float('inf')
                for cm in cands:
                    if not all(cm in rd.predictions for rd in recent_days):
                        continue
                    mae = sum(rd.predictions[cm].error for rd in recent_days
                              if rd.predictions[cm].error is not None) / auto_lookback
                    if mae < best_recent_mae:
                        best_recent_mae = mae
                        best_model_name = cm
                # 用最佳子模型当日预测构造 auto pred
                base_pred = preds[best_model_name]
                # 复制并改名
                from copy import copy as _copy
                auto_pred = _copy(base_pred)
                auto_pred.model_name = f"auto({best_model_name})"
                auto_pred.notes = f"自动选择 {best_model_name}, 近{auto_lookback}日 MAE={best_recent_mae:.3f}pp"
                preds["calib_auto"] = auto_pred
            else:
                # warmup: fallback to hybrid
                from copy import copy as _copy
                auto_pred = _copy(preds["hybrid"])
                auto_pred.model_name = "auto(fallback=hybrid)"
                preds["calib_auto"] = auto_pred

            days.append(DayResult(
                date=target_date, prev_date=prev_date,
                actual_pct=actual_pct, predictions=preds,
            ))

            # 把当日 hybrid 预测 + 真实结果加入校准历史
            calib_history.append((target_date, hybrid_pred, actual_pct))

        # 5) 汇总指标
        metrics = {}
        all_models = ["top10_only", "region_proxy", "hybrid",
                      "calib_bias", "calib_scale", "calib_split", "calib_full",
                      "calib_bias_ema", "calib_scale_ema",
                      "calib_blend", "calib_it_resid", "calib_auto"]
        for model_name in all_models:
            errs = [d.predictions[model_name].error for d in days
                    if d.predictions[model_name].error is not None]
            sgn = [d.predictions[model_name].signed_error for d in days
                   if d.predictions[model_name].signed_error is not None]
            metrics[model_name] = compute_metrics(errs, sgn)

            # 仅看 calibrated 实际生效阶段（剔除 fallback）的指标
            if model_name.startswith("calib_"):
                eff_errs = [d.predictions[model_name].error for d in days
                            if d.predictions[model_name].error is not None
                            and "fallback" not in d.predictions[model_name].model_name]
                eff_sgn = [d.predictions[model_name].signed_error for d in days
                           if d.predictions[model_name].signed_error is not None
                           and "fallback" not in d.predictions[model_name].model_name]
                if eff_errs:
                    metrics[model_name + "_active"] = compute_metrics(eff_errs, eff_sgn)

        # 6) 暴露摘要
        exposure_summary = {
            "report_quarter": exposure.report_quarter,
            "top10_count": len(exposure.top10),
            "top10_with_ticker": len(exposure.top10_with_ticker()),
            "top10_total_pct": round(exposure.top10_total_pct() * 100, 2),
            "total_equity_pct": round(exposure.total_equity_pct * 100, 2),
            "cash_pct": round(exposure.cash_pct * 100, 2),
            "foreign_pct": round(exposure.foreign_pct * 100, 2),
            "market_dist": {
                k: round(v * 100, 2) for k, v in exposure.market_dist.items()
            },
            "industry_dist": {
                k: round(v * 100, 2) for k, v in exposure.industry_dist.items()
            },
            "top10": [
                {
                    "code": h["code"], "name": h["name"],
                    "pct": round(h["pct"] * 100, 2),
                    "ticker": h.get("ticker"),
                }
                for h in exposure.top10
            ],
        }

        return BacktestResult(
            fund_code=fund_code,
            fund_name=short_name or fund_code,
            report_quarter=exposure.report_quarter,
            backtest_start=backtest_start,
            backtest_end=backtest_end,
            nav_series=nav_df,
            days=days,
            metrics=metrics,
            exposure_summary=exposure_summary,
        )
