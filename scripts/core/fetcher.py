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
            logger.warning("天天基金获取 %s 失败: %s", code, e)
            self._record_fail()
        except Exception as e:
            logger.warning("天天基金获取 %s 异常: %s", code, e)
            self._record_fail()

        logger.error("无法获取基金 %s，返回降级结果", code)
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

    def _cross_validate_fund(self, primary: FundInfo) -> FundInfo:
        """内部校验：NAV API 独立计算收益率与主页面比对"""
        if primary.data_unavailable or not primary.code:
            return primary

        nav_return = getattr(primary, "_nav_return_1y", None)
        main_return = FundFetcher._to_float(getattr(primary, "return_1y", None))

        if nav_return is not None and main_return is not None:
            diff = abs(nav_return - main_return)
            threshold = 2.0 if abs(main_return) > 50 else 1.0
            if diff <= threshold:
                primary._cross_validated.append({"field": "收益率", "source": "主页面与NAV数据一致"})

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
                cross_validate: bool = True, include_csrc: bool = True,
                include_prediction: bool = False) -> FundDataResult:
        """获取多只基金对比数据。

        include_prediction:
            True  - 同时为每只 QDII 基金计算 T-1 估值预测，结果写入 fund._t1_prediction。
                    适用于推送 / UI 等需要"今日估算"信息的场景。
            False - 跳过预测（默认），适用于 CLI 控制台简洁输出。
        """
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

        # T-1 估值预测（默认关闭，仅推送/UI 启用）
        if include_prediction:
            self._enrich_with_t1_prediction(fund_list)

        return self._validate_and_build_result(fund_list, profile="compare")

    def _enrich_with_t1_prediction(self, fund_list: list[FundInfo]) -> None:
        """并行为每只 QDII 基金计算最新涨跌（真值或估算）。"""
        try:
            from core.predict_inline import predict_t1_for_fund
        except ImportError as e:
            logger.warning("预测模块不可用: %s", e)
            return

        targets = []
        for f in fund_list:
            if f.data_unavailable:
                continue
            type_str = (f.type or "") + (f.name or "")
            if "QDII" not in type_str and "美元" not in type_str and "全球" not in type_str:
                continue
            targets.append(f)

        if not targets:
            return

        with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as ex:
            fut_map = {}
            for f in targets:
                fut = ex.submit(
                    predict_t1_for_fund,
                    f.code,
                    f.code,
                    f.short_name or f.name,
                )
                fut_map[fut] = f
            for fut in as_completed(fut_map):
                f = fut_map[fut]
                try:
                    f._t1_prediction = fut.result(timeout=30)
                except Exception as e:
                    logger.warning("基金 %s 预测失败: %s", f.code, e)
                    f._t1_prediction = {}

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
            result._warnings.append(f"✅ {validated_count}/{len(funds)} 只基金已通过内部校验")

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
