from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from core.models import FundInfo
from core.sources.base import BaseSource, SourceError

logger = logging.getLogger(__name__)

HOWBUY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.howbuy.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_FUND_URL = "https://www.howbuy.com/fund/{code}/"
_FEE_URL = "https://www.howbuy.com/fund/{code}/#jjfl"


class HowbuySource(BaseSource):
    name = "howbuy"

    def fetch_detail(self, code: str) -> FundInfo:
        info = FundInfo(code=code, data_source="howbuy")

        urls = [
            _FUND_URL.format(code=code),
            _FEE_URL.format(code=code),
        ]

        results: list[tuple[str, str | None]] = []
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut = {ex.submit(self._get_with_retry, u, 0, 15, HOWBUY_HEADERS): u for u in urls}
            for f in as_completed(fut):
                url = fut[f]
                try:
                    results.append((url, f.result()))
                except Exception:
                    results.append((url, None))

        html_pool = dict(results)

        main_html = html_pool.get(urls[0])
        if not main_html:
            raise SourceError(self.name, f"无法获取基金 {code} 主页")

        try:
            parsed = self._parse_main_page(main_html, code)
            for k, v in parsed.items():
                setattr(info, k, v)
        except Exception as e:
            logger.warning("howbuy _parse_main_page(%s) 部分解析失败: %s", code, e)

        fee_html = html_pool.get(urls[1])
        if fee_html:
            try:
                fee_parsed = self._parse_fee_page(fee_html, code)
                for k, v in fee_parsed.items():
                    if v is not None and not getattr(info, k, None):
                        setattr(info, k, v)
            except Exception as e:
                logger.warning("howbuy _parse_fee_page(%s) 部分解析失败: %s", code, e)

        info.update_date = time.strftime("%Y-%m-%d")
        return info

    def fetch_batch(self, codes: list[str]) -> list[FundInfo]:
        results: list[FundInfo] = []
        for code in codes:
            try:
                info = self.fetch_detail(code)
                results.append(info)
            except SourceError as e:
                logger.warning("howbuy fetch_detail(%s) 失败: %s", code, e)
            except Exception as e:
                logger.warning("howbuy fetch_detail(%s) 异常: %s", code, e)
            self._sleep(0.3, 0.5)
        return results

    def search_funds(self, keyword: str, fund_type: str = "") -> list[dict]:
        logger.warning("howbuy 不支持搜索功能，请使用 eastmoney 数据源")
        return []

    def _parse_main_page(self, html: str, code: str) -> dict:
        info: dict = {}

        m = re.search(r'<h1>([^<]+?)<span>\((\d{6})\)</span></h1>', html)
        if m:
            info["name"] = m.group(1).strip()

        m = re.search(r'<div class="dRate">\s*<div class="c[^"]*">\s*([\d.]+)\s*</div>', html)
        if m:
            info["nav"] = m.group(1).strip()

        m = re.search(r'单位净值\s*\[(\d{2}-\d{2})\]', html)
        if m:
            info["nav_date"] = m.group(1)

        m = re.search(r'<span>QDII</span>', html)
        if m:
            info["type"] = "QDII"

        m = re.search(r'<li>(中高风险|高风险|中风险|中低风险|低风险)</li>', html)
        if m:
            info["risk_level"] = m.group(1)

        m = re.search(r'最新规模<span>([\d.]+)亿</span>', html)
        if m:
            info["scale"] = m.group(1)

        m = re.search(r'成立时间<span>([\d-]+)</span>', html)
        if m:
            info["found_date"] = m.group(1)

        info.update(self._parse_returns_table(html))
        info.update(self._parse_purchase_status(html))

        return info

    def _parse_returns_table(self, html: str) -> dict:
        info: dict = {}

        row_match = re.search(
            r'<td class="t-bg">区间回报</td>(.*?)</tr>',
            html, re.DOTALL,
        )
        if not row_match:
            return info

        row = row_match.group(1)
        values = re.findall(r'<span class="c(?:Red|Green)">([-\d.]+)%</span>', row)

        labels = ["return_ytd", "return_1w", "return_1m", "return_3m", "return_6m", "return_1y", "return_2y", "return_3y"]
        for i, val in enumerate(values):
            if i < len(labels):
                info[labels[i]] = val

        return info

    def _parse_purchase_status(self, html: str) -> dict:
        result = {"purchase_status": "未知", "purchase_limit": "", "effectively_closed": False}

        if re.search(r'class="dis_buy_btn"[^>]*>不能购买<', html):
            result["purchase_status"] = "暂停"
            result["purchase_limit"] = "0"
            result["effectively_closed"] = True
            return result

        if re.search(r'class="buy_btn"', html) and not re.search(r'style="display:none"', html[:html.find('class="buy_btn"') + 100] if 'class="buy_btn"' in html else ''):
            result["purchase_status"] = "开放"
            result["purchase_limit"] = "无限制"
            return result

        if re.search(r'class="dis_buy_btn"', html):
            result["purchase_status"] = "暂停"
            result["purchase_limit"] = "0"
            result["effectively_closed"] = True
            return result

        return result

    def _parse_fee_page(self, html: str, code: str) -> dict:
        info: dict = {}

        m = re.search(r'管理费率.*?([\d.]+)%', html)
        if m:
            info["mgmt_fee"] = float(m.group(1))
        m = re.search(r'托管费率.*?([\d.]+)%', html)
        if m:
            info["custody_fee"] = float(m.group(1))
        m = re.search(r'销售服务费.*?([\d.]+)%', html)
        if m:
            info["service_fee"] = float(m.group(1))

        fees = [info.get("mgmt_fee", 0), info.get("custody_fee", 0), info.get("service_fee", 0)]
        if any(f > 0 for f in fees):
            info["total_fee"] = round(sum(fees), 4)

        return info
