# -*- coding: utf-8 -*-
"""模型 ① 纯 Top10 加权

公式：
    预测涨跌 = Σ(top10_i 占比 × top10_i 当日涨跌) × FX调整(可选)

误差来源：
    Top10 通常只覆盖 30~60% NAV，剩余仓位被默认当作 0 收益，
    会导致系统性低估涨跌幅度。这里作为基线模型用于对比。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from core.predict.models.base import BaseModel, FundExposure, PredictionResult


class Top10OnlyModel(BaseModel):
    name = "top10_only"

    def __init__(self, apply_fx: bool = True, fx_ticker: str = "USDCNY=X"):
        self.apply_fx = apply_fx
        self.fx_ticker = fx_ticker

    def predict(
        self,
        exposure: FundExposure,
        prices: dict[str, pd.DataFrame],
        target_date: str,
        prev_nav_date: str,
        actual_pct: Optional[float] = None,
    ) -> PredictionResult:
        components: dict[str, float] = {}
        missing: list[str] = []
        weighted_return = 0.0
        covered = 0.0

        for h in exposure.top10:
            ticker = h.get("ticker")
            pct = h.get("pct", 0.0)
            if not ticker or ticker not in prices:
                missing.append(f"{h.get('code')}({h.get('name')})")
                continue
            ret = self._safe_pct_change(prices[ticker], target_date, prev_nav_date)
            if ret is None:
                missing.append(f"{ticker}@{target_date}")
                continue
            contribution = pct * ret
            components[h.get("name", ticker)] = contribution
            weighted_return += contribution
            covered += pct

        # 汇率调整：海外持仓部分会随 USD/CNY 浮动
        # Top10 中海外持仓占比 ≈ 海外 ticker 的 pct 之和
        if self.apply_fx and self.fx_ticker in prices:
            fx_ret = self._safe_pct_change(
                prices[self.fx_ticker], target_date, prev_nav_date
            )
            if fx_ret is not None:
                # 估算 Top10 中海外占比（A 股 ticker 含 .SS/.SZ/.HK 视为本币）
                foreign_pct_in_top10 = sum(
                    h["pct"]
                    for h in exposure.top10
                    if h.get("ticker") and not h["ticker"].endswith((".SS", ".SZ", ".HK"))
                )
                fx_contribution = foreign_pct_in_top10 * fx_ret
                components["__fx_USDCNY"] = fx_contribution
                weighted_return += fx_contribution

        return PredictionResult(
            fund_code=exposure.fund_code,
            target_date=target_date,
            model_name=self.name,
            predicted_pct=weighted_return,
            actual_pct=actual_pct,
            components=components,
            coverage_pct=covered,
            inputs_missing=missing,
            notes=f"Top10 覆盖 {covered*100:.1f}% NAV；剩余 {(1-covered)*100:.1f}% 默认 0 收益",
        )
