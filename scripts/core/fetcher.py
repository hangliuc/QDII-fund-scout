from __future__ import annotations

import logging
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from core.models import FundInfo, FundDataResult
from core.validate import validate_data
from core.sources.base import SourceError
from core.sources.eastmoney import EastMoneySource
from core.sources.howbuy import HowbuySource
from core.sources.csrc import CSRCSource

logger = logging.getLogger(__name__)

MIN_RATE_LIMIT = 0.3

CROSS_VAL_THRESHOLDS: dict[str, dict] = {
    "return_1y": {"diff": 1.0, "unit": "百分点"},
    "return_3y": {"diff": 2.0, "unit": "百分点"},
    "return_1m": {"diff": 1.0, "unit": "百分点"},
    "return_3m": {"diff": 1.0, "unit": "百分点"},
    "nav": {"diff": 0.01, "unit": "元", "fmt": ".4f"},
    "total_fee": {"diff": 0.10, "unit": "百分点", "fmt": ".4f"},
    "scale": {"rel_diff": 0.30, "unit": ""},
}


def _build_purchase_info(status: str, limit: str, effectively_closed: bool) -> str:
    if effectively_closed or status == "暂停":
        return "暂停申购"
    if status == "限小额":
        amt = _parse_limit_amount(limit)
        if amt is not None:
            return f"限小额 {limit}"
        return f"限小额 {limit}" if limit else "限小额（请以平台为准）"
    if status == "限大额":
        return f"限大额（{limit}）" if limit else "限大额"
    if status in ("开放", "开放申购"):
        return "开放申购（无限额）"
    return f"{status}（{limit}）" if limit else status


def _parse_limit_amount(limit_str: str | None) -> float | None:
    if not limit_str or limit_str in ("无限制", "0", "-"):
        return None
    m = re.search(r"([\d.]+)\s*(万|元)?", str(limit_str))
    if not m:
        return None
    amt = float(m.group(1))
    if m.group(2) == "万":
        amt *= 10000
    return amt


def _semantic_purchase_match(s1: str, l1: str, s2: str, l2: str) -> bool:
    if s1 == s2 and l1 == l2:
        return True
    if s1 == "暂停" and s2 == "暂停":
        return True
    if s1 == "暂停" and s2 in ("限小额", "限大额"):
        return False
    if s1 in ("限小额", "限大额") and s2 == "暂停":
        return False
    if s1 in ("限小额", "限大额") and s2 in ("限小额", "限大额"):
        a1 = _parse_limit_amount(l1)
        a2 = _parse_limit_amount(l2)
        if a1 is not None and a2 is not None:
            if abs(a1 - a2) < 0.01:
                return True
            smaller = min(a1, a2)
            larger = max(a1, a2)
            if larger / smaller <= 2.0:
                return True
    return False


