# -*- coding: utf-8 -*-
"""模型 ④ Hybrid + 滚动校准（自校准模型）

设计原则：少参数、高鲁棒性。回测样本量短（QDII 季报间隔 90 天，
且单只基金每天只有一个观测），自由度极有限，必须避免过拟合。

提供 6 个变体：
    - bias       : actual = α + hybrid_pred           (1 参数)
    - scale      : actual = α + β · hybrid_pred       (2 参数)
    - split      : actual = α + β1·top10 + β2·residual (3 参数)
    - full       : 5 因子岭回归 (5+1 参数)
    - blend      : actual = blend × mean_bias + hybrid (保守版 bias，避免过度修正)
    - it_residual: 当近期 bias 显著为负时, 加权 IT 行业残余（应对持仓老化导致系统性低估）
    - auto       : 滚动选择最近 lookback 天表现最好的子模型（per-day 自适应）

默认使用 calibrated_scale，在回测样本上稳定且有适度灵活性。
auto 模式在跨基金类型（指数/主动/FoF）的混合场景下平均 MAE 最低。
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from core.predict.models.base import BaseModel, FundExposure, PredictionResult
from core.predict.models.hybrid import HybridModel

logger = logging.getLogger(__name__)


class CalibratedModel(BaseModel):
    name = "calibrated"

    def __init__(
        self,
        window: int = 20,
        min_window: int = 10,
        ridge_lambda: float = 1e-3,
        clip_beta: tuple[float, float] = (0.5, 1.5),
        variant: str = "scale",  # bias / scale / split / full / blend / it_residual
        ema_decay: float = 0.0,  # 0 = 等权；>0 启用指数加权（最近样本权重更高）
        blend: float = 0.3,      # variant=blend 时的混合系数 (alpha 应用强度)
        residual_mult: float = 1.5,  # variant=it_residual 时 IT 残余加成倍数
        bias_threshold: float = -0.2,  # variant=it_residual 时触发的负偏差阈值（pp）
    ):
        self.window = window
        self.min_window = min_window
        self.ridge_lambda = ridge_lambda
        self.clip_beta = clip_beta
        self.variant = variant
        self.ema_decay = ema_decay
        self.blend = blend
        self.residual_mult = residual_mult
        self.bias_threshold = bias_threshold
        self.hybrid = HybridModel()

    def _features(self, pred: PredictionResult) -> dict[str, float]:
        f = {"top10": 0.0, "us": 0.0, "cn": 0.0, "hk": 0.0, "fx": 0.0}
        for k, v in pred.components.items():
            if k.startswith("top10:"):
                f["top10"] += v
            elif k.startswith("residual_us:"):
                f["us"] += v
            elif k.startswith("residual_cn:"):
                f["cn"] += v
            elif k.startswith("residual_hk:"):
                f["hk"] += v
            elif k == "__fx_USDCNY":
                f["fx"] += v
        f["all"] = f["top10"] + f["us"] + f["cn"] + f["hk"] + f["fx"]
        f["residual"] = f["us"] + f["cn"] + f["hk"]
        return f

    def _build_X(self, history: list[tuple[str, PredictionResult, float]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """根据 variant 构造设计矩阵。返回 (X, y, w)。"""
        feats = []
        ys = []
        for d, p, a in history:
            f = self._features(p)
            if self.variant == "bias":
                feats.append([1.0, f["all"]])  # alpha + hybrid 不带 beta，回归只解 alpha
            elif self.variant == "scale":
                feats.append([1.0, f["all"]])  # alpha + beta · hybrid
            elif self.variant == "split":
                feats.append([1.0, f["top10"], f["residual"]])
            else:  # full
                feats.append([1.0, f["top10"], f["us"], f["cn"], f["hk"], f["fx"]])
            ys.append(a)
        X = np.array(feats, dtype=float)
        y = np.array(ys, dtype=float)

        n = len(y)
        if self.ema_decay > 0:
            # 越靠近现在权重越大: w_i = exp(-decay × (n-1-i))
            w = np.exp(-self.ema_decay * (n - 1 - np.arange(n)))
            w = w / w.sum() * n  # 归一化使总权重 = n（保持 OLS 解的尺度）
        else:
            w = np.ones(n)
        return X, y, w

    def _solve(self, X: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
        """加权岭回归： β = (X' W X + λI)^{-1} X' W y"""
        W = np.diag(w)
        XtWX = X.T @ W @ X
        reg = self.ridge_lambda * np.eye(XtWX.shape[0])
        reg[0, 0] = 0  # 不正则化截距
        try:
            beta = np.linalg.solve(XtWX + reg, X.T @ W @ y)
        except np.linalg.LinAlgError:
            return np.zeros(X.shape[1])
        return beta

    def predict_with_history(
        self,
        exposure: FundExposure,
        prices: dict[str, pd.DataFrame],
        target_date: str,
        prev_nav_date: str,
        history: list[tuple[str, PredictionResult, float]],
        actual_pct: Optional[float] = None,
    ) -> PredictionResult:
        # 当日 hybrid
        hybrid_pred = self.hybrid.predict(
            exposure, prices, target_date, prev_nav_date, actual_pct
        )

        if len(history) < self.min_window:
            hybrid_pred.model_name = f"{self.name}({self.variant},fallback=hybrid)"
            hybrid_pred.notes += f"； 校准历史不足({len(history)}<{self.min_window})，退化"
            return hybrid_pred

        recent = history[-self.window:]
        X, y, w = self._build_X(recent)

        if self.variant == "bias":
            # 只解截距：α = mean(y - hybrid)，等价于零回归（ β 固定为 1）
            alpha = float(np.average(y - X[:, 1], weights=w))
            f_today = self._features(hybrid_pred)
            predicted = alpha + f_today["all"]
            beta = np.array([alpha, 1.0])
            comp = {
                "α(bias)": alpha,
                "1.0·hybrid": f_today["all"],
            }
            sigma = float(np.std(y - X[:, 1] - alpha)) if len(y) > 1 else 0.0
        elif self.variant == "blend":
            # 部分应用滚动 bias: α_blend = blend × mean_bias
            # 比 bias 更保守，避免在 bias 不稳定时过度修正
            mean_bias = float(np.average(y - X[:, 1], weights=w))
            alpha = self.blend * mean_bias
            f_today = self._features(hybrid_pred)
            predicted = alpha + f_today["all"]
            comp = {
                f"α(blend×bias, blend={self.blend})": alpha,
                "1.0·hybrid": f_today["all"],
            }
            sigma = float(np.std(y - X[:, 1] - alpha)) if len(y) > 1 else 0.0
        elif self.variant == "it_residual":
            # 当历史 bias 显著为负时，把 IT 残余部分加权
            mean_bias = float(np.average(y - X[:, 1], weights=w))
            f_today = self._features(hybrid_pred)
            # 计算今日 IT 残余总贡献
            it_residual_pp = 0.0
            for k, v in hybrid_pred.components.items():
                if k.startswith("residual_us:") or k.startswith("residual_cn:"):
                    if any(s in k for s in ["信息技术", "信息科技", ":科技", "技术"]):
                        it_residual_pp += v
            if mean_bias < self.bias_threshold / 100:  # bias 是 pp，转 decimal
                adjustment = it_residual_pp * (self.residual_mult - 1)
            else:
                adjustment = 0.0
            predicted = f_today["all"] + adjustment
            comp = {
                f"recent_bias({len(recent)}d)": mean_bias,
                f"hybrid_total": f_today["all"],
                f"it_residual_boost(×{self.residual_mult-1:+.1f})": adjustment,
            }
            sigma = 0.0
        else:
            beta = self._solve(X, y, w)
            # 截断 β（除截距）
            for i in range(1, len(beta)):
                beta[i] = float(np.clip(beta[i], self.clip_beta[0], self.clip_beta[1]))

            f_today = self._features(hybrid_pred)
            if self.variant == "scale":
                alpha, b1 = float(beta[0]), float(beta[1])
                predicted = alpha + b1 * f_today["all"]
                comp = {
                    f"α(intercept)": alpha,
                    f"β(scale)·hybrid[{b1:.3f}]": b1 * f_today["all"],
                }
            elif self.variant == "split":
                alpha, b1, b2 = float(beta[0]), float(beta[1]), float(beta[2])
                predicted = alpha + b1 * f_today["top10"] + b2 * f_today["residual"]
                comp = {
                    "α(intercept)": alpha,
                    f"β1·top10[{b1:.3f}]": b1 * f_today["top10"],
                    f"β2·residual[{b2:.3f}]": b2 * f_today["residual"],
                }
            else:  # full
                a, bs = float(beta[0]), [float(x) for x in beta[1:]]
                fs = [f_today["top10"], f_today["us"], f_today["cn"], f_today["hk"], f_today["fx"]]
                predicted = a + sum(b * f for b, f in zip(bs, fs))
                comp = {
                    "α(intercept)": a,
                    f"β1·top10[{bs[0]:.3f}]": bs[0] * fs[0],
                    f"β2·us[{bs[1]:.3f}]": bs[1] * fs[1],
                    f"β3·cn[{bs[2]:.3f}]": bs[2] * fs[2],
                    f"β4·hk[{bs[3]:.3f}]": bs[3] * fs[3],
                    f"β5·fx[{bs[4]:.3f}]": bs[4] * fs[4],
                }
            # 残差标准差
            y_hat = X @ beta
            sigma = float(np.std(y - y_hat)) if len(y) > 1 else 0.0

        return PredictionResult(
            fund_code=exposure.fund_code,
            target_date=target_date,
            model_name=f"{self.name}({self.variant})",
            predicted_pct=predicted,
            actual_pct=actual_pct,
            components=comp,
            coverage_pct=hybrid_pred.coverage_pct,
            inputs_missing=hybrid_pred.inputs_missing,
            notes=(
                f"window={len(recent)}天 σ={sigma*100:.3f}pp "
                f"95%区间≈±{1.96*sigma*100:.2f}pp"
            ),
        )

    def predict(
        self,
        exposure: FundExposure,
        prices: dict[str, pd.DataFrame],
        target_date: str,
        prev_nav_date: str,
        actual_pct: Optional[float] = None,
    ) -> PredictionResult:
        # 无历史的单点预测，退化到 Hybrid
        return self.hybrid.predict(
            exposure, prices, target_date, prev_nav_date, actual_pct
        )
