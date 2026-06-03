# -*- coding: utf-8 -*-
"""yfinance 行情拉取 + 本地 CSV 缓存

提供统一接口：
- get_history(ticker, start, end) -> pd.DataFrame  (Date 索引, columns: Open/High/Low/Close/Volume)
- batch_get(tickers, start, end) -> dict[ticker, DataFrame]

支持的 ticker 形式：
- 美股 : 'TSM' / 'GOOGL'
- 港股 : '0700.HK'
- A股(深) : '300502.SZ'
- A股(沪) : '688498.SS' / '600519.SS'
- 汇率 : 'USDCNY=X' / 'CNY=X'
- ETF代理 : 'QQQ' / 'XLK' / '510050.SS'
"""
from __future__ import annotations

import json
import logging
import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import re

import pandas as pd

logger = logging.getLogger(__name__)

# 静音 yfinance 内部 urllib3 告警
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

# 默认缓存目录
DEFAULT_CACHE_DIR = os.path.expanduser("~/.fund-scout/quotes_cache")


class QuoteError(Exception):
    """行情数据获取失败"""


def _normalize_a_share(code: str) -> str:
    """把 6 位 A 股代码加上 yfinance 后缀"""
    if not code or not code.isdigit() or len(code) != 6:
        return code
    # 沪市: 600/601/603/605/688/689/900
    # 深市: 000/001/002/003/300/301/200
    if code.startswith(("60", "68", "69", "90")):
        return f"{code}.SS"
    if code.startswith(("00", "30", "20")):
        return f"{code}.SZ"
    return code


def _normalize_hk_share(code: str) -> str:
    """5 位港股代码 -> '0XXXX.HK'"""
    if not code:
        return code
    bare = code.strip().lstrip("0")
    if bare.isdigit():
        return f"{int(bare):04d}.HK"
    return code


# 港股代码 → ADR 备用映射（部分港股代码冲突 A 股的情况）
# 已知的中港同代码冲突：
#   000660 = SK海力士(韩国) ≠ 老白干(A股)
#   005930 = 三星电子(韩国) ≠ 莱茵生物 (000591) - 不冲突，但 005930 在 A 股不存在
#   035420 = NAVER(韩国)
# 海外股票（非数字开头的代码）在 eastmoney 持仓中常常用其原市场代码，
# 我们用名字辅助判断
KR_NAME_HINTS = {
    "SK海力士": "000660.KS", "海力士": "000660.KS", "Hynix": "000660.KS",
    "三星电子": "005930.KS", "Samsung Electronics": "005930.KS",
    "NAVER": "035420.KS", "Kakao": "035720.KS",
    "现代汽车": "005380.KS", "起亚": "000270.KS",
    "LG化学": "051910.KS", "POSCO": "005490.KS",
}
JP_NAME_HINTS = {
    "丰田": "7203.T", "丰田汽车": "7203.T",
    "索尼": "6758.T", "索尼集团": "6758.T",
    "任天堂": "7974.T", "三菱UFJ": "8306.T",
    "软银": "9984.T", "软银集团": "9984.T",
    "日立": "6501.T", "村田制作所": "6981.T",
    "信越化学": "4063.T", "东京电子": "8035.T",
    "迅销": "9983.T", "优衣库": "9983.T",
    "电装": "6902.T", "捷太格特": "6473.T",
}