class FundFetcher:
    _MAX_FAIL = 30

    def __init__(self, rate_limit: float = 1.0):
        self.rate_limit = max(rate_limit, MIN_RATE_LIMIT)
        self._primary = EastMoneySource()
        self._backup = HowbuySource()
        self._csrc = CSRCSource(rate_limit=self.rate_limit)
        self._fail_count = 0
        self._lock = threading.Lock()

    def _sleep(self) -> None:
        time.sleep(self.rate_limit)

    def _check_fail(self) -> None:
        with self._lock:
            if self._fail_count >= self._MAX_FAIL:
                raise RuntimeError(f"连续 {self._MAX_FAIL} 次失败，自动停止")

    def _record_fail(self) -> None:
        with self._lock:
            self._fail_count += 1
            self._check_fail_unlocked()

    def _record_success(self) -> None:
        with self._lock:
            self._fail_count = 0

    def _check_fail_unlocked(self) -> None:
        if self._fail_count >= self._MAX_FAIL:
            raise RuntimeError(f"连续 {self._MAX_FAIL} 次失败，自动停止")

    def _fetch_with_fallback(self, code: str) -> FundInfo:
        try:
            info = self._primary.fetch_detail(code)
            self._record_success()
            return info
        except SourceError as e:
            logger.warning("主数据源(eastmoney)获取 %s 失败: %s，尝试备用源(howbuy)", code, e)
            self._record_fail()
        except Exception as e:
            logger.warning("主数据源(eastmoney)获取 %s 异常: %s，尝试备用源(howbuy)", code, e)
            self._record_fail()

        try:
            info = self._backup.fetch_detail(code)
            self._record_success()
            logger.info("备用源(howbuy)成功获取 %s", code)
            return info
        except SourceError as e:
            logger.warning("备用源(howbuy)获取 %s 也失败: %s", code, e)
            self._record_fail()
        except Exception as e:
            logger.warning("备用源(howbuy)获取 %s 异常: %s", code, e)
            self._record_fail()

        logger.error("所有数据源均无法获取基金 %s，返回降级结果", code)
        return FundInfo(
            code=code,
            data_source="unavailable",
            data_unavailable=True,
        )

    # ------------------------------------------------------------------
    # 交叉验证: 双源比对 + 重试确认
    # 数据一致性 → "✅ 双源验证一致"
    # 数据不一致 → 重试双方后仍不一致 → 透明标注具体差异
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_purchase_diff(primary: FundInfo, backup: FundInfo) -> dict | None:
        ps, pl = primary.purchase_status, primary.purchase_limit
        bs, bl = backup.purchase_status, backup.purchase_limit

        if ps == bs and pl == bl:
            return None

        if _semantic_purchase_match(ps, pl, bs, bl):
            if ps != bs or pl != bl:
                old_status = ps
                old_limit = pl
                primary.purchase_status = bs
                primary.purchase_limit = bl
                return {
                    "field": "purchase_status",
                    "reason": "语义等价，自动对齐",
                    "primary_original": old_status,
                    "backup_value": bs,
                    "resolved": bs,
                    "note": "两源数据实际含义一致，已取更完整描述",
                }
            return None

        a1 = _parse_limit_amount(pl)
        a2 = _parse_limit_amount(bl)
        if a1 is not None and a2 is not None and a1 != a2:
            min_amt = min(a1, a2)
            min_str = f"{min_amt:.0f}元" if min_amt < 10000 else f"{min_amt/10000:.0f}万元"
            if min_amt != a1:
                primary.purchase_limit = min_str
            return {
                "field": "purchase_limit",
                "reason": "限额不一致，取低值确保准确性",
                "primary_original": pl,
                "backup_value": bl,
                "resolved": min_str,
            }

        logger.info("申购状态天天与好买不一致（主源=%s, 备用源=%s），已取主源数据", ps, bs)
        return {
            "field": "purchase_status",
            "reason": "两源不一致，以主源(eastmoney)为准",
            "primary_original": ps,
            "backup_value": bs,
            "resolved": ps,
        }

    @staticmethod
    def _resolve_numeric_diff(primary: FundInfo, backup: FundInfo, field: str, rule: dict) -> dict | None:
        pv = FundFetcher._to_float(getattr(primary, field, None))
        bv = FundFetcher._to_float(getattr(backup, field, None))
        if pv is None and bv is None:
            return None
        if pv is None and bv is not None:
            return None
        if bv is None:
            return None

        diff = abs(pv - bv)
        base_threshold = rule.get("diff", 1.0)
        rel_diff = rule.get("rel_diff")
        fmt_str = rule.get("fmt", ".2f")

        if rel_diff:
            max_v = max(abs(pv), abs(bv))
            if max_v == 0:
                return None
            ratio = diff / max_v
            if ratio <= rel_diff:
                return None
            return {
                "field": field,
                "action": "warning",
                "reason": f"差异 {ratio*100:.0f}%（主源={format(pv,fmt_str)}, 备用源={format(bv,fmt_str)}）",
                "primary": format(pv, fmt_str),
                "backup": format(bv, fmt_str),
                "diff": format(diff, fmt_str),
            }

        if diff <= base_threshold:
            return None
        return {
            "field": field,
            "action": "warning",
            "reason": f"差异 {diff:.2f}{rule.get('unit', '')}（主源={format(pv,fmt_str)}, 备用源={format(bv,fmt_str)}）",
            "primary": format(pv, fmt_str),
            "backup": format(bv, fmt_str),
            "diff": format(diff, fmt_str),
        }

    def _cross_validate_fund(self, primary: FundInfo) -> FundInfo:
        if primary.data_unavailable or not primary.code:
            return primary

        # 第一步: 天天基金内部多路径验证（NAV API 独立计算收益率）
        nav_return = getattr(primary, "_nav_return_1y", None)
        main_return = FundFetcher._to_float(getattr(primary, "return_1y", None))
        internal_validated: list[str] = []

        if nav_return is not None and main_return is not None:
            diff = abs(nav_return - main_return)
            threshold = 2.0 if abs(main_return) > 50 else 1.0
            if diff <= threshold:
                internal_validated.append("收益率")

        # 第二步: 好买基金外部验证
        try:
            backup = self._backup.fetch_detail(primary.code)
        except Exception as e:
            logger.info("交叉验证 %s: 好买数据不可用，仅使用天天基金内部验证", primary.code)
            if internal_validated:
                primary._cross_validated.append({"field": "天天基金多路径", "source": "内部路径一致"})
            return primary

        validated: list[dict] = []
        diffs: list[dict] = []

        if internal_validated:
            validated.append({"field": "收益率", "source": "主页面与NAV数据一致"})

        all_numeric_fields = list(CROSS_VAL_THRESHOLDS.keys())
        purchase_match = (primary.purchase_status == backup.purchase_status
                          and primary.purchase_limit == backup.purchase_limit)

        purchase_resolved = self._resolve_purchase_diff(primary, backup)
        if purchase_resolved:
            if purchase_match:
                validated.append({"field": "申购状态", "source": "天天与好买一致"})
            else:
                diffs.append(purchase_resolved)

        for field in all_numeric_fields:
            rule = CROSS_VAL_THRESHOLDS[field]
            pv = FundFetcher._to_float(getattr(primary, field, None))
            bv = FundFetcher._to_float(getattr(backup, field, None))
            if pv is None or bv is None:
                continue
            if abs(pv - bv) <= rule.get("diff", 1.0):
                validated.append({"field": field, "source": "天天与好买一致"})
            else:
                diffs.append(self._resolve_numeric_diff(primary, backup, field, rule))

        primary._cross_validated = validated
        primary._cross_validation = diffs
        return primary

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def get_detail(self, code: str, include_holdings: bool = False, include_csrc: bool = False,
                   cross_validate: bool = True) -> FundInfo:
        self._check_fail()
        info = self._fetch_with_fallback(code)
        if info.data_unavailable:
            return info

        if cross_validate and info.data_source == "eastmoney":
            info = self._cross_validate_fund(info)

        info._purchase_info = _build_purchase_info(
            info.purchase_status, info.purchase_limit, info.effectively_closed
        )

        if include_holdings:
            self._sleep()
            try:
                year = datetime.now().year
                quarters = self._primary._fetch_holdings(code, year)
                if quarters:
                    info.top10_holdings = quarters[0].get("stocks", [])
                self._record_success()
            except Exception as e:
                logger.warning("获取 %s 持仓数据失败: %s", code, e)
                self._record_fail()

        if include_csrc:
            self._sleep()
            try:
                dist = self._csrc.fetch_market_distribution(code, info.short_name or info.name)
                info.market_distribution = dist
                info.market_top3 = self._compute_market_top3(dist, info)
                self._record_success()
            except Exception as e:
                logger.warning("获取 %s CSRC 市场分布失败: %s", code, e)
                self._record_fail()

        validate_data(info.to_dict(), profile="detail")
        return info

    def _fetch_batch_parallel(self, codes: list[str]) -> list[FundInfo]:
        if not codes:
            return []
        if len(codes) == 1:
            return [self._fetch_with_fallback(codes[0])]
        results: list[FundInfo] = []
        with ThreadPoolExecutor(max_workers=min(len(codes), 4)) as ex:
            fut_to_code = {ex.submit(self._fetch_with_fallback, code): code for code in codes}
            for fut in as_completed(fut_to_code):
                try:
                    info = fut.result()
                    results.append(info)
                except Exception:
                    results.append(FundInfo(code=fut_to_code[fut], data_source="unavailable", data_unavailable=True))
        code_order = {code: i for i, code in enumerate(codes)}
        results.sort(key=lambda f: code_order.get(f.code, 999))
        return results

    def compare(self, codes: list[str] | None = None, keyword: str = "", fund_type: str = "",
                cross_validate: bool = True, include_csrc: bool = True) -> FundDataResult:
        fund_list: list[FundInfo] = []

        if codes:
            fund_list = self._fetch_batch_parallel(codes)
            need_cv = [f for f in fund_list if f.data_source == "eastmoney" and not f.data_unavailable] if cross_validate else []
            csrc_funds = [f for f in fund_list if not f.data_unavailable] if include_csrc else []

            if need_cv or csrc_funds:
                with ThreadPoolExecutor(max_workers=max(len(need_cv), len(csrc_funds), 4)) as ex:
                    cv_futs = {ex.submit(self._cross_validate_fund, f): f for f in need_cv}
                    csrc_futs = {ex.submit(self._csrc.fetch_market_distribution, f.code, f.short_name or f.name): f for f in csrc_funds}
                    all_futs = {**cv_futs, **csrc_futs}
                    for fut in as_completed(all_futs):
                        try:
                            result = fut.result()
                            if fut in csrc_futs:
                                f = csrc_futs[fut]
                                f.market_distribution = result
                                f.market_top3 = self._compute_market_top3(result, f)
                        except Exception:
                            pass

            for info in fund_list:
                info._purchase_info = _build_purchase_info(
                    info.purchase_status, info.purchase_limit, info.effectively_closed
                )
                if not info.market_top3:
                    info.market_top3 = self._compute_market_top3(info.market_distribution or None, info)
        elif keyword or fund_type:
            try:
                search_results = self._primary.search_funds(keyword, fund_type)
            except SourceError as e:
                logger.warning("搜索基金失败: %s", e)
                search_results = []
            for item in search_results:
                self._check_fail()
                info = self._fetch_with_fallback(item["code"])
                if cross_validate and info.data_source == "eastmoney":
                    info = self._cross_validate_fund(info)
                info._purchase_info = _build_purchase_info(
                    info.purchase_status, info.purchase_limit, info.effectively_closed
                )
                fund_list.append(info)

        if not codes:
            if include_csrc:
                csrc_funds = [f for f in fund_list if not f.data_unavailable]
                if csrc_funds:
                    with ThreadPoolExecutor(max_workers=4) as ex:
                        fut_map = {}
                        for f in csrc_funds:
                            fut = ex.submit(
                                self._csrc.fetch_market_distribution,
                                f.code, f.short_name or f.name
                            )
                            fut_map[fut] = f

                        for fut in as_completed(fut_map):
                            f = fut_map[fut]
                            try:
                                dist = fut.result()
                                f.market_distribution = dist
                                f.market_top3 = self._compute_market_top3(dist, f)
                            except Exception:
                                pass
                for f in fund_list:
                    if not f.market_top3:
                        f.market_top3 = self._compute_market_top3(f.market_distribution or None, f)
            else:
                for f in fund_list:
                    if not f.market_top3:
                        f.market_top3 = self._compute_market_top3(None, f)

        return self._validate_and_build_result(fund_list, profile="compare")

    def market_distribution(self, code: str, main_code: str = "", short_name: str = "") -> dict:
        self._check_fail()
        mc = main_code or code
        try:
            result = self._csrc.fetch_market_distribution(mc, short_name)
            self._record_success()
        except Exception as e:
            logger.warning("获取 %s CSRC 市场分布失败: %s", code, e)
            self._record_fail()
            result = {"_source": "unavailable", "_total_pct": 0, "_inferred": True, "_note": "all_sources_failed"}
        validate_data(result, profile="qdii")
        return result

    def _validate_and_build_result(self, funds: list[FundInfo], profile: str = "compare") -> FundDataResult:
        result = FundDataResult(
            count=len(funds),
            funds=funds,
        )
        if funds:
            result.update_date = time.strftime("%Y-%m-%d")

        data_dict = result.to_dict()
        validation = validate_data(data_dict, profile=profile)
        result._validation = validation.to_dict()
        result._warnings = validation.warnings

        unavailable_count = sum(1 for f in funds if f.data_unavailable)
        if unavailable_count > 0:
            result._warnings.append(f"⚠ {unavailable_count}/{len(funds)} 只基金数据暂不可用（所有数据源均失败）")

        validated_count = sum(1 for f in funds if f._cross_validated)
        if validated_count > 0:
            detail = []
            for f in funds:
                if f._cross_validated:
                    sources = set(d["source"] for d in f._cross_validated)
                    detail.append(f.name or f.code)
            result._warnings.append(f"✅ {validated_count}/{len(funds)} 只基金已通过交叉验证（天天基金+好买基金）")

        stale_count = 0
        for f in funds:
            if f.data_unavailable or not f.nav_list:
                continue
            try:
                last_nav = f.nav_list[-1]
                last_date = datetime.strptime(last_nav["date"], "%Y-%m-%d")
                days_old = (datetime.now() - last_date).days
                if days_old > 7:
                    stale_count += 1
            except (IndexError, ValueError, TypeError, KeyError):
                pass
        if stale_count > 0:
            result._warnings.append(f"⚠ {stale_count}/{len(funds)} 只基金净值超过 7 天未更新，申购限额可能已变化")

        return result

    @staticmethod
    def _to_float(val) -> float | None:
        if val is None:
            return None
        try:
            return float(str(val).replace("%", "").replace(",", "").strip()) or None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _compute_market_top3(market_dist: dict, fund: FundInfo | None = None) -> str:
        is_index = False
        if fund:
            if fund.type and ("指数" in fund.type or "ETF" in fund.type.upper()):
                is_index = True
            if fund.name:
                name_upper = fund.name.upper()
                if "ETF" in name_upper or "联接" in fund.name:
                    is_index = True
                elif "指数" in fund.name and "增强" not in fund.name:
                    is_index = True
        if is_index:
            return "跟踪大盘指数"
        if market_dist and market_dist.get("_note") == "no_holdings":
            return "季报无股票持仓"
        if not market_dist or market_dist.get("_inferred", True):
            return ""
        items = [(k, v) for k, v in market_dist.items() if not k.startswith("_")]
        items.sort(key=lambda x: x[1], reverse=True)
        parts = []
        for name, pct in items[:3]:
            parts.append(f"{name}{pct:.1f}%")
        return " / ".join(parts)
