from __future__ import annotations

import io
import json
import logging
import random
import re
import time

import pdfplumber
import requests

from core.sources.base import SourceError
from core.sources.csrc_cache import CSRCCache

logger = logging.getLogger(__name__)

CSRC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://eid.csrc.gov.cn/fund/disclose/advanced_search.html",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

CSRC_SEARCH_URL = "http://eid.csrc.gov.cn/fund/disclose/advanced_search_report.do"
CSRC_PDF_URL = "http://eid.csrc.gov.cn/fund/disclose/instance_show_pdf_id.do?instanceid={iid}"

COUNTRY_PATTERN = re.compile(
    r'(美国|中国内地|中国香港|香港|日本|韩国|英国|德国|法国|印度|新加坡|'
    r'澳大利亚|加拿大|瑞士|荷兰|巴西|以色列|开曼群岛|百慕大|中国台湾|台湾|'
    r'意大利|西班牙|墨西哥|南非|泰国|印度尼西亚|马来西亚|越南|中国)'
    r'\s+([\d,，.]+)\s+([\d.]+)'
)

INDUSTRY_PATTERN = re.compile(
    # 兼容多种披露格式：
    # 1) 申万行业：「C 制造业 ... 5.23」前缀为单字母 + 中文行业名
    # 2) GICS 全球行业(中文)：「信息技术 6,990,209,186.83 70.85」纯中文行业名
    # 3) GICS 全球行业(数字代码)：「45 信息技术 ... 70.85」前缀为 GICS 类别码
    r'^([A-Z]?\d{0,2}\s*[\u4e00-\u9fa5][\u4e00-\u9fa5、\-\s]*?)\s+[\d,，.]+\s+([\d.]+)\s*$'
)

COUNTRY_ALIAS = {
    "香港": "中国香港",
    "台湾": "中国台湾",
    "中国": "中国内地",   # 部分基金披露用"中国"指代 A 股市场
}


