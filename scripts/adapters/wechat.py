# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from typing import Any

import requests

from adapters import BaseAdapter, register
from core.models import FundDataResult, DISCLAIMER


class WechatAdapter(BaseAdapter):
    name = "wechat"
    required_config = ["webhook_url"]

    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url or os.environ.get("WECHAT_WEBHOOK_URL", "")

    def send(self, data: FundDataResult, fmt: str = "markdown", **kwargs) -> bool:
        if not self.webhook_url:
            print("[wechat] webhook_url 未配置")
            return False
        if fmt == "markdown" or fmt == "card":
            payload = self._build_markdown(data)
        elif fmt == "text":
            payload = self._build_text(data)
        elif fmt == "image":
            print("[wechat] image 格式暂不支持")
            return False
        else:
            payload = self._build_markdown(data)
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            result = resp.json()
            return result.get("errcode", -1) == 0
        except Exception as e:
            print(f"[wechat] 发送失败: {e}")
            return False

    def test_connection(self) -> bool:
        if not self.webhook_url:
            return False
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": "### QDII-fund-scout 连接测试\n> 企业微信适配器连接成功"},
        }
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            result = resp.json()
            return result.get("errcode", -1) == 0
        except Exception as e:
            print(f"[wechat] 连接测试失败: {e}")
            return False

    def _build_markdown(self, data: FundDataResult) -> dict[str, Any]:
        lines = []
        lines.append(f"### QDII 基金数据")
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

            r1y_val = WechatAdapter._to_float(fund.return_1y)
            if r1y_val is not None:
                r1y_color = "warning" if r1y_val > 0 else "info"
                r1y_line = f'近1年: <font color="{r1y_color}">{WechatAdapter._fmt_return(fund.return_1y)}</font>'
            else:
                r1y_line = "近1年: -"

            if fund.purchase_status == "暂停":
                status_line = '<font color="warning">申购: 暂停</font>'
                limit_line = '<font color="warning">限额: 暂停</font>'
            elif fund.purchase_status == "开放":
                status_line = '<font color="info">申购: 开放</font>'
                limit_line = '<font color="info">限额: 无限制</font>'
            else:
                status_line = f'<font color="info">申购: {fund.purchase_status}</font>'
                limit_val = fund.purchase_limit or "-"
                limit_line = f'<font color="info">限额: {limit_val}</font>'

            fund_blocks.append(
                f"**{idx + 1}. {name}** {code}\n"
                f"{r1y_line}  |  {status_line}  |  {limit_line}"
            )

        lines.append("\n---\n".join(fund_blocks))

        lines.append("")
        lines.append(f"> <font color=\"comment\">{DISCLAIMER}</font>")

        return {
            "msgtype": "markdown",
            "markdown": {"content": "\n".join(lines)},
        }

    def _build_text(self, data: FundDataResult) -> dict[str, Any]:
        lines = [f"QDII 基金数据 {data.update_date}  ·  共 {data.count} 只基金", ""]
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
