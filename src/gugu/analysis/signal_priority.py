"""信号优先级排序：解决信号泛滥 vs 执行能力不匹配问题。

P0-2 优化：根据多专家评审报告（2026-07-24）：
- 问题：6个信号策略同时运行，每天可能产生10+条信号，用户无法全部执行
- 解决：按优先级排序，每日推送信号≤3条

优先级评分公式：
    priority_score = confidence × win_rate_factor × regime_factor × position_factor

其中：
- confidence: 信号置信度（0-1）
- win_rate_factor: 策略历史胜率因子（0.5-1.5）
- regime_factor: 市场状态因子（牛市1.2/反弹1.1/震荡1.0/熊市0.5）
- position_factor: 仓位空间因子（有足够仓位时1.0，仓位不足时0.5）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()

MAX_DAILY_SIGNALS = 3


@dataclass
class SignalPriority:
    """带优先级的信号"""

    signal: dict[str, Any]
    priority_score: float
    rank: int


class SignalPrioritySorter:
    """信号优先级排序器。"""

    REGIME_FACTORS = {
        "bull": 1.2,
        "rally": 1.1,
        "sideways": 1.0,
        "bear": 0.5,
        "crash": 0.3,
        "unknown": 0.8,
    }

    STRATEGY_WIN_RATES = {
        "box_breakout": 0.55,
        "double_yin": 0.48,
        "cloud_trend": 0.45,
        "hot_money": 0.40,
        "ma20_trend": 0.42,
        "zhen_dahu": 0.50,
    }

    def __init__(self, strategy_pool: Any | None = None) -> None:
        self._strategy_pool = strategy_pool
        self._max_daily = MAX_DAILY_SIGNALS

    def sort_signals(
        self,
        signals: list[dict[str, Any]],
        regime: str | None = None,
        available_position_ratio: float = 1.0,
    ) -> list[SignalPriority]:
        """对信号列表按优先级排序并限制数量。

        Args:
            signals: 信号列表
            regime: 市场状态
            available_position_ratio: 可用仓位比例（0-1）

        Returns:
            排序后的信号列表（最多MAX_DAILY_SIGNALS条）
        """
        if not signals:
            return []

        scored_signals: list[SignalPriority] = []
        for sig in signals:
            score = self._calc_priority_score(sig, regime, available_position_ratio)
            scored_signals.append(SignalPriority(signal=sig, priority_score=score, rank=0))

        scored_signals.sort(key=lambda x: x.priority_score, reverse=True)

        for i, sp in enumerate(scored_signals):
            sp.rank = i + 1

        limited = scored_signals[: self._max_daily]

        if len(scored_signals) > self._max_daily:
            logger.info(
                f"信号优先级排序：{len(scored_signals)}条信号 → 截断至{self._max_daily}条，"
                f"Top1={limited[0].signal['symbol']}({limited[0].priority_score:.3f})"
            )

        return limited

    def _calc_priority_score(
        self,
        signal: dict[str, Any],
        regime: str | None,
        available_position_ratio: float,
    ) -> float:
        """计算信号优先级评分。

        priority_score = confidence × win_rate_factor × regime_factor × position_factor
        """
        confidence = float(signal.get("confidence", 0.5))

        win_rate_factor = self._get_win_rate_factor(signal)

        regime_factor = self.REGIME_FACTORS.get(regime or "unknown", 0.8)

        position_factor = 1.0 if available_position_ratio >= 0.15 else 0.5

        score = confidence * win_rate_factor * regime_factor * position_factor

        return round(score, 4)

    def _get_win_rate_factor(self, signal: dict[str, Any]) -> float:
        """获取策略历史胜率因子。

        从StrategyPool获取真实胜率，如果没有则使用默认胜率表。
        """
        strategies = signal.get("strategies", [])
        if not strategies:
            return 1.0

        if self._strategy_pool is not None:
            stats = self._strategy_pool.get_stats()
            win_rates = []
            for name in strategies:
                if name in stats and stats[name]["total_signals"] >= 5:
                    win_rates.append(stats[name]["recent_win_rate"])
            if win_rates:
                avg_win_rate = sum(win_rates) / len(win_rates)
                return self._win_rate_to_factor(avg_win_rate)

        default_rates = [self.STRATEGY_WIN_RATES.get(n, 0.45) for n in strategies]
        avg_default = sum(default_rates) / len(default_rates)
        return self._win_rate_to_factor(avg_default)

    @staticmethod
    def _win_rate_to_factor(win_rate: float) -> float:
        """将胜率转换为因子（0.5-1.5）。"""
        if win_rate >= 0.6:
            return 1.5
        elif win_rate >= 0.5:
            return 1.2
        elif win_rate >= 0.4:
            return 1.0
        elif win_rate >= 0.3:
            return 0.7
        else:
            return 0.5


def sort_and_limit_signals(
    signals: list[dict[str, Any]],
    regime: str | None = None,
    available_position_ratio: float = 1.0,
    strategy_pool: Any | None = None,
) -> list[dict[str, Any]]:
    """便捷函数：对信号排序并限制数量。

    Args:
        signals: 信号列表
        regime: 市场状态
        available_position_ratio: 可用仓位比例
        strategy_pool: 策略池（用于获取真实胜率）

    Returns:
        排序后的信号列表（最多MAX_DAILY_SIGNALS条）
    """
    sorter = SignalPrioritySorter(strategy_pool)
    prioritized = sorter.sort_signals(signals, regime, available_position_ratio)
    return [p.signal for p in prioritized]
