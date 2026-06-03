# -*- coding: utf-8 -*-
"""模型 ② 纯地区 ETF 代理

公式：
    预测涨跌 = Σ(地区_i 占比 × 地区_i 代表 ETF 涨跌) × FX 调整

代理 ETF 选择：
    美国 -> SPY (标普500，覆盖最广)
    中国内地 -> 510050.SS (上证50) 或 159915.SZ (创业板)
    中国香港 -> 2800.HK (盈富基金)
    日本 -> EWJ
    韩国 -> EWY
    印度 -> INDA
    ...

误差来源：
    完全忽略基金的实际行业偏向，例如 012922 重仓信息技术，
    用 SPY 代理"美国 46%" 会显著低估科技板块上涨日的收益。
    作为对比基线。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from core.predict.models.base import BaseModel, FundExposure, PredictionResult

# 地区到代理 ETF 的映射（可在 predictor 中覆盖）
DEFAULT_REGION_ETF = {
    "美国": "SPY",
    "中国内地": "510050.SS",   # 上证50
    "中国香港": "2800.HK",     # 盈富基金
    "日本": "EWJ",
    "韩国": "EWY",
    "印度": "INDA",
    "英国": "EWU",
    "德国": "EWG",
    "法国": "EWQ",
    "新加坡": "EWS",
    "澳大利亚": "EWA",
    "加拿大": "EWC",
    "瑞士": "EWL",
    "荷兰": "EWN",
    "巴西": "EWZ",
    "中国台湾": "EWT",
    "墨西哥": "EWW",
    "南非": "EZA",
}


class RegionProxyModel(BaseModel):
    name = "region_proxy"

    def __init__(
        self,
        region_etf_map: dict[str, str] | None = None,
        apply_fx: bool = True,
        fx_ticker: str = "USDCNY=X",
    ):
        self.region_etf_map = region_etf_map or DEFAULT_REGION_ETF
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

        for region, pct in exposure.market_dist.items():
            if region.startswith("_"):  # 元信息字段
                continue
            etf = self.region_etf_map.get(region)
            if not etf:
                missing.append(f"region={region}(no_proxy)")
                continue
            df = prices.get(etf)
            if df is None or df.empty:
                missing.append(f"{etf}(price_missing)")
                continue
            ret = self._safe_pct_change(df, target_date, prev_nav_date)
            if ret is None:
                missing.append(f"{etf}@{target_date}")
                continue
            contribution = pct * ret
            components[f"{region}({etf})"] = contribution
            weighted_return += contribution
            covered += pct

        # 汇率层：海外占比 × USDCNY 涨跌
        if self.apply_fx and self.fx_ticker in prices:
            fx_ret = self._safe_pct_change(
                prices[self.fx_ticker], target_date, prev_nav_date
            )
            if fx_ret is not None:
                foreign_pct = sum(
                    pct for region, pct in exposure.market_dist.items()
                    if not region.startswith("_") and region not in ("中国内地",)
                )
                fx_contribution = foreign_pct * fx_ret
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
            notes=f"地区代理覆盖 {covered*100:.1f}% NAV",
        )
