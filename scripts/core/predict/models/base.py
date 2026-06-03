# -*- coding: utf-8 -*-
"""预测模型基类与通用数据结构"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class FundExposure:
    """基金真实暴露画像（来自 CSRC 季报 + Top10 持仓）

    所有数据必须从 CSRC PDF 或 eastmoney 持仓接口抓取，禁止伪造。
    """
    fund_code: str
    fund_name: str
    main_code: str
    report_quarter: str  # 如 "2026Q1"

    # Top10 持仓: list of {code, name, pct(0~1), ticker(yfinance)}
    top10: list[dict] = field(default_factory=list)

    # 市场分布: {"美国": 0.46, "中国内地": 0.33, ...} 数值为占基金净值小数
    market_dist: dict[str, float] = field(default_factory=dict)

    # 行业分布: {"信息技术": 0.71, "工业": 0.07, ...}
    industry_dist: dict[str, float] = field(default_factory=dict)

    # CSRC 报告中的 "股票占基金资产净值合计" (84.75% -> 0.8475)
    total_equity_pct: float = 0.0

    # 现金占比 (从 CSRC 7. 银行存款行解析或 1 - total_equity_pct)
    cash_pct: float = 0.0

    # 美元/海外资产敞口（人民币计价基金的汇率因子权重）
    foreign_pct: float = 0.0

    def top10_total_pct(self) -> float:
        return sum(h.get("pct", 0) for h in self.top10)

    def top10_with_ticker(self) -> list[dict]:
        """只返回能映射到 yfinance ticker 的持仓"""
        return [h for h in self.top10 if h.get("ticker")]


@dataclass
class PredictionResult:
    """单日预测输出"""
    fund_code: str
    target_date: str  # 预测的目标交易日 (T 日)
    model_name: str

    predicted_pct: float  # 预测涨跌幅（小数，0.01 = +1%）
    actual_pct: float | None = None  # 真实涨跌幅（如果已知）

    # 因子分解（用于解释模型）
    components: dict[str, float] = field(default_factory=dict)

    # 元信息
    coverage_pct: float = 0.0  # 模型解释的 NAV 占比
    inputs_missing: list[str] = field(default_factory=list)  # 缺失的因子
    notes: str = ""

    @property
    def error(self) -> float | None:
        """绝对误差(percentage point), 如 0.3 表示预测偏差 0.3pp"""
        if self.actual_pct is None:
            return None
        return abs(self.predicted_pct - self.actual_pct) * 100

    @property
    def signed_error(self) -> float | None:
        """有符号误差（预测 - 实际，pp）"""
        if self.actual_pct is None:
            return None
        return (self.predicted_pct - self.actual_pct) * 100

    def to_dict(self) -> dict:
        return {
            "fund_code": self.fund_code,
            "target_date": self.target_date,
            "model_name": self.model_name,
            "predicted_pct": round(self.predicted_pct * 100, 4),
            "actual_pct": round(self.actual_pct * 100, 4) if self.actual_pct is not None else None,
            "error_pp": round(self.error, 4) if self.error is not None else None,
            "signed_error_pp": round(self.signed_error, 4) if self.signed_error is not None else None,
            "components": {k: round(v * 100, 4) for k, v in self.components.items()},
            "coverage_pct": round(self.coverage_pct * 100, 2),
            "inputs_missing": self.inputs_missing,
            "notes": self.notes,
        }


class BaseModel:
    """所有预测模型的抽象基类"""

    name: str = "base"

    def predict(
        self,
        exposure: FundExposure,
        prices: dict[str, pd.DataFrame],  # ticker -> price DataFrame
        target_date: str,                  # 要预测的交易日 (yyyy-mm-dd)
        prev_nav_date: str,                # 上一个已公布 NAV 的交易日
        actual_pct: Optional[float] = None,
    ) -> PredictionResult:
        raise NotImplementedError

    @staticmethod
    def _safe_pct_change(
        df: pd.DataFrame,
        target_date: str,
        prev_date: str,
    ) -> Optional[float]:
        """从价格 DataFrame 计算 prev_date -> target_date 的涨跌幅。

        关键细节：
        - 如果 target_date / prev_date 在该 ticker 时区不是交易日（如美股周末），
          会自动取最近一个有效交易日的收盘价。
        - 海外股票（如美股）的 T 日收盘是北京时间 T+1 凌晨 5:00，因此
          相对中国基金 T 日 NAV 而言，应使用 yfinance 索引中的 (T-1) 日收盘
          —— 这一对齐由调用方在 backtest 中显式处理，本函数不主动 shift。
        """
        if df is None or df.empty:
            return None
        try:
            close = df["Close"]
            tgt = pd.Timestamp(target_date)
            prv = pd.Timestamp(prev_date)
            # 用 .asof 找 ≤ 该日期的最后一个交易日收盘
            tgt_close = close.asof(tgt)
            prv_close = close.asof(prv)
            if pd.isna(tgt_close) or pd.isna(prv_close) or prv_close == 0:
                return None
            return float((tgt_close - prv_close) / prv_close)
        except Exception:
            return None