class CSRCSource:
    """证监会基金披露数据源

    支持两种模式：
    1. 指定季度模式 (target_quarter="第1季度")：只搜该季度
    2. 自动模式 (target_quarter="auto")：取该基金最新季报，自动适应季报披露周期

    默认走自动模式，省去每季度手动更新 target_quarter 的工作。
    """

    def __init__(
        self,
        report_year: str = "",          # 空字符串 = 不限年份（自动模式）
        report_type: str = "FB030",
        target_quarter: str = "auto",   # auto / 第1季度 / 第2季度 / 第3季度 / 第4季度
        rate_limit: float = 1.0,
        cache: CSRCCache | None = None,
        use_cache: bool = True,
    ):
        self.report_year = report_year
        self.report_type = report_type
        self.target_quarter = target_quarter
        self.rate_limit = rate_limit
        self.cache = cache if cache is not None else (CSRCCache() if use_cache else None)
        self.use_cache = use_cache and self.cache is not None

    def _source_tag(self, rec: dict | None = None) -> str:
        """生成 _source 标记。auto 模式下根据实际命中的季报生成"""
        if rec:
            # 从 reportName 解析年份和季度（兼容"第1季度"、"第一季度"两种写法）
            name = rec.get("reportName", "")
            m = re.search(r"(\d{4})年第(\d)季度", name)
            if m:
                return f"csrc_{m.group(1)}Q{m.group(2)}"
            # 兼容中文数字
            cn_q = {"一": "1", "二": "2", "三": "3", "四": "4"}
            m = re.search(r"(\d{4})年第([一二三四])季度", name)
            if m:
                return f"csrc_{m.group(1)}Q{cn_q[m.group(2)]}"
        # 显式指定模式
        if self.target_quarter and self.target_quarter != "auto":
            q_map = {"第1季度": "Q1", "第2季度": "Q2", "第3季度": "Q3", "第4季度": "Q4"}
            q = q_map.get(self.target_quarter, "?")
            return f"csrc_{self.report_year or 'latest'}{q}"
        return "csrc_latest"

    def _ao_data(self, fund_code: str = "", fund_short_name: str = "") -> list[dict]:
        return [
            {"name": "sEcho", "value": 1},
            {"name": "iColumns", "value": 6},
            {"name": "sColumns", "value": ""},
            {"name": "iDisplayStart", "value": 0},
            {"name": "iDisplayLength", "value": 20},
            {"name": "mDataProp_0", "value": "fund"},
            {"name": "mDataProp_1", "value": "fund"},
            {"name": "mDataProp_2", "value": "reportName"},
            {"name": "mDataProp_3", "value": "reportName"},
            {"name": "mDataProp_4", "value": "reportDesp"},
            {"name": "mDataProp_5", "value": "reportSendDate"},
            {"name": "iSortingCols", "value": 0},
            {"name": "fundType", "value": ""},
            {"name": "reportType", "value": self.report_type},
            {"name": "reportYear", "value": self.report_year},
            {"name": "fundCompanyShortName", "value": ""},
            {"name": "fundCode", "value": fund_code},
            {"name": "fundShortName", "value": fund_short_name},
            {"name": "startUploadDate", "value": ""},
            {"name": "endUploadDate", "value": ""},
        ]

    def _csrc_search(self, fund_code: str = "", fund_short_name: str = "") -> dict | None:
        try:
            resp = requests.get(
                CSRC_SEARCH_URL,
                params={"aoData": json.dumps(self._ao_data(fund_code, fund_short_name))},
                headers=CSRC_HEADERS,
                timeout=20,
            )
            if resp.status_code != 200:
                logger.warning("csrc 搜索返回 HTTP %d (code=%s name=%s)", resp.status_code, fund_code, fund_short_name)
                return None
            data = resp.json()
        except requests.RequestException as e:
            logger.warning("csrc 搜索请求失败 (code=%s): %s", fund_code, e)
            return None
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("csrc 搜索响应解析失败 (code=%s): %s", fund_code, e)
            return None

        records = data.get("aaData", [])
        if not records:
            logger.info("csrc 搜索无结果 (code=%s name=%s)", fund_code, fund_short_name)
            return None

        # 自动模式：按 reportSendDate 倒序选最新；同时偏好 quarterly (第X季度)
        if self.target_quarter == "auto" or not self.target_quarter:
            # 优先取季度报告（季报最新最准），按发布日期倒序
            quarterly = [r for r in records if "季度" in (r.get("reportName") or "")]
            sorted_recs = sorted(
                quarterly or records,
                key=lambda x: x.get("reportSendDate", ""),
                reverse=True,
            )
            return sorted_recs[0]

        for item in records:
            if self.target_quarter in (item.get("reportName") or ""):
                return item
        return records[0]

    @staticmethod
    def _simplify_name(name: str) -> list[str]:
        if not name:
            return []
        variations: list[str] = []
        _CURRENCY_RE = re.compile(r'(人民币|美元|港元|美元现汇|美元现钞)')
        _TYPE_RE = re.compile(r'(混合|股票|债券|灵活配置|指数)')
        _SUFFIX_RE = re.compile(r'[ACDE]$')
        _QDII_TAG_RE = re.compile(r'\(QDII-LOF\)|\(QDII\)')

        base = _SUFFIX_RE.sub('', name).strip()
        if base and base != name:
            variations.append(base)

        no_currency = _CURRENCY_RE.sub('', base).strip()
        if no_currency and no_currency != name and no_currency not in variations:
            variations.append(no_currency)

        no_type = _TYPE_RE.sub('', no_currency).strip()
        no_type = re.sub(r'\s+', '', no_type)
        if no_type and no_type != name and no_type not in variations:
            variations.append(no_type)

        no_qdii_tag = _QDII_TAG_RE.sub('', no_type).strip()
        no_qdii_tag = re.sub(r'\s+', '', no_qdii_tag)
        if no_qdii_tag and no_qdii_tag != name and no_qdii_tag not in variations:
            variations.append(no_qdii_tag)

        no_qdii_from_base = _QDII_TAG_RE.sub('', base).strip()
        no_qdii_from_base = re.sub(r'\s+', '', no_qdii_from_base)
        if no_qdii_from_base and no_qdii_from_base != name and no_qdii_from_base not in variations:
            variations.append(no_qdii_from_base)

        bare = _SUFFIX_RE.sub('', name)
        bare = _CURRENCY_RE.sub('', bare)
        bare = _TYPE_RE.sub('', bare)
        bare = _QDII_TAG_RE.sub('', bare)
        bare = re.sub(r'\s+', '', bare).strip()
        if bare and bare != name and bare not in variations:
            variations.append(bare)

        return variations

    def search_report(self, main_code: str, short_name: str = "") -> dict | None:
        # 自动模式: 优先看缓存索引（24h TTL），命中且 instance_id 已知就直接返回
        if self.use_cache and self.target_quarter == "auto":
            cached = self.cache.get_cached_index(main_code)
            if cached and cached.get("instance_id"):
                return {
                    "uploadInfoId": cached["instance_id"],
                    "reportName": cached.get("report_name", ""),
                    "reportSendDate": cached.get("report_send_date", ""),
                    "fundCode": main_code,
                    "_from_cache": True,
                }

        rec = self._csrc_search(fund_code=main_code)
        if not rec and short_name:
            time.sleep(random.uniform(0.3, 0.6))
            rec = self._csrc_search(fund_short_name=short_name)
        if not rec:
            for name in self._simplify_name(short_name):
                time.sleep(random.uniform(0.3, 0.6))
                rec = self._csrc_search(fund_short_name=name)
                if rec:
                    break

        # 把命中结果写入索引
        if rec and self.use_cache and self.target_quarter == "auto":
            self.cache.save_index(
                main_code=main_code,
                instance_id=str(rec.get("uploadInfoId", "")),
                report_name=rec.get("reportName", ""),
                report_send_date=rec.get("reportSendDate", ""),
            )
        return rec

    def _download_pdf(self, instance_id: str) -> bytes | None:
        # 先查本地缓存
        if self.use_cache:
            cached = self.cache.get_pdf(instance_id)
            if cached:
                return cached

        url = CSRC_PDF_URL.format(iid=instance_id)
        try:
            resp = requests.get(url, headers=CSRC_HEADERS, timeout=45)
            if resp.status_code != 200:
                logger.warning("csrc PDF 下载返回 HTTP %d (iid=%s)", resp.status_code, instance_id)
                return None
            if not resp.content.startswith(b"%PDF"):
                logger.warning("csrc PDF 内容非 PDF 格式 (iid=%s, size=%d)", instance_id, len(resp.content))
                return None
            # 写入缓存
            if self.use_cache:
                self.cache.save_pdf(instance_id, resp.content)
            return resp.content
        except requests.RequestException as e:
            logger.warning("csrc PDF 下载失败 (iid=%s): %s", instance_id, e)
            return None

    def _parse_pdf_market_dist(self, pdf_bytes: bytes) -> dict:
        result: dict = {}
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                # 全文拼接以防表格跨页
                all_text = "\n".join((p.extract_text() or "") for p in pdf.pages)

                if "国家" not in all_text:
                    return result

                # 检查"未持有股票"标记
                if "未持有股票" in all_text or ("未持有" in all_text and "国家" in all_text):
                    # 双重检查：必须在国家分布段落附近出现
                    m_section = re.search(
                        r"5\.2[^\n]*?国家(.*?)(?=5\.3|前十名股票)",
                        all_text, re.DOTALL,
                    )
                    if m_section and "未持有" in m_section.group(1):
                        return {"_no_holdings": True}

                # 仅截取 5.2 节
                m_section = re.search(
                    r"5\.2[^\n]*?国家(.*?)(?=5\.3|前十名股票)",
                    all_text, re.DOTALL,
                )
                section_text = m_section.group(1) if m_section else all_text

                in_section = False
                for raw_line in section_text.split("\n"):
                    line = raw_line.strip()
                    if "国家" in line and ("地区" in line or "公允" in line):
                        in_section = True
                        continue
                    if not in_section:
                        continue
                    if line.startswith("合计") or line.startswith("注") or line.startswith("小计"):
                        in_section = False
                        continue
                    if "第" in line and "页" in line:
                        continue
                    m = COUNTRY_PATTERN.match(line)
                    if not m:
                        continue
                    country = COUNTRY_ALIAS.get(m.group(1), m.group(1))
                    pct = float(m.group(3))
                    if country in result:
                        result[country] = max(result[country], pct)
                    else:
                        result[country] = pct
        except Exception as e:
            logger.warning("csrc 市场分布 PDF 解析失败: %s", e)
            return {}
        return result

    def _parse_pdf_fund_holdings(self, pdf_bytes: bytes) -> list[dict]:
        """解析 FoF / QDII-LOF 的"前十名基金投资明细"

        识别多行表格行，比如:
            ARK
            ARK
            指数基 交易型开 Investment
            1 Innovation 246,681,339.06 18.74
            金 放式 Management
            ETF
            LLC

        策略：
        1. 把所有页文本拼接（表格可能跨页）
        2. 用包含"序号 + 公允价值 + 百分比"的锚定行作为基准
        3. 每个 ETF 的英文名分散在锚定行前后约 6 行内，按出现顺序拼接
        """
        result: list[dict] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                # 拼接所有页（表格常跨页）
                all_text = "\n".join((p.extract_text() or "") for p in pdf.pages)

                if "基金投资明细" not in all_text:
                    return []

                m = re.search(
                    r"基金投资明细(.*?)(?=5\.10|5\.11|投资组合报告附注)",
                    all_text, re.DOTALL,
                )
                if not m:
                    return []
                target_text = m.group(1)

                lines = target_text.split("\n")
                anchor_re = re.compile(
                    r"^\s*(\d+)\s+(.+?)\s+([\d,]+\.\d+)\s+([\d.]+)\s*$"
                )

                # 第一遍：找出所有锚点行号
                anchor_indices: list[tuple[int, int, str, str, float]] = []
                for idx, line in enumerate(lines):
                    m = anchor_re.match(line.strip())
                    if not m:
                        continue
                    seq = int(m.group(1))
                    if seq < 1 or seq > 10:
                        continue
                    pct = float(m.group(4))
                    if pct < 0.1 or pct > 50:
                        continue
                    anchor_indices.append((idx, seq, m.group(2), m.group(3), pct))

                # 锚点之间的窗口边界
                for i, (idx, seq, middle, value_str, pct) in enumerate(anchor_indices):
                    # 上界：上一个锚点之后 / 文本起点
                    upper = anchor_indices[i - 1][0] + 1 if i > 0 else 0
                    # 下界：下一个锚点之前 / 文本终点（且最多向前 4 行，避免跨条目串）
                    if i + 1 < len(anchor_indices):
                        lower = anchor_indices[i + 1][0]
                    else:
                        lower = min(len(lines), idx + 5)
                    window = lines[upper:lower]

                    candidates: list[str] = []
                    if re.search(r"[A-Za-z]", middle):
                        candidates.append(middle.strip())
                    for w in window:
                        wl = w.strip()
                        if not wl or wl == lines[idx].strip():
                            continue
                        if any(skip in wl for skip in ("序号", "基金名称", "比例", "占基金", "基金类")):
                            continue
                        ascii_tokens = re.findall(
                            r"[A-Za-z][A-Za-z0-9&\.\-/\s]*[A-Za-z0-9\)]", wl
                        )
                        for tok in ascii_tokens:
                            tok = re.sub(r"\s+", " ", tok).strip()
                            # 排除显著的"管理人"标记
                            if any(co in tok for co in (
                                " LLC", " Inc", " Inc.", " Corp", " Advisors",
                                " Capital Management", "Fund Advisors",
                                "BlackRock", "Invesco Capital", "State Street",
                                "Van Eck Associates", "ProShares Capital",
                                "ARK Investment Management",
                                "Global X Management", "SSgA Funds Management",
                                "Citibank",
                            )):
                                continue
                            candidates.append(tok)

                    seen = set()
                    parts = []
                    for c in candidates:
                        c2 = re.sub(r"\s+", " ", c).strip()
                        if not c2 or len(c2) < 2:
                            continue
                        if len(c2) <= 2 and c2.upper() not in ("X", "AI"):
                            continue
                        if c2 in seen:
                            continue
                        seen.add(c2)
                        parts.append(c2)
                    full_name = " ".join(parts)
                    full_name = re.sub(r"^\d+\s*", "", full_name).strip()
                    full_name = self._normalize_etf_name(full_name)

                    result.append({
                        "seq": seq,
                        "name": full_name,
                        "pct": pct,
                        "value": value_str,
                    })

                # 按 seq 去重并排序
                seen_seq = set()
                uniq = []
                for r in result:
                    if r["seq"] in seen_seq:
                        continue
                    seen_seq.add(r["seq"])
                    uniq.append(r)
                uniq.sort(key=lambda x: x["seq"])
                return uniq
        except Exception as e:
            logger.warning("csrc 基金投资明细 PDF 解析失败: %s", e)
            return []

    def _parse_pdf_industry_dist(self, pdf_bytes: bytes) -> dict:
        result: dict = {}
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                # 全文拼接（行业表常常跨页）
                all_text = "\n".join(
                    (p.extract_text() or "") for p in pdf.pages
                )
                if "行业" not in all_text:
                    return result
                # 仅截取 5.3 节，避免误抓其他表
                m = re.search(
                    r"5\.3[^\n]*?行业(.*?)(?=5\.4|前十名股票)",
                    all_text, re.DOTALL,
                )
                section_text = m.group(1) if m else all_text

                in_section = False
                for raw_line in section_text.split("\n"):
                    line = raw_line.strip()
                    if "行业" in line and ("分类" in line or "公允" in line or "类别" in line):
                        in_section = True
                        continue
                    if not in_section:
                        continue
                    if line.startswith("合计") or line.startswith("注") or line.startswith("小计"):
                        # 一个表结束，但可能还有续表（如 5.3.1）
                        in_section = False
                        continue
                    # 跳过页眉/页脚（"第 X 页"、基金简称重复等）
                    if "第" in line and "页" in line:
                        continue
                    m_match = INDUSTRY_PATTERN.match(line)
                    if not m_match:
                        continue
                    industry = m_match.group(1).strip()
                    # 去掉 GICS 数字前缀（如 "45 信息技术" -> "信息技术"）
                    industry = re.sub(r'^[A-Z]?\d{0,2}\s+', '', industry).strip()
                    pct = float(m_match.group(2))
                    if industry in result:
                        result[industry] = max(result[industry], pct)
                    else:
                        result[industry] = pct
        except Exception as e:
            logger.warning("csrc 行业分布 PDF 解析失败: %s", e)
            return {}
        return result

    @staticmethod
    def _normalize_etf_name(raw: str) -> str:
        """从碎片化的英文 token 中尝试组合出标准 ETF 名"""
        if not raw:
            return raw
        s = re.sub(r"\s+", " ", raw).strip()
        return s

    def fetch_exposure(self, main_code: str, short_name: str = "") -> dict:
        """一次性获取该基金的完整披露数据 (market_dist + industry_dist + fund_holdings)。

        优先从本地解析缓存读取（按 instance_id 命中），
        缓存未命中时下载 PDF + 解析 + 写缓存。

        返回结构：
            {
              "instance_id": "...",
              "report_name": "...",
              "report_quarter": "2026Q1",
              "market_dist": {...},
              "industry_dist": {...},
              "fund_holdings": [...],   # FoF/LOF 才有
              "from_cache": bool,
            }

        如果该基金未找到任何披露，返回 {"instance_id": "", "report_quarter": "", ...} 空字段。
        """
        rec = self.search_report(main_code, short_name)
        if not rec:
            return {
                "instance_id": "",
                "report_name": "",
                "report_quarter": "",
                "market_dist": {"_inferred": True, "_note": "not_found"},
                "industry_dist": {"_inferred": True, "_note": "not_found"},
                "fund_holdings": [],
                "from_cache": False,
            }

        instance_id = str(rec.get("uploadInfoId", ""))
        report_name = rec.get("reportName", "")
        # 解析季度 tag（兼容"第1季度"、"第一季度"两种写法）
        m = re.search(r"(\d{4})年第(\d)季度", report_name)
        if m:
            report_quarter = f"{m.group(1)}Q{m.group(2)}"
        else:
            cn_q = {"一": "1", "二": "2", "三": "3", "四": "4"}
            m = re.search(r"(\d{4})年第([一二三四])季度", report_name)
            report_quarter = f"{m.group(1)}Q{cn_q[m.group(2)]}" if m else ""

        # 先查解析缓存
        if self.use_cache:
            parsed = self.cache.get_parsed(instance_id)
            if parsed:
                parsed["from_cache"] = True
                return parsed

        # 未命中，下载并解析
        pdf_bytes = self._download_pdf(instance_id)
        if not pdf_bytes:
            return {
                "instance_id": instance_id,
                "report_name": report_name,
                "report_quarter": report_quarter,
                "market_dist": {"_inferred": True, "_note": "pdf_download_failed"},
                "industry_dist": {"_inferred": True, "_note": "pdf_download_failed"},
                "fund_holdings": [],
                "from_cache": False,
            }

        market_raw = self._parse_pdf_market_dist(pdf_bytes)
        industry_raw = self._parse_pdf_industry_dist(pdf_bytes)
        fund_holdings = self._parse_pdf_fund_holdings(pdf_bytes)

        # 拼成对外标准结构
        market_total = round(sum(v for k, v in market_raw.items() if not k.startswith("_")), 2)
        industry_total = round(sum(v for k, v in industry_raw.items() if not k.startswith("_")), 2)

        market_dist = {
            **{k: v for k, v in market_raw.items() if not k.startswith("_")},
            "_source": self._source_tag(rec),
            "_total_pct": market_total,
            "_inferred": market_total == 0,
            "_instance_id": instance_id,
        }
        if market_raw.get("_no_holdings"):
            market_dist["_note"] = "no_holdings"

        industry_dist = {
            **{k: v for k, v in industry_raw.items() if not k.startswith("_")},
            "_source": self._source_tag(rec),
            "_total_pct": industry_total,
            "_inferred": industry_total == 0,
            "_instance_id": instance_id,
        }

        result = {
            "instance_id": instance_id,
            "report_name": report_name,
            "report_quarter": report_quarter,
            "market_dist": market_dist,
            "industry_dist": industry_dist,
            "fund_holdings": fund_holdings,
            "from_cache": False,
        }

        if self.use_cache:
            self.cache.save_parsed(instance_id, result)

        return result

    def fetch_market_distribution(self, main_code: str, short_name: str = "") -> dict:
        """获取市场分布。内部走 fetch_exposure 以共享缓存。"""
        return self.fetch_exposure(main_code, short_name).get("market_dist", {
            "_inferred": True, "_note": "not_found"
        })

    def fetch_industry_distribution(self, main_code: str, short_name: str = "") -> dict:
        return self.fetch_exposure(main_code, short_name).get("industry_dist", {
            "_inferred": True, "_note": "not_found"
        })

    def fetch_fund_holdings(self, main_code: str, short_name: str = "") -> list[dict]:
        """对于 FoF/QDII-LOF/ETF联接 类基金，从 CSRC PDF 解析"前十名基金投资明细" """
        return self.fetch_exposure(main_code, short_name).get("fund_holdings", [])

    # 保留旧实现作为内部细粒度接口（已废弃，新代码请用 fetch_exposure）
    def _fetch_market_distribution_legacy(self, main_code: str, short_name: str = "") -> dict:
        rec = self.search_report(main_code, short_name)
        if not rec:
            return {"_source": self._source_tag(), "_total_pct": 0, "_inferred": True, "_note": "not_found"}

        instance_id = str(rec.get("uploadInfoId", ""))
        pdf_bytes = self._download_pdf(instance_id)
        if not pdf_bytes:
            return {"_source": self._source_tag(rec), "_total_pct": 0, "_inferred": True, "_note": "pdf_download_failed", "_instance_id": instance_id}

        dist = self._parse_pdf_market_dist(pdf_bytes)
        total = round(sum(dist.values()), 2) if dist else 0

        time.sleep(random.uniform(0.1, 0.3))

        if dist.get("_no_holdings"):
            return {"_source": self._source_tag(rec), "_total_pct": 0, "_inferred": True, "_note": "no_holdings", "_instance_id": instance_id}
        non_meta = {k: v for k, v in dist.items() if not k.startswith("_")}
        if non_meta:
            return {**dist, "_source": self._source_tag(rec), "_total_pct": total, "_inferred": False, "_instance_id": instance_id}
        return {"_source": self._source_tag(rec), "_total_pct": 0, "_inferred": True, "_note": "no_table", "_instance_id": instance_id}