def map_holding_to_ticker(stock_code: str, stock_name: str = "") -> str | None:
    """把 csrc / eastmoney 持仓数据中的股票代码转为 yfinance ticker

    关键设计：当代码可能歧义（如韩国 000660 vs A股 000660），优先使用 stock_name
    匹配韩国/日本股票名提示。
    """
    if not stock_code:
        if stock_name:
            t = map_fund_name_to_ticker(stock_name)
            if t:
                return t
        return None

    code = stock_code.strip().upper()
    name = (stock_name or "").strip()

    # 先用 name 判断是否韩国/日本股票（这种情况下代码不能用 A 股映射）
    for hint_name, hint_ticker in KR_NAME_HINTS.items():
        if hint_name in name:
            return hint_ticker
    for hint_name, hint_ticker in JP_NAME_HINTS.items():
        if hint_name in name:
            return hint_ticker

    # 已经是 yfinance 格式
    if "." in code or "=" in code or "-" in code:
        return code

    # 6 位数字：A 股 (除非 name 含外国股票特征)
    if code.isdigit() and len(code) == 6:
        return _normalize_a_share(code)

    # 5 位数字港股
    if code.isdigit() and len(code) == 5:
        return _normalize_hk_share(code)

    # 字母代码（美股 NASDAQ/NYSE）
    if code.isalpha() and 1 <= len(code) <= 5:
        return code

    # 字母 + 数字 / 字母 + 横线（特殊格式）
    if code.replace("-", "").replace(".", "").isalnum():
        return code

    logger.info("无法映射持仓代码 %r (name=%r) 到 yfinance ticker", stock_code, stock_name)
    return None


# 常见 QDII 基金可能持有的境外 ETF / ADR 名称 → yfinance ticker
# 所有映射均为公开市场标准代码
FUND_NAME_TICKER_MAP = {
    # ARK 系列
    "ARK Innovation ETF": "ARKK",
    "ARK Genomic Revolution ETF": "ARKG",
    "ARK Autonomous Technology & Robotics ETF": "ARKQ",
    "ARK Next Generation Internet ETF": "ARKW",
    "ARK Fintech Innovation ETF": "ARKF",
    "ARK Space Exploration & Innovation ETF": "ARKX",
    # iShares 系列
    "iShares Semiconductor ETF": "SOXX",
    "iShares MSCI China ETF": "MCHI",
    "iShares 20+ Year Treasury Bond ETF": "TLT",
    # SPDR Sector
    "Technology Select Sector SPDR ETF": "XLK",
    "Technology Select Sector SPDR Fund": "XLK",
    "SPDR S&P 500 ETF Trust": "SPY",
    # Invesco
    "Invesco QQQ Trust Series 1": "QQQ",
    "Invesco QQQ Trust": "QQQ",
    "Invesco QQQ": "QQQ",
    # Global X
    "Global X Artificial Intelligence & Technology ETF": "AIQ",
    "Global X Robotics & Artificial Intelligence ETF": "BOTZ",
    "Global X FinTech ETF": "FINX",
    "Global X Semiconductor ETF": "SOXQ",
    "Global X China Electric Vehicle and Battery ETF": "2845.HK",
    # VanEck
    "VanEck Semiconductor ETF": "SMH",
    "VanEck Vectors Semiconductor ETF": "SMH",
    # Vanguard
    "Vanguard Total Stock Market ETF": "VTI",
    "Vanguard FTSE Emerging Markets ETF": "VWO",
    "Vanguard S&P 500 ETF": "VOO",
    # 指数基金/纳斯达克类
    "Nasdaq 100 ETF": "QQQ",
    # 美股龙头 ADR / 个股（中文译名）
    "苹果": "AAPL", "苹果公司": "AAPL",
    "微软": "MSFT",
    "亚马逊": "AMZN",
    "META": "META", "Meta": "META", "Facebook": "META",
    "特斯拉": "TSLA",
    "英伟达": "NVDA",
    "台积电": "TSM",
    "谷歌": "GOOGL", "谷歌-A": "GOOGL", "谷歌-C": "GOOG",
    "奈飞": "NFLX", "Netflix": "NFLX",
    "博通": "AVGO",
    "美光": "MU",
    "应用材料": "AMAT",
    "拉姆研究": "LRCX",
    "高通": "QCOM",
    "礼来": "LLY",
    "强生": "JNJ",
    "ASML": "ASML", "阿斯麦": "ASML",
    "ARM": "ARM",
    "Coherent": "COHR",
    "Lumentum": "LITE",
    "康宁": "GLW",
}


