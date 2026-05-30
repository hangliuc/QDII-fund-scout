# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def _safe_print(s: str) -> str:
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return "".join(c if not (0xD800 <= ord(c) <= 0xDFFF) else "\ufffd" for c in s)


class ValidationError(Exception):
    pass


@dataclass
class ValidationResult:
    schema_missing: list[str] = field(default_factory=list)
    range_violations: list[dict] = field(default_factory=list)
    consistency_violations: list[dict] = field(default_factory=list)
    cross_source_violations: list[dict] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def fatal_count(self) -> int:
        return len(self.fatal)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    def to_dict(self) -> dict:
        return {
            "schema": "ok" if not self.schema_missing else "incomplete",
            "schema_missing": self.schema_missing,
            "range_violations": self.range_violations,
            "consistency_violations": self.consistency_violations,
            "cross_source_violations": self.cross_source_violations,
            "fatal_count": self.fatal_count,
            "warning_count": self.warning_count,
        }


PROFILE_REQUIRED = {
    "quick": [
        "code",
        "name",
        "nav",
        "return_1y",
        "purchase_status",
    ],
    "compare": [
        "code",
        "name",
        "scale",
        "return_1y",
        "purchase_status",
        "total_fee",
    ],
    "detail": [
        "code",
        "name",
        "scale",
        "nav",
        "nav_date",
        "purchase_status",
        "mgmt_fee",
        "custody_fee",
        "total_fee",
        "manager_name",
        "top10_holdings",
    ],
    "qdii": [
        "code",
        "name",
        "scale",
        "return_1y",
        "purchase_status",
        "market_distribution",
    ],
}


RANGE_RULES = {
    "scale":               {"min": 0, "max": 10000, "fatal": True},
    "nav":                 {"min": 0.1, "max": 50, "fatal": False},
    "return_1w":           {"min": -100, "max": 500, "fatal": False},
    "return_1m":           {"min": -100, "max": 500, "fatal": False},
    "return_3m":           {"min": -100, "max": 500, "fatal": False},
    "return_6m":           {"min": -100, "max": 500, "fatal": False},
    "return_1y":           {"min": -100, "max": 500, "fatal": True},
    "return_3y":           {"min": -100, "max": 500, "fatal": True},
    "return_ytd":          {"min": -100, "max": 500, "fatal": False},
    "return_since_inception": {"min": -100, "max": 500, "fatal": False},
    "mgmt_fee":            {"min": 0, "max": 5, "fatal": True},
    "custody_fee":         {"min": 0, "max": 5, "fatal": True},
    "service_fee":         {"min": 0, "max": 5, "fatal": False},
    "total_fee":           {"min": 0, "max": 5, "fatal": True},
    "tracking_error":      {"min": 0, "max": 30, "fatal": False},
    "drawdown_1y":         {"min": 0, "max": 90, "fatal": False},
    "manager_tenure":      {"min": 0, "max": 30, "fatal": False},
    "manager_return":      {"min": -100, "max": 500, "fatal": False},
}

CODE_PATTERN = re.compile(r"^\d{6}$")
DATE_PATTERN = re.compile(r"^\d{2}-\d{2}$|^\d{4}-\d{2}-\d{2}$")
PURCHASE_STATUS_VALUES = {"开放", "限大额", "限小额", "暂停", "未知", "暂停(限大额)", "暂停(限小额)"}


