# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


DISCLAIMER = (
    "数据来源：天天基金、好买基金、证监会等公开渠道 | "
    "仅供个人学习参考，不构成任何投资建议 | "
    "历史业绩不代表未来表现，申购限额随时变动 | "
    "数据版权归原始平台所有，禁止商业转载和使用"
)


@dataclass
class FundInfo:
    code: str = ""
    name: str = ""
    short_name: str = ""
    type: str = ""
    benchmark: str = ""
    nav: float | None = None
    nav_date: str = ""
    scale: float | None = None
    mgmt_fee: float | None = None
    custody_fee: float | None = None
    service_fee: float | None = None
    total_fee: float | None = None
    return_1w: float | None = None
    return_1m: float | None = None
    return_3m: float | None = None
    return_6m: float | None = None
    return_1y: float | None = None
    return_3y: float | None = None
    return_ytd: float | None = None
    return_since_inception: float | None = None
    purchase_status: str = ""
    purchase_limit: str = ""
    effectively_closed: bool = False
    drawdown_1y: float | None = None
    risk_level: str = ""
    manager_name: str = ""
    manager_avatar: str = ""
    manager_tenure: float | None = None
    manager_return: float | None = None
    top10_holdings: list[dict] = field(default_factory=list)
    market_distribution: dict = field(default_factory=dict)
    company: str = ""
    found_date: str = ""
    tracking_error: float | None = None
    data_source: str = ""
    data_unavailable: bool = False
    _cross_validation: list[dict] = field(default_factory=list)
    _cross_resolved: list[dict] = field(default_factory=list)
    _cross_validated: list[dict] = field(default_factory=list)
    _nav_return_1y: float | None = None
    market_top3: str = ""
    _purchase_info: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["purchase_info"] = self._purchase_info
        return d


@dataclass
class FundDataResult:
    update_date: str = ""
    count: int = 0
    funds: list[FundInfo] = field(default_factory=list)
    _warnings: list[str] = field(default_factory=list)
    _validation: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "update_date": self.update_date,
            "count": self.count,
            "funds": [f.to_dict() for f in self.funds],
            "_warnings": self._warnings,
            "_validation": self._validation,
        }
