"""事件驱动引擎：基于 vn.py EventEngine 模式的简化实现。

事件类型常量：
- EVENT_CYCLE_START / EVENT_CYCLE_END: 交易循环生命周期
- EVENT_MARKET_REGIME: 市场状态变更（bull/bear/sideways/crash/rally）
- EVENT_SIGNAL: 策略信号产生（buy/sell）
- EVENT_ORDER_SUBMITTED / EVENT_ORDER_FILLED: 订单生命周期
- EVENT_RISK_ALERT: 风控告警（L1/L2/L3）
- EVENT_STOP_LOSS: 止损触发
- EVENT_DAILY_LOSS_WARN / EVENT_DAILY_LOSS_HALT: 日亏预警/熔断
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from gugu.utils.log import get_logger

logger = get_logger()

HandlerType = Callable[[dict[str, Any]], None]

# ========== 事件类型常量 ==========

EVENT_CYCLE_START = "cycle_start"
"""交易循环开始。data: {timestamp}"""

EVENT_CYCLE_END = "cycle_end"
"""交易循环结束。data: {timestamp, status: str, positions_count: int}"""

EVENT_MARKET_REGIME = "market_regime_change"
"""市场状态变更。data: {regime: str, reason: str, confidence: float}"""

EVENT_SIGNAL = "signal_generated"
"""策略信号产生。data: {symbol, direction, confidence, strategies, ...}"""

EVENT_ORDER_SUBMITTED = "order_submitted"
"""订单已提交。data: {symbol, direction, quantity, price}"""

EVENT_ORDER_FILLED = "order_filled"
"""订单已成交。data: {symbol, direction, quantity, price, commission}"""

EVENT_RISK_ALERT = "risk_alert"
"""风控告警。data: {level: str, message: str, suggestion: str}"""

EVENT_STOP_LOSS = "stop_loss_triggered"
"""止损触发。data: {symbol, price, quantity}"""

EVENT_DAILY_LOSS_WARN = "daily_loss_warning"
"""日亏预警（亏损 >= warn_threshold）。data: {loss_pct: float}"""

EVENT_DAILY_LOSS_HALT = "daily_loss_halted"
"""日亏熔断（亏损 >= halt_threshold）。data: {loss_pct: float}"""

EVENT_TYPES = [
    EVENT_CYCLE_START, EVENT_CYCLE_END,
    EVENT_MARKET_REGIME, EVENT_SIGNAL,
    EVENT_ORDER_SUBMITTED, EVENT_ORDER_FILLED,
    EVENT_RISK_ALERT, EVENT_STOP_LOSS,
    EVENT_DAILY_LOSS_WARN, EVENT_DAILY_LOSS_HALT,
]
"""所有注册的事件类型列表。"""


class EventEngine:
    """事件驱动引擎。

    注册事件类型和处理函数，通过 put() 分发事件。
    线程不安全，设计用于单线程异步环境。

    用法：:

        engine = EventEngine()
        engine.register(EVENT_RISK_ALERT, my_handler)
        engine.put(EVENT_RISK_ALERT, {"level": "warn", "message": "..."})
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[HandlerType]] = defaultdict(list)
        self._active = True

    def register(self, event_type: str, handler: HandlerType) -> None:
        """注册事件处理器。

        Args:
            event_type: 事件类型（建议使用 EVENT_* 常量）
            handler: 处理函数，签名为 handler(event: dict) -> None
        """
        self._handlers[event_type].append(handler)
        logger.debug(f"事件处理器注册: {event_type} -> {handler.__name__}")

    def unregister(self, event_type: str, handler: HandlerType) -> None:
        """注销事件处理器。"""
        if handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)

    def put(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """推送事件，同步触发所有注册的处理器。

        Args:
            event_type: 事件类型（建议使用 EVENT_* 常量）
            data: 事件数据

        Note:
            处理器按注册顺序同步执行。单个处理器抛异常不影响其他处理器。
        """
        if not self._active:
            return
        event = {"type": event_type, "data": data or {}}
        for handler in self._handlers.get(event_type, []):
            try:
                handler(event)
            except Exception as e:
                logger.error(f"事件处理异常 [{event_type}]: {e}")

    def start(self) -> None:
        """启动事件引擎。"""
        self._active = True
        logger.info("事件引擎已启动")

    def stop(self) -> None:
        """停止事件引擎。"""
        self._active = False
        logger.info("事件引擎已停止")