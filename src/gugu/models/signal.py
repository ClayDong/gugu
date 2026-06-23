"""强类型交易信号数据模型。

替代所有模块间传递的 dict[str, Any] 信号，
提供 IDE 补全、运行时类型校验和默认值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Direction = Literal["buy", "sell"]
"""信号方向。"""
Action = Literal["buy", "sell", "hold", "filter"]
"""LLM/wisdom 决策动作。"""
FusionMode = Literal["any", "majority", "unanimous"]
"""信号融合模式。"""


@dataclass
class Signal:
    """交易信号（强类型版本）。

    所有模块通过此类型传递信号，替代游离的 dict。
    新增字段时在此添加类型注解即可获得 IDE 补全。
    """

    # ========== 核心字段 ==========
    symbol: str = ""
    direction: Direction = "buy"
    price: float = 0.0
    name: str = ""

    # ========== 策略信号 ==========
    strategy: str = ""
    strategies: list[str] = field(default_factory=list)
    reason: str = ""
    confidence: float = 1.0
    timestamp: str = ""

    # ========== 仓位相关 ==========
    suggested_position_ratio: float = 0.0
    has_position: bool = False
    current_position_ratio: float = 0.0
    stop_loss_price: float | None = None

    # ========== L3 元数据 ==========
    prev_close: float = 0.0
    is_st: bool = False
    is_suspended: bool = False

    # ========== 过滤链结果 ==========
    wisdom_filtered: bool = False
    filter_reason: str = ""
    fundamental: dict[str, Any] = field(default_factory=dict)
    money_flow: dict[str, Any] = field(default_factory=dict)
    industry_check: dict[str, Any] = field(default_factory=dict)

    # ========== 市场上下文 ==========
    market_context: dict[str, Any] = field(default_factory=dict)

    # ========== 决策层 ==========
    wisdom: dict[str, Any] = field(default_factory=dict)
    wisdom_decision: dict[str, Any] = field(default_factory=dict)

    # ========== 下单结果 ==========
    order_result: dict[str, Any] | None = None

    # ========== 序列化兼容 ==========

    def to_dict(self) -> dict[str, Any]:
        """转为 dict（兼容旧接口和 JSON 序列化）。"""
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "price": self.price,
            "name": self.name,
            "strategy": self.strategy,
            "strategies": list(self.strategies),
            "reason": self.reason,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "suggested_position_ratio": self.suggested_position_ratio,
            "has_position": self.has_position,
            "current_position_ratio": self.current_position_ratio,
            "stop_loss_price": self.stop_loss_price,
            "prev_close": self.prev_close,
            "is_st": self.is_st,
            "is_suspended": self.is_suspended,
            "wisdom_filtered": self.wisdom_filtered,
            "filter_reason": self.filter_reason,
            "fundamental": dict(self.fundamental),
            "money_flow": dict(self.money_flow),
            "industry_check": dict(self.industry_check),
            "market_context": dict(self.market_context),
            "wisdom": dict(self.wisdom),
            "wisdom_decision": dict(self.wisdom_decision),
            "order_result": self.order_result,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Signal:
        """从 dict 创建 Signal（兼容旧接口）。"""
        return cls(
            symbol=str(d.get("symbol", "")),
            direction=str(d.get("direction", "buy")),  # type: ignore[assignment]
            price=float(d.get("price", 0)),
            name=str(d.get("name", "")),
            strategy=str(d.get("strategy", "")),
            strategies=list(d.get("strategies", [])),
            reason=str(d.get("reason", "")),
            confidence=float(d.get("confidence", 1.0)),
            timestamp=str(d.get("timestamp", "")),
            suggested_position_ratio=float(d.get("suggested_position_ratio", 0)),
            has_position=bool(d.get("has_position", False)),
            current_position_ratio=float(d.get("current_position_ratio", 0)),
            stop_loss_price=(
                float(d["stop_loss_price"]) if d.get("stop_loss_price") is not None else None
            ),
            prev_close=float(d.get("prev_close", 0)),
            is_st=bool(d.get("is_st", False)),
            is_suspended=bool(d.get("is_suspended", False)),
            wisdom_filtered=bool(d.get("wisdom_filtered", False)),
            filter_reason=str(d.get("filter_reason", "")),
            fundamental=dict(d.get("fundamental", {})),
            money_flow=dict(d.get("money_flow", {})),
            industry_check=dict(d.get("industry_check", {})),
            market_context=dict(d.get("market_context", {})),
            wisdom=dict(d.get("wisdom", {})),
            wisdom_decision=dict(d.get("wisdom_decision", {})),
            order_result=d.get("order_result"),
        )


@dataclass
class OrderResult:
    """下单结果（强类型版本）。"""

    success: bool = False
    symbol: str = ""
    direction: str = ""
    quantity: int = 0
    price: float = 0.0
    commission: float = 0.0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "symbol": self.symbol,
            "direction": self.direction,
            "quantity": self.quantity,
            "price": self.price,
            "commission": self.commission,
            "message": self.message,
        }