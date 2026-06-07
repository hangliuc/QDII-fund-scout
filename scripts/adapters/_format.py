# -*- coding: utf-8 -*-
"""适配器共享的格式化辅助函数"""
from __future__ import annotations


def to_float(val) -> float | None:
    """安全转换为 float。0 不会被误判为 None。"""
    if val is None:
        return None
    try:
        return float(str(val).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def fmt_return(val) -> str:
    """收益率格式化：正数前缀 +，保留 2 位小数"""
    v = to_float(val)
    if v is None:
        return "-"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def format_prediction_lark(pred: dict) -> str:
    """飞书 lark_md 格式：T-1 估值预测一行"""
    if not pred:
        return ""
    val = pred.get("value")
    if val is None:
        return ""
    nav_date = pred.get("date", "")
    is_est = pred.get("is_estimate", False)
    sign = "+" if val > 0 else ""
    color = "red" if val > 0 else "green"
    suffix = "（估算，仅供参考）" if is_est else ""
    return f'{nav_date} 涨跌: <font color="{color}">{sign}{val:.2f}%</font>{suffix}'


def format_prediction_wxmd(pred: dict) -> str:
    """企业微信 markdown 格式：T-1 估值预测一行"""
    if not pred:
        return ""
    val = pred.get("value")
    if val is None:
        return ""
    nav_date = pred.get("date", "")
    is_est = pred.get("is_estimate", False)
    sign = "+" if val > 0 else ""
    color = "warning" if val > 0 else "info"
    suffix = "（估算，仅供参考）" if is_est else ""
    return f'{nav_date} 涨跌: <font color="{color}">{sign}{val:.2f}%</font>{suffix}'
