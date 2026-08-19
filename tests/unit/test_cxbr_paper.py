"""CXBR 策略级模拟执行测试。"""

from __future__ import annotations

from pathlib import Path

from gugu.execution.cxbr_paper import CXBRMarketSnapshot, CXBRPaperExecutor
from gugu.research.cxbr_v1 import CXBRSignal


def _buy_signal() -> CXBRSignal:
    return CXBRSignal(
        action="buy",
        stage="entry",
        code="600001",
        asof="2026-08-18",
        reason="日线硬规则买点成立",
        buy_point="A",
        ma10=10.0,
        strict_stop_price=9.7,
        conditional_stop_price=9.6,
        suggested_position=0.20,
    )


def _sell_signal() -> CXBRSignal:
    return CXBRSignal(
        action="sell",
        stage="exit",
        code="600001",
        asof="2026-08-19",
        reason="连续两日收盘低于 MA10",
    )


def _executor(tmp_path: Path) -> CXBRPaperExecutor:
    return CXBRPaperExecutor(
        initial_capital=1_000_000,
        state_file=tmp_path / "cxbr_state.json",
        ledger_file=tmp_path / "cxbr_ledger.jsonl",
    )


def test_cxbr_uses_independent_state_and_records_fill(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.execute_signal(
        _buy_signal(),
        CXBRMarketSnapshot(price=10.0, prev_close=9.5, market_value_billion=45.0),
        100,
    )
    assert result is not None
    assert result.success
    assert result.event_type == "order_filled"
    assert result.quantity == 100
    assert result.fill_price > 10.0
    assert result.commission >= 5.0
    assert executor.broker.get_position("600001") is not None
    assert len(executor.executions) == 1


def test_cxbr_rejects_limit_up_with_real_prev_close(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.execute_signal(
        _buy_signal(), CXBRMarketSnapshot(price=10.45, prev_close=9.5), 100
    )
    assert result is not None
    assert not result.success
    assert "涨停" in result.message
    assert executor.broker.get_position("600001") is None


def test_cxbr_enforces_t_plus_1_and_deduplicates(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    snapshot = CXBRMarketSnapshot(price=10.0, prev_close=9.5)
    first = executor.execute_signal(_buy_signal(), snapshot, 100)
    assert first is not None and first.success
    duplicate = executor.execute_signal(_buy_signal(), snapshot, 100)
    assert duplicate is not None
    assert duplicate.event_type == "order_duplicate"

    sell = executor.execute_signal(
        _sell_signal(), CXBRMarketSnapshot(price=10.5, prev_close=10.0), 100
    )
    assert sell is not None
    assert not sell.success
    assert "T+1" in sell.message or "可卖" in sell.message

    executor.settle_t_plus_1()
    sell_after_settle = executor.execute_signal(
        _sell_signal(), CXBRMarketSnapshot(price=10.5, prev_close=10.0), 100
    )
    assert sell_after_settle is not None
    assert sell_after_settle.success
    assert sell_after_settle.stamp_tax > 0


def test_observe_signal_does_not_create_order(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    signal = CXBRSignal(
        action="observe",
        stage="next_day_watch",
        code="600001",
        asof="2026-08-18",
        reason="等待确认",
    )
    assert executor.execute_signal(signal, CXBRMarketSnapshot(price=10.0, prev_close=9.5)) is None
    assert executor.executions == []
