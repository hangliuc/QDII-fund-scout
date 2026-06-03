# -*- coding: utf-8 -*-
"""QDII 基金 T-1 估值预测模块

核心思路：
基金 T 日真实净值在 T+1 9:00~10:00 公布，但 T 日海外市场收盘价
（亚洲收盘 → 欧洲收盘 → 美国收盘）在 T 日早晨 5:00 之前就已确定，
A 股 T 日收盘价在 T 日 15:00 确定。因此可在 T 日 16:00 后，
利用：
  - CSRC 季报披露的精确 Top10 持仓
  - CSRC 季报披露的市场分布 / 行业分布
  - yfinance 实时行情
  - 美元兑人民币当日汇率变动
预测 T 日 NAV 涨跌幅，公布的 NAV 即为 T 日真实结果（与预测的差距
即模型误差）。

模型层级：
  - top10_only      : 仅按 Top10 加权（基线，会显著低估覆盖率）
  - region_proxy    : 全部用地区 ETF 代理（粗粒度）
  - hybrid          : Top10 精确 + 残余按行业 ETF 代理（推荐）
  - calibrated      : hybrid + 滚动 OLS 回归校准（最优）
"""
from core.predict.predictor import Predictor
from core.predict.backtest import Backtester, BacktestResult

__all__ = ["Predictor", "Backtester", "BacktestResult"]
