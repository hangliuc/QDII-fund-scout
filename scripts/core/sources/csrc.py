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
    r'意大利|西班牙|墨西哥|南非|泰国|印度尼西亚|马来西亚|越南)'
    r'\s+([\d,，.]+)\s+([\d.]+)'
)

INDUSTRY_PATTERN = re.compile(
    r'^([A-Z]\s*[\u4e00-\u9fa5、]+)\s+[\d,，.]+\s+([\d.]+)\s*$'
)

COUNTRY_ALIAS = {
    "香港": "中国香港",
    "台湾": "中国台湾",
}


class CSRCSource:
    def __init__(
        self,
        report_year: str = "2026",
        report_type: str = "FB030",
        target_quarter: str = "第1季度",
        rate_limit: float = 1.0,
    ):
        self.report_year = report_year
        self.report_type = report_type
        self.target_quarter = target_quarter
        self.rate_limit = rate_limit

    def _source_tag(self) -> str:
        q_map = {"第1季度": "Q1", "第2季度": "Q2", "第3季度": "Q3", "第4季度": "Q4"}
        q = q_map.get(self.target_quarter, "Q1")
        return f"csrc_{self.report_year}{q}"

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
        rec = self._csrc_search(fund_code=main_code)
        if rec:
            return rec
        if short_name:
            time.sleep(random.uniform(0.3, 0.6))
            rec = self._csrc_search(fund_short_name=short_name)
            if rec:
                return rec
        for name in self._simplify_name(short_name):
            time.sleep(random.uniform(0.3, 0.6))
            rec = self._csrc_search(fund_short_name=name)
            if rec:
                return rec
        return None

    def _download_pdf(self, instance_id: str) -> bytes | None:
        url = CSRC_PDF_URL.format(iid=instance_id)
        try:
            resp = requests.get(url, headers=CSRC_HEADERS, timeout=45)
            if resp.status_code != 200:
                logger.warning("csrc PDF 下载返回 HTTP %d (iid=%s)", resp.status_code, instance_id)
                return None
            if not resp.content.startswith(b"%PDF"):
                logger.warning("csrc PDF 内容非 PDF 格式 (iid=%s, size=%d)", instance_id, len(resp.content))
                return None
            return resp.content
        except requests.RequestException as e:
            logger.warning("csrc PDF 下载失败 (iid=%s): %s", instance_id, e)
            return None

    def _parse_pdf_market_dist(self, pdf_bytes: bytes) -> dict:
        result: dict = {}
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    if "国家" not in text:
                        continue
                    if "未持有股票" in text or "未持有" in text:
                        for raw_line in text.split("\n"):
                            line = raw_line.strip()
                            if "国家" in line and ("地区" in line or "证券市场" in line):
                                next_lines = text.split("\n")
                                idx = next_lines.index(raw_line) if raw_line in next_lines else -1
                                if idx >= 0 and idx + 1 < len(next_lines):
                                    next_line = next_lines[idx + 1].strip()
                                    if "未持有" in next_line:
                                        return {"_no_holdings": True}
                    if "公允" not in text and "比例" not in text:
                        continue

                    in_section = False
                    for raw_line in text.split("\n"):
                        line = raw_line.strip()
                        if "国家" in line and ("地区" in line or "公允" in line):
                            in_section = True
                            continue
                        if not in_section:
                            continue
                        if line.startswith("合计") or line.startswith("注") or line.startswith("小计"):
                            in_section = False
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

    def _parse_pdf_industry_dist(self, pdf_bytes: bytes) -> dict:
        result: dict = {}
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    if "行业" not in text:
                        continue
                    if "公允" not in text and "比例" not in text:
                        continue

                    in_section = False
                    for raw_line in text.split("\n"):
                        line = raw_line.strip()
                        if "行业" in line and ("分类" in line or "公允" in line):
                            in_section = True
                            continue
                        if not in_section:
                            continue
                        if line.startswith("合计") or line.startswith("注") or line.startswith("小计"):
                            in_section = False
                            continue
                        m = INDUSTRY_PATTERN.match(line)
                        if not m:
                            continue
                        industry = m.group(1).strip()
                        pct = float(m.group(2))
                        if industry in result:
                            result[industry] = max(result[industry], pct)
                        else:
                            result[industry] = pct
        except Exception as e:
            logger.warning("csrc 行业分布 PDF 解析失败: %s", e)
            return {}
        return result

    def fetch_market_distribution(self, main_code: str, short_name: str = "") -> dict:
        rec = self.search_report(main_code, short_name)
        if not rec:
            return {"_source": self._source_tag(), "_total_pct": 0, "_inferred": True, "_note": "not_found"}

        instance_id = str(rec.get("uploadInfoId", ""))
        pdf_bytes = self._download_pdf(instance_id)
        if not pdf_bytes:
            return {"_source": self._source_tag(), "_total_pct": 0, "_inferred": True, "_note": "pdf_download_failed", "_instance_id": instance_id}

        dist = self._parse_pdf_market_dist(pdf_bytes)
        total = round(sum(dist.values()), 2) if dist else 0

        time.sleep(random.uniform(0.1, 0.3))

        if dist.get("_no_holdings"):
            return {"_source": self._source_tag(), "_total_pct": 0, "_inferred": True, "_note": "no_holdings", "_instance_id": instance_id}
        non_meta = {k: v for k, v in dist.items() if not k.startswith("_")}
        if non_meta:
            return {**dist, "_source": self._source_tag(), "_total_pct": total, "_inferred": False}
        return {"_source": self._source_tag(), "_total_pct": 0, "_inferred": True, "_note": "no_table", "_instance_id": instance_id}

    def fetch_industry_distribution(self, main_code: str, short_name: str = "") -> dict:
        rec = self.search_report(main_code, short_name)
        if not rec:
            return {"_source": self._source_tag(), "_total_pct": 0, "_inferred": True, "_note": "not_found"}

        instance_id = str(rec.get("uploadInfoId", ""))
        pdf_bytes = self._download_pdf(instance_id)
        if not pdf_bytes:
            return {"_source": self._source_tag(), "_total_pct": 0, "_inferred": True, "_note": "pdf_download_failed", "_instance_id": instance_id}

        dist = self._parse_pdf_industry_dist(pdf_bytes)
        total = round(sum(dist.values()), 2) if dist else 0

        time.sleep(random.uniform(0.1, 0.3))

        if dist:
            return {**dist, "_source": self._source_tag(), "_total_pct": total, "_inferred": False}
        return {"_source": self._source_tag(), "_total_pct": 0, "_inferred": True, "_note": "no_table", "_instance_id": instance_id}
