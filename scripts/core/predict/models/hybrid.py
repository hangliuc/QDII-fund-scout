# -*- coding: utf-8 -*-
"""模型 ③ Top10 精确 + 残余按行业 ETF 代理

公式：
    Top10 部分    = Σ(top10_i pct × top10_i 涨跌) ， 占 NAV 的 X%
    残余股票部分  = Σ(行业_j 残余比例 × 行业_j 代理 ETF 涨跌)
                   残余 = 行业总比例 - Top10 中归属该行业的占比
    现金部分      = cash_pct × 0  (假设)
    汇率层        = 海外占比 × USDCNY 涨跌

行业代理 ETF 选择（针对 GICS 分类）：
    信息技术    -> XLK   (科技板块 ETF, 含苹果/英伟达/微软等)
    半导体细分  -> SOXX  (用于电子/半导体重仓基金的更精确代理)
    通讯/电信   -> XLC
    可选消费    -> XLY
    必需消费    -> XLP
    工业        -> XLI
    金融        -> XLF
    保健        -> XLV
    能源        -> XLE
    材料        -> XLB
    公用事业    -> XLU
    房地产      -> XLRE

A 股残余代理（针对 "中国内地" 占比）:
    根据基金调性选择，默认科技股基金用 159915.SZ (创业板)，
    传统行业基金用 510050.SS (上证50)。
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from core.predict.models.base import BaseModel, FundExposure, PredictionResult

logger = logging.getLogger(__name__)


# GICS 行业到美股 ETF 的映射
# 默认 "信息技术" 用 XLK（覆盖最广），但对半导体重仓基金可在 predictor 中
# 覆盖为 SOXX 以提高精度。
DEFAULT_INDUSTRY_ETF_US = {
    "信息技术": "XLK",
    "信息科技": "XLK",     # 浦银安盛等用此名
    "科技": "XLK",         # 部分基金用此名
    "电信服务": "XLC",
    "通讯": "XLC",         # 旧 GICS 名
    "通信服务": "XLC",     # 华夏全球科技等用此名
    "可选消费": "XLY",
    "非必需消费品": "XLY",
    "非必须消费品": "XLY",  # 错别字也支持
    "消费者非必需品": "XLY",  # GICS 中文译法之一
    "非周期性消费品": "XLP",
    "必需消费品": "XLP",
    "必须消费品": "XLP",
    "日常消费": "XLP",
    "消费者常用品": "XLP",  # GICS 中文译法之一
    "工业": "XLI",
    "金融": "XLF",
    "保健": "XLV",
    "医疗保健": "XLV",
    "能源": "XLE",
    "材料": "XLB",
    "原材料": "XLB",
    "基础材料": "XLB",
    "公用事业": "XLU",
    "房地产": "XLRE",
    "地产建筑业": "XLRE",
    "其他-GICS未分类": "SPY",
    "其他-GICS 未分类": "SPY",
}

# A 股行业代理（暂用最相关的宽基/行业 ETF）
# 信息技术 / 工业 / 通信 这些重仓基金多数集中在科技板块，
# 优先用 512760.SS（半导体 ETF）反映高弹性，其次 159915.SZ（创业板）
DEFAULT_INDUSTRY_ETF_CN = {
    "信息技术": "512760.SS",
    "信息科技": "512760.SS",
    "科技": "512760.SS",
    "工业": "159915.SZ",
    "金融": "510050.SS",
    "可选消费": "510050.SS",
    "非必需消费品": "510050.SS",
    "非必须消费品": "510050.SS",
    "消费者非必需品": "510050.SS",
    "非周期性消费品": "510050.SS",
    "必需消费品": "510050.SS",
    "必须消费品": "510050.SS",
    "日常消费": "510050.SS",
    "消费者常用品": "510050.SS",
    "保健": "159915.SZ",
    "医疗保健": "159915.SZ",
    "能源": "510050.SS",
    "材料": "510050.SS",
    "原材料": "510050.SS",
    "基础材料": "510050.SS",
    "电信服务": "512760.SS",
    "通讯": "512760.SS",
    "通信服务": "512760.SS",
    "公用事业": "510050.SS",
    "房地产": "510050.SS",
    "地产建筑业": "510050.SS",
    "其他-GICS未分类": "510050.SS",
    "其他-GICS 未分类": "510050.SS",
}


class HybridModel(BaseModel):
    name = "hybrid"

    # 半导体相关股票名/代码识别（用于自动选择 SOXX 代理）
    SEMI_KEYWORDS = (
        "TSM", "TSEM", "AXTI", "AVGO", "NVDA", "AMD", "INTC", "MU", "AMAT",
        "ASML", "LRCX", "KLAC", "QCOM", "ARM", "MCHP", "ON", "MPWR",
        "台积电", "Tower", "AXT", "英伟达", "美光", "应材", "阿斯麦",
        "高通", "博通", "联电",
        "中芯", "韦尔", "兆易", "汇顶", "圣邦", "卓胜微", "北方华创",
        "中微", "拓荆", "新易盛", "中际旭创", "源杰", "光迅", "天孚",
        "Lumentum", "Coherent",  # 光通信但归 IT
    )
    # 中国 A 股科技代理：当 Top10 中创业板/科创板权重高时，用 159915.SZ；
    # 否则用沪深300 / 上证50 更稳定
    A_TECH_CODE_PREFIX = ("300", "688", "301")

    def __init__(
        self,
        industry_etf_us: dict[str, str] | None = None,
        industry_etf_cn: dict[str, str] | None = None,
        apply_fx: bool = True,
        fx_ticker: str = "USDCNY=X",
        # 当 Top10 中半导体相关股占比超过该阈值时，IT 残余代理切换为 SOXX
        semi_threshold: float = 0.10,
        semi_etf: str = "SOXX",
    ):
        self.industry_etf_us = industry_etf_us or DEFAULT_INDUSTRY_ETF_US
        self.industry_etf_cn = industry_etf_cn or DEFAULT_INDUSTRY_ETF_CN
        self.apply_fx = apply_fx
        self.fx_ticker = fx_ticker
        self.semi_threshold = semi_threshold
        self.semi_etf = semi_etf

    def _pick_us_it_proxy(self, exposure: FundExposure, prices: dict) -> str:
        """根据 Top10 中半导体股占比动态选择 IT 残余代理"""
        semi_pct = 0.0
        for h in exposure.top10:
            ticker = (h.get("ticker") or "").upper()
            name = h.get("name", "")
            code = (h.get("code") or "").upper()
            for kw in self.SEMI_KEYWORDS:
                kw_u = kw.upper()
                if kw_u == ticker or kw_u == code or kw in name or kw_u in name.upper():
                    semi_pct += h.get("pct", 0.0)
                    break
        if semi_pct >= self.semi_threshold and self.semi_etf in prices:
            return self.semi_etf
        return self.industry_etf_us.get("信息技术", "XLK")

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
        total_return = 0.0
        covered = 0.0

        # ====================== 第一步: Top10 精确部分 ======================
        top10_us_pct = 0.0   # Top10 中海外股票占比（用于汇率层）
        top10_per_industry: dict[str, float] = {}
        # 我们没有逐股票的行业归属信息，所以从 top10 整体扣减时，
        # 简单做法：把 Top10 总占比按 market_dist 中"美国/中国内地"比例反推

        for h in exposure.top10:
            ticker = h.get("ticker")
            pct = h.get("pct", 0.0)
            if not ticker:
                missing.append(f"{h.get('code')}({h.get('name')}, no_ticker)")
                continue
            df = prices.get(ticker)
            if df is None or df.empty:
                missing.append(f"{ticker}(price_missing)")
                continue
            ret = self._safe_pct_change(df, target_date, prev_nav_date)
            if ret is None:
                missing.append(f"{ticker}@{target_date}")
                continue
            contribution = pct * ret
            components[f"top10:{h.get('name', ticker)}"] = contribution
            total_return += contribution
            covered += pct

            # 标记是否海外（用于汇率层）
            if not ticker.endswith((".SS", ".SZ", ".HK")):
                top10_us_pct += pct

        # ====================== 第二步: 残余按行业代理 ======================
        # 残余股票总占比 = total_equity_pct - top10 已覆盖部分
        top10_total = exposure.top10_total_pct()
        residual_equity = max(0.0, exposure.total_equity_pct - top10_total)

        if residual_equity > 0 and exposure.industry_dist:
            # 每个行业的"残余"比例 = 该行业总比例 × (residual_equity / total_equity_pct)
            # 也即按比例缩放，等价于假设 Top10 在行业上的分布与全基金一致
            scale = residual_equity / max(exposure.total_equity_pct, 1e-9)

            # 计算各市场在残余中的比例（按地区分布加权）
            market_pcts = {
                k: v for k, v in exposure.market_dist.items()
                if not k.startswith("_") and v > 0
            }
            total_market = sum(market_pcts.values())
            if total_market <= 0:
                missing.append("market_dist_empty")
                total_market = 1.0
                market_ratios = {"美国": 1.0}  # fallback
            else:
                market_ratios = {k: v / total_market for k, v in market_pcts.items()}

            us_ratio = market_ratios.get("美国", 0.0)
            cn_ratio = market_ratios.get("中国内地", 0.0)
            hk_ratio = market_ratios.get("中国香港", 0.0)
            # 其他区域（韩国/日本/印度/英国/德国/...）也参与残余拟合
            other_regions = {
                k: v for k, v in market_ratios.items()
                if k not in ("美国", "中国内地", "中国香港")
            }

            for industry, ind_pct in exposure.industry_dist.items():
                if industry.startswith("_"):
                    continue
                residual_in_industry = ind_pct * scale  # 这部分在残余中的占比

                # 美股代理（IT 行业根据 Top10 半导体权重动态切换 XLK/SOXX）
                us_part = residual_in_industry * us_ratio
                if us_part > 0:
                    if industry in ("信息技术", "信息科技", "科技"):
                        etf_us = self._pick_us_it_proxy(exposure, prices)
                    else:
                        etf_us = self.industry_etf_us.get(industry)
                    if etf_us and etf_us in prices:
                        ret = self._safe_pct_change(
                            prices[etf_us], target_date, prev_nav_date
                        )
                        if ret is not None:
                            contrib = us_part * ret
                            components[f"residual_us:{industry}({etf_us})"] = contrib
                            total_return += contrib
                            covered += us_part
                            top10_us_pct += us_part  # 残余美股也走汇率层
                        else:
                            missing.append(f"{etf_us}@{target_date}")
                    else:
                        missing.append(f"residual_us:{industry}(no_etf)")

                # A 股代理
                cn_part = residual_in_industry * cn_ratio
                if cn_part > 0:
                    etf_cn = self.industry_etf_cn.get(industry)
                    if etf_cn and etf_cn in prices:
                        ret = self._safe_pct_change(
                            prices[etf_cn], target_date, prev_nav_date
                        )
                        if ret is not None:
                            contrib = cn_part * ret
                            components[f"residual_cn:{industry}({etf_cn})"] = contrib
                            total_return += contrib
                            covered += cn_part
                        else:
                            missing.append(f"{etf_cn}@{target_date}")
                    else:
                        missing.append(f"residual_cn:{industry}(no_etf)")

                # 港股代理（统一用 2800.HK）
                hk_part = residual_in_industry * hk_ratio
                if hk_part > 0:
                    etf_hk = "2800.HK"
                    if etf_hk in prices:
                        ret = self._safe_pct_change(
                            prices[etf_hk], target_date, prev_nav_date
                        )
                        if ret is not None:
                            contrib = hk_part * ret
                            components[f"residual_hk:{industry}({etf_hk})"] = contrib
                            total_return += contrib
                            covered += hk_part
                            top10_us_pct += hk_part * 0.0  # 港股不走 USDCNY

                # 其他海外区域代理（韩国/日本/印度/英国/...）
                # 跨区域的行业 ETF 缺失，简化为用区域 ETF 全量代理
                # 注意：这部分不再做行业切分，因为行业 ETF 在韩国等市场不成熟
                for region, region_ratio in other_regions.items():
                    if region_ratio <= 0:
                        continue
                    # 该地区在残余中的总占比 = 残余 × region_ratio
                    # 但要避免在每个 industry 循环里重复加：只在第一次循环时加
                    pass  # 见下面的 region-level 处理

            # ====== Region-level 残余（一次性，不按行业拆分） ======
            for region, region_ratio in other_regions.items():
                region_part = residual_equity * region_ratio
                if region_part <= 0:
                    continue
                # 该地区代理 ETF
                from core.predict.models.region_proxy import DEFAULT_REGION_ETF
                etf_r = DEFAULT_REGION_ETF.get(region)
                if etf_r and etf_r in prices:
                    ret = self._safe_pct_change(
                        prices[etf_r], target_date, prev_nav_date
                    )
                    if ret is not None:
                        contrib = region_part * ret
                        components[f"residual_{region}({etf_r})"] = contrib
                        total_return += contrib
                        covered += region_part
                        # 海外区域算入汇率敞口（韩元/日元/...单独汇率简化为 USDCNY 近似）
                        top10_us_pct += region_part
                    else:
                        missing.append(f"{etf_r}@{target_date}")
                else:
                    missing.append(f"residual_{region}(no_etf)")

        # ====================== 第三步: 汇率层 ======================
        if self.apply_fx and self.fx_ticker in prices:
            fx_ret = self._safe_pct_change(
                prices[self.fx_ticker], target_date, prev_nav_date
            )
            if fx_ret is not None:
                # 海外股票占比 ≈ Top10 海外 + 残余的美股/港股部分
                # （上面累加 top10_us_pct 已包含残余美股；港股汇率冲击约 0.7×USDCNY 但简化处理）
                fx_contribution = top10_us_pct * fx_ret
                components["__fx_USDCNY"] = fx_contribution
                total_return += fx_contribution

        # ====================== 第四步: 现金 + 费率拖累（占位） ======================
        # 现金按 0 收益处理（货币市场利息忽略，单日 < 0.01%）
        # 费率拖累平均 0.4% / 365 ≈ 0.001% / 天，单日忽略不计

        notes_parts = [
            f"Top10 覆盖 {top10_total*100:.1f}%",
            f"残余股票 {residual_equity*100:.1f}% 用行业代理",
            f"现金 {exposure.cash_pct*100:.1f}% 计 0 收益",
        ]

        return PredictionResult(
            fund_code=exposure.fund_code,
            target_date=target_date,
            model_name=self.name,
            predicted_pct=total_return,
            actual_pct=actual_pct,
            components=components,
            coverage_pct=covered,
            inputs_missing=missing,
            notes="；".join(notes_parts),
        )
