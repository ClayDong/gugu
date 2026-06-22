"""信号路由：多策略信号融合。

融合规则：
- unanimous: 全票通过
- majority: 多数通过
- any: 任一策略触发
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from gugu.config import settings
from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class SignalRouter:
    """多策略信号融合器。"""

    def __init__(self, strategies: list[Strategy] | None = None) -> None:
        cfg = settings().get("strategy", {})
        self._fusion_rule = cfg.get("signal_fusion", "majority")
        self._min_confidence = float(cfg.get("min_confidence", 0.6))
        self._strategies = strategies or []

    def add_strategy(self, strategy: Strategy) -> None:
        """添加策略。"""
        self._strategies.append(strategy)

    def route(
        self, df: pd.DataFrame, symbol: str, name: str = ""
    ) -> dict[str, Any] | None:
        """对单只股票融合多策略信号。

        Args:
            df: 行情数据
            symbol: 股票代码
            name: 股票名称（可选，用于飞书卡片展示）

        Returns:
            融合后的信号 dict，无信号返回 None
            {
                "symbol": str,
                "name": str,
                "direction": "buy"/"sell"/"hold",
                "confidence": float,
                "strategy": str,           # 触发策略字符串（逗号分隔）
                "strategies": [触发策略名],
                "reason": str,
            }
        """
        if df.empty or len(df) < 30:
            return None

        votes: list[tuple[str, int, float]] = []  # (strategy_name, signal, confidence)

        for strategy in self._strategies:
            try:
                result = strategy.generate_signals(df)
                if result.empty:
                    continue
                last = result.iloc[-1]
                sig = int(last.get("signal", 0))
                conf = float(last.get("confidence", 0))
                if sig != 0:
                    votes.append((strategy.name, sig, conf))
            except Exception as e:
                logger.warning(f"策略 {strategy.name} 在 {symbol} 执行失败: {e}")

        if not votes:
            return None

        # 统计买卖票数（仅统计实际触发投票的策略）
        buy_votes = [(n, c) for n, s, c in votes if s == 1]
        sell_votes = [(n, c) for n, s, c in votes if s == -1]
        total_votes = len(buy_votes) + len(sell_votes)

        direction = "hold"
        triggered = []

        if self._fusion_rule == "unanimous":
            # 所有投票策略方向一致
            if total_votes > 0 and len(buy_votes) == total_votes:
                direction = "buy"
                triggered = [n for n, _ in buy_votes]
            elif total_votes > 0 and len(sell_votes) == total_votes:
                direction = "sell"
                triggered = [n for n, _ in sell_votes]
        elif self._fusion_rule == "majority":
            # 多数投票策略同方向
            if total_votes > 0 and len(buy_votes) > total_votes / 2:
                direction = "buy"
                triggered = [n for n, _ in buy_votes]
            elif total_votes > 0 and len(sell_votes) > total_votes / 2:
                direction = "sell"
                triggered = [n for n, _ in sell_votes]
        else:  # any
            if buy_votes:
                direction = "buy"
                triggered = [n for n, _ in buy_votes]
            elif sell_votes:
                direction = "sell"
                triggered = [n for n, _ in sell_votes]

        if direction == "hold":
            return None

        # 置信度：获胜方向策略的平均置信度
        chosen_votes = buy_votes if direction == "buy" else sell_votes
        avg_conf = sum(c for _, c in chosen_votes) / len(chosen_votes) if chosen_votes else 0

        if avg_conf < self._min_confidence:
            logger.info(
                f"{symbol} 信号 {direction} 置信度 {avg_conf:.2f} < {self._min_confidence}，过滤"
            )
            return None

        return {
            "symbol": symbol,
            "name": name,
            "direction": direction,
            "confidence": round(avg_conf, 3),
            "strategy": ",".join(triggered),
            "strategies": triggered,
            "reason": f"策略 {','.join(triggered)} 触发 {direction} 信号",
        }