def map_fund_name_to_ticker(name: str) -> str | None:
    """根据基金/ETF 中文或英文名称查找 yfinance ticker。

    匹配优先级：精确匹配 > 关键词 token 匹配 > 子串匹配。
    PDF 解析常常把名字打散，所以用 token-级匹配最稳。
    """
    if not name:
        return None
    cleaned = re.sub(r"\s+", " ", name).strip()
    # 精确
    if cleaned in FUND_NAME_TICKER_MAP:
        return FUND_NAME_TICKER_MAP[cleaned]

    cleaned_upper = cleaned.upper()

    # 优先级特征关键词（强信号）
    # 顺序很重要：更具体的特征要排在前面
    SIGNATURE = [
        # (特征 token 集合, ticker)
        ({"INNOVATION", "ARK"}, "ARKK"),
        ({"GENOMIC", "REVOLUTION"}, "ARKG"),
        ({"AUTONOMOUS", "ROBOTICS"}, "ARKQ"),
        ({"NEXT GENERATION INTERNET"}, "ARKW"),
        ({"FINTECH", "ARK"}, "ARKF"),
        ({"ARK SPACE"}, "ARKX"),
        ({"GLOBAL X", "ARTIFICIAL", "INTELLIGENCE", "TECHNOLOGY"}, "AIQ"),
        ({"GLOBAL X", "ROBOTICS", "ARTIFICIAL"}, "BOTZ"),
        ({"GLOBAL X", "FINTECH"}, "FINX"),
        ({"GLOBAL X", "SEMICONDUCTOR"}, "SOXQ"),
        ({"GLOBAL X", "ELECTRIC VEHICLE"}, "2845.HK"),
        ({"VANECK", "SEMICONDUCTOR"}, "SMH"),
        ({"VAN ECK", "SEMICONDUCTOR"}, "SMH"),
        ({"ISHARES", "SEMICONDUCTOR"}, "SOXX"),
        ({"INVESCO", "QQQ"}, "QQQ"),
        ({"TECHNOLOGY", "SELECT", "SECTOR"}, "XLK"),
        ({"SPDR", "S&P 500"}, "SPY"),
        ({"VANGUARD", "TOTAL STOCK"}, "VTI"),
        ({"VANGUARD", "S&P 500"}, "VOO"),
        ({"VANGUARD", "EMERGING"}, "VWO"),
        ({"ISHARES", "MSCI CHINA"}, "MCHI"),
    ]

    for feature_set, ticker in SIGNATURE:
        if all(feat in cleaned_upper for feat in feature_set):
            return ticker

    # 退到子串匹配（保留原逻辑）
    for k, v in FUND_NAME_TICKER_MAP.items():
        if k in cleaned or cleaned in k:
            return v
    return None