def _get(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("，", "").replace("%", "").replace("亿", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def check_schema(entry: dict, profile: str, result: ValidationResult, code: str = "") -> None:
    required = PROFILE_REQUIRED.get(profile, [])
    for path in required:
        v = _get(entry, path)
        if v is None or v == "" or v == [] or v == {}:
            tag = f"{code}.{path}" if code else path
            result.schema_missing.append(tag)
            result.fatal.append(f"L1 schema 缺失字段: {tag}")


def check_range(entry: dict, result: ValidationResult, code: str = "") -> None:
    for field_name, rule in RANGE_RULES.items():
        v = entry.get(field_name)
        if v is None:
            continue
        f = _to_float(v)
        if f is None:
            continue
        if f < rule["min"] or f > rule["max"]:
            violation = {
                "code": code,
                "field": field_name,
                "value": f,
                "rule": f"{rule['min']} ~ {rule['max']}",
            }
            result.range_violations.append(violation)
            tag = f"{code} {field_name}={f} 越界({rule['min']}~{rule['max']})"
            if rule.get("fatal"):
                result.fatal.append(f"L2 range fatal: {tag}")
            else:
                result.warnings.append(f"L2 range warn: {tag}")

    if entry.get("code") and not CODE_PATTERN.match(str(entry["code"])):
        result.range_violations.append({"code": code, "field": "code", "value": entry["code"]})
        result.fatal.append(f"L2 code 格式错误: {entry['code']}")

    status = entry.get("purchase_status")
    if status and status not in PURCHASE_STATUS_VALUES:
        result.range_violations.append({
            "code": code, "field": "purchase_status", "value": status,
        })
        result.warnings.append(f"L2 purchase_status 非标准值: {status}")

    nav_date = entry.get("nav_date")
    if nav_date and not DATE_PATTERN.match(str(nav_date)):
        result.warnings.append(f"L2 nav_date 格式错误: {nav_date}")


def check_consistency(entry: dict, result: ValidationResult, code: str = "") -> None:
    name = entry.get("name", "")

    def cv(rule: str, msg: str, fatal: bool = False) -> None:
        v = {"code": code, "rule": rule, "msg": msg}
        result.consistency_violations.append(v)
        target = result.fatal if fatal else result.warnings
        target.append(f"L3 {rule}: {code} {msg}")

    mgmt = _to_float(entry.get("mgmt_fee"))
    custody = _to_float(entry.get("custody_fee"))
    service = _to_float(entry.get("service_fee"))
    total = _to_float(entry.get("total_fee"))

    if mgmt is not None and custody is not None and service is not None and total is not None:
        expected = round(mgmt + custody + service, 4)
        if abs(expected - total) > 0.02:
            cv("费率合计", f"total_fee={total} != mgmt+custody+service={expected}")

    if "C" in name and (service is None or service == 0):
        cv("费率/C类", "C类基金应有 service_fee")

    top10 = entry.get("top10_holdings") or []
    if top10:
        pcts = []
        for s in top10:
            f = _to_float(s.get("pct"))
            if f is not None:
                pcts.append(f)
        if pcts:
            tot = sum(pcts)
            if tot > 100.5:
                cv("持仓/合计", f"前十大占比合计 {tot:.2f}% > 100%")
            if 0 < tot < 5:
                cv("持仓/合计", f"前十大占比合计 {tot:.2f}% 异常低")
            if any(p > 50 for p in pcts):
                cv("持仓/单只过大", "单只持仓 > 50%")

        codes = [s.get("code") for s in top10 if s.get("code")]
        if len(codes) != len(set(codes)):
            cv("持仓/重复", f"top10 代码有重复 {codes}")

    dist = entry.get("market_distribution") or {}
    country_items = {k: v for k, v in dist.items() if not str(k).startswith("_")}
    if country_items:
        tot = sum(_to_float(v) or 0 for v in country_items.values())
        if tot > 105:
            cv("市场分布/合计", f"国家占比合计 {tot:.2f}% > 100%", fatal=True)
        if 0 < tot < 30:
            cv("市场分布/合计", f"国家占比合计 {tot:.2f}% 过低，可能漏抓")


def check_cross_source(entry: dict, result: ValidationResult, code: str = "") -> None:
    home_scale = _to_float(entry.get("_home_scale"))
    arch_scale = _to_float(entry.get("_arch_scale"))
    if home_scale and arch_scale:
        pct_diff = abs(home_scale - arch_scale) / max(home_scale, arch_scale)
        if pct_diff > 0.20:
            v = {
                "code": code,
                "field": "scale",
                "home": home_scale,
                "arch": arch_scale,
                "pct_diff": round(pct_diff, 3),
            }
            result.cross_source_violations.append(v)
            result.warnings.append(
                f"L4 scale 多源差异: {code} 主页={home_scale} 档案={arch_scale} 差={pct_diff:.1%}"
            )

    rank = _to_float(entry.get("_rank_return_1y"))
    home = _to_float(entry.get("_home_return_1y") or entry.get("return_1y"))
    if rank is not None and home is not None:
        diff = abs(rank - home)
        if diff > 1.0:
            v = {"code": code, "field": "return_1y", "rank": rank, "home": home, "diff": round(diff, 2)}
            result.cross_source_violations.append(v)
            result.warnings.append(
                f"L4 return_1y 多源差异: {code} 排行={rank} 主页={home} 差={diff:.2f}"
            )


def validate_fund_entry(entry: dict, profile: str = "compare") -> ValidationResult:
    result = ValidationResult()
    code = str(entry.get("code") or "")
    check_schema(entry, profile, result, code)
    check_range(entry, result, code)
    check_consistency(entry, result, code)
    check_cross_source(entry, result, code)
    return result


def validate_data(data: dict, profile: str = "compare") -> ValidationResult:
    result = ValidationResult()

    if not data.get("update_date"):
        result.warnings.append("L1 顶层缺 update_date")

    funds = data.get("funds")
    if isinstance(funds, list):
        for entry in funds:
            r = validate_fund_entry(entry, profile=profile)
            result.schema_missing.extend(r.schema_missing)
            result.range_violations.extend(r.range_violations)
            result.consistency_violations.extend(r.consistency_violations)
            result.cross_source_violations.extend(r.cross_source_violations)
            result.fatal.extend(r.fatal)
            result.warnings.extend(r.warnings)
    else:
        r = validate_fund_entry(data, profile=profile)
        result.schema_missing.extend(r.schema_missing)
        result.range_violations.extend(r.range_violations)
        result.consistency_violations.extend(r.consistency_violations)
        result.cross_source_violations.extend(r.cross_source_violations)
        result.fatal.extend(r.fatal)
        result.warnings.extend(r.warnings)

    return result


def print_report(result: ValidationResult) -> None:
    print("\n" + "=" * 60)
    print(f"数据自校验  fatal={result.fatal_count}  warning={result.warning_count}")
    print("=" * 60)
    if result.fatal:
        print("\n❌ Fatal:")
        for f in result.fatal:
            print(f"  - {_safe_print(f)}")
    if result.warnings:
        print(f"\n⚠ Warnings ({result.warning_count}):")
        for w in result.warnings[:30]:
            print(f"  - {_safe_print(w)}")
        if len(result.warnings) > 30:
            print(f"  ... 还有 {len(result.warnings) - 30} 条")
    if not result.fatal and not result.warnings:
        print("✅ 全部通过")


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("用法: python validate.py <data.json> [profile=compare]")
        print("  profile 可选: quick / compare / detail / qdii")
        sys.exit(1)

    path = sys.argv[1]
    profile = sys.argv[2] if len(sys.argv) > 2 else "compare"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = validate_data(data, profile=profile)
    print_report(result)
    sys.exit(0 if result.fatal_count == 0 else 1)
