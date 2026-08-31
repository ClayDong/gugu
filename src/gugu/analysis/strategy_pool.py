"""策略池管理：多策略权重管理，参考 qlib 的 TopkDropoutStrategy.

核心功能：
1. 策略权重管理：根据近期表现动态调整各策略权重
2. 信号验证：多个策略交叉验证，减少假信号
3. 策略失效降级：连续表现差的策略自动大幅降权（但不禁用，保留恢复可能）
4. 权重持久化：重启后恢复权重状态，防止重复训练期
5. 信号加权：多策略信号按权重加权融合
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gugu.config import PROJECT_ROOT, get_config
from gugu.utils.atomic import atomic_write_json
from gugu.utils.log import get_logger

logger = get_logger()

# 权重调整参数
MIN_WEIGHT = 0.05  # 策略最低权重（v0.8.1 修复：原 0.01 过低，导致单策略可垄断 94% 权重）
MAX_WEIGHT = 0.40  # v0.8.1 新增：单策略权重上限，防止动态调整塌缩（mean_revert 曾占 94.5%）
WIN_RATE_BOOST = 1.2  # 高胜率权重提升
WIN_RATE_CUT = 0.7  # 低胜率权重降低
DEGRADE_FACTOR = 0.05  # 连续亏损时的降级因子
RECOVER_FACTOR = 1.5  # 连续盈利时的恢复因子
CONSECUTIVE_LOSS_THRESHOLD = 5  # 触发降级的连续亏损次数
CONSECUTIVE_WIN_THRESHOLD = 3  # 触发恢复的连续盈利次数


@dataclass
class StrategyWeight:
    """策略权重。"""

    name: str
    weight: float  # 当前权重 0-1
    enabled: bool = True  # 保留字段以兼容历史调用，但实际不再禁用
    initial_weight: float = 0.0  # 初始权重（用于恢复上限）
    win_count: int = 0  # 近期盈利次数
    loss_count: int = 0  # 近期亏损次数
    total_signals: int = 0  # 总信号数
    recent_returns: list[float] = field(default_factory=list)  # 近期收益记录


class StrategyPool:
    """策略池管理器。

    对标 qlib 的 TopkDropoutStrategy：
    - 维护策略权重
    - 根据近期表现动态调整
    - 支持信号加权融合
    - 策略失效时大幅降权但不禁用（尊重用户"不删除策略"的要求）
    - 权重持久化到 data/strategy_pool_weights.json
    """

    _WEIGHTS_FILE = Path(PROJECT_ROOT) / "data" / "strategy_pool_weights.json"
    _PAPER_WEIGHTS_FILE = Path(PROJECT_ROOT) / "data" / "paper_strategy_pool_weights.json"
    _PRODUCTION_MODES = {"track_live", "signal_only", "live"}

    def __init__(self, execution_mode: str | None = None):
        self._weights: dict[str, StrategyWeight] = {}
        self._persisted_weights: dict[str, float] = {}
        self._execution_mode = execution_mode
        self._weights_file = self._get_weights_file()
        self._load_weights()
        self._init_weights()

    def _get_weights_file(self) -> Path:
        """选择与执行模式隔离的权重文件。"""
        if self._execution_mode == "paper":
            return self._PAPER_WEIGHTS_FILE
        return self._WEIGHTS_FILE

    def _is_production_mode(self) -> bool:
        return self._execution_mode in self._PRODUCTION_MODES

    def _load_weights(self) -> None:
        """从文件加载持久化权重。"""
        if not self._weights_file.exists():
            return
        try:
            with self._weights_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            weights = data.get("weights", {})
            self._persisted_weights = {
                k: float(v) for k, v in weights.items() if isinstance(v, (int, float))
            }
            logger.debug("加载持久化策略权重: {}", list(self._persisted_weights.keys()))
        except Exception as e:
            logger.warning("加载策略权重文件失败: {}", e)
            self._persisted_weights = {}

    def _save_weights(self) -> None:
        """持久化当前权重到文件。"""
        try:
            self._weights_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "weights": {name: round(w.weight, 6) for name, w in self._weights.items()},
                "updated_at": datetime.now().isoformat(),
                "execution_mode": self._execution_mode or "legacy",
            }
            atomic_write_json(self._weights_file, data)
        except Exception as e:
            logger.warning("保存策略权重文件失败: {}", e)

    def _init_weights(self) -> None:
        """初始化策略权重（从配置读取，并用持久化权重覆盖）。"""
        config = get_config().strategy
        verified = set(getattr(config, "verified_strategies", None) or [])
        experimental = set(getattr(config, "experimental_strategies", None) or [])
        weights = getattr(config, "weights", {}) if config else {}
        exp_weights = getattr(config, "experimental_weights", {}) if config else {}

        if self._is_production_mode():
            # 生产/监控模式只管理 verified，避免实验策略进入核心融合或污染生产持久化权重。
            names = verified
            initial_weights = weights
        elif self._execution_mode == "paper":
            # paper 使用独立权重文件，可同时观察 verified 与 experimental。
            names = verified | experimental
            initial_weights = {**weights, **exp_weights}
        else:
            # 保留未传 execution_mode 的旧调用语义，兼容独立分析和历史测试。
            names = set(getattr(config, "enabled", None) or []) | verified | experimental
            initial_weights = {**weights, **exp_weights}

        if not names:
            self._normalize_weights()
            return

        default_weight = 1.0 / len(names)
        for name in names:
            initial = float(initial_weights.get(name, default_weight))
            persisted = self._persisted_weights.get(name)
            weight = persisted if persisted is not None else initial
            self._weights[name] = StrategyWeight(
                name=name,
                weight=weight,
                enabled=True,
                initial_weight=initial,
            )

        self._normalize_weights()

    def _normalize_weights(self) -> None:
        """归一化权重，并保证下限与上限（v0.8.1 修复权重塌缩）。"""
        if not self._weights:
            return
        if len(self._weights) == 1:
            next(iter(self._weights.values())).weight = 1.0
            return
        total = sum(max(v.weight, 0.0) for v in self._weights.values())
        if total <= 0:
            base = 1.0 / len(self._weights)
            for v in self._weights.values():
                v.weight = base
            return

        values = [max(v.weight, 0.0) / total for v in self._weights.values()]
        count = len(values)
        result = [MIN_WEIGHT] * count
        remaining = 1.0 - MIN_WEIGHT * count
        free = set(range(count))
        while free:
            free_total = sum(values[i] for i in free)
            changed = False
            for i in tuple(free):
                share = remaining * values[i] / free_total if free_total else remaining / len(free)
                if MIN_WEIGHT + share > MAX_WEIGHT:
                    result[i] = MAX_WEIGHT
                    remaining -= MAX_WEIGHT - MIN_WEIGHT
                    free.remove(i)
                    changed = True
            if not changed:
                for i in free:
                    result[i] += (
                        remaining * values[i] / free_total if free_total else remaining / len(free)
                    )
                break

        for value, weight in zip(result, self._weights.values(), strict=True):
            weight.weight = value

    def get_weights(self) -> dict[str, float]:
        """获取当前策略权重。"""
        if not self._weights:
            return {}
        return {k: v.weight for k, v in self._weights.items()}

    def get_enabled_names(self) -> list[str]:
        """获取启用的策略名称列表（所有策略均启用）。"""
        return list(self._weights.keys())

    def weighted_fusion(
        self,
        signals: list[dict[str, Any]],
        override_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """加权融合多个策略信号。

        Args:
            signals: 各策略的信号列表，每个包含 direction/confidence/strategy_name
            override_weights: 临时权重覆盖（不修改内部状态），用于市场状态自适应调整

        Returns:
            dict: 融合后的信号（direction/confidence/strategies/weighted_score）
        """
        weights = override_weights if override_weights is not None else self.get_weights()
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
            sig_conf = sig.get("confidence")
            conf = float(sig_conf) if sig_conf is not None else 0.0
            if sig.get("direction") == "buy":
                buy_score += conf * w
            elif sig.get("direction") == "sell":
                sell_score += conf * w
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

        规则（降权不删除）：
        - 近期胜率 > 60%：权重 +20%
        - 近期胜率 < 30%：权重 -30%
        - 连续亏损 5 次：权重 ×0.1（大幅降级，但保留最低权重）
        - 连续盈利 3 次：权重 ×1.5（逐步恢复，上限为初始权重的 2 倍）
        """
        for name, w in self._weights.items():
            if w.total_signals < 5:
                continue

            recent = w.recent_returns[-10:] if len(w.recent_returns) >= 10 else w.recent_returns
            win_rate = sum(1 for r in recent if r > 0) / len(recent) if recent else 0

            if win_rate > 0.6:
                w.weight = min(w.weight * WIN_RATE_BOOST, MAX_WEIGHT)  # v0.8.1: 钳制上限
                logger.info("策略 {} 胜率 {:.0%}，权重提升至 {:.3f}", name, win_rate, w.weight)
            elif win_rate < 0.3:
                w.weight = max(w.weight * WIN_RATE_CUT, MIN_WEIGHT)  # v0.8.1: 钳制下限
                logger.info("策略 {} 胜率 {:.0%}，权重降低至 {:.3f}", name, win_rate, w.weight)

            # 连续亏损检查：大幅降权但不禁用
            consecutive_losses = 0
            for r in reversed(w.recent_returns):
                if r <= 0:
                    consecutive_losses += 1
                else:
                    break
            if consecutive_losses >= CONSECUTIVE_LOSS_THRESHOLD:
                old_weight = w.weight
                # 基于初始权重降级，避免之前的高胜率 boost 稀释降级效果
                base = min(w.weight, w.initial_weight)
                w.weight = max(base * DEGRADE_FACTOR, MIN_WEIGHT)
                logger.warning(
                    "策略 {} 连续亏损{}次，已降级（权重 {:.3f} → {:.3f}，仍保留）",
                    name,
                    consecutive_losses,
                    old_weight,
                    w.weight,
                )

            # 恢复检查：连续盈利逐步恢复
            consecutive_wins = 0
            for r in reversed(w.recent_returns):
                if r > 0:
                    consecutive_wins += 1
                else:
                    break
            if consecutive_wins >= CONSECUTIVE_WIN_THRESHOLD:
                old_weight = w.weight
                upper = min(
                    w.initial_weight * 2.0, MAX_WEIGHT
                )  # v0.8.1: 恢复上限受 MAX_WEIGHT 约束
                w.weight = min(w.weight * RECOVER_FACTOR, upper)
                logger.info(
                    "策略 {} 连续盈利{}次，权重恢复（{:.3f} → {:.3f}）",
                    name,
                    consecutive_wins,
                    old_weight,
                    w.weight,
                )

        self._normalize_weights()
        self._save_weights()

    def get_stats(self) -> dict[str, dict[str, Any]]:
        """获取策略统计信息。"""
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
