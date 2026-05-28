from __future__ import annotations

import json
import random
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

import requests

from core.models import FundInfo

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

CSRC_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Referer": "http://eid.csrc.gov.cn/fund/disclose/advanced_search.html",
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


class BaseSource(ABC):

    @abstractmethod
    def fetch_detail(self, code: str) -> FundInfo:
        ...

    @abstractmethod
    def fetch_batch(self, codes: list[str]) -> list[FundInfo]:
        ...

    @abstractmethod
    def search_funds(self, keyword: str, fund_type: str = "") -> list[dict]:
        ...


class EastMoneySource(BaseSource):

    def _get(self, url: str, timeout: int = 20, headers: dict | None = None) -> str:
        hdrs = headers or HEADERS
        resp = requests.get(url, headers=hdrs, timeout=timeout)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} - {url}")
        return resp.text

    def _sleep(self, lo: float = 0.5, hi: float = 2.0) -> None:
        time.sleep(random.uniform(lo, hi))

    @staticmethod
    def _strip_tags(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()

    def fetch_all_fund_codes(self) -> list[list[str]]:
        text = self._get(_FUND_CODES_URL)
        m = re.search(r"var r = (\[.*\])", text, re.DOTALL)
        if not m:
            raise RuntimeError("无法解析 fundcode_search.js")
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
        info = FundInfo(code=code)

        main_html = self._get_with_retry(_FUND_URL.format(code=code))
        if main_html:
            parsed = self._parse_main_page(main_html, code)
            for k, v in parsed.items():
                setattr(info, k, v)
        self._sleep(1.0, 2.0)

        archive_html = self._get_with_retry(_ARCHIVE_URL.format(code=code))
        if archive_html:
            parsed = self._parse_archive_page(archive_html, code)
            for k, v in parsed.items():
                setattr(info, k, v)
        self._sleep(1.0, 2.0)

        manager_html = self._get_with_retry(_MANAGER_URL.format(code=code))
        if manager_html:
            parsed = self._parse_manager_page(manager_html, code)
            for k, v in parsed.items():
                setattr(info, k, v)
        self._sleep(1.0, 2.0)

        nav_result = self._fetch_nav_and_drawdown(code)
        if nav_result:
            info.nav_list = nav_result["nav_list"]
            info.drawdown_1y = nav_result["drawdown_1y"]
            info.drawdown_3y = nav_result["drawdown_3y"]

        info.update_date = time.strftime("%Y-%m-%d")
        return info

    def fetch_batch(self, codes: list[str]) -> list[FundInfo]:
        results: list[FundInfo] = []
        for code in codes:
            try:
                info = self.fetch_detail(code)
                results.append(info)
            except Exception as e:
                print(f"  ! fetch_detail({code}) 失败: {e}")
            self._sleep(0.5, 1.0)
        return results

    def _get_with_retry(self, url: str, retries: int = 2, timeout: int = 20) -> Optional[str]:
        for i in range(retries + 1):
            try:
                return self._get(url, timeout=timeout)
            except Exception as e:
                print(f"  ! 请求失败 ({i + 1}/{retries + 1}) {url} - {e}")
                self._sleep()
        return None

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

    def _fetch_nav_and_drawdown(self, code: str) -> Optional[dict]:
        today = datetime.now().strftime("%Y-%m-%d")
        start_1y = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y-%m-%d")
        start_3y = (datetime.now().replace(year=datetime.now().year - 3)).strftime("%Y-%m-%d")

        all_navs: list[tuple[str, float]] = []
        page = 1
        while True:
            url = _NAV_URL.format(code=code, page=page, start=start_3y, end=today)
            try:
                text = self._get(url)
            except Exception:
                break
            m = re.search(r'"Data":\s*(\{.*?\})\s*\}', text, re.DOTALL)
            if not m:
                break
            try:
                data = json.loads("{" + m.group(0) if not m.group(0).startswith("{") else m.group(0))
            except json.JSONDecodeError:
                data_match = re.search(r'"LSJZList"\s*:\s*(\[.*?\])', text, re.DOTALL)
                total_match = re.search(r'"TotalCount"\s*:\s*(\d+)', text)
                if not data_match:
                    break
                items = json.loads(data_match.group(1))
                total = int(total_match.group(1)) if total_match else 0
            else:
                items = data.get("Data", {}).get("LSJZList", [])
                total = data.get("Data", {}).get("TotalCount", 0)

            if not items:
                break
            for item in items:
                try:
                    nav_val = float(item.get("DWJZ", "0"))
                    date_val = item.get("FSRQ", "")
                    if nav_val > 0 and date_val:
                        all_navs.append((date_val, nav_val))
                except (ValueError, TypeError):
                    continue

            if page * 20 >= total:
                break
            page += 1
            self._sleep(0.3, 0.8)

        if not all_navs:
            return None

        all_navs.sort(key=lambda x: x[0])

        drawdown_1y = self._calc_drawdown(all_navs, start_1y)
        drawdown_3y = self._calc_drawdown(all_navs, start_3y)

        return {
            "nav_list": [{"date": d, "nav": n} for d, n in all_navs],
            "drawdown_1y": drawdown_1y,
            "drawdown_3y": drawdown_3y,
        }

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
