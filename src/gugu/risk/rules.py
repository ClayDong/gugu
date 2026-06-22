"""Risk rules constants and data classes.

Three-level risk control for A-share trading:
- L1: Single position limit (<= 30% per stock by default)
- L2: Daily loss circuit breaker (warn at 3%, halt at 5% by default)
- L3: System rules (T+1 settlement, price limits, suspension)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from gugu.models import Position

__all__ = ["RiskLevel", "RiskAction", "RiskCheckResult", "Position"]


class RiskLevel(Enum):
    """Risk check level."""

    L1_POSITION = "L1_POSITION"
    L2_DAILY_LOSS = "L2_DAILY_LOSS"
    L3_SYSTEM = "L3_SYSTEM"


class RiskAction(Enum):
    """Action taken by a risk check.

    ALLOW: order is approved
    WARN: order is approved with a warning (e.g. daily loss approaching halt)
    HALT: order is rejected (iron law, cannot be bypassed)
    """

    ALLOW = "ALLOW"
    WARN = "WARN"
    HALT = "HALT"


@dataclass
class RiskCheckResult:
    """Result of a risk check.

    Attributes:
        level: Which risk level triggered this result.
        action: ALLOW / WARN / HALT.
        message: Human-readable detail for logging and notification.
    """

    level: RiskLevel
    action: RiskAction
    message: str

    @property
    def allowed(self) -> bool:
        """Whether the order may proceed. WARN is allowed, HALT is not."""
        return self.action != RiskAction.HALT
