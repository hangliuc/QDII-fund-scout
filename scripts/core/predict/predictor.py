# -*- coding: utf-8 -*-
"""预测调度器：组装基金暴露 + 拉取行情 + 调用模型"""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import pdfplumber
import requests

from core.predict.models import (
    BaseModel,
    FundExposure,
    PredictionResult,
    Top10OnlyModel,
    RegionProxyModel,
    HybridModel,
    CalibratedModel,
)
from core.predict.models.hybrid import (
    DEFAULT_INDUSTRY_ETF_US,
    DEFAULT_INDUSTRY_ETF_CN,
)
from core.predict.models.region_proxy import DEFAULT_REGION_ETF
from core.quotes import QuoteSource, map_holding_to_ticker
from core.sources.csrc import CSRCSource, CSRC_PDF_URL, CSRC_HEADERS
from core.sources.eastmoney import EastMoneySource

logger = logging.getLogger(__name__)


class Predictor:
    """统一调度入口

    使用方式：
        p = Predictor()
        exposure = p.build_exposure("012922", main_code="012920", short_name="易方达全球成长精选混合（QDII）")
        prices = p.fetch_prices(exposure, start="2026-04-21", end="2026-06-02")
        result = p.predict_one(exposure, prices, target_date="2026-06-02",
                                prev_nav_date="2026-05-29", model="hybrid")
    """

    def __init__(
        self,
        report_year: str = "",
        target_quarter: str = "auto",
    ):
        self.csrc = CSRCSource(report_year=report_year, target_quarter=target_quarter)
        self.em = EastMoneySource()
        self.quotes = QuoteSource()

    # ------------------------------------------------------------------
    # 暴露画像构建（持仓 + 地区分布 + 行业分布 + 现金占比）
    # ------------------------------------------------------------------

    def build_exposure(
        self,
        fund_code: str,
        main_code: str = "",
        short_name: str = "",
        report_year: int | None = None,
    ) -> FundExposure:
        """从 CSRC + eastmoney 抓取真实数据组装基金暴露画像"""
        if not main_code:
            main_code = fund_code
        if report_year is None:
            report_year = datetime.now().year

        # 1) Top10 持仓 (eastmoney) - 取当年的最新季报
        quarters = self.em._fetch_holdings(fund_code, report_year)
        if not quarters:
            # 主代码可能比 C 类代码更稳定
            quarters = self.em._fetch_holdings(main_code, report_year)
        # 如果当年没数据(如 1 月初新年还没披露), 回退到去年
        if not quarters:
            quarters = self.em._fetch_holdings(fund_code, report_year - 1)
            if not quarters:
                quarters = self.em._fetch_holdings(main_code, report_year - 1)
        top10 = []
        if quarters:
            stocks = quarters[0].get("stocks", [])
            for s in stocks:
                pct_str = s.get("pct", "0%").rstrip("%").rstrip("％")
                try:
                    pct = float(pct_str) / 100.0
                except ValueError:
                    pct = 0.0
                ticker = map_holding_to_ticker(s.get("code", ""), s.get("name", ""))
                top10.append({
                    "code": s.get("code", ""),
                    "name": s.get("name", ""),
                    "pct": pct,
                    "ticker": ticker,
                    "shares": s.get("shares", ""),
                    "value": s.get("value", ""),
                })

        # 1b) 如果 eastmoney 没拿到 top10（FoF / QDII-LOF 持有 ETF 而非个股），
        # 退而求其次：从 CSRC PDF 解析"前十名基金投资明细"
        is_fof = False
        if not top10:
            fund_holdings = self.csrc.fetch_fund_holdings(main_code, short_name)
            if fund_holdings:
                is_fof = True
                from core.quotes import map_fund_name_to_ticker
                for fh in fund_holdings[:10]:
                    ticker = map_fund_name_to_ticker(fh["name"])
                    top10.append({
                        "code": ticker or "",  # 基金没有股票代码，借用 ticker
                        "name": fh["name"],
                        "pct": fh["pct"] / 100.0,
                        "ticker": ticker,
                        "value": fh.get("value", ""),
                    })

        # 2) 市场分布 (CSRC PDF)
        market_dist_raw = self.csrc.fetch_market_distribution(main_code, short_name)
        market_dist = {
            k: v / 100.0
            for k, v in market_dist_raw.items()
            if not k.startswith("_") and isinstance(v, (int, float))
        }
        total_equity_pct = market_dist_raw.get("_total_pct", 0.0) / 100.0

        # 3) 行业分布 (CSRC PDF)
        industry_dist_raw = self.csrc.fetch_industry_distribution(main_code, short_name)
        industry_dist = {
            k: v / 100.0
            for k, v in industry_dist_raw.items()
            if not k.startswith("_") and isinstance(v, (int, float))
        }

        # 4) 现金占比 = 1 - total_equity_pct  (粗略；精确值需解析"银行存款"行)
        # 但 total_equity_pct 已经是"股票占基金净值合计"，更精确做法见下面 enrich
        cash_pct = max(0.0, 1.0 - total_equity_pct) if total_equity_pct > 0 else 0.0

        # 5) 海外占比 = market_dist 中除"中国内地"外之和
        foreign_pct = sum(
            v for k, v in market_dist.items() if k != "中国内地"
        )

        report_quarter = market_dist_raw.get("_source", "").replace("csrc_", "") or f"{report_year}Q1"

        # 6) 兜底：FoF / ETF联接 没有市场分布表，但 Top10 已经是 ETF
        # 这种情况下 total_equity_pct 应该约等于 sum(top10 pct)，
        # 因为 FoF 的"权益投资"为 0，但"基金投资"接近 100%
        if is_fof and top10:
            total_equity_pct = sum(h["pct"] for h in top10)
            # 现金从 PDF 精确读
            cash_from_pdf = self._fetch_cash_pct(main_code, short_name)
            cash_pct = cash_from_pdf if cash_from_pdf > 0 else max(0.0, 1.0 - total_equity_pct - 0.02)
            # market_dist 由各 ETF 加总（多数美股 ETF 都是 100% 美国资产），简化处理
            if not market_dist:
                # 对 FoF 默认用美股加权，因为 mainstream QDII-FoF 几乎全是美股 ETF
                us_etf_pct = sum(
                    h["pct"] for h in top10
                    if h.get("ticker") and not h["ticker"].endswith((".SS", ".SZ", ".HK"))
                )
                if us_etf_pct > 0:
                    market_dist = {"美国": us_etf_pct}
                    foreign_pct = us_etf_pct

        # 6.5) ETF 联接基金兜底：top10 仍为空，且 fund_name 中含"ETF联接"/"指数"
        # 这类基金持有目标 ETF，整体收益 ≈ 目标 ETF × (1 - cash%)
        # 用基金简称匹配最相关的 yfinance ticker
        if not top10 and short_name:
            mapped = self._map_index_fund_to_etf(short_name)
            if mapped:
                ticker, est_weight = mapped
                top10 = [{
                    "code": ticker, "name": f"目标ETF代理:{ticker}",
                    "pct": est_weight, "ticker": ticker, "value": "",
                }]
                # 估算 cash% from PDF；retry 一次
                cash_from_pdf = self._fetch_cash_pct(main_code, short_name)
                cash_pct = cash_from_pdf if cash_from_pdf > 0 else (1.0 - est_weight)
                total_equity_pct = est_weight
                market_dist = {"美国": est_weight}
                foreign_pct = est_weight
                is_fof = True  # 走类 FoF 流程

        # 7) 兜底：如果 CSRC 没拿到 total_equity_pct，用 top10 + 大致估算
        if total_equity_pct == 0 and top10:
            cash_pct = self._fetch_cash_pct(main_code, short_name)
            if cash_pct > 0:
                total_equity_pct = max(0.0, 1.0 - cash_pct - 0.01)
            else:
                top10_total = sum(h["pct"] for h in top10)
                total_equity_pct = min(0.95, top10_total / 0.6)
                cash_pct = 0.05

        # 8) 如果 total_equity_pct 已知但 cash_pct 0，仍尝试从 PDF 精确读
        if total_equity_pct > 0 and cash_pct == 0:
            cash_pct_pdf = self._fetch_cash_pct(main_code, short_name)
            if cash_pct_pdf > 0:
                cash_pct = cash_pct_pdf

        return FundExposure(
            fund_code=fund_code,
            fund_name=short_name or fund_code,
            main_code=main_code,
            report_quarter=report_quarter,
            top10=top10,
            market_dist=market_dist,
            industry_dist=industry_dist,
            total_equity_pct=total_equity_pct,
            cash_pct=cash_pct,
            foreign_pct=foreign_pct,
        )

    # 指数基金/ETF联接 简称 → 代理 ETF + 估算权重（除掉 cash 拖累）
    # 这些都是公开的指数追踪关系
    _INDEX_FUND_PROXY = [
        # (匹配关键词列表, yfinance ticker, 默认权重)
        (["纳斯达克100", "纳指100", "纳斯达克 100"], "QQQ", 0.95),
        (["纳斯达克科技", "纳指科技"], "QQQ", 0.92),  # NDXT 与 QQQ 高度相关
        (["标普500", "标普 500", "标普500ETF"], "SPY", 0.95),
        (["标普信息技术", "标普科技"], "XLK", 0.95),
        (["道琼斯", "道指"], "DIA", 0.95),
        (["纳斯达克生物", "标普生物"], "IBB", 0.95),
        (["恒生科技", "恒科"], "3032.HK", 0.95),
        (["恒生互联网", "恒生互"], "513770.SS", 0.95),
        (["MSCI 美国", "MSCI美国"], "SPY", 0.93),
        (["费城半导体", "标普半导体"], "SOXX", 0.95),
        (["MSCI 中国", "MSCI中国"], "MCHI", 0.95),
        (["新兴市场"], "VWO", 0.93),
        (["全球科技", "全球互联网", "全球互联"], "QQQ", 0.85),
        (["欧洲", "MSCI 欧洲"], "VGK", 0.93),
        (["日本", "日经"], "EWJ", 0.93),
        (["印度"], "INDA", 0.93),
        (["黄金"], "GLD", 0.95),
        (["原油", "石油"], "USO", 0.95),
    ]

    def _map_index_fund_to_etf(self, short_name: str) -> tuple[str, float] | None:
        """根据基金简称识别指数追踪关系，返回 (代理ticker, 估算权重)

        权重 = 1 - 通常的现金拖累。被预测时还会加上汇率层。
        """
        if not short_name:
            return None
        # 是否为指数/ETF联接基金
        if not any(kw in short_name for kw in ("ETF联接", "指数", "ETF", "LOF")):
            return None
        for keywords, ticker, weight in self._INDEX_FUND_PROXY:
            for kw in keywords:
                if kw in short_name:
                    return ticker, weight
        return None

    def _fetch_cash_pct(self, main_code: str, short_name: str = "") -> float:
        """从 CSRC PDF 中抓取"银行存款和结算备付金"占基金净值比例"""
        try:
            rec = self.csrc.search_report(main_code, short_name)
            if not rec:
                return 0.0
            iid = str(rec.get("uploadInfoId", ""))
            resp = requests.get(
                CSRC_PDF_URL.format(iid=iid),
                headers=CSRC_HEADERS,
                timeout=45,
            )
            if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
                return 0.0
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    if "银行存款" in text:
                        # 形如 "7 银行存款和结算备付金合计 1,605,640,786.09 15.84"
                        m = re.search(
                            r"银行存款[^\n]*?[\d,，.]+\s+([\d.]+)\s*$",
                            text,
                            re.MULTILINE,
                        )
                        if m:
                            return float(m.group(1)) / 100.0
                        # 备用：行内匹配
                        for ln in text.split("\n"):
                            if "银行存款" in ln:
                                nums = re.findall(r"[\d,]+\.\d+", ln)
                                if nums:
                                    try:
                                        # 最后一个数字一般是百分比
                                        return float(nums[-1].replace(",", "")) / 100.0
                                    except ValueError:
                                        pass
        except Exception as e:
            logger.warning("拉取现金占比失败：%s", e)
        return 0.0

    # ------------------------------------------------------------------
    # 行情数据装载
    # ------------------------------------------------------------------

    def collect_required_tickers(self, exposure: FundExposure) -> list[str]:
        """收集模型需要的所有 yfinance ticker"""
        tickers: set[str] = set()
        # Top10
        for h in exposure.top10:
            if h.get("ticker"):
                tickers.add(h["ticker"])
        # 地区代理 ETF
        for region in exposure.market_dist:
            if region.startswith("_"):
                continue
            if region in DEFAULT_REGION_ETF:
                tickers.add(DEFAULT_REGION_ETF[region])
        # 行业代理 ETF
        for ind in exposure.industry_dist:
            if ind.startswith("_"):
                continue
            if ind in DEFAULT_INDUSTRY_ETF_US:
                tickers.add(DEFAULT_INDUSTRY_ETF_US[ind])
            if ind in DEFAULT_INDUSTRY_ETF_CN:
                tickers.add(DEFAULT_INDUSTRY_ETF_CN[ind])
        # 港股残余代理
        tickers.add("2800.HK")
        # 半导体专项代理（如果 Top10 半导体重仓，hybrid 会切到 SOXX）
        tickers.add("SOXX")
        # 汇率
        tickers.add("USDCNY=X")
        return sorted(tickers)

    def fetch_prices(
        self,
        exposure: FundExposure,
        start: str,
        end: str,
    ) -> dict[str, pd.DataFrame]:
        tickers = self.collect_required_tickers(exposure)
        return self.quotes.batch_get(tickers, start, end)

    # ------------------------------------------------------------------
    # 单日预测 / 多模型对比
    # ------------------------------------------------------------------

    MODELS = {
        "top10_only": Top10OnlyModel,
        "region_proxy": RegionProxyModel,
        "hybrid": HybridModel,
        "calibrated": CalibratedModel,
    }

    def predict_one(
        self,
        exposure: FundExposure,
        prices: dict[str, pd.DataFrame],
        target_date: str,
        prev_nav_date: str,
        model: str = "hybrid",
        actual_pct: Optional[float] = None,
    ) -> PredictionResult:
        if model not in self.MODELS:
            raise ValueError(f"未知模型 {model}, 可选: {list(self.MODELS)}")
        m = self.MODELS[model]()
        return m.predict(exposure, prices, target_date, prev_nav_date, actual_pct)

    def predict_all(
        self,
        exposure: FundExposure,
        prices: dict[str, pd.DataFrame],
        target_date: str,
        prev_nav_date: str,
        actual_pct: Optional[float] = None,
    ) -> dict[str, PredictionResult]:
        results = {}
        for name, cls in self.MODELS.items():
            if name == "calibrated":
                continue  # calibrated 需要历史，单点调用退化为 hybrid
            m = cls()
            results[name] = m.predict(
                exposure, prices, target_date, prev_nav_date, actual_pct
            )
        return results
