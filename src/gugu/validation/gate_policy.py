"""统一策略准入门禁。

该模块是验证脚本、参数优化和启动一致性检查共用的唯一阈值来源。
门禁对缺失、非法和非有限指标一律失败，避免调用方因默认值而误放行。

gate-v2 变更（2026-09-10）：
- 新增 profit_factor ≥ 1.5（盈亏比，弥补只看胜率不看盈亏比的缺口）
- max_drawdown 按策略类型分层：短线(trend/breakout/mean_revert) < 15%，中长线(main_force) < 25%
- evaluate() 新增可选 category 参数，按类型覆盖 max_drawdown 规则

gate-v2 变更（2026-11-05）：
- 新增 selection_alpha > 0（选股 alpha）。仅要求「超额 > 0」不够：策略的每笔
  收益等价于其所选 (日期, 标的) 上的无条件收益，因此超额完全可以由「买对了
  日子」贡献——那是 beta 择时，不是选股能力。实测（39 只池、2024-02 起）三个
  在用策略的净alpha 为正/接近零，但拆开后选股alpha 全部为负：
  box_breakout -0.20%、linjudaye -0.06%、double_yin -0.15%。缺失该指标一律失败，
  不提供默认值，否则等于给「没测过」发通行证。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

GATE_VERSION = "gate-v2"

# 按策略类型的最大回撤阈值
_SHORT_TERM_CATEGORIES = {"trend", "breakout", "mean_revert"}
_SHORT_TERM_MAX_DRAWDOWN = 0.15
_LONG_TERM_MAX_DRAWDOWN = 0.25


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
        if self.direction == "lower" and numeric < 0:
            # 回撤等"越低越好"的指标应为非负幅值；负值（如 -0.24）会令
            # "数值 < 阈值" 规则假通过，视为数据错误直接判失败。
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
        "profit_factor": GateRule(1.5, "higher", "盈亏比 ≥ 1.5"),
        "param_stability": GateRule(0.80, "higher", "参数稳定性 ≥ 80%"),
        "paper_signals": GateRule(30, "higher", "paper 信号数 ≥ 30"),
        "paper_return": GateRule(0.0, "higher", "paper 收益 ≥ 0"),
        # 选股能力：扣掉「同池漂移 + 池子同期涨幅」之后，策略还必须真的挑中了
        # 更好的股票。用 strict 是刻意的：恰好等于 0 意味着与池子其他股票无差别，
        # 那是市场状态指示器，不是选股策略。
        "selection_alpha": GateRule(
            0.0, "higher", "选股alpha > 0（同日相对池子其余股票）", strict=True
        ),
    }

    # 参数写回和实验策略升级使用的最小样本外门禁。
    WRITEBACK_RULES: dict[str, GateRule] = {
        "avg_sharpe": FULL_RULES["avg_sharpe"],
        "param_stability": FULL_RULES["param_stability"],
        "max_drawdown": FULL_RULES["max_drawdown"],
        "avg_return": FULL_RULES["avg_return"],
        "profit_factor": FULL_RULES["profit_factor"],
        "oos_trades": GateRule(30, "higher", "样本外交易次数 ≥ 30"),
    }

    @classmethod
    def _resolve_rules(
        cls, rules: dict[str, GateRule] | None, category: str | None
    ) -> dict[str, GateRule]:
        """按策略类型覆盖 max_drawdown 规则。"""
        active = dict(cls.FULL_RULES if rules is None else rules)
        if category and category in _SHORT_TERM_CATEGORIES:
            active["max_drawdown"] = GateRule(
                _SHORT_TERM_MAX_DRAWDOWN,
                "lower",
                f"短线策略最大回撤 < {_SHORT_TERM_MAX_DRAWDOWN:.0%}",
                strict=True,
            )
        return active

    @classmethod
    def evaluate(
        cls,
        metrics: dict[str, Any],
        rules: dict[str, GateRule] | None = None,
        *,
        category: str | None = None,
    ) -> dict[str, Any]:
        """按指定规则评估指标，缺失指标视为失败。

        Args:
            metrics: 策略指标字典
            rules: 自定义规则集，None 时用 FULL_RULES
            category: 策略类型（trend/breakout/mean_revert/main_force），
                      用于按类型覆盖 max_drawdown 阈值
        """
        active_rules = cls._resolve_rules(rules, category)
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
            "category": category,
            "passed": not failed,
            "failed_checks": failed,
            "checks": checks,
        }

    @classmethod
    def evaluate_writeback(cls, metrics: dict[str, Any]) -> dict[str, Any]:
        """评估参数写回/实验策略升级所需的样本外门禁。"""
        return cls.evaluate(metrics, cls.WRITEBACK_RULES)

    @classmethod
    def classify_evidence(
        cls, verdict: dict[str, Any], *, sample_key: str | None = None, minimum: int = 30
    ) -> str:
        """区分通过、明确失败和样本不足，不降低任何 Gate 阈值。

        sample_key 为 None（默认）时走 manifest 兼容路径：优先取 oos_trades，
        缺失则回退到 total_trades——因为 manifest 派生裁决常缺 oos_trades，
        回退可避免把 manifest 派生裁决一律误判为 insufficient_evidence。
        显式传入 sample_key（如 "oos_trades"）且缺失时，则诚实返回
        insufficient_evidence，不静默回退。
        """
        checks = verdict.get("checks") or {}
        if sample_key is None:
            sample = checks.get("oos_trades", {}).get("value")
            if sample is None:
                sample = checks.get("total_trades", {}).get("value")
        else:
            sample = checks.get(sample_key, {}).get("value")
        try:
            if sample is None or float(sample) < minimum:
                return "insufficient_evidence"
        except (TypeError, ValueError):
            return "insufficient_evidence"
        return "passed" if verdict.get("passed") else "failed"

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
