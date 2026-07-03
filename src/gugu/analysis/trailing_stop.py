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
try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass
from typing import Any

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class TrailingStopSignal(StrEnum):
    """移动止损信号。"""

    HOLD = "hold"  # 持有，止损未触发
    ALERT = "alert"  # 警惕，跌破浪谷1次
    WARNING = "warning"  # 警告，跌破浪谷2次
    EXIT = "exit"  # 清仓，跌破浪谷3次或触发止损价
    TIGHTEN = "tighten"  # 收紧止损（出现危险信号时）


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
    # 上次跌破日期（防止同日多次扫描重复计数）
    last_break_date: str = ""
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
    DEFAULT_INITIAL_STOP_PCT = 0.08  # 初始止损8%（固定比例模式）
    DEFAULT_ATR_MULTIPLIER = 3.0  # ATR 止损倍数（ATR 模式）
    WAVE_WINDOW = 5  # 浪谷识别窗口（基准值，实际由 ATR 自适应）
    MAX_DRAWDOWN_STOP_PCT = 0.15  # 最大回撤止损15%
    TIGHTEN_FACTOR = 0.5  # 危险信号收紧因子
    MIN_VALLEY_PCT = 0.005  # P2 修复：浪谷之间最小价格差异比例（避免接近的重复浪谷）

    # 市场状态自适应参数（P3 优化）
    # 不同市场状态下 ATR 倍数调整：高波动市场放宽止损，低波动市场收紧止损
    REGIME_ATR_MULTIPLIERS = {
        "bull": 3.5,      # 牛市放宽止损，避免被震荡出局
        "bear": 2.5,      # 熊市收紧止损，保护本金
        "sideways": 2.0,  # 震荡市收紧止损，减少假突破损失
        "crash": 2.0,     # 暴跌市收紧止损
        "rally": 3.0,     # 反弹市适中
    }

    # 不同市场状态下最大回撤止损调整
    REGIME_MAX_DRAWDOWN = {
        "bull": 0.18,     # 牛市允许更大回撤
        "bear": 0.10,     # 熊市严格限制回撤
        "sideways": 0.12, # 震荡市适中
        "crash": 0.08,    # 暴跌市严格限制
        "rally": 0.15,    # 反弹市适中
    }

    # 不同市场状态下危险信号收紧因子
    REGIME_TIGHTEN_FACTORS = {
        "bull": 0.3,      # 牛市收紧幅度小，给更多空间
        "bear": 0.6,      # 熊市收紧幅度大，快速保护
        "sideways": 0.5,  # 震荡市适中
        "crash": 0.7,     # 暴跌市大幅收紧
        "rally": 0.4,     # 反弹市适度收紧
    }

    def init_stop(
        self,
        entry_price: float,
        initial_stop_pct: float | None = None,
        df: pd.DataFrame | None = None,
        atr_multiplier: float | None = None,
        regime: str | None = None,
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
            regime: 市场状态（bull/bear/sideways/crash/rally），提供时使用自适应参数

        Returns:
            TrailingStopState 初始状态
        """
        pct: float | None = None
        # ATR 自适应止损（优先）
        if df is not None and not df.empty:
            try:
                from gugu.analysis.technical import atr as calc_atr

                # 市场状态自适应 ATR 倍数
                if regime and regime in self.REGIME_ATR_MULTIPLIERS:
                    mult = self.REGIME_ATR_MULTIPLIERS[regime]
                    logger.info(f"[trailing-stop] 使用市场状态 {regime} 的 ATR 倍数: {mult}")
                else:
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

        if pct is None:
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
        regime: str | None = None,
    ) -> tuple[TrailingStopState, TrailingStopSignal]:
        """更新移动止损状态。

        每日交易循环中调用，用最新行情更新止损价和信号。

        Args:
            state: 当前止损状态
            df: 行情DataFrame（含 close, high, low 列）
            danger_signals: 危险信号列表（来自DangerSignalDetector）
            regime: 市场状态（bull/bear/sideways/crash/rally），
                   提供时使用自适应参数（回撤止损、收紧因子）

        Returns:
            (更新后的状态, 信号)
        """
        if df.empty:
            return state, TrailingStopSignal.HOLD

        current_price = float(df.iloc[-1]["close"])
        low_series = df["low"].astype(float) if "low" in df.columns else df["close"].astype(float)

        # 1. 更新最高价
        if current_price > state.highest_price:
            state.highest_price = current_price

        # 2. 识别浪谷（自适应窗口：高波动用大窗口过滤噪音，低波动用小窗口捕捉细节）
        wave_window = self._adaptive_wave_window(df)
        new_valleys = self._find_wave_valleys(low_series, wave_window)
        if new_valleys:
            # 合并历史浪谷和新浪谷（均为时间顺序），按价格近邻去重后保留最近5个
            all_valleys = state.wave_valleys + new_valleys
            deduped: list[float] = []
            for v in all_valleys:
                if deduped and abs(v - deduped[-1]) / max(deduped[-1], 1.0) < self.MIN_VALLEY_PCT:
                    deduped[-1] = v  # 价格相近时替换为更新的浪谷
                else:
                    deduped.append(v)
            state.wave_valleys = deduped[-5:]

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

        # 4. 危险信号收紧止损（市场状态自适应）
        tighten_factor = self.TIGHTEN_FACTOR
        if regime and regime in self.REGIME_TIGHTEN_FACTORS:
            tighten_factor = self.REGIME_TIGHTEN_FACTORS[regime]
        if danger_signals:
            if not state.tightened:
                tighten_price = round(
                    state.current_stop_price
                    + (current_price - state.current_stop_price) * tighten_factor,
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
        else:
            # 危险信号解除后复位收紧状态，下次收紧可再次触发
            if state.tightened:
                state.tightened = False
                logger.info("[trailing-stop] 危险信号解除，收紧状态已复位")

        # 5. 最大回撤止损兜底（市场状态自适应）
        max_drawdown_pct = self.MAX_DRAWDOWN_STOP_PCT
        if regime and regime in self.REGIME_MAX_DRAWDOWN:
            max_drawdown_pct = self.REGIME_MAX_DRAWDOWN[regime]
        max_dd_stop = round(state.highest_price * (1 - max_drawdown_pct), 2)
        if max_dd_stop > state.current_stop_price and current_price > max_dd_stop:
            state.current_stop_price = max_dd_stop

        # 6. 判断信号
        signal = self._evaluate_signal(state, current_price, danger_signals)

        # 7. 更新状态
        state.last_update = pd.Timestamp.now().strftime("%Y-%m-%d")
        state.last_signal = signal.value

        return state, signal

    def _adaptive_wave_window(self, df: pd.DataFrame) -> int:
        """根据 ATR 波动率自适应计算浪谷窗口。

        高波动股票（ATR/price > 3%%）：用 8 日窗口（过滤更多噪音）
        中波动股票（ATR/price 1-3%%）：用 5 日窗口（默认）
        低波动股票（ATR/price < 1%%）：用 3 日窗口（捕捉更多细节）
        """
        try:
            from gugu.analysis.technical import atr as calc_atr

            atr_value = float(calc_atr(df, period=14).iloc[-1])
            price = float(df["close"].iloc[-1])
            if price > 0:
                atr_ratio = atr_value / price
                if atr_ratio > 0.03:
                    return 8
                elif atr_ratio < 0.01:
                    return 3
        except Exception:
            pass
        return self.WAVE_WINDOW

    @classmethod
    def _find_wave_valleys(cls, low_series: pd.Series, window: int) -> list[float]:
        """识别浪谷（局部低点）。

        在最近30根K线中寻找局部低点。
        优化1：使用更稳健的峰谷识别算法（双侧比较 + 置信度评分）
        优化2：仅保留置信度 >= 0.6 的浪谷，过滤震荡市的伪浪谷
        """
        lookback = min(30, len(low_series))
        recent = low_series.iloc[-lookback:]
        raw_valleys: list[tuple[float, float]] = []  # (price, confidence)

        for i in range(window, len(recent) - window):
            left = recent.iloc[i - window : i]
            right = recent.iloc[i + 1 : i + window + 1]
            center = recent.iloc[i]

            # 必须是局部最低点
            if center != recent.iloc[i - window : i + window + 1].min():
                continue

            # 计算置信度：
            # 1. 左右两侧的上升幅度（浪谷越深，置信度越高）
            left_min = left.min()
            right_min = right.min()
            left_rise = (left.iloc[-1] - center) / center if center > 0 else 0
            right_rise = (right.iloc[0] - center) / center if center > 0 else 0

            # 2. 谷底相对于左右最低点的优势
            left_advantage = (center - left_min) / left_min if left_min > 0 else 0
            right_advantage = (center - right_min) / right_min if right_min > 0 else 0

            # 置信度 = 平均上升幅度 + 谷底优势
            confidence = (left_rise + right_rise + abs(left_advantage) + abs(right_advantage)) / 4
            confidence = min(confidence * 5, 1.0)  # 归一化到 0-1

            raw_valleys.append((float(center), confidence))

        # 末尾低点（权重低）
        if len(recent) > 0:
            raw_valleys.append((float(recent.iloc[-1]), 0.3))

        # 过滤低置信度浪谷（置信度 < 0.6 的震荡伪浪谷被过滤）
        filtered = [(p, c) for p, c in raw_valleys if c >= 0.6]

        # 如果没有高置信度浪谷，回退到普通识别但使用更大的窗口
        if not filtered:
            # 用更宽松的阈值重新筛选
            filtered = [(p, c) for p, c in raw_valleys if c >= 0.4]
        if not filtered:
            # 最终回退：取最近最低点
            return [float(recent.min())]

        # 去重（按时间顺序，价格相近时保留更新的浪谷 — 与 update() 逻辑一致）
        valleys: list[float] = []
        for p, _ in filtered:
            if valleys and abs(p - valleys[-1]) / max(valleys[-1], 1.0) < cls.MIN_VALLEY_PCT:
                valleys[-1] = p  # 价格相近时替换为更新的浪谷
            else:
                valleys.append(p)

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

        跌破计数重置规则（完善修复）：
        - 旧逻辑：价格回升超过浪谷2%时每天递减1（过于激进，单日跌破被快速原谅）
        - 新逻辑：价格显著恢复（超过浪谷2%）时完全重置计数为0
          理由：陈江挺的"递进警报"是事件计数，不应因短暂回升而衰减；
          显著恢复才意味着趋势恢复，此时重置计数合理。

        同日去重（P0 修复）：
        - 盘中多次扫描（4次/日）同日跌破同一浪谷只计1次
        - 用 last_break_date 防止同日累计计数导致提前清仓
        """
        # 触发止损价
        if current_price <= state.current_stop_price:
            logger.warning(
                f"[trailing-stop] 止损触发: 现价 {current_price:.2f} <= 止损价 {state.current_stop_price:.2f}"
            )
            return TrailingStopSignal.EXIT

        # 检查跌破浪谷次数
        if state.wave_valleys:
            latest_valley = state.wave_valleys[-1]
            today = pd.Timestamp.now().strftime("%Y-%m-%d")
            if current_price < latest_valley:
                # P0 修复：同日多次扫描只计1次（防止盘中4次扫描误触3次EXIT）
                if state.last_break_date != today:
                    state.valley_break_count += 1
                    state.last_break_date = today
                    logger.warning(
                        f"[trailing-stop] 跌破浪谷 {latest_valley:.2f}，第 {state.valley_break_count} 次"
                    )

                if state.valley_break_count >= 3:
                    return TrailingStopSignal.EXIT
                elif state.valley_break_count == 2:
                    return TrailingStopSignal.WARNING
                else:
                    return TrailingStopSignal.ALERT
            elif current_price > latest_valley * 1.02 and state.valley_break_count > 0:
                # 显著恢复（超过浪谷2%），完全重置计数
                logger.info(
                    f"[trailing-stop] 价格显著恢复 {current_price:.2f} > 浪谷 {latest_valley:.2f} × 1.02，"
                    f"跌破计数 {state.valley_break_count} → 0"
                )
                state.valley_break_count = 0

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
