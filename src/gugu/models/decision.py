"""统一决策合同与入场门禁。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DataQuality = Literal["fresh", "stale", "partial", "invalid", "unavailable"]
DecisionStatus = Literal[
    "candidate",
    "blocked",
    "approved",
    "notified",
    "executed",
    "expired",
    "closed",
    "evaluated",
]
DecisionAction = Literal["buy", "sell", "hold", "observe", "blocked"]
DecisionType = Literal["entry", "exit", "observe"]


@dataclass
class DecisionContract:
    """可序列化、可审计的决策状态。"""

    symbol: str = ""
    decision_type: DecisionType = "entry"
    action: DecisionAction = "observe"
    status: DecisionStatus = "candidate"
    data_quality: DataQuality = "unavailable"
    data_timestamp: str = ""
    data_sources: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    entry_condition: str = ""
    invalidation_condition: str = ""
    expires_at: str = ""
    execution_status: str = "pending"
    outcome_status: str = "pending"
    outcome_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed_to_trade(self) -> bool:
        return self.status == "approved" and self.action in {"buy", "sell"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "decision_type": self.decision_type,
            "action": self.action,
            "status": self.status,
            "data_quality": self.data_quality,
            "data_timestamp": self.data_timestamp,
            "data_sources": list(self.data_sources),
            "block_reasons": list(self.block_reasons),
            "entry_condition": self.entry_condition,
            "invalidation_condition": self.invalidation_condition,
            "expires_at": self.expires_at,
            "execution_status": self.execution_status,
            "outcome_status": self.outcome_status,
            "outcome_metrics": dict(self.outcome_metrics),
            "allowed_to_trade": self.allowed_to_trade,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionContract:
        return cls(
            symbol=str(data.get("symbol", "")),
            decision_type=data.get("decision_type", "entry"),
            action=data.get("action", "observe"),
            status=data.get("status", "candidate"),
            data_quality=data.get("data_quality", "unavailable"),
            data_timestamp=str(data.get("data_timestamp", "")),
            data_sources=list(data.get("data_sources", [])),
            block_reasons=list(data.get("block_reasons", [])),
            entry_condition=str(data.get("entry_condition", "")),
            invalidation_condition=str(data.get("invalidation_condition", "")),
            expires_at=str(data.get("expires_at", "")),
            execution_status=str(data.get("execution_status", "pending")),
            outcome_status=str(data.get("outcome_status", "pending")),
            outcome_metrics=dict(data.get("outcome_metrics", {})),
        )


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def evaluate_entry_gate(signal: Any, perspective_result: Any = None) -> DecisionContract:
    """统一评估买入入场资格，缺少可信证据时保守阻断。"""
    nested_decision = _get_value(signal, "decision", {}) or {}
    symbol = str(_get_value(signal, "symbol", ""))
    direction = _get_value(signal, "direction", "")
    data_quality = _get_value(nested_decision, "data_quality", None)
    if data_quality is None:
        data_quality = _get_value(signal, "data_quality", "unavailable")
    if data_quality not in {"fresh", "stale", "partial", "invalid", "unavailable"}:
        data_quality = "invalid"

    contract = DecisionContract(symbol=symbol, data_quality=data_quality)
    if direction != "buy":
        return contract

    if data_quality != "fresh":
        contract.status = "blocked"
        contract.block_reasons.append("数据状态不是 fresh")
        return contract

    if perspective_result is None:
        contract.status = "blocked"
        contract.block_reasons.append("缺少十大视角评分")
        return contract

    error_count = _get_value(perspective_result, "error_count", 0) or 0
    scores = _get_value(perspective_result, "scores", []) or []
    has_score_error = any(_get_value(score, "status", "ok") == "error" for score in scores)
    if (
        error_count > 0
        or _get_value(perspective_result, "status", "ok") == "error"
        or has_score_error
    ):
        contract.status = "blocked"
        contract.block_reasons.append("存在视角评分异常")
    if not bool(_get_value(perspective_result, "passed", False)):
        contract.status = "blocked"
        contract.block_reasons.append("十大视角未通过统一门禁")
    if contract.status == "blocked":
        return contract

    contract.action = "buy"
    contract.status = "approved"
    return contract
