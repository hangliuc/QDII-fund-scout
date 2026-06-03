# -*- coding: utf-8 -*-
"""行情数据层：yfinance 包装 + 本地缓存。

设计原则：
- 真实数据来源唯一（yfinance），失败显式抛出，不伪造价格
- 本地 Parquet/CSV 缓存避免重复请求
- 港股 / 美股 / A 股 / 汇率统一接口
"""
from core.quotes.yfinance_quotes import (
    QuoteSource,
    QuoteError,
    map_holding_to_ticker,
    map_fund_name_to_ticker,
)

__all__ = ["QuoteSource", "QuoteError", "map_holding_to_ticker", "map_fund_name_to_ticker"]
