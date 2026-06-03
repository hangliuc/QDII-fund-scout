# -*- coding: utf-8 -*-
"""为每只基金生成「最新涨跌」— 一行展示

规则：
- 美股最近已收盘日 = us_last (从 yfinance 取)
- 基金最新已公布 NAV 日 = nav_last (从天天基金 API 取)
- 如果 nav_last >= us_last → 显示真值（NAV 已公布）
- 如果 nav_last < us_last → 估算 us_last 那天的涨跌（NAV 未公布）

输出:
{
  "value": -0.83 | 1.90,            # 涨跌值（%）
  "date": "2026-06-01",             # 对应日期
  "is_estimate": False | True,      # 是否为估算
}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _get_us_last_trading_day() -> str | None:
    """获取美股最近已收盘交易日。

    判断方式：基于时钟（美股收盘 = 北京 04:00 夏令时 / 05:00 冬令时，统一用 05:00）。
    - 北京时间 >= 05:00 → 美股"今天"已收盘（如果今天是美股交易日）
    - 北京时间 < 05:00 → 美股"今天"还没收盘，最近收盘是"昨天"

    美股交易日 = 周一到周五（不含美国节假日，此处简化为只排周末）。
    """
    from datetime import date
    now = datetime.now()
    
    if now.hour >= 5:
        # 今天（北京日期）的美股交易日已收盘
        # 美股日期 = 北京日期 - 1 天（因为北京 05:00 对应美东前一天 16:00/17:00）
        us_date = (now.date() - timedelta(days=1))
    else:
        # 还没到 05:00，美股"今天"没收盘
        # 最近收盘是"前天"的美股日期
        us_date = (now.date() - timedelta(days=2))
    
    # 回退到最近一个工作日（排除周末）
    while us_date.weekday() >= 5:  # 5=周六, 6=周日
        us_date -= timedelta(days=1)
    
    return us_date.isoformat()


def predict_t1_for_fund(
    fund_code: str,
    main_code: str,
    short_name: str,
    nav_history: list[dict] | None = None,
    model: str = "hybrid",
) -> dict:
    """一行规则：NAV 已公布显示真值，否则显示估算。"""
    try:
        from core.predict.predictor import Predictor
        from core.predict.backtest import fetch_nav_series
        from core.predict.models import HybridModel
    except ImportError:
        return {"value": None, "date": "", "is_estimate": False}

    # 1) 拉基金最新 NAV
    if not nav_history:
        try:
            today = datetime.now().date()
            start = (today - timedelta(days=30)).isoformat()
            df = fetch_nav_series(fund_code, start, today.isoformat())
            if df.empty:
                return {"value": None, "date": "", "is_estimate": False}
            nav_history = df.to_dict("records")
        except Exception:
            return {"value": None, "date": "", "is_estimate": False}

    if not nav_history:
        return {"value": None, "date": "", "is_estimate": False}

    last_nav = nav_history[-1]
    nav_last_date = last_nav["date"]
    nav_last_change = last_nav.get("change_pct")

    # 2) 美股最近已收盘日
    us_last = _get_us_last_trading_day()
    if not us_last:
        # 拿不到美股数据，退回显示已公布真值
        if nav_last_change is not None:
            return {"value": round(nav_last_change * 100, 2), "date": nav_last_date, "is_estimate": False}
        return {"value": None, "date": "", "is_estimate": False}

    # 3) 判断
    if nav_last_date >= us_last:
        # NAV 已公布到美股最近收盘日（或更晚），直接显示真值
        return {"value": round(nav_last_change * 100, 2) if nav_last_change is not None else None,
                "date": nav_last_date, "is_estimate": False}

    # 4) NAV 未公布 → 估算 us_last 那天的涨跌
    target_date = us_last
    last_dt = datetime.fromisoformat(nav_last_date).date()
    target_dt = datetime.fromisoformat(target_date).date()

    try:
        p = Predictor(target_quarter="auto")
        exposure = p.build_exposure(fund_code, main_code, short_name)
        price_start = (last_dt - timedelta(days=10)).isoformat()
        price_end = (target_dt + timedelta(days=2)).isoformat()
        prices = p.fetch_prices(exposure, price_start, price_end)
        m = HybridModel()
        pred = m.predict(exposure, prices, target_date, nav_last_date)
        return {"value": round(pred.predicted_pct * 100, 2), "date": target_date, "is_estimate": True}
    except Exception as e:
        logger.debug("T-1 估算失败 %s: %s", fund_code, e)
        # 估算失败，退回显示已公布真值
        if nav_last_change is not None:
            return {"value": round(nav_last_change * 100, 2), "date": nav_last_date, "is_estimate": False}
        return {"value": None, "date": "", "is_estimate": False}
