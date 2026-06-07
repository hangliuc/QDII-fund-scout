# -*- coding: utf-8 -*-
"""天天基金 NAV 历史数据公共抓取模块

集中放置日净值（含涨跌幅）拉取逻辑，被以下模块复用：
- core/sources/eastmoney.py:fetch_detail（HTML 详情页路径）
- core/sources/eastmoney_bulk.py:enrich_fund（快照路径补充回撤）
- core/predict/backtest.py:fetch_nav_series（回测的真值序列）

接口：
    fetch_nav_pages(code, start, end, max_pages=14, page_size=20, timeout=20)
        -> list[dict]   # [{"date": "yyyy-mm-dd", "nav": float, "change_pct": float|None}, ...]

    calc_max_drawdown(navs, since="")
        -> float|None   # 0~1 之间的小数；since 是过滤的最早日期
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NAV_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://fund.eastmoney.com/",
}

NAV_URL = (
    "https://api.fund.eastmoney.com/f10/lsjz"
    "?callback=jQuery&fundCode={code}&pageIndex={page}"
    "&pageSize={page_size}&startDate={start}&endDate={end}"
)


def _parse_nav_response(text: str) -> list[dict]:
    """解析单页 NAV API 返回的 JSONP 文本"""
    text = text.strip()
    if text.startswith("jQuery(") and text.endswith(")"):
        text = text[7:-1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = (data.get("Data", {}) or {}).get("LSJZList", [])
    rows = []
    for it in items:
        try:
            nav = float(it.get("DWJZ", "0"))
            date = it.get("FSRQ", "")
            if nav <= 0 or not date:
                continue
            jz = it.get("JZZZL", "")
            change_pct = None
            if jz and jz not in ("", "None"):
                try:
                    # API 返回的是百分比数值（如 -0.83），转成小数
                    change_pct = float(jz) / 100.0
                except (ValueError, TypeError):
                    change_pct = None
            rows.append({"date": date, "nav": nav, "change_pct": change_pct})
        except (ValueError, TypeError):
            continue
    return rows


def fetch_nav_pages(
    code: str,
    start: str,
    end: str,
    max_pages: int = 14,
    page_size: int = 20,
    timeout: int = 20,
) -> list[dict]:
    """分页拉取一段时间的 NAV 历史。

    返回按日期升序的 list[dict]，每项包含 date / nav / change_pct。
    遇到不足一页或异常即停止。
    """
    rows: list[dict] = []
    seen_dates: set[str] = set()
    for page in range(1, max_pages + 1):
        url = NAV_URL.format(code=code, page=page, page_size=page_size, start=start, end=end)
        try:
            resp = requests.get(url, headers=NAV_HEADERS, timeout=timeout)
        except requests.RequestException as e:
            logger.warning("NAV API 请求失败 code=%s page=%d: %s", code, page, e)
            break
        items = _parse_nav_response(resp.text)
        if not items:
            break
        for it in items:
            d = it["date"]
            if d in seen_dates:
                continue
            seen_dates.add(d)
            rows.append(it)
        if len(items) < page_size:
            break
    rows.sort(key=lambda r: r["date"])
    return rows


def calc_max_drawdown(nav_list: list[tuple[str, float]] | list[dict], since: str = "") -> Optional[float]:
    """计算最大回撤（小数 0~1）。

    支持两种输入：
    - list[tuple[date_str, nav_float]]
    - list[dict] with keys 'date' / 'nav'
    """
    if not nav_list:
        return None
    # 标准化为 (date, nav) 对
    if isinstance(nav_list[0], dict):
        pairs = [(r.get("date", ""), float(r.get("nav", 0))) for r in nav_list]
    else:
        pairs = list(nav_list)

    if since:
        pairs = [(d, n) for d, n in pairs if d >= since]
    if not pairs:
        return None

    peak = pairs[0][1]
    max_dd = 0.0
    for _, nav in pairs:
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 6)


def calc_period_return(nav_list: list[tuple[str, float]] | list[dict], since: str = "") -> Optional[float]:
    """从首末 NAV 计算区间累计收益率（百分比，2 位小数）"""
    if not nav_list:
        return None
    if isinstance(nav_list[0], dict):
        pairs = [(r.get("date", ""), float(r.get("nav", 0))) for r in nav_list]
    else:
        pairs = list(nav_list)
    if since:
        pairs = [(d, n) for d, n in pairs if d >= since]
    if len(pairs) < 2:
        return None
    start_nav = pairs[0][1]
    end_nav = pairs[-1][1]
    if start_nav <= 0:
        return None
    return round((end_nav - start_nav) / start_nav * 100, 4)
