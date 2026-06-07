# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

import requests

from adapters import BaseAdapter, register
from adapters._format import to_float, fmt_return, format_prediction_lark
from core.models import FundDataResult, DISCLAIMER

logger = logging.getLogger(__name__)


class FeishuAdapter(BaseAdapter):
    name = "feishu"
    required_config = ["webhook_url"]

    def __init__(self, webhook_url: str = "", timeout: int = 20):
        self.webhook_url = webhook_url or os.environ.get("FEISHU_WEBHOOK_URL", "")
        self.timeout = timeout

    def send(self, data: FundDataResult, fmt: str = "card", **kwargs) -> bool:
        if not self.webhook_url:
            print("[feishu] webhook_url 未配置")
            return False
        if fmt == "card":
            payload = self._build_card(data, **kwargs)
        elif fmt == "text":
            payload = self._build_text(data)
        elif fmt == "image":
            print("[feishu] image 格式暂不支持")
            return False
        else:
            payload = self._build_card(data, **kwargs)
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=15)
            result = resp.json()
            return result.get("code", -1) == 0
        except Exception as e:
            logger.warning("[feishu] 发送失败: %s", e)
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
            resp = requests.post(self.webhook_url, json=payload, timeout=15)
            result = resp.json()
            return result.get("code", -1) == 0
        except Exception as e:
            logger.warning("[feishu] 连接测试失败: %s", e)
            return False

    def _build_card(self, data: FundDataResult, **kwargs) -> dict[str, Any]:
        title = kwargs.get("title", "QDII 基金数据")
        elements: list[dict] = []

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"{data.update_date}  ·  共 {data.count} 只基金",
            },
        })

        sorted_funds = sorted(
            data.funds,
            key=lambda f: to_float(f.return_1y) if to_float(f.return_1y) is not None else float("-inf"),
            reverse=True,
        )

        for idx, fund in enumerate(sorted_funds):
            name = fund.short_name or fund.name or "-"
            code = fund.code

            cross_mark = ""
            if fund._cross_validated:
                cross_mark = ' ✅'

            r1y_val = to_float(fund.return_1y)
            if r1y_val is not None:
                r1y_color = "red" if r1y_val > 0 else "green"
                r1y_display = f'<font color="{r1y_color}">{fmt_return(fund.return_1y)}</font>'
            else:
                r1y_display = "-"

            purchase_info = fund._purchase_info or "-"
            if "暂停" in purchase_info:
                purchase_color = "red"
            elif "限小额" in purchase_info:
                purchase_color = "orange"
            elif "限大额" in purchase_info:
                purchase_color = "orange"
            else:
                purchase_color = "green"
            purchase_display = f'<font color="{purchase_color}">{purchase_info}</font>'

            elements.append({"tag": "hr"})

            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{idx + 1}. {name}** {code}{cross_mark}\n"
                        f"近1年: {r1y_display}  |  {purchase_display}"
                    ),
                },
            })

            # T-1 估值预测（如果启用且数据有效）
            pred = fund._t1_prediction or {}
            if pred:
                pred_text = format_prediction_lark(pred)
                if pred_text:
                    elements.append({
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": pred_text},
                    })

            if fund.market_top3:
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"市场投资TOP3：{fund.market_top3}",
                    },
                })

        if data._warnings:
            elements.append({"tag": "hr"})
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
                    "title": {"tag": "plain_text", "content": title},
                    "template": "indigo",
                },
                "elements": elements,
            },
        }

    def _build_text(self, data: FundDataResult) -> dict[str, Any]:
        lines = [f"QDII 基金数据 {data.update_date}", ""]
        for fund in data.funds:
            name = fund.short_name or fund.name
            r1y = fmt_return(fund.return_1y)
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


register(FeishuAdapter.name, FeishuAdapter)