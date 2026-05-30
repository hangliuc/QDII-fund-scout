from __future__ import annotations

import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests

from core.models import FundInfo
from core.sources.base import BaseSource, SourceError

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://fund.eastmoney.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_FUND_URL = "http://fund.eastmoney.com/{code}.html"
_ARCHIVE_URL = "http://fundf10.eastmoney.com/jbgk_{code}.html"
_MANAGER_URL = "http://fundf10.eastmoney.com/jjjl_{code}.html"
_HOLDING_URL = (
    "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    "?type=jjcc&code={code}&topline=10&year={year}&month=&rt=0.1"
)
_NAV_URL = (
    "https://api.fund.eastmoney.com/f10/lsjz"
    "?callback=jQuery&fundCode={code}&pageIndex={page}"
    "&pageSize=20&startDate={start}&endDate={end}"
)
_FUND_CODES_URL = "http://fund.eastmoney.com/js/fundcode_search.js"


class EastMoneySource(BaseSource):
    name = "eastmoney"

    def _get(self, url: str, timeout: int = 20, headers: dict | None = None) -> str:
        hdrs = headers or HEADERS
        try:
            resp = requests.get(url, headers=hdrs, timeout=timeout)
            resp.encoding = "utf-8"
            if resp.status_code != 200:
                raise SourceError(self.name, f"HTTP {resp.status_code} - {url}")
            return resp.text
        except requests.RequestException as e:
            raise SourceError(self.name, f"请求失败 {url}", original=e) from e

    def fetch_all_fund_codes(self) -> list[list[str]]:
        text = self._get(_FUND_CODES_URL)
        m = re.search(r"var r = (\[.*\])", text, re.DOTALL)
        if not m:
            raise SourceError(self.name, "无法解析 fundcode_search.js")
        return json.loads(m.group(1))

    def search_funds(self, keyword: str, fund_type: str = "") -> list[dict]:
        all_funds = self.fetch_all_fund_codes()
        results: list[dict] = []
        for code, abbr, name, ft, pinyin in all_funds:
            if keyword and keyword not in name and keyword not in pinyin and keyword not in abbr:
                continue
            if fund_type and fund_type not in ft:
                continue
            results.append({"code": code, "abbr": abbr, "name": name, "type": ft, "pinyin": pinyin})
        return results

    def fetch_detail(self, code: str) -> FundInfo:
        info = FundInfo(code=code, data_source="eastmoney")
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y-%m-%d")

        # 13 页 = 260 条，覆盖 1 年交易日（~250），一次性并行拉取
        urls = [
            _FUND_URL.format(code=code),
            _ARCHIVE_URL.format(code=code),
            _MANAGER_URL.format(code=code),
        ] + [
            _NAV_URL.format(code=code, page=p, start=start, end=today)
            for p in range(1, 14)
        ]

        results: list[tuple[str, str | None]] = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            fut = {ex.submit(self._get_with_retry, u): u for u in urls}
            for f in as_completed(fut):
                url = fut[f]
                try:
                    results.append((url, f.result()))
                except Exception:
                    results.append((url, None))

        html_pool = dict(results)

        # 解析（全部走内存，无 IO）
        main_html = html_pool.get(urls[0])
        if main_html:
            try:
                parsed = self._parse_main_page(main_html, code)
                for k, v in parsed.items():
                    setattr(info, k, v)
            except Exception as e:
                logger.warning("eastmoney _parse_main_page(%s) 部分解析失败: %s", code, e)

        archive_html = html_pool.get(urls[1])
        if archive_html:
            try:
                parsed = self._parse_archive_page(archive_html, code)
                for k, v in parsed.items():
                    setattr(info, k, v)
            except Exception as e:
                logger.warning("eastmoney _parse_archive_page(%s) 部分解析失败: %s", code, e)

        manager_html = html_pool.get(urls[2])
        if manager_html:
            try:
                parsed = self._parse_manager_page(manager_html, code)
                for k, v in parsed.items():
                    setattr(info, k, v)
            except Exception as e:
                logger.warning("eastmoney _parse_manager_page(%s) 部分解析失败: %s", code, e)

        all_navs: list[tuple[str, float]] = []
        all_changes: list[tuple[str, float]] = []
        for nav_url in urls[3:]:
            nav_html = html_pool.get(nav_url)
            if not nav_html:
                continue
            try:
                clean = nav_html.strip()
                if clean.startswith("jQuery(") and clean.endswith(")"):
                    clean = clean[7:-1]
                data = json.loads(clean)
                items = (data.get("Data", {}) or {}).get("LSJZList", [])
                for item in items:
                    try:
                        nav_val = float(item.get("DWJZ", "0"))
                        date_val = item.get("FSRQ", "")
                        if nav_val > 0 and date_val:
                            all_navs.append((date_val, nav_val))
                        jz_str = item.get("JZZZL", "")
                        if jz_str and jz_str not in ("", "None"):
                            all_changes.append((date_val, float(jz_str)))
                    except (ValueError, TypeError):
                        continue
            except Exception:
                pass

        if all_navs:
            all_navs.sort(key=lambda x: x[0])
            info.nav_list = [{"date": d, "nav": n} for d, n in all_navs]
            info.drawdown_1y = self._calc_drawdown(all_navs, start)
            # 从 NAV 数据独立计算近1年收益率，用于交叉验证
            info._nav_return_1y = self._calc_return_from_nav(all_navs, start)

        info.update_date = time.strftime("%Y-%m-%d")
        return info

    def fetch_purchase_limit(self, code: str) -> str:
        """从费率页面获取单日限额作为第三验证源"""
        url = _ARCHIVE_URL.format(code=code)
        html = self._get_with_retry(url, retries=1, timeout=10)
        if not html:
            return ""

        m = re.search(r'单日累计购买上限(?:[：(]*)([\d.,]+)\s*(万?)\s*元', html)
        if m:
            amt = m.group(1).replace(",", "")
            unit = m.group(2)
            return f"{amt}{unit}元" if unit else f"{amt}元"
        return ""

    def fetch_batch(self, codes: list[str]) -> list[FundInfo]:
        if not codes:
            return []
        if len(codes) == 1:
            try:
                return [self.fetch_detail(codes[0])]
            except (SourceError, Exception) as e:
                logger.warning("eastmoney fetch_detail(%s) 失败: %s", codes[0], e)
                return []
        results: list[FundInfo | None] = [None] * len(codes)
        with ThreadPoolExecutor(max_workers=min(len(codes), 3)) as ex:
            fut_to_idx = {ex.submit(self.fetch_detail, code): i for i, code in enumerate(codes)}
            for fut in as_completed(fut_to_idx):
                idx = fut_to_idx[fut]
                try:
                    results[idx] = fut.result()
                except (SourceError, Exception) as e:
                    logger.warning("eastmoney fetch_detail(%s) 失败: %s", codes[idx], e)
        return [r for r in results if r is not None]

    def _parse_main_page(self, html: str, code: str) -> dict:
        info: dict = {}

        m = re.search(r"<title>(.*?)\((\d{6})\)", html)
        if m:
            info["name"] = m.group(1).strip()

        info_block_match = re.search(
            r'<div class="infoOfFund">(.*?)基金评级', html, re.DOTALL
        )
        if info_block_match:
            block = self._strip_tags(info_block_match.group(0))
            m = re.search(r"类型[：:]\s*([^\s|]+)\s*(?:&nbsp;|\s)*\|\s*(?:&nbsp;|\s)*([^\s]+风险)", block)
            if m:
                info["type"] = m.group(1)
                info["risk"] = m.group(2)
            m = re.search(r"规模\s*[：:]\s*([\d.,]+亿元)", block)
            if m:
                info["scale"] = m.group(1)
            m = re.search(r"基金经理[：:]\s*([\u4e00-\u9fa5A-Za-z]+)", block)
            if m:
                info["manager_name"] = m.group(1)
            m = re.search(r"成\s*立\s*日\s*[：:]\s*([\d-]+)", block)
            if m:
                info["found_date"] = m.group(1)
            m = re.search(r"管\s*理\s*人\s*[：:]\s*([\u4e00-\u9fa5]+)", block)
            if m:
                info["company"] = m.group(1)

        m = re.search(r'class="fix_dwjz[^"]*"[^>]*>([\d.]+)<', html)
        if m:
            info["nav"] = m.group(1)
        m = re.search(r'class="fix_date">\((\d{2}-\d{2})', html)
        if m:
            info["nav_date"] = m.group(1)

        for key, label in [
            ("return_1m", "近1月"), ("return_3m", "近3月"),
            ("return_6m", "近6月"), ("return_1y", "近1年"),
            ("return_3y", "近3年"), ("return_sl", "成立来"),
        ]:
            m = re.search(
                re.escape(label) + r"[：:]\s*</span>\s*<span[^>]*>([-\d.]+)%?</span>",
                html,
            )
            if m:
                info[key] = m.group(1)

        info.update(self._parse_purchase_status(html))
        return info

    def _parse_archive_page(self, html: str, code: str) -> dict:
        info: dict = {}

        m = re.search(r"管理费率.*?([\d.]+)%", html)
        if m:
            info["mgmt_fee"] = float(m.group(1))
        m = re.search(r"托管费率.*?([\d.]+)%", html)
        if m:
            info["custody_fee"] = float(m.group(1))
        m = re.search(r"销售服务费率.*?([\d.]+)%", html)
        if m:
            info["service_fee"] = float(m.group(1))
        m = re.search(r"跟踪标的.*?<td[^>]*>(.*?)</td>", html, re.DOTALL)
        if m:
            info["benchmark"] = self._strip_tags(m.group(1))[:60]
        m = re.search(r"资产规模.*?([\d.,]+)\s*亿", html)
        if m:
            info["scale"] = m.group(1)

        fees = [info.get("mgmt_fee", 0), info.get("custody_fee", 0), info.get("service_fee", 0)]
        if any(f > 0 for f in fees):
            info["total_fee"] = round(sum(fees), 4)

        return info

    def _parse_manager_page(self, html: str, code: str) -> dict:
        info: dict = {}

        all_tables = re.findall(
            r'<table[^>]*class="[^"]*jloff[^"]*"[^>]*>(.+?)</table>',
            html, re.DOTALL,
        )
        tenures: list[dict] = []
        if all_tables:
            first = all_tables[0]
            for tr in re.findall(r"<tr[^>]*>(.+?)</tr>", first, re.DOTALL):
                if "<th" in tr:
                    continue
                tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
                if len(tds) < 5:
                    continue
                start = self._strip_tags(tds[0])
                end = self._strip_tags(tds[1])
                managers = re.findall(r">([^<>]+)</a>", tds[2]) or [self._strip_tags(tds[2])]
                tenures.append({
                    "start": start, "end": end,
                    "period": f"{start} ~ {end}",
                    "managers": managers,
                    "days": self._strip_tags(tds[3]),
                    "return": self._strip_tags(tds[4]),
                })
        info["tenures"] = tenures
        info["current_tenure"] = next((t for t in tenures if "至今" in t["end"]), None)

        avatar = ""
        ma = re.search(r'<div[^>]*class="[^"]*pic[^"]*"[^>]*>.*?<img[^>]+src="([^"]+)"', html, re.DOTALL)
        if ma:
            avatar = ma.group(1)
        if not avatar:
            ma = re.search(r'<div class="jl_intro">.*?<img[^>]+src="([^"]+)"', html, re.DOTALL)
            if ma:
                avatar = ma.group(1)
        if avatar.startswith("//"):
            avatar = "https:" + avatar

        profiles: list[dict] = []
        for m in re.finditer(r'<div class="jl_intro">(.+?)</div>\s*</div>\s*</div>', html, re.DOTALL):
            block = m.group(1)
            name_m = re.search(r"<strong>姓名：</strong>\s*<a[^>]*>([^<]+)</a>", block) \
                     or re.search(r"<strong>姓名：</strong>\s*([^<]+)", block)
            start_m = re.search(r"<strong>上任日期：</strong>\s*([\d-]+)", block)
            bio = ""
            for p in re.findall(r"<p[^>]*>(.*?)</p>", block, re.DOTALL):
                text = self._strip_tags(p)
                if text.startswith("姓名") or text.startswith("上任日期"):
                    continue
                if "查看" in text and len(text) < 10:
                    continue
                if len(text) > 20:
                    bio = text
                    break
            profiles.append({
                "name": name_m.group(1).strip() if name_m else "",
                "avatar": avatar,
                "appoint_date": start_m.group(1) if start_m else "",
                "bio": bio,
            })
        info["manager_profiles"] = profiles
        return info

    def _parse_purchase_status(self, html: str) -> dict:
        result = {"purchase_status": "未知", "purchase_limit": "", "effectively_closed": False}

        m = re.search(
            r'限大额\s*\([^)]*单日累计购买上限([\d.,]+)(万?)元[^)]*\)',
            html,
        )
        if m:
            amt = float(m.group(1).replace(",", ""))
            unit = m.group(2)
            if unit == "万":
                result["purchase_limit"] = f"{amt:g}万"
                result["purchase_status"] = "限大额"
            else:
                result["purchase_limit"] = f"{amt:g}元"
                result["purchase_status"] = "限小额"
            return result

        if re.search(r'暂停申购', html):
            result["purchase_status"] = "暂停"
            result["purchase_limit"] = "0"
            result["effectively_closed"] = True
            return result

        if re.search(r'开放申购', html):
            result["purchase_status"] = "开放"
            result["purchase_limit"] = "无限制"
            return result

        m = re.search(r'单日累计购买上限[（(]?([\d.,]+)(万?)元[）)]?', html)
        if m:
            amt = float(m.group(1).replace(",", ""))
            unit = m.group(2)
            if unit == "万":
                result["purchase_limit"] = f"{amt:g}万"
                result["purchase_status"] = "限大额"
            else:
                result["purchase_limit"] = f"{amt:g}元"
                result["purchase_status"] = "限小额"
            return result

        if re.search(r'class="fix_buy unbuy"', html):
            result["purchase_status"] = "暂停"
            result["purchase_limit"] = "0"
            result["effectively_closed"] = True
            return result

        return result

    @staticmethod
    def _calc_drawdown(nav_list: list[tuple[str, float]], since: str) -> Optional[float]:
        filtered = [(d, n) for d, n in nav_list if d >= since]
        if not filtered:
            return None
        peak = filtered[0][1]
        max_dd = 0.0
        for _, nav in filtered:
            if nav > peak:
                peak = nav
            dd = (peak - nav) / peak
            if dd > max_dd:
                max_dd = dd
        return round(max_dd, 6)

    @staticmethod
    def _calc_return_from_nav(nav_list: list[tuple[str, float]], since: str) -> Optional[float]:
        filtered = [(d, n) for d, n in nav_list if d >= since]
        if len(filtered) < 2:
            return None
        start_nav = filtered[0][1]
        end_nav = filtered[-1][1]
        if start_nav <= 0:
            return None
        return round((end_nav - start_nav) / start_nav * 100, 4)

    def _fetch_holdings(self, code: str, year: int) -> list[dict]:
        text = self._get_with_retry(_HOLDING_URL.format(code=code, year=year))
        if not text:
            return []
        return self._parse_holdings_response(text)

    def _parse_holdings_response(self, text: str) -> list[dict]:
        m = re.search(r"content:\"(.+?)\",arryear:", text, re.DOTALL)
        if not m:
            return []
        content = m.group(1).replace('\\"', '"').replace("\\/", "/").replace("\\'", "'")

        quarters: list[dict] = []
        boxes = re.findall(r"<div class='box'>(.+?)(?=<div class='box'>|$)", content, re.DOTALL) or [content]
        for box in boxes:
            tm = re.search(r"(\d{4}年\s*\d季度)", box)
            if not tm:
                continue
            quarter = tm.group(1).replace(" ", "")

            tbody = re.search(r"<tbody>(.+?)</tbody>", box, re.DOTALL)
            if not tbody:
                continue
            stocks: list[dict] = []
            for tr in re.findall(r"<tr[^>]*>(.+?)</tr>", tbody.group(1), re.DOTALL):
                tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)
                if len(tds) < 5:
                    continue
                raw = [self._strip_tags(td) for td in tds]
                if not raw[0].isdigit():
                    continue
                name_m = re.search(r">([^<>]+)</a>", tds[2])
                name = name_m.group(1).strip() if name_m else raw[2]
                pct = next((v for v in raw if v.endswith("%")), "")
                stocks.append({
                    "code": raw[1], "name": name, "pct": pct,
                    "shares": raw[-2] if len(raw) >= 2 else "",
                    "value": raw[-1] if len(raw) >= 1 else "",
                })
            if stocks:
                quarters.append({"quarter": quarter, "stocks": stocks})
        return quarters
