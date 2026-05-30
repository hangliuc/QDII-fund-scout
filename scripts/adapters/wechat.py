# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

import requests

from adapters import BaseAdapter, register
from core.models import FundDataResult, DISCLAIMER

logger = logging.getLogger(__name__)


class WechatAdapter(BaseAdapter):
    name = "wechat"
    required_config = ["webhook_url"]

    def __init__(self, webhook_url: str = "", timeout: int = 20):
        self.webhook_url = webhook_url or os.environ.get("WECHAT_WEBHOOK_URL", "")
        self.timeout = timeout

    def send(self, data: FundDataResult, fmt: str = "markdown", **kwargs) -> bool:
        if not self.webhook_url:
            print("[wechat] webhook_url 未配置")
            return False
        if fmt == "markdown" or fmt == "card":
            payload = self._build_markdown(data, **kwargs)
        elif fmt == "text":
            payload = self._build_text(data, **kwargs)
        elif fmt == "image":
            print("[wechat] image 格式暂不支持")
            return False
        else:
            payload = self._build_markdown(data, **kwargs)
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=15)
            result = resp.json()
            return result.get("errcode", -1) == 0
        except Exception as e:
            logger.warning("[wechat] 发送失败: %s", e)
            return False

    def test_connection(self) -> bool:
        if not self.webhook_url:
            return False
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": "### QDII-fund-scout 连接测试\n> 企业微信适配器连接成功"},
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=15)
            result = resp.json()
            return result.get("errcode", -1) == 0
        except Exception as e:
            logger.warning("[wechat] 连接测试失败: %s", e)
            return False

    def _build_markdown(self, data: FundDataResult, **kwargs) -> dict[str, Any]:
        title = kwargs.get("title", "QDII 基金数据")
        lines = []
        lines.append(f"### {title}")
        lines.append(f"> {data.update_date}  ·  共 {data.count} 只基金")
        lines.append("")

        sorted_funds = sorted(
            data.funds,
            key=lambda f: WechatAdapter._to_float(f.return_1y) or float("-inf"),
            reverse=True,
        )

        fund_blocks: list[str] = []

        for idx, fund in enumerate(sorted_funds):
            name = fund.short_name or fund.name or "-"
            code = fund.code

            cross_mark = ""
            if fund._cross_validated:
                cross_mark = ' ✅'

            r1y_val = WechatAdapter._to_float(fund.return_1y)
            if r1y_val is not None:
                r1y_color = "warning" if r1y_val > 0 else "info"
                r1y_line = f'近1年: <font color="{r1y_color}">{WechatAdapter._fmt_return(fund.return_1y)}</font>'
            else:
                r1y_line = "近1年: -"

            purchase_info = fund._purchase_info or "-"
            if "暂停" in purchase_info:
                purchase_color = "warning"
            elif "限小额" in purchase_info:
                purchase_color = "warning"
            elif "限大额" in purchase_info:
                purchase_color = "warning"
            else:
                purchase_color = "info"
            purchase_line = f'<font color="{purchase_color}">{purchase_info}</font>'

            market_line = f"\n市场投资TOP3：{fund.market_top3}" if fund.market_top3 else ""

            fund_blocks.append(
                f"**{idx + 1}. {name}** {code}{cross_mark}\n"
                f"{r1y_line}  |  {purchase_line}{market_line}"
            )

        lines.append("\n---\n".join(fund_blocks))

        lines.append("")
        lines.append(f"> <font color=\"comment\">{DISCLAIMER}</font>")

        return {
            "msgtype": "markdown",
            "markdown": {"content": "\n".join(lines)},
        }

    def _build_text(self, data: FundDataResult, **kwargs) -> dict[str, Any]:
        title = kwargs.get("title", "QDII 基金数据")
        lines = [f"{title} {data.update_date}  ·  共 {data.count} 只基金", ""]
        sorted_funds = sorted(
            data.funds,
            key=lambda f: WechatAdapter._to_float(f.return_1y) or float("-inf"),
            reverse=True,
        )
        for idx, fund in enumerate(sorted_funds):
            name = fund.short_name or fund.name or "-"
            r1y = WechatAdapter._fmt_return(fund.return_1y)
            status = fund.purchase_status
            limit = fund.purchase_limit or "-"
            if not fund.purchase_limit or fund.purchase_limit in ("无限制", ""):
                limit = "-"
            lines.append(f"{idx + 1}. {name} {fund.code}")
            lines.append(f"   近1年: {r1y}  申购: {status}  限额: {limit}")
        if data._warnings:
            lines.append("")
            for w in data._warnings:
                lines.append(f"  {w}")
        lines.append("")
        lines.append(DISCLAIMER)
        return {
            "msgtype": "text",
            "text": {"content": "\n".join(lines)},
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
        v = WechatAdapter._to_float(val)
        if v is None:
            return "-"
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.2f}%"


register(WechatAdapter.name, WechatAdapter)
