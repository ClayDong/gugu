"""断板反包战法抢救出的认知资产——通用风险/认知规则组件。

背景
----
陈学长「断板反包」战法经 2026-07-31 事件研究（152 事件）+ 2026-08-01 Walk-Forward
证伪：形态整体负期望（5日超额 -1.5%），不能作为独立策略。但有五条规则被数据验证有效，
拆解为通用组件供全项目策略复用（三条选股/入场前置 + 两条持仓管理）：

1. ``count_limit_ups``          —— 活跃度过滤（历史涨停≥N 的妖股池）。
   唯一被验证有效的选股前置因子：低活跃股 -5.6% → 高活跃股打平/转正。

2. ``position_percentile``      —— 位置分位（相对低位 vs 上涨后）。
   断板反包要求「相对低位横盘后首板」，而非「上涨后追高」；位置分位偏低 = 低位。

3. ``entry_gap_filter``        —— 开盘溢价过滤（次日开盘相对昨收的溢价区间）。
   2026-08-15 用 500d 长历史深挖入场信号根因时发现：
   - 首板次日开盘溢价均值 +2.39%、中位 +1.49%，25.9% 高开>5%。
   - 按 5 日收益呈「微笑曲线」：低开>2% = -3.30%（资金不看好持续下跌），
     高开 1-3% = +1.18%（健康接力）、高开>5% = -2.31%（投机过度冲高回落）。
   - 这是「入场信号减亏」的根本原因：无条件以开盘价买入会吞掉 35% 高开>3% 的样本。
   - 修复：只在 ``gap_min ≤ open/prev_close - 1 ≤ gap_max`` 时入场。

4. ``apply_support_break_stop`` —— 破位止损（收盘跌破支撑价即走）。
   手册铁律「首板开盘价 = 主力成本底线，收盘有效跌破 = 形态失败」，数据验证有效。
   通用卖出规则：持仓期间任一交易日收盘跌破支撑价，即置卖出信号。

5. ``detect_lock_profit_days``  —— 板上减半止盈（放量反包/涨停先锁利）。
   盈亏比思维「反包成功至少减仓一半」。通用止盈触发判定：入场后放量大阳日 = 减半日。

设计约束
--------
- 纯函数：输入 DataFrame，输出 DataFrame 或值，无副作用、无全局状态。
- 无前视：所有判定只用当日及之前可得信息（收盘后信号），禁止引用未来数据。
- 可单测：合成 K 线即可覆盖关键路径与边界。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 涨停判定口径（主板 9.5%，兼容实体/一字板差异）
DEFAULT_LIMIT_UP_PCT = 0.095
# 涨停判定容差（接近涨停也计，一字/实体差）
LIMIT_UP_TOL = 0.005


def count_limit_ups(
    df: pd.DataFrame,
    lookback_days: int = 250,
    limit_up_pct: float = DEFAULT_LIMIT_UP_PCT,
    min_periods: int = 30,
) -> pd.Series:
    """滚动涨停次数（活跃度/辨识度指标）。

    返回 Series，每行 = 截至当日（含当日）的近 ``lookback_days`` 个交易日涨停次数。
    涨停判定：收盘涨幅 ≥ ``limit_up_pct - 0.005``（主板 9.5% 口径）。

    用法::

        df["limit_cnt"] = count_limit_ups(df)
        active = bool(df["limit_cnt"].iloc[-1] >= 20)  # 妖股特征

    Args:
        df: 含 date/open/high/low/close/volume 的 K 线 DataFrame。
        lookback_days: 回看窗口（交易日），默认 250（近一年）。
        limit_up_pct: 涨停涨幅阈值，默认 0.095。
        min_periods: 滚动窗口最小样本数，默认 30（不足则 NaN）。

    Returns:
        与 df 等长的 Series，值为滚动涨停次数（float，NaN 表示样本不足）。
    """
    df = df.sort_values("date").reset_index(drop=True)
    pct = df["close"].pct_change()
    is_limit = pct >= (limit_up_pct - LIMIT_UP_TOL)
    return is_limit.rolling(lookback_days, min_periods=min_periods).sum()


def apply_support_break_stop(
    df: pd.DataFrame,
    support_price: float | None = None,
    support_from_entry_open: bool = False,
    signal_col: str = "signal",
    entry_signal: int = 1,
    exit_signal: int = -1,
) -> pd.DataFrame:
    """破位止损：持仓期间收盘跌破支撑价 → 在该日置卖出信号。

    语义：从每个 ``entry_signal``（买入）之后的下一根 K 线起跟踪，一旦某日
    ``close < support``（收盘有效跌破，无容忍线），即在该日标记
    ``exit_signal``（卖出），该持仓周期结束；直到下一个买入信号重新建立跟踪。

    支撑价两种来源（二选一）：
    - ``support_price``：固定标量支撑价（如断板反包的形态支撑位）。
    - ``support_from_entry_open``：用每个入场日的 ``open`` 作为该持仓的支撑价
      （首板战法的「破首板开盘价」铁律，每笔持仓支撑不同）。

    无前视：买入信号按收盘确认，止损从次日起判定；只用当日收盘价，不引用未来。

    Args:
        df: 含 date/open/close 与 ``signal_col`` 的 DataFrame（需已排序按日期）。
        support_price: 固定支撑价（标量），与 support_from_entry_open 二选一。
        support_from_entry_open: True 时用入场日 open 作为支撑价。
        signal_col: 信号列名，默认 "signal"。
        entry_signal: 买入信号值，默认 1。
        exit_signal: 卖出信号值，默认 -1。

    Returns:
        返回新的 DataFrame（不修改入参），``signal_col`` 中追加了破位止损卖点。
    """
    if support_price is None and not support_from_entry_open:
        raise ValueError("必须提供 support_price 或设置 support_from_entry_open=True")

    df = df.copy()
    if signal_col not in df.columns:
        df[signal_col] = 0
    signal = df[signal_col].to_numpy(dtype=int, copy=True)
    close = df["close"].to_numpy(dtype=float)
    open_ = df["open"].to_numpy(dtype=float)
    n = len(df)
    holding = False
    support = support_price
    for i in range(n):
        if signal[i] == entry_signal:
            holding = True
            # 首板铁律：支撑 = 入场日开盘价
            support = open_[i] if support_from_entry_open else support_price
            continue
        if not holding:
            continue
        # 策略已自行卖出，结束本持仓周期跟踪
        if signal[i] == exit_signal:
            holding = False
            continue
        if close[i] < support:
            signal[i] = exit_signal
            holding = False
    df[signal_col] = signal
    return df


def position_percentile(df: pd.DataFrame, window: int = 60) -> pd.Series:
    """位置分位：现价在近 ``window`` 日高低点区间的百分位（0~1）。

    0 = 位于区间最低点，1 = 位于区间最高点，0.5 = 区间中点。
    高低点用 ``shift(1)`` 排除当日（当日 high/low 盘中未定型，避免前视）。

    用途：刻画「相对低位 vs 上涨后」的位置维度——断板反包要求相对低位
    （位置分位偏低），而非上涨后追高（位置分位偏高）。与 first_board 的
    ``position_pct`` 语义一致，收敛为单一事实来源供全项目复用。

    Args:
        df: 含 date/close/high/low 的 K 线 DataFrame（需已排序按日期）。
        window: 回看窗口（交易日），默认 60。

    Returns:
        与 df 等长的 Series，值为位置分位（0~1），NaN 表示样本不足或区间为 0。
    """
    df = df.sort_values("date").reset_index(drop=True)
    high = df["high"].rolling(window).max().shift(1)
    low = df["low"].rolling(window).min().shift(1)
    rng = high - low
    return (df["close"] - low) / rng.replace(0, np.nan)


def detect_lock_profit_days(
    df: pd.DataFrame,
    signal_col: str = "signal",
    entry_signal: int = 1,
    exit_signal: int = -1,
    lock_pct: float = 0.05,
    vol_ratio_min: float = 1.2,
) -> list[int]:
    """板上减半：持仓期间出现放量大阳 → 返回减半止盈日的索引列表。

    语义：从每个买入信号之后，找到第一根满足「实体阳线（close > open）且
    涨幅 ≥ ``lock_pct`` 且 量比 ≥ ``vol_ratio_min``」的 K 线，该日为减半止盈日
    （TP1：反包成功先锁利）。每个持仓周期至多触发一次。

    不修改 ``signal_col``（减半涉及仓位语义，二值信号无法表达），仅返回触发日
    索引，由调用方决定如何处置（如降半仓、上移止损线）。

    Args:
        df: 含 date/close/volume 与 ``signal_col`` 的 DataFrame（需已排序按日期）。
        signal_col: 信号列名，默认 "signal"；若列不存在则视为全 0（无持仓）。
        entry_signal: 买入信号值，默认 1。
        exit_signal: 卖出信号值，默认 -1。
        lock_pct: 触发减半的单日涨幅阈值，默认 0.05。
        vol_ratio_min: 触发减半的量比下限（当日量 / MA5），默认 1.2。

    Returns:
        触发减半止盈日的整数位置索引列表（按时间升序）。
    """
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].to_numpy(dtype=float)
    open_ = df["open"].to_numpy(dtype=float)
    volume = df["volume"].to_numpy(dtype=float)
    if signal_col in df.columns:
        signal = df[signal_col].to_numpy(dtype=int, copy=True)
    else:
        signal = np.zeros(len(df), dtype=int)
    # min_periods=1：数据不足 5 根时用已有均值，避免 MA5=NaN 导致量比 fallback 失真
    vol_ma5 = df["volume"].rolling(5, min_periods=1).mean().to_numpy(dtype=float)
    n = len(df)

    result: list[int] = []
    holding = False
    locked = False
    for i in range(n):
        if signal[i] == entry_signal:
            holding = True
            locked = False
            continue
        if not holding:
            continue
        if signal[i] == exit_signal:
            holding = False
            continue
        if locked:
            continue
        if i < 1:
            continue
        day_chg = close[i] / close[i - 1] - 1.0 if close[i - 1] > 0 else 0.0
        vol_r = volume[i] / vol_ma5[i] if (np.isfinite(vol_ma5[i]) and vol_ma5[i] > 0) else 1.0
        # 实体阳线（close > open）——避免高开低走的"假阳线"被误判为大阳
        if close[i] > open_[i] and day_chg >= lock_pct and vol_r >= vol_ratio_min:
            result.append(i)
            locked = True
    return result


def entry_gap_filter(
    df: pd.DataFrame,
    gap_min: float = 0.0,
    gap_max: float = 0.03,
    signal_col: str = "signal",
    entry_signal: int = 1,
) -> pd.DataFrame:
    """开盘溢价过滤：根据次日开盘相对昨收的溢价区间，过滤掉非健康高开/低开的入场信号。

    语义：对每个 ``entry_signal``（买入信号），检查「次日开盘价 / 当日收盘价 - 1」
    是否落在 ``[gap_min, gap_max]`` 区间内。若超出区间则取消该买入信号（置 0），
    表示该入场点因开盘溢价不合理（过度透支或资金不看好）而放弃。

    数据依据（500d 长历史事件研究，N=599 首板事件）：
    - 低开>2%：5日均值 -3.30%，胜率 35.8%（资金不看好持续下跌）
    - 平盘~小高开<1%：5日均值 -0.03%，胜率 44.1%（中性）
    - 高开 1-3%：5日均值 +1.18%，胜率 47.4%（健康接力，最优区间）
    - 高开 3-5%：5日均值 +0.68%，胜率 50.9%（次优）
    - 高开>5%：5日均值 -2.31%，胜率 31.6%（投机过度，冲高回落）

    无前视：用 ``df[i+1].open`` / ``df[i].close``，二者在 ``i+1`` 开盘竞价时
    均可得（昨收是历史，开盘是当日开盘竞价结果），入场决策在 ``i+1`` 开盘时做出。

    Args:
        df: 含 date/open/close 与 ``signal_col`` 的 DataFrame（需已排序按日期）。
        gap_min: 允许的最小开盘溢价（默认 0.0，禁止低开）。
        gap_max: 允许的最大开盘溢价（默认 0.03，禁止过度高开）。
        signal_col: 信号列名，默认 "signal"。
        entry_signal: 买入信号值，默认 1。

    Returns:
        返回新的 DataFrame（不修改入参），``signal_col`` 中超出溢价区间的入场信号被置 0。
        同时新增 ``gap_pct`` 列记录次日开盘溢价（NaN 表示无后续 K 线）。
    """
    df = df.copy()
    if signal_col not in df.columns:
        df[signal_col] = 0
    signal = df[signal_col].to_numpy(dtype=int, copy=True)
    open_ = df["open"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)
    gap_pct = np.full(n, np.nan)
    for i in range(n):
        if signal[i] != entry_signal:
            continue
        if i + 1 >= n:
            signal[i] = 0  # 无次日数据，无法判定溢价，取消信号
            continue
        prev_close = close[i]
        next_open = open_[i + 1]
        if prev_close <= 0 or next_open <= 0:
            signal[i] = 0
            continue
        gap = next_open / prev_close - 1.0
        gap_pct[i] = gap
        if not (gap_min <= gap <= gap_max):
            signal[i] = 0  # 超出区间，取消入场
    df[signal_col] = signal
    df["gap_pct"] = gap_pct
    return df
