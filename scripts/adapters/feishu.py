# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import Any

import requests

from adapters import BaseAdapter, register
from core.models import FundDataResult, DISCLAIMER


class FeishuAdapter(BaseAdapter):
    name = "feishu"
    required_config = ["webhook_url"]

    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url or os.environ.get("FEISHU_WEBHOOK_URL", "")

    def send(self, data: FundDataResult, fmt: str = "card", **kwargs) -> bool:
        if not self.webhook_url:
            print("[feishu] webhook_url 未配置")
            return False
        if fmt == "card":
            payload = self._build_card(data)
        elif fmt == "text":
            payload = self._build_text(data)
        elif fmt == "image":
            print("[feishu] image 格式暂不支持")
            return False
        else:
            payload = self._build_card(data)
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            result = resp.json()
            return result.get("code", -1) == 0
        except Exception as e:
            print(f"[feishu] 发送失败: {e}")
            return False

    def test_connection(self) -> bool:
        if not self.webhook_url:
            return False
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "QDII-fund-scout 连接测试"},
                    "template": "turquoise",
                },
                "elements": [
                    {"tag": "markdown", "content": "飞书适配器连接成功"},
                ],
            },
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            result = resp.json()
            return result.get("code", -1) == 0
        except Exception as e:
            print(f"[feishu] 连接测试失败: {e}")
            return False

    def _build_card(self, data: FundDataResult) -> dict[str, Any]:
        elements: list[dict] = []

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"{data.update_date}  ·  共 {data.count} 只基金",
            },
        })

        elements.append({"tag": "hr"})

        sorted_funds = sorted(
            data.funds,
            key=lambda f: self._to_float(f.return_1y) or float("-inf"),
            reverse=True,
        )

        rank_col: list[str] = ["**#**"]
        name_col: list[str] = ["**基金**"]
        return_col: list[str] = ["**近1年**"]
        status_col: list[str] = ["**申购**"]
        limit_col: list[str] = ["**限额**"]

        for idx, fund in enumerate(sorted_funds):
            name = fund.short_name or fund.name or "-"

            r1y_val = self._to_float(fund.return_1y)
            if r1y_val is not None:
                r1y_color = "red" if r1y_val > 0 else "green"
                r1y_display = f'<font color="{r1y_color}">{self._fmt_return(fund.return_1y)}</font>'
            else:
                r1y_display = "-"

            if fund.purchase_status == "开放":
                status_display = '<font color="green">开放</font>'
                limit_display = '<font color="green">无限制</font>'
            elif fund.purchase_status == "限大额":
                status_display = '<font color="green">限大额</font>'
                limit_display = f'<font color="green">{fund.purchase_limit}</font>'
            elif fund.purchase_status == "限小额":
                status_display = '<font color="green">限小额</font>'
                limit_display = f'<font color="green">{fund.purchase_limit}</font>'
            elif fund.purchase_status == "暂停":
                status_display = '<font color="red">暂停</font>'
                limit_display = '<font color="red">暂停</font>'
            else:
                status_display = '<font color="red">未知</font>'
                limit_display = '<font color="red">-</font>'

            rank_col.append(str(idx + 1))
            name_col.append(f"{name} {fund.code}")
            return_col.append(r1y_display)
            status_col.append(status_display)
            limit_col.append(limit_display)

        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "horizontal_spacing": "default",
            "columns": [
                {
                    "tag": "column",
                    "width_type": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [{"tag": "markdown", "content": "\n".join(rank_col)}],
                },
                {
                    "tag": "column",
                    "width_type": "weighted",
                    "weight": 5,
                    "vertical_align": "top",
                    "elements": [{"tag": "markdown", "content": "\n".join(name_col)}],
                },
                {
                    "tag": "column",
                    "width_type": "weighted",
                    "weight": 2,
                    "vertical_align": "top",
                    "elements": [{"tag": "markdown", "content": "\n".join(return_col)}],
                },
                {
                    "tag": "column",
                    "width_type": "weighted",
                    "weight": 2,
                    "vertical_align": "top",
                    "elements": [{"tag": "markdown", "content": "\n".join(status_col)}],
                },
                {
                    "tag": "column",
                    "width_type": "weighted",
                    "weight": 2,
                    "vertical_align": "top",
                    "elements": [{"tag": "markdown", "content": "\n".join(limit_col)}],
                },
            ],
        })

        if data._warnings:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(data._warnings),
                },
            })

        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": DISCLAIMER},
            ],
        })

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "QDII 基金数据"},
                    "template": "indigo",
                },
                "elements": elements,
            },
        }

    def _build_text(self, data: FundDataResult) -> dict[str, Any]:
        lines = [f"QDII 基金数据 {data.update_date}", ""]
        for fund in data.funds:
            name = fund.short_name or fund.name
            r1y = self._fmt_return(fund.return_1y)
            status = fund.purchase_status
            lines.append(f"  {fund.code} {name}")
            lines.append(f"    近1年:{r1y} 申购:{status}")
        if data._warnings:
            lines.append("")
            for w in data._warnings:
                lines.append(f"! {w}")
        lines.append("")
        lines.append(DISCLAIMER)
        return {
            "msg_type": "text",
            "content": {"text": "\n".join(lines)},
        }

    @staticmethod
    def _to_float(val) -> float | None:
        if val is None:
            return None
        try:
            return float(str(val).replace("%", "").replace(",", "").strip()) or None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _fmt_return(val, invert: bool = False) -> str:
        v = FeishuAdapter._to_float(val)
        if v is None:
            return "-"
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.2f}%"


register(FeishuAdapter.name, FeishuAdapter)