"""策略池管理：多策略权重管理，参考 qlib 的 TopkDropoutStrategy。

核心功能：
1. 策略权重管理：根据近期表现动态调整各策略权重
2. 信号验证：多个策略交叉验证，减少假信号
3. 策略淘汰：连续表现差的策略自动降权或禁用
4. 信号加权：多策略信号按权重加权融合
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gugu.config import settings
from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class StrategyWeight:
    """策略权重"""
    name: str
    weight: float            # 当前权重 0-1
    enabled: bool            # 是否启用
    win_count: int = 0       # 近期盈利次数
    loss_count: int = 0      # 近期亏损次数
    total_signals: int = 0   # 总信号数
    recent_returns: list[float] = field(default_factory=list)  # 近期收益记录


class StrategyPool:
    """策略池管理器。

    对标 qlib 的 TopkDropoutStrategy：
    - 维护策略权重
    - 根据近期表现动态调整
    - 支持信号加权融合
    - 支持策略淘汰和恢复
    """

    def __init__(self):
        self._weights: dict[str, StrategyWeight] = {}
        self._init_weights()

    def _init_weights(self) -> None:
        """初始化策略权重（从配置读取）"""
        enabled = settings().get("strategy", {}).get("enabled", [])
        n = max(len(enabled), 1)
        for name in enabled:
            self._weights[name] = StrategyWeight(
                name=name, weight=1.0 / n, enabled=True
            )

    def get_weights(self) -> dict[str, float]:
        """获取当前策略权重"""
        enabled_weights = {k: v for k, v in self._weights.items() if v.enabled}
        total = sum(v.weight for v in enabled_weights.values())
        if total == 0:
            return {}
        return {k: v.weight / total for k, v in enabled_weights.items()}

    def get_enabled_names(self) -> list[str]:
        """获取启用的策略名称列表"""
        return [k for k, v in self._weights.items() if v.enabled]

    def weighted_fusion(self, signals: list[dict[str, Any]]) -> dict[str, Any]:
        """加权融合多个策略信号。

        Args:
            signals: 各策略的信号列表，每个包含 direction/confidence/strategy_name

        Returns:
            dict: 融合后的信号（direction/confidence/strategies/weighted_score）
        """
        weights = self.get_weights()
        if not signals or not weights:
            return {"direction": "hold", "confidence": 0.0, "strategies": [], "weighted_score": 0.0}

        buy_score = 0.0
        sell_score = 0.0
        total_weight = 0.0
        active_strategies = []

        for sig in signals:
            name = sig.get("strategy_name", "")
            if name not in weights:
                continue
            w = weights[name]
            if sig.get("direction") == "buy":
                buy_score += sig.get("confidence", 0) * w
            elif sig.get("direction") == "sell":
                sell_score += sig.get("confidence", 0) * w
            total_weight += w
            active_strategies.append(name)

        if total_weight == 0:
            return {"direction": "hold", "confidence": 0.0, "strategies": [], "weighted_score": 0.0}

        # 归一化
        buy_score /= total_weight
        sell_score /= total_weight

        if buy_score > sell_score and buy_score > 0.3:
            return {
                "direction": "buy",
                "confidence": round(buy_score, 2),
                "strategies": active_strategies,
                "weighted_score": round(buy_score - sell_score, 2),
            }
        elif sell_score > buy_score and sell_score > 0.3:
            return {
                "direction": "sell",
                "confidence": round(sell_score, 2),
                "strategies": active_strategies,
                "weighted_score": round(sell_score - buy_score, 2),
            }
        else:
            return {
                "direction": "hold",
                "confidence": max(buy_score, sell_score),
                "strategies": active_strategies,
                "weighted_score": 0.0,
            }

    def update_performance(self, strategy_name: str, pnl: float) -> None:
        """更新策略表现（交易完成后调用）

        Args:
            strategy_name: 策略名称
            pnl: 该笔交易的盈亏
        """
        if strategy_name not in self._weights:
            return

        w = self._weights[strategy_name]
        w.total_signals += 1
        w.recent_returns.append(pnl)

        if pnl > 0:
            w.win_count += 1
        else:
            w.loss_count += 1

        # 只保留最近20笔记录
        if len(w.recent_returns) > 20:
            w.recent_returns = w.recent_returns[-20:]

        # 动态调整权重
        self._adjust_weights()

    def _adjust_weights(self) -> None:
        """根据近期表现调整权重。

        规则：
        - 近期胜率 > 60%：权重 +20%
        - 近期胜率 < 30%：权重 -30%
        - 连续亏损5次：禁用该策略
        - 禁用后连续盈利3次：恢复
        """
        for name, w in self._weights.items():
            if w.total_signals < 5:
                continue

            recent = w.recent_returns[-10:] if len(w.recent_returns) >= 10 else w.recent_returns
            win_rate = sum(1 for r in recent if r > 0) / len(recent) if recent else 0

            if win_rate > 0.6:
                w.weight *= 1.2
                logger.info(f"策略 {name} 胜率 {win_rate:.0%}，权重提升至 {w.weight:.3f}")
            elif win_rate < 0.3:
                w.weight *= 0.7
                logger.info(f"策略 {name} 胜率 {win_rate:.0%}，权重降低至 {w.weight:.3f}")

            # 连续亏损检查
            consecutive_losses = 0
            for r in reversed(w.recent_returns):
                if r <= 0:
                    consecutive_losses += 1
                else:
                    break
            if consecutive_losses >= 5:
                w.enabled = False
                logger.warning(f"策略 {name} 连续亏损{consecutive_losses}次，已禁用")

            # 恢复检查
            consecutive_wins = 0
            for r in reversed(w.recent_returns):
                if r > 0:
                    consecutive_wins += 1
                else:
                    break
            if not w.enabled and consecutive_wins >= 3:
                w.enabled = True
                logger.info(f"策略 {name} 连续盈利{consecutive_wins}次，已恢复")

        # 归一化权重
        enabled_total = sum(v.weight for v in self._weights.values() if v.enabled)
        if enabled_total > 0:
            for v in self._weights.values():
                if v.enabled:
                    v.weight /= enabled_total

    def get_stats(self) -> dict[str, dict[str, Any]]:
        """获取策略统计信息"""
        stats = {}
        for name, w in self._weights.items():
            recent = w.recent_returns[-10:] if w.recent_returns else []
            win_rate = sum(1 for r in recent if r > 0) / len(recent) if recent else 0
            stats[name] = {
                "weight": w.weight,
                "enabled": w.enabled,
                "total_signals": w.total_signals,
                "win_count": w.win_count,
                "loss_count": w.loss_count,
                "recent_win_rate": round(win_rate, 2),
            }
        return stats