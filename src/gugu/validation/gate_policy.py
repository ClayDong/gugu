"""统一策略准入门禁。

该模块是验证脚本、参数优化和启动一致性检查共用的唯一阈值来源。
门禁对缺失、非法和非有限指标一律失败，避免调用方因默认值而误放行。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

GATE_VERSION = "gate-v1"


@dataclass(frozen=True)
class GateRule:
    """单项门禁规则。"""

    threshold: float
    direction: str
    label: str
    strict: bool = False

    def check(self, value: Any) -> bool:
        """判断一个指标是否通过该规则。"""
        if value is None:
            return False
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(numeric):
            return False
        if self.direction == "higher":
            return numeric > self.threshold if self.strict else numeric >= self.threshold
        if self.direction == "lower":
            return numeric < self.threshold if self.strict else numeric <= self.threshold
        raise ValueError(f"未知门禁方向: {self.direction}")


class GatePolicy:
    """策略准入门禁的版本化策略。"""

    VERSION = GATE_VERSION

    # 完整准入报告使用的规则，供 validate_strategy_gate.py 调用。
    FULL_RULES: dict[str, GateRule] = {
        "total_trades": GateRule(100, "higher", "总交易次数 ≥ 100"),
        "avg_return": GateRule(0.0, "higher", "平均收益 ≥ 0"),
        "median_return": GateRule(0.0, "higher", "收益中位数 ≥ 0"),
        "avg_sharpe": GateRule(0.0, "higher", "平均 Sharpe > 0", strict=True),
        "max_drawdown": GateRule(0.25, "lower", "最大回撤 < 25%", strict=True),
        "profitable_ratio": GateRule(0.50, "higher", "盈利策略比例 ≥ 50%"),
        "param_stability": GateRule(0.80, "higher", "参数稳定性 ≥ 80%"),
        "paper_signals": GateRule(30, "higher", "paper 信号数 ≥ 30"),
        "paper_return": GateRule(0.0, "higher", "paper 收益 ≥ 0"),
    }

    # 参数写回和实验策略升级使用的最小样本外门禁。
    WRITEBACK_RULES: dict[str, GateRule] = {
        "avg_sharpe": FULL_RULES["avg_sharpe"],
        "param_stability": FULL_RULES["param_stability"],
        "max_drawdown": FULL_RULES["max_drawdown"],
        "avg_return": FULL_RULES["avg_return"],
        "oos_trades": GateRule(30, "higher", "样本外交易次数 ≥ 30"),
    }

    @classmethod
    def evaluate(
        cls,
        metrics: dict[str, Any],
        rules: dict[str, GateRule] | None = None,
    ) -> dict[str, Any]:
        """按指定规则评估指标，缺失指标视为失败。"""
        active_rules = cls.FULL_RULES if rules is None else rules
        checks: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        for key, rule in active_rules.items():
            value = metrics.get(key)
            passed = rule.check(value)
            checks[key] = {
                "value": value,
                "threshold": rule.threshold,
                "direction": rule.direction,
                "strict": rule.strict,
                "passed": passed,
            }
            if not passed:
                failed.append(key)
        return {
            "version": cls.VERSION,
            "passed": not failed,
            "failed_checks": failed,
            "checks": checks,
        }

    @classmethod
    def evaluate_writeback(cls, metrics: dict[str, Any]) -> dict[str, Any]:
        """评估参数写回/实验策略升级所需的样本外门禁。"""
        return cls.evaluate(metrics, cls.WRITEBACK_RULES)

    @classmethod
    def thresholds(cls, rules: dict[str, GateRule] | None = None) -> dict[str, float]:
        """返回兼容旧脚本的纯阈值映射。"""
        return {key: rule.threshold for key, rule in (rules or cls.FULL_RULES).items()}

    @classmethod
    def rules(cls, keys: Iterable[str] | None = None) -> dict[str, GateRule]:
        """按键返回规则副本，供报告格式化使用。"""
        if keys is None:
            return dict(cls.FULL_RULES)
        return {key: cls.FULL_RULES[key] for key in keys}
