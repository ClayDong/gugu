"""信号路由：多策略信号融合。

融合规则：
- unanimous: 全票通过
- majority: 多数通过
- any: 任一策略触发
- weighted: 加权融合（支持市场状态自适应权重）
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from gugu.analysis.regime_strategy_adjuster import compute_regime_weights
from gugu.analysis.strategy_pool import StrategyPool
from gugu.config import get_config
from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class SignalRouter:
    """多策略信号融合器。"""

    def __init__(
        self,
        strategies: list[Strategy] | None = None,
        fusion_rule: str | None = None,
        min_confidence: float | None = None,
        *,
        strategy_pool: StrategyPool | None = None,
    ) -> None:
        """初始化信号路由器。

        Args:
            strategies: 策略列表，默认从配置加载。
            fusion_rule: 融合规则（any/majority/unanimous），默认从配置读取。
            min_confidence: 最低置信度阈值，默认从配置读取。
            strategy_pool: 策略池（用于加权融合 + 动态调整），
                默认创建新实例。传入共享实例可实现策略表现跟踪。
                显式传入参数时优先于配置，便于测试注入（T-02 修复）。
        """
        cfg = get_config().strategy
        self._fusion_rule = fusion_rule or cfg.signal_fusion
        self._min_confidence = (
            float(min_confidence) if min_confidence is not None else cfg.min_confidence
        )
        self._strategies = strategies or []
        # P2 修复：集成 StrategyPool 实现加权融合（代替简单的 majority/any/unanimous）
        self._pool: StrategyPool | None = strategy_pool
        # 当 fusion_rule 显式传入 'weighted' 且提供了 pool 时启用加权融合
        if self._pool is None and self._fusion_rule == "weighted":
            self._pool = StrategyPool()

    def add_strategy(self, strategy: Strategy) -> None:
        """添加策略。"""
        self._strategies.append(strategy)

    def route(
        self,
        df: pd.DataFrame,
        symbol: str,
        name: str = "",
        regime: str | None = None,
    ) -> dict[str, Any] | None:
        """对单只股票融合多策略信号。

        Args:
            df: 行情数据
            symbol: 股票代码
            name: 股票名称（可选，用于飞书卡片展示）
            regime: 市场状态（bull/bear/sideways/crash/rally），
                   提供时启用市场状态自适应权重调整

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
                "regime": str,             # 市场状态（如有）
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

        # P0 修复：卖出硬否决（趋势破坏时不入场 — 《炒股的智慧》铁律）
        # 趋势类/突破类策略若发出 sell，说明趋势已破坏，禁止买入
        # 这些策略的 sell 信号覆盖任何 buy 信号
        _TREND_BREAKDOWN_STRATEGIES = {
            "turtle", "dual_ma", "chen_jiangting", "money_flow_follow",
            "box_breakout", "breakout",
        }
        sell_strategy_names = {n for n, _ in sell_votes}
        trend_breakdown = sell_strategy_names & _TREND_BREAKDOWN_STRATEGIES

        direction = "hold"
        triggered = []

        # P2 修复：weighted 模式使用 StrategyPool 加权融合
        if self._fusion_rule == "weighted" and self._pool is not None:
            # 市场状态自适应权重调整
            if regime:
                base_weights = self._pool.get_weights()
                adjusted_weights = compute_regime_weights(base_weights, regime)
                # P0 修复：深拷贝原始权重，防止就地修改 WeightEntry 导致并发数据竞争
                import copy as _copy
                original_weights = _copy.deepcopy(self._pool._weights)
                for name, w in self._pool._weights.items():
                    if name in adjusted_weights:
                        w.weight = adjusted_weights[name] * sum(
                            v.weight for v in original_weights.values()
                        )
                # 归一化
                total = sum(v.weight for v in self._pool._weights.values() if v.enabled)
                if total > 0:
                    for v in self._pool._weights.values():
                        if v.enabled:
                            v.weight /= total

            # 将 votes 转为 StrategyPool 可接受的信号格式
            pool_signals = [
                {"direction": "buy" if s == 1 else "sell", "confidence": c, "strategy_name": n}
                for n, s, c in votes
            ]
            pool_result = self._pool.weighted_fusion(pool_signals)

            # 恢复原始权重（避免影响后续交易的性能追踪）
            if regime:
                self._pool._weights = original_weights

            if pool_result["direction"] == "buy":
                direction = "buy"
                triggered = pool_result.get("strategies", ["weighted"])
                avg_conf = pool_result["confidence"]
            elif pool_result["direction"] == "sell":
                direction = "sell"
                triggered = pool_result.get("strategies", ["weighted"])
                avg_conf = pool_result["confidence"]
            else:
                return None

        elif self._fusion_rule == "unanimous":
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

        # P0 修复：卖出硬否决 — 趋势破坏时强制 sell，覆盖任何 buy
        # 即使加权融合结果是 buy，只要趋势类策略发出 sell，就强制改为 sell
        if direction == "buy" and trend_breakdown:
            logger.warning(
                f"{symbol} 趋势类策略 {trend_breakdown} 发出卖出信号，"
                f"强制覆盖 buy → sell（趋势破坏时不入场）"
            )
            direction = "sell"
            triggered = list(trend_breakdown) + [t for t in triggered if t not in trend_breakdown]
            # 卖出置信度取趋势策略的卖出置信度（如有）否则用原置信度
            trend_sell_conf = [c for n, c in sell_votes if n in trend_breakdown]
            if trend_sell_conf:
                avg_conf = sum(trend_sell_conf) / len(trend_sell_conf)

        # P0 修复：多策略共识折扣
        # 原逻辑：单策略触发时 avg_conf = 该策略 confidence（常为 1.0），无共识惩罚
        # 修复：单策略信号置信度打折，多策略共识才给高置信度
        # A股高噪声环境，单策略信号可靠性远低于多策略共识
        # 非加权模式：计算获胜方向的平均置信度
        if self._fusion_rule != "weighted":
            chosen_votes = buy_votes if direction == "buy" else sell_votes
            avg_conf = sum(c for _, c in chosen_votes) / len(chosen_votes) if chosen_votes else 0
        total_strategies = max(len(self._strategies), 1)
        agreeing_count = len(triggered)
        if total_strategies > 1:
            consensus_ratio = agreeing_count / total_strategies
            # 单策略: ratio=1/N → 折扣 0.7; 全策略共识: ratio=1.0 → 折扣 1.0
            consensus_factor = 0.7 + 0.3 * consensus_ratio
            avg_conf *= consensus_factor

        if avg_conf < self._min_confidence:
            logger.info(
                f"{symbol} 信号 {direction} 置信度 {avg_conf:.2f} < {self._min_confidence}，过滤"
            )
            return None

        result = {
            "symbol": symbol,
            "name": name,
            "direction": direction,
            "confidence": round(avg_conf, 3),
            "strategy": ",".join(triggered),
            "strategies": triggered,
            "reason": f"策略 {','.join(triggered)} 触发 {direction} 信号",
        }

        # 附加市场状态信息
        if regime:
            result["regime"] = regime

        return result
