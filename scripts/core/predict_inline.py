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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# 北京时区（基金会计基于 CST = UTC+8）
CST = timezone(timedelta(hours=8))


def _now_cst() -> datetime:
    """统一返回北京时区的当前时间（不依赖本机 tz）"""
    return datetime.now(CST)


def _today_cst() -> "datetime.date":
    return _now_cst().date()


def _get_us_last_trading_day() -> str | None:
    """获取美股最近已收盘交易日。

    判断方式：基于时钟（美股收盘 = 北京 04:00 夏令时 / 05:00 冬令时，统一用 05:00）。
    - 北京时间 >= 05:00 → 美股"今天"已收盘（如果今天是美股交易日）
    - 北京时间 < 05:00 → 美股"今天"还没收盘，最近收盘是"昨天"

    美股交易日 = 周一到周五（不含美国节假日，此处简化为只排周末）。
    """
    now = _now_cst()

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


def predict_t1_batch(
    funds: list[dict],
    target_date: str | None = None,
) -> dict[str, dict]:
    """批量为多只基金计算 T-1 估值。

    与 predict_t1_for_fund 的关键区别：所有基金共享一次 fetch_prices 调用，
    把全部 ticker 一次性拉完。在大多数 QDII 基金 ticker 高度重叠的场景下，
    比逐只调用快 3-5 倍。

    funds: [{"code": "012922", "main_code": "012920", "short_name": "易方达全球成长精选"}, ...]
    返回: {"012922": {"value": -1.85, "date": "2026-06-05", "is_estimate": True}, ...}
    """
    out: dict[str, dict] = {}
    if not funds:
        return out

    try:
        from core.predict.predictor import Predictor
        from core.predict.backtest import fetch_nav_series
        from core.predict.models import HybridModel
    except ImportError:
        return {f["code"]: {"value": None, "date": "", "is_estimate": False} for f in funds}

    today = _today_cst()
    nav_start = (today - timedelta(days=30)).isoformat()
    today_iso = today.isoformat()

    # 1) 并行拉每只基金的 NAV 历史
    nav_results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(len(funds), 6)) as ex:
        fut_map = {
            ex.submit(fetch_nav_series, f["code"], nav_start, today_iso): f["code"]
            for f in funds
        }
        for fut in as_completed(fut_map):
            code = fut_map[fut]
            try:
                df = fut.result(timeout=20)
                nav_results[code] = df.to_dict("records") if not df.empty else []
            except Exception:
                nav_results[code] = []

    us_last = _get_us_last_trading_day()

    # 2) 分类基金：哪些 NAV 已公布（直接返回真值）、哪些需要估算
    need_estimate: list[dict] = []
    for f in funds:
        code = f["code"]
        nav_history = nav_results.get(code) or []
        if not nav_history:
            out[code] = {"value": None, "date": "", "is_estimate": False}
            continue
        last_nav = nav_history[-1]
        nav_last_date = last_nav["date"]
        nav_last_change = last_nav.get("change_pct")

        if not us_last:
            # 拿不到美股数据，直接显示真值
            out[code] = {
                "value": round(nav_last_change * 100, 2) if nav_last_change is not None else None,
                "date": nav_last_date, "is_estimate": False,
            }
            continue

        if nav_last_date >= us_last:
            # NAV 已公布到美股 T 日，直接返回真值
            out[code] = {
                "value": round(nav_last_change * 100, 2) if nav_last_change is not None else None,
                "date": nav_last_date, "is_estimate": False,
            }
            continue

        # 需要估算
        need_estimate.append({**f, "nav_history": nav_history, "us_last": us_last})

    if not need_estimate:
        return out

    # 3) 并行 build_exposure
    p = Predictor(target_quarter="auto")
    exposures: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=min(len(need_estimate), 4)) as ex:
        fut_map = {
            ex.submit(p.build_exposure, f["code"], f.get("main_code") or f["code"], f.get("short_name", "")): f["code"]
            for f in need_estimate
        }
        for fut in as_completed(fut_map):
            code = fut_map[fut]
            try:
                exposures[code] = fut.result(timeout=15)
            except Exception as e:
                logger.warning("build_exposure %s 失败: %s", code, e)

    # 4) 收集所有 ticker，一次性拉 prices（关键优化）
    all_tickers: set[str] = set()
    for exp in exposures.values():
        try:
            all_tickers.update(p.collect_required_tickers(exp))
        except Exception:
            pass

    if not all_tickers:
        # 没有 ticker 可拉，所有基金 fallback 到真值
        for f in need_estimate:
            code = f["code"]
            last_nav = f["nav_history"][-1]
            chg = last_nav.get("change_pct")
            out[code] = {
                "value": round(chg * 100, 2) if chg is not None else None,
                "date": last_nav["date"], "is_estimate": False,
            }
        return out

    # 价格区间：覆盖最早 prev_date 之前 10 天 ~ us_last 之后 2 天
    earliest_prev = min(f["nav_history"][-1]["date"] for f in need_estimate)
    earliest_dt = datetime.fromisoformat(earliest_prev).date()
    target_dt = datetime.fromisoformat(us_last).date()
    price_start = (earliest_dt - timedelta(days=10)).isoformat()
    price_end = (target_dt + timedelta(days=2)).isoformat()

    try:
        prices = p.quotes.batch_get(sorted(all_tickers), price_start, price_end)
    except Exception as e:
        logger.warning("批量 fetch_prices 失败: %s", e)
        prices = {}

    # 5) 对每只基金单独跑 HybridModel.predict（共享 prices）
    model = HybridModel()
    for f in need_estimate:
        code = f["code"]
        exp = exposures.get(code)
        if exp is None:
            # build_exposure 失败，fallback
            last_nav = f["nav_history"][-1]
            chg = last_nav.get("change_pct")
            out[code] = {
                "value": round(chg * 100, 2) if chg is not None else None,
                "date": last_nav["date"], "is_estimate": False,
            }
            continue
        try:
            nav_last_date = f["nav_history"][-1]["date"]
            pred = model.predict(exp, prices, us_last, nav_last_date)
            out[code] = {
                "value": round(pred.predicted_pct * 100, 2),
                "date": us_last, "is_estimate": True,
            }
        except Exception as e:
            logger.debug("predict %s 失败: %s", code, e)
            last_nav = f["nav_history"][-1]
            chg = last_nav.get("change_pct")
            out[code] = {
                "value": round(chg * 100, 2) if chg is not None else None,
                "date": last_nav["date"], "is_estimate": False,
            }

    return out


def predict_t1_for_fund(
    fund_code: str,
    main_code: str,
    short_name: str,
    nav_history: list[dict] | None = None,
    nav_date_hint: str | None = None,        # "MM-DD" 或 "YYYY-MM-DD" - BulkSnapshot 提供
    nav_change_hint: float | None = None,    # 该日涨跌（小数）- 暂未使用，因 BulkSnapshot 不提供
    model: str = "hybrid",
) -> dict:
    """一行规则：NAV 已公布显示真值，否则显示估算。

    优化：调用方可传入 nav_date_hint 跳过 fetch_nav_pages 的网络调用。
    当 nav_date_hint 表明 NAV 已公布到美股 T 日时，直接返回真值（仍需要拉一次 NAV
    取真实 change_pct，因 BulkSnapshot 不带 change 字段）。
    """
    try:
        from core.predict.predictor import Predictor
        from core.predict.backtest import fetch_nav_series
        from core.predict.models import HybridModel
    except ImportError:
        return {"value": None, "date": "", "is_estimate": False}

    # 1) 拉基金最新 NAV（如果调用方未提供）
    if not nav_history:
        try:
            today = _today_cst()
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
