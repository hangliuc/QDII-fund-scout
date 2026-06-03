# -*- coding: utf-8 -*-
"""天天基金全市场 JSON 快照源（高效批量）

灵感来自 alipay-nasdaq-fund-monitor 的数据源架构。
两个接口各发一次 HTTP 请求，拿到全市场 2.6 万只基金的核心数据：

1) JJJZ 接口 (Fund_JJJZ_Data.aspx, t=8)
   → 基金代码、名称、类型、净值、净值日期、申购状态、日累计限额（元）、手续费

2) RANKING 接口 (rankhandler.aspx, dx=0)
   → 近1周/1月/3月/6月/1年/3年/今年以来 收益率

组合使用后，compare 命令从 "N 只 × 16 次 HTTP" 降低到 "2 次 HTTP + 按需 CSRC"。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import requests

from core.models import FundInfo
from core.sources.base import SourceError

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://fund.eastmoney.com/",
    "Accept": "*/*",
}

JJJZ_URL = "https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx"
RANKING_URL = "http://fund.eastmoney.com/data/rankhandler.aspx"


def _safe_float(v) -> Optional[float]:
    if v in (None, "", "-", "--"):
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _format_limit(day_limit_yuan: Optional[float]) -> tuple[str, str]:
    """把元为单位的日限额转为 (purchase_status, purchase_limit) 元组

    天天基金的逻辑:
    - day_limit == 0 或 None → 暂停申购
    - day_limit > 0 → 限大额/限小额
    - 不限 → purchase_status 已经是 "开放申购"
    """
    if day_limit_yuan is None or day_limit_yuan <= 0:
        return "暂停", "0"
    if day_limit_yuan >= 10000:
        wan = day_limit_yuan / 10000
        if wan == int(wan):
            return "限大额", f"{int(wan)}万"
        return "限大额", f"{wan:.1f}万"
    return "限小额", f"{int(day_limit_yuan)}元"


class BulkSnapshot:
    """全市场快照，缓存在内存中复用"""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._jjjz: dict[str, list] = {}
        self._ranking: dict[str, list] = {}
        self._loaded = False
        self._load_time: float = 0

    def load(self, force: bool = False) -> None:
        """一次加载全市场数据（两个 HTTP）。10 分钟内缓存复用。"""
        if self._loaded and not force and time.time() - self._load_time < 600:
            return
        self._load_jjjz()
        self._load_ranking()
        self._loaded = True
        self._load_time = time.time()
        logger.info("BulkSnapshot: JJJZ=%d, RANKING=%d", len(self._jjjz), len(self._ranking))

    def _load_jjjz(self) -> None:
        """JJJZ: 限购状态 + 净值 + 日限额"""
        params = {"t": "8", "page": "1,50000", "js": "reData", "sort": "fcode,asc"}
        try:
            resp = requests.get(JJJZ_URL, params=params, headers=HEADERS, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise SourceError("eastmoney_bulk", f"JJJZ 请求失败: {e}") from e

        m = re.search(r"datas:(\[.*?\]\])\s*,", resp.text, re.DOTALL)
        if not m:
            raise SourceError("eastmoney_bulk", "JJJZ 解析失败: 未找到 datas")
        try:
            rows = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise SourceError("eastmoney_bulk", f"JJJZ JSON 解析失败: {e}") from e

        self._jjjz = {row[0]: row for row in rows if row and row[0]}

    def _load_ranking(self) -> None:
        """RANKING: 收益率"""
        today = time.strftime("%Y-%m-%d")
        params = {
            "op": "ph", "dt": "kf", "ft": "all", "rs": "", "gs": "0",
            "sc": "1nzf", "st": "desc",
            "sd": "", "ed": today,
            "qdii": "", "tabSubtype": ",,,,,",
            "pi": "1", "pn": "50000", "dx": "0",
        }
        try:
            resp = requests.get(RANKING_URL, params=params,
                                headers={**HEADERS, "Referer": "http://fund.eastmoney.com/data/fundranking.html"},
                                timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("RANKING 请求失败（非致命）: %s", e)
            return

        m = re.search(r'datas:\[(.*?)\]', resp.text, re.DOTALL)
        if not m:
            logger.warning("RANKING 解析失败: 未找到 datas")
            return
        items = m.group(1).split('","')
        items = [i.strip('"') for i in items]
        for item in items:
            fields = item.split(",")
            if fields and fields[0]:
                self._ranking[fields[0]] = fields

    def get_fund(self, code: str) -> FundInfo:
        """从快照中构造 FundInfo"""
        self.load()
        info = FundInfo(code=code, data_source="eastmoney_bulk")

        # JJJZ 数据
        jjjz = self._jjjz.get(code)
        if jjjz:
            info.name = jjjz[1] or ""
            info.type = jjjz[2] or ""
            info.nav = _safe_float(jjjz[3])
            info.nav_date = jjjz[4] or ""
            raw_status = jjjz[5] or ""
            day_limit = _safe_float(jjjz[9])

            if "暂停" in raw_status:
                info.purchase_status = "暂停"
                info.purchase_limit = "0"
                info.effectively_closed = True
            elif "开放" in raw_status:
                info.purchase_status = "开放"
                info.purchase_limit = "无限制"
            elif "限" in raw_status:
                # 用 day_limit 数值格式化具体额度
                if day_limit is not None and day_limit > 0:
                    if day_limit >= 10000:
                        wan = day_limit / 10000
                        info.purchase_limit = f"{int(wan)}万" if wan == int(wan) else f"{wan:.1f}万"
                        info.purchase_status = "限大额"
                    else:
                        info.purchase_limit = f"{int(day_limit)}元"
                        info.purchase_status = "限小额"
                        # 不在这里设 effectively_closed，交给 _build_purchase_info 判断
                else:
                    info.purchase_status = "暂停"
                    info.purchase_limit = "0"
                    info.effectively_closed = True
            else:
                info.purchase_status = raw_status
                info.purchase_limit = ""

            fee = jjjz[12] if len(jjjz) > 12 else ""
            if fee and fee != "0.00%":
                try:
                    info.service_fee = float(fee.rstrip("%"))
                except (ValueError, TypeError):
                    pass
        else:
            info.data_unavailable = True
            info.data_source = "unavailable"
            return info

        # RANKING 数据
        rank = self._ranking.get(code)
        if rank and len(rank) >= 15:
            # [6]=1w [7]=1m [8]=3m [9]=6m [10]=1y [11]=2y [12]=3y [13]=5y [14]=ytd [15]=since
            info.return_1w = _safe_float(rank[6])
            info.return_1m = _safe_float(rank[7])
            info.return_3m = _safe_float(rank[8])
            info.return_6m = _safe_float(rank[9])
            info.return_1y = _safe_float(rank[10])
            info.return_3y = _safe_float(rank[12])
            info.return_ytd = _safe_float(rank[14])
            info.return_since_inception = _safe_float(rank[15])

        return info

    def get_batch(self, codes: list[str]) -> list[FundInfo]:
        """批量获取，内部只触发 1~2 次 HTTP"""
        self.load()
        return [self.get_fund(code) for code in codes]

    @property
    def fund_count(self) -> int:
        return len(self._jjjz)

    @staticmethod
    def enrich_fund(info: FundInfo, timeout: int = 10) -> None:
        """从天天基金档案页补充 scale / fee / drawdown（单只 ~1秒）。

        仅当这些字段为 None 时才拉取，已有值不覆盖。
        供 fetch_batch 并行调用。
        """
        if info.data_unavailable:
            return
        code = info.code
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "http://fund.eastmoney.com/"}

        # 1) 档案页: 规模 + 费率
        if info.scale is None or info.total_fee is None:
            try:
                url = f"http://fundf10.eastmoney.com/jbgk_{code}.html"
                resp = requests.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    text = resp.text
                    import re
                    m = re.search(r'资产规模.*?([\d.,]+)\s*亿', text)
                    if m and info.scale is None:
                        info.scale = float(m.group(1).replace(",", ""))
                    m = re.search(r'管理费率.*?([\d.]+)%', text)
                    if m:
                        info.mgmt_fee = float(m.group(1))
                    m = re.search(r'托管费率.*?([\d.]+)%', text)
                    if m:
                        info.custody_fee = float(m.group(1))
                    m = re.search(r'销售服务费率.*?([\d.]+)%', text)
                    if m:
                        info.service_fee = float(m.group(1))
                    fees = [info.mgmt_fee or 0, info.custody_fee or 0, info.service_fee or 0]
                    if any(f > 0 for f in fees):
                        info.total_fee = round(sum(fees), 4)
            except Exception:
                pass

        # 2) NAV API: 近 1 年回撤（需要 ~250 条 NAV 数据）
        if info.drawdown_1y is None:
            try:
                import json, time
                from datetime import datetime, timedelta
                today = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
                all_navs = []
                for page in range(1, 14):
                    nav_url = (
                        f"https://api.fund.eastmoney.com/f10/lsjz"
                        f"?callback=jQuery&fundCode={code}&pageIndex={page}"
                        f"&pageSize=20&startDate={start}&endDate={today}"
                    )
                    r = requests.get(nav_url, headers=headers, timeout=timeout)
                    t = r.text.strip()
                    if t.startswith("jQuery(") and t.endswith(")"):
                        t = t[7:-1]
                    d = json.loads(t)
                    items = (d.get("Data", {}) or {}).get("LSJZList", [])
                    for it in items:
                        try:
                            nav = float(it.get("DWJZ", "0"))
                            date = it.get("FSRQ", "")
                            if nav > 0 and date:
                                all_navs.append((date, nav))
                        except (ValueError, TypeError):
                            continue
                    if len(items) < 20:
                        break
                if all_navs:
                    all_navs.sort(key=lambda x: x[0])
                    # 从 start 算最大回撤
                    peak = all_navs[0][1]
                    max_dd = 0.0
                    for _, nav in all_navs:
                        if nav > peak:
                            peak = nav
                        dd = (peak - nav) / peak
                        if dd > max_dd:
                            max_dd = dd
                    info.drawdown_1y = round(max_dd, 6)
            except Exception:
                pass
