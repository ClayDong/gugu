"""技术指标工具函数：统一指标计算，消除重复。

集中提供 ATR、MA、波动率等常用技术指标的计算。
各模块通过此模块获取指标，而非自行实现。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算 Average True Range (ATR)。

    ATR 衡量市场波动率，常用于止损/止盈设置。

    Args:
        df: 含 'high'/'low'/'close' 列的 DataFrame。
        period: ATR 计算周期，默认 14。

    Returns:
        ATR 值的 pd.Series，长度与 df 相同。
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.rolling(window=period, min_periods=1).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """简单移动平均。"""
    return series.rolling(window=period, min_periods=1).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均。"""
    return series.ewm(span=period, adjust=False).mean()