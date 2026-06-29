"""移动止损引擎：基于《炒股的智慧》的浪谷递进移动止损。

陈江挺的移动止损原则：
- 止损随升势上移，不随跌势下移
- 追踪持仓的"浪谷"（最近的有效低点）
- 跌破浪谷1次→警惕，2次→大概率反转，3次→立即清仓

与固定止损的区别：
- 固定止损：入场时设一个价格，到点就卖
- 移动止损：随着股价上涨，止损价也随之上移，锁定浮盈

实现逻辑：
1. 买入时，初始止损价 = 入场价 * (1 - 初始止损比例)
2. 价格创新高后，寻找最近的浪谷作为新的止损参考
3. 止损价只能上移，不能下移
4. 当危险信号出现时，收紧止损至最近浪谷
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class TrailingStopSignal(str, Enum):
    """移动止损信号。"""
    HOLD = "hold"               # 持有，止损未触发
    ALERT = "alert"             # 警惕，跌破浪谷1次
    WARNING = "warning"         # 警告，跌破浪谷2次
    EXIT = "exit"               # 清仓，跌破浪谷3次或触发止损价
    TIGHTEN = "tighten"         # 收紧止损（出现危险信号时）


@dataclass
class TrailingStopState:
    """移动止损状态（持久化到Position）。

    存储在Position.trailing_stop字段中，每日更新。
    """
    # 初始止损价（入场时设定）
    initial_stop_price: float = 0.0
    # 当前止损价（随浪谷上移）
    current_stop_price: float = 0.0
    # 历史最高价
    highest_price: float = 0.0
    # 浪谷列表（最近的低点序列）
    wave_valleys: list[float] = field(default_factory=list)
    # 跌破浪谷次数
    valley_break_count: int = 0
    # 上次更新日期
    last_update: str = ""
    # 最近信号
    last_signal: str = TrailingStopSignal.HOLD.value
    # 是否收紧止损（危险信号触发）
    tightened: bool = False


class TrailingStopEngine:
    """移动止损引擎。

    核心算法：
    1. 波浪识别：使用N日窗口找局部低点（浪谷）
    2. 止损上移：当价格创新高后，将止损价移至最近浪谷
    3. 递进警报：跌破浪谷计数，1次警惕2次警告3次清仓
    4. 危险信号联动：检测到危险信号时收紧止损至最近浪谷

    参数来源：
    - 初始止损：支持 ATR 自适应（推荐）或固定比例（可配置）
    - 浪谷窗口：5日（可配置）
    - 最大回撤止损：15%（防止浪谷止损过远时兜底）

    ATR 自适应止损（A-05 修复）：
    固定止损比例对高波动股票太近、低波动股票太远。
    ATR 止损 = 入场价 - ATR_MULTIPLIER × ATR(14)
    默认 ATR_MULTIPLIER = 3.0（相当于 3 倍平均真实波幅）
    """

    # 默认参数
    DEFAULT_INITIAL_STOP_PCT = 0.08    # 初始止损8%（固定比例模式）
    DEFAULT_ATR_MULTIPLIER = 3.0       # ATR 止损倍数（ATR 模式）
    WAVE_WINDOW = 5                     # 浪谷识别窗口
    MAX_DRAWDOWN_STOP_PCT = 0.15       # 最大回撤止损15%
    TIGHTEN_FACTOR = 0.5               # 危险信号收紧因子

    def init_stop(
        self,
        entry_price: float,
        initial_stop_pct: float | None = None,
        df: pd.DataFrame | None = None,
        atr_multiplier: float | None = None,
    ) -> TrailingStopState:
        """初始化移动止损状态。

        在买入时调用，设定初始止损价。
        ATR 自适应优先：如果提供了 df 和 atr_multiplier，计算 ATR 止损。
        Fallback 到固定比例止损。

        Args:
            entry_price: 入场价格
            initial_stop_pct: 初始止损比例（固定比例模式，默认8%）
            df: 行情 DataFrame（含 high/low/close），用于 ATR 计算
            atr_multiplier: ATR 倍数（默认 3.0），止损 = 入场价 - multiplier × ATR

        Returns:
            TrailingStopState 初始状态
        """
        # ATR 自适应止损（优先）
        if df is not None and not df.empty:
            try:
                from gugu.analysis.technical import atr as calc_atr

                mult = atr_multiplier or self.DEFAULT_ATR_MULTIPLIER
                atr_value = float(calc_atr(df, period=14).iloc[-1])
                if atr_value > 0:
                    stop_price = round(entry_price - mult * atr_value, 2)
                    # 安全护栏：ATR 止损不低于 2%，不高于 20%
                    stop_pct = (entry_price - stop_price) / entry_price
                    if stop_pct < 0.02:
                        stop_price = round(entry_price * 0.98, 2)
                    elif stop_pct > 0.20:
                        stop_price = round(entry_price * 0.80, 2)
                    logger.info(
                        f"[trailing-stop] ATR 初始止损: 入场价={entry_price:.2f}, "
                        f"ATR={atr_value:.2f}, 倍数={mult}, 止损价={stop_price:.2f}"
                    )
                else:
                    # ATR 为 0，fallback
                    raise ValueError("ATR is zero")
            except Exception as e:
                logger.debug(f"[trailing-stop] ATR 止损计算失败，回退到固定比例: {e}")
                pct = initial_stop_pct or self.DEFAULT_INITIAL_STOP_PCT
                stop_price = round(entry_price * (1 - pct), 2)
        else:
            # 固定比例止损（fallback）
            pct = initial_stop_pct or self.DEFAULT_INITIAL_STOP_PCT
            stop_price = round(entry_price * (1 - pct), 2)

        if 'pct' not in dir() or pct is None:
            pct = (entry_price - stop_price) / entry_price

        state = TrailingStopState(
            initial_stop_price=stop_price,
            current_stop_price=stop_price,
            highest_price=entry_price,
            wave_valleys=[entry_price * (1 - pct)],  # 初始浪谷 = 止损价
            valley_break_count=0,
            last_update=pd.Timestamp.now().strftime("%Y-%m-%d"),
            last_signal=TrailingStopSignal.HOLD.value,
            tightened=False,
        )

        logger.info(
            f"[trailing-stop] 初始化: 入场价={entry_price:.2f}, "
            f"初始止损={stop_price:.2f} (-{pct:.0%})"
        )
        return state

    def update(
        self,
        state: TrailingStopState,
        df: pd.DataFrame,
        danger_signals: list[str] | None = None,
    ) -> tuple[TrailingStopState, TrailingStopSignal]:
        """更新移动止损状态。

        每日交易循环中调用，用最新行情更新止损价和信号。

        Args:
            state: 当前止损状态
            df: 行情DataFrame（含 close, high, low 列）
            danger_signals: 危险信号列表（来自DangerSignalDetector）

        Returns:
            (更新后的状态, 信号)
        """
        if df.empty:
            return state, TrailingStopSignal.HOLD

        current_price = float(df.iloc[-1]["close"])
        high_series = df["high"].astype(float) if "high" in df.columns else df["close"].astype(float)
        low_series = df["low"].astype(float) if "low" in df.columns else df["close"].astype(float)

        # 1. 更新最高价
        if current_price > state.highest_price:
            state.highest_price = current_price

        # 2. 识别浪谷
        new_valleys = self._find_wave_valleys(low_series, self.WAVE_WINDOW)
        if new_valleys:
            # 只保留最近5个浪谷
            all_valleys = state.wave_valleys + new_valleys
            state.wave_valleys = sorted(set(all_valleys))[-5:]

        # 3. 更新止损价
        latest_valley = state.wave_valleys[-1] if state.wave_valleys else state.initial_stop_price

        # 止损价 = max(最近浪谷, 当前止损价) — 只能上移
        # 但不能超过当前价格（否则立即触发）
        if current_price > latest_valley > state.current_stop_price:
            old_stop = state.current_stop_price
            state.current_stop_price = round(latest_valley, 2)
            logger.info(
                f"[trailing-stop] 止损上移: {old_stop:.2f} → {state.current_stop_price:.2f}"
            )

        # 4. 危险信号收紧止损
        if danger_signals and not state.tightened:
            tighten_price = round(
                state.current_stop_price + (current_price - state.current_stop_price) * self.TIGHTEN_FACTOR,
                2,
            )
            if tighten_price > state.current_stop_price:
                old_stop = state.current_stop_price
                state.current_stop_price = tighten_price
                state.tightened = True
                logger.warning(
                    f"[trailing-stop] 危险信号收紧止损: {old_stop:.2f} → {tighten_price:.2f}, "
                    f"信号={danger_signals}"
                )

        # 5. 最大回撤止损兜底
        max_dd_stop = round(
            state.highest_price * (1 - self.MAX_DRAWDOWN_STOP_PCT), 2
        )
        if max_dd_stop > state.current_stop_price and current_price > max_dd_stop:
            state.current_stop_price = max_dd_stop

        # 6. 判断信号
        signal = self._evaluate_signal(state, current_price, danger_signals)

        # 7. 更新状态
        state.last_update = pd.Timestamp.now().strftime("%Y-%m-%d")
        state.last_signal = signal.value

        return state, signal

    @staticmethod
    def _find_wave_valleys(low_series: pd.Series, window: int) -> list[float]:
        """识别浪谷（局部低点）。

        在最近30根K线中寻找局部低点。
        """
        lookback = min(30, len(low_series))
        recent = low_series.iloc[-lookback:]
        valleys: list[float] = []

        for i in range(window, len(recent) - window):
            segment = recent.iloc[i - window : i + window + 1]
            if recent.iloc[i] == segment.min():
                valleys.append(float(recent.iloc[i]))

        # 末尾低点
        if len(recent) > 0:
            valleys.append(float(recent.iloc[-1]))

        return valleys

    def _evaluate_signal(
        self,
        state: TrailingStopState,
        current_price: float,
        danger_signals: list[str] | None,
    ) -> TrailingStopSignal:
        """评估止损信号。

        判断优先级：
        1. 触发止损价 → EXIT
        2. 跌破浪谷3次 → EXIT
        3. 跌破浪谷2次 → WARNING
        4. 跌破浪谷1次 → ALERT
        5. 有危险信号 → TIGHTEN
        6. 正常 → HOLD
        """
        # 触发止损价
        if current_price <= state.current_stop_price:
            logger.warning(
                f"[trailing-stop] 止损触发: 现价 {current_price:.2f} <= 止损价 {state.current_stop_price:.2f}"
            )
            return TrailingStopSignal.EXIT

        # 检查跌破浪谷次数
        latest_valley = state.wave_valleys[-1] if state.wave_valleys else 0
        if latest_valley > 0 and current_price < latest_valley:
            # 跌破最近浪谷
            state.valley_break_count += 1
            logger.warning(
                f"[trailing-stop] 跌破浪谷 {latest_valley:.2f}，"
                f"第 {state.valley_break_count} 次"
            )

            if state.valley_break_count >= 3:
                return TrailingStopSignal.EXIT
            elif state.valley_break_count == 2:
                return TrailingStopSignal.WARNING
            else:
                return TrailingStopSignal.ALERT
        else:
            # 价格回到浪谷之上，重置计数（但不完全重置，保留记忆）
            if current_price > latest_valley * 1.02:  # 超过浪谷2%以上
                state.valley_break_count = max(0, state.valley_break_count - 1)

        # 危险信号
        if danger_signals:
            return TrailingStopSignal.TIGHTEN

        return TrailingStopSignal.HOLD

    @staticmethod
    def state_to_dict(state: TrailingStopState) -> dict[str, Any]:
        """将状态转为字典（用于持久化）。"""
        return {
            "initial_stop_price": state.initial_stop_price,
            "current_stop_price": state.current_stop_price,
            "highest_price": state.highest_price,
            "wave_valleys": state.wave_valleys,
            "valley_break_count": state.valley_break_count,
            "last_update": state.last_update,
            "last_signal": state.last_signal,
            "tightened": state.tightened,
        }

    @staticmethod
    def dict_to_state(d: dict[str, Any]) -> TrailingStopState:
        """从字典恢复状态。"""
        return TrailingStopState(
            initial_stop_price=d.get("initial_stop_price", 0.0),
            current_stop_price=d.get("current_stop_price", 0.0),
            highest_price=d.get("highest_price", 0.0),
            wave_valleys=d.get("wave_valleys", []),
            valley_break_count=d.get("valley_break_count", 0),
            last_update=d.get("last_update", ""),
            last_signal=d.get("last_signal", TrailingStopSignal.HOLD.value),
            tightened=d.get("tightened", False),
        )
