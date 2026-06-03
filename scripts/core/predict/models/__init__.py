# -*- coding: utf-8 -*-
"""预测模型集合"""
from core.predict.models.base import BaseModel, FundExposure, PredictionResult
from core.predict.models.top10_only import Top10OnlyModel
from core.predict.models.region_proxy import RegionProxyModel
from core.predict.models.hybrid import HybridModel
from core.predict.models.calibrated import CalibratedModel

__all__ = [
    "BaseModel",
    "FundExposure",
    "PredictionResult",
    "Top10OnlyModel",
    "RegionProxyModel",
    "HybridModel",
    "CalibratedModel",
]