class QuoteSource:
    """yfinance 行情数据源（带本地 CSV 缓存）"""

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR, max_age_days: int = 1):
        self.cache_dir = cache_dir
        self.max_age_days = max_age_days
        os.makedirs(self.cache_dir, exist_ok=True)
        # 延迟导入 yfinance 减少模块加载时间
        self._yf = None

    def _get_yf(self):
        if self._yf is None:
            import yfinance as yf  # noqa: WPS433
            self._yf = yf
        return self._yf

    def _cache_path(self, ticker: str) -> str:
        safe = ticker.replace("/", "_").replace("=", "_eq_")
        return os.path.join(self.cache_dir, f"{safe}.csv")

    def _load_cache(self, ticker: str) -> pd.DataFrame | None:
        path = self._cache_path(ticker)
        if not os.path.exists(path):
            return None
        # 缓存过期判定：超过 max_age_days 则丢弃，强制重拉
        mtime = os.path.getmtime(path)
        if time.time() - mtime > self.max_age_days * 86400:
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            return df
        except Exception as e:
            logger.warning("读取缓存 %s 失败：%s", path, e)
            return None

    def _save_cache(self, ticker: str, df: pd.DataFrame) -> None:
        try:
            df.to_csv(self._cache_path(ticker))
        except Exception as e:
            logger.warning("写缓存 %s 失败：%s", ticker, e)

    def get_history(
        self,
        ticker: str,
        start: str,
        end: str,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """获取单个 ticker 的历史日线（adjusted close）

        返回的 DataFrame 索引为日期（按 ticker 时区的交易日），
        列至少包括 'Close'，对应除权除息调整后的收盘价。
        """
        # 先看缓存
        if use_cache:
            cached = self._load_cache(ticker)
            if cached is not None:
                # 检查覆盖区间是否够（注意：ticker 时区可能让 start/end 在缓存范围外略微浮动）
                cstart = cached.index.min().date().isoformat() if len(cached) else "9999-01-01"
                cend = cached.index.max().date().isoformat() if len(cached) else "0000-01-01"
                if cstart <= start and cend >= end:
                    mask = (cached.index >= pd.Timestamp(start)) & (cached.index <= pd.Timestamp(end))
                    return cached.loc[mask].copy()

        # 拉网络
        yf = self._get_yf()
        # 多拉 5 天 buffer 防止时区/节假日导致缺数
        buf_start = (datetime.fromisoformat(start) - timedelta(days=5)).date().isoformat()
        buf_end = (datetime.fromisoformat(end) + timedelta(days=2)).date().isoformat()
        try:
            df = yf.download(
                ticker,
                start=buf_start,
                end=buf_end,
                auto_adjust=True,
                progress=False,
                threads=False,
            )
        except Exception as e:
            raise QuoteError(f"yfinance 下载 {ticker} 失败: {e}") from e

        if df is None or df.empty:
            raise QuoteError(f"yfinance 返回空数据: {ticker} ({start}~{end})")

        # yfinance 在新版本会返回 MultiIndex 列；如果是 (field, ticker) 形式，
        # 取出对应 ticker 的列；否则按第一层 field 平铺
        if isinstance(df.columns, pd.MultiIndex):
            # 标准结构: ('Close', 'TSM') / ('High', 'TSM') ...
            # 也有: ('TSM', 'Close') 反向结构（罕见）
            # 取出与 ticker 匹配的列
            if ticker in df.columns.get_level_values(-1):
                df = df.xs(ticker, axis=1, level=-1)
            elif ticker in df.columns.get_level_values(0):
                df = df.xs(ticker, axis=1, level=0)
            else:
                # 退回去重命名第一层
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                # 去重列名（防止 Close, Close, Close 重复）
                df = df.loc[:, ~df.columns.duplicated()]

        # 必须保留 Close
        if "Close" not in df.columns:
            raise QuoteError(f"yfinance 返回缺少 Close 列: {ticker}, columns={list(df.columns)}")

        # 标准化索引：去掉时区信息，只保留日期
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()

        if use_cache:
            self._save_cache(ticker, df)

        mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
        return df.loc[mask].copy()

    def batch_get(
        self,
        tickers: list[str],
        start: str,
        end: str,
        use_cache: bool = True,
        max_workers: int = 2,
    ) -> dict[str, pd.DataFrame]:
        """并发批量拉取。失败的 ticker 不在返回 dict 中，但会 log。"""
        results: dict[str, pd.DataFrame] = {}
        if not tickers:
            return results

        # 去重
        unique_tickers = list(dict.fromkeys(tickers))

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {
                ex.submit(self.get_history, t, start, end, use_cache): t
                for t in unique_tickers
            }
            for fut in as_completed(fut_map):
                ticker = fut_map[fut]
                try:
                    df = fut.result()
                    if not df.empty:
                        results[ticker] = df
                    else:
                        logger.warning("ticker %s 返回空，跳过", ticker)
                except QuoteError as e:
                    logger.warning("ticker %s 拉取失败：%s", ticker, e)
                except Exception as e:
                    logger.warning("ticker %s 异常：%s", ticker, e)

        return results

    def get_close_series(
        self,
        ticker: str,
        start: str,
        end: str,
        use_cache: bool = True,
    ) -> pd.Series:
        """便利接口，仅返回 Close 列"""
        df = self.get_history(ticker, start, end, use_cache=use_cache)
        return df["Close"].rename(ticker)

    def get_pct_change(
        self,
        ticker: str,
        start: str,
        end: str,
        use_cache: bool = True,
    ) -> pd.Series:
        """日收益率序列（小数，0.01 表示 +1%）"""
        s = self.get_close_series(ticker, start, end, use_cache=use_cache)
        return s.pct_change().dropna().rename(ticker)
