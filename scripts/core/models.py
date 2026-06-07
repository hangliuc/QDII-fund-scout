# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


DISCLAIMER = (
    "数据来源：天天基金、证监会等公开渠道 | "
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
    risk: str = ""
    benchmark: str = ""
    nav: float | None = None
    nav_date: str = ""
    nav_list: list[dict] = field(default_factory=list)
    update_date: str = ""
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
    return_sl: float | None = None  # 成立以来收益率（eastmoney 主页字段）
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
    market_top3: str = ""
    # 内部字段（以下划线开头），to_dict 默认过滤
    _cross_validation: list[dict] = field(default_factory=list)
    _cross_resolved: list[dict] = field(default_factory=list)
    _cross_validated: list[dict] = field(default_factory=list)
    _nav_return_1y: float | None = None
    _purchase_info: str = ""
    _t1_prediction: dict = field(default_factory=dict)  # T-1 估值预测结果（详见 predict_inline.predict_t1_for_fund）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # 过滤所有以下划线开头的内部字段，再以可读名称重新暴露
        out = {k: v for k, v in d.items() if not k.startswith("_")}
        out["purchase_info"] = self._purchase_info
        out["t1_prediction"] = self._t1_prediction
        return out


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
