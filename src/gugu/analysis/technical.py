"""技术指标工具函数：统一指标计算，消除重复。

集中提供 ATR、MA、RSI、MACD、布林带等常用技术指标的计算。
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


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """计算相对强弱指标 RSI。

    使用 Wilder 平滑方法（与普通 SMA 不同）：
    1. 首次均值 = 前 period 个值的 SMA
    2. 后续值 = (前值 * (period-1) + 当前值) / period

    Args:
        series: 价格序列（通常是收盘价）。
        period: RSI 计算周期，默认 14。

    Returns:
        RSI 值的 pd.Series，范围 0-100。
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    # 前 period 个点用 NaN（数据不足），从第 period+1 个点开始计算
    avg_gain = pd.Series(np.nan, index=series.index)
    avg_loss = pd.Series(np.nan, index=series.index)

    if len(series) < period + 1:
        return pd.Series(50.0, index=series.index)

    # 首次均值：前 period 个 gain/loss 的 SMA
    avg_gain.iloc[period] = gain.iloc[1:period + 1].mean()
    avg_loss.iloc[period] = loss.iloc[1:period + 1].mean()

    # Wilder 平滑
    for i in range(period + 1, len(series)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    # 当 avg_loss 为 0 时，RSI = 100（持续上涨无下跌）
    # 当 avg_gain 为 0 时，RSI = 0（持续下跌无上涨）
    rsi_series = pd.Series(50.0, index=series.index)
    for i in range(period, len(series)):
        ag = avg_gain.iloc[i]
        al = avg_loss.iloc[i]
        if pd.isna(ag) or pd.isna(al):
            continue
        if al == 0 and ag > 0:
            rsi_series.iloc[i] = 100.0
        elif ag == 0 and al > 0:
            rsi_series.iloc[i] = 0.0
        elif al > 0:
            rs = ag / al
            rsi_series.iloc[i] = 100 - (100 / (1 + rs))
    return rsi_series


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """计算 MACD 指标。

    Args:
        series: 价格序列（通常是收盘价）。
        fast: 快线周期，默认 12。
        slow: 慢线周期，默认 26。
        signal: 信号线周期，默认 9。

    Returns:
        (DIF, DEA, MACD 柱) 三元组。
        DIF = EMA(fast) - EMA(slow)
        DEA = EMA(DIF, signal)
        MACD 柱 = (DIF - DEA) * 2
    """
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    dif = ema_fast - ema_slow
    dea = ema(dif, signal)
    macd_hist = (dif - dea) * 2
    return dif, dea, macd_hist


def bollinger(
    series: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """计算布林带。

    Args:
        series: 价格序列。
        period: 周期，默认 20。
        num_std: 标准差倍数，默认 2。

    Returns:
        (中轨, 上轨, 下轨) 三元组。
    """
    middle = sma(series, period)
    std = series.rolling(window=period, min_periods=1).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return middle, upper, lower


def obv(df: pd.DataFrame) -> pd.Series:
    """计算能量潮 OBV（On-Balance Volume）。

    Args:
        df: 含 'close' 和 'volume' 列的 DataFrame。

    Returns:
        OBV 值的 pd.Series。
    """
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    direction = np.sign(close.diff())
    direction.iloc[0] = 0
    return (direction * volume).cumsum()
