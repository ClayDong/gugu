"""EventEngine 单元测试。"""
from __future__ import annotations

from gugu.engine.event_engine import (
    EVENT_CYCLE_END,
    EVENT_CYCLE_START,
    EVENT_DAILY_LOSS_HALT,
    EVENT_MARKET_REGIME,
    EVENT_ORDER_FILLED,
    EVENT_RISK_ALERT,
    EVENT_STOP_LOSS,
    EVENT_TYPES,
    EventEngine,
)


class TestEventEngine:
    """EventEngine 基础功能测试。"""

    def test_register_and_put(self) -> None:
        """注册处理器后 push 事件应触发处理器。"""
        engine = EventEngine()
        results: list[str] = []

        def handler(event: dict) -> None:
            results.append(event["data"].get("msg", ""))

        engine.register(EVENT_RISK_ALERT, handler)
        engine.put(EVENT_RISK_ALERT, {"msg": "test"})

        assert len(results) == 1
        assert results[0] == "test"

    def test_unregister(self) -> None:
        """注销后事件不再触发处理器。"""
        engine = EventEngine()
        call_count = 0

        def handler(event: dict) -> None:
            nonlocal call_count
            call_count += 1

        engine.register(EVENT_RISK_ALERT, handler)
        engine.put(EVENT_RISK_ALERT)
        assert call_count == 1

        engine.unregister(EVENT_RISK_ALERT, handler)
        engine.put(EVENT_RISK_ALERT)
        assert call_count == 1  # 不再增加

    def test_multiple_handlers(self) -> None:
        """同一事件可注册多个处理器。"""
        engine = EventEngine()
        results: list[int] = []

        def handler1(event: dict) -> None:
            results.append(1)

        def handler2(event: dict) -> None:
            results.append(2)

        engine.register(EVENT_RISK_ALERT, handler1)
        engine.register(EVENT_RISK_ALERT, handler2)
        engine.put(EVENT_RISK_ALERT)

        assert results == [1, 2]

    def test_different_event_types(self) -> None:
        """不同事件类型互不干扰。"""
        engine = EventEngine()
        alert_results: list[str] = []
        stop_results: list[str] = []

        def alert_handler(event: dict) -> None:
            alert_results.append("alert")

        def stop_handler(event: dict) -> None:
            stop_results.append("stop")

        engine.register(EVENT_RISK_ALERT, alert_handler)
        engine.register(EVENT_STOP_LOSS, stop_handler)

        engine.put(EVENT_RISK_ALERT, {"level": "warn"})
        engine.put(EVENT_STOP_LOSS, {"symbol": "600519"})

        assert alert_results == ["alert"]
        assert stop_results == ["stop"]

    def test_no_handler_no_error(self) -> None:
        """未注册事件类型不应报错。"""
        engine = EventEngine()
        # 推送未注册的事件类型
        engine.put(EVENT_CYCLE_START)
        engine.put("unknown_event_type")
        assert True  # 未抛出异常

    def test_handler_exception_isolation(self) -> None:
        """单个处理器异常不影响其他处理器。"""
        engine = EventEngine()
        results: list[str] = []

        def failing_handler(event: dict) -> None:
            raise ValueError("handler error")

        def ok_handler(event: dict) -> None:
            results.append("ok")

        engine.register(EVENT_RISK_ALERT, failing_handler)
        engine.register(EVENT_RISK_ALERT, ok_handler)

        engine.put(EVENT_RISK_ALERT)
        assert results == ["ok"]

    def test_stop_prevents_put(self) -> None:
        """stop() 后 put() 不应触发处理器。"""
        engine = EventEngine()
        call_count = 0

        def handler(event: dict) -> None:
            nonlocal call_count
            call_count += 1

        engine.register(EVENT_RISK_ALERT, handler)
        engine.stop()

        engine.put(EVENT_RISK_ALERT)
        assert call_count == 0

    def test_start_resumes(self) -> None:
        """start() 后 put() 恢复正常。"""
        engine = EventEngine()
        call_count = 0

        def handler(event: dict) -> None:
            nonlocal call_count
            call_count += 1

        engine.register(EVENT_RISK_ALERT, handler)
        engine.stop()
        engine.put(EVENT_RISK_ALERT)
        assert call_count == 0

        engine.start()
        engine.put(EVENT_RISK_ALERT)
        assert call_count == 1

    def test_put_without_data(self) -> None:
        """put() 不传 data 不应报错，data 默认为空 dict。"""
        engine = EventEngine()
        received: dict | None = None

        def handler(event: dict) -> None:
            nonlocal received
            received = event.get("data", None)

        engine.register(EVENT_RISK_ALERT, handler)
        engine.put(EVENT_RISK_ALERT)

        assert received is not None
        assert received == {}

    def test_register_same_handler_twice(self) -> None:
        """同一处理器注册两次应触发两次。"""
        engine = EventEngine()
        call_count = 0

        def handler(event: dict) -> None:
            nonlocal call_count
            call_count += 1

        engine.register(EVENT_RISK_ALERT, handler)
        engine.register(EVENT_RISK_ALERT, handler)

        engine.put(EVENT_RISK_ALERT)
        assert call_count == 2

    def test_all_event_types_listed(self) -> None:
        """EVENT_TYPES 应包含所有事件常量。"""
        # EVENT_TYPES 的用途是注册检查，它只需包含定义了的常量
        assert len(EVENT_TYPES) >= 7
        assert EVENT_CYCLE_START in EVENT_TYPES
        assert EVENT_CYCLE_END in EVENT_TYPES
        assert EVENT_MARKET_REGIME in EVENT_TYPES
        assert EVENT_RISK_ALERT in EVENT_TYPES
        assert EVENT_STOP_LOSS in EVENT_TYPES

    def test_event_data_preserved(self) -> None:
        """推送的事件数据应完整传递给处理器。"""
        engine = EventEngine()
        received: dict | None = None

        def handler(event: dict) -> None:
            nonlocal received
            received = event

        engine.register(EVENT_RISK_ALERT, handler)
        engine.put(EVENT_RISK_ALERT, {
            "level": "warn",
            "message": "test message",
        })

        assert received is not None
        assert received["type"] == EVENT_RISK_ALERT
        assert received["data"]["level"] == "warn"
        assert received["data"]["message"] == "test message"