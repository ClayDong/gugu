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
    assert result.strategy_name == "cxbr_v1"
    assert result.rule_version == "cxbr-v1"
    assert result.entry_assumption == "next_session_open"
    assert result.quantity == 100
    assert result.fill_price > 10.0
    assert result.commission >= 5.0
    assert result.fill_time == result.order_time
    assert result.gross_amount == round(result.fill_price * result.quantity, 2)
    assert result.net_cash_flow < 0
    assert result.total_fees == round(result.commission + result.stamp_tax + result.transfer_fee, 2)
    assert result.realized_pnl is None
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
    assert sell_after_settle.fill_time == sell_after_settle.order_time
    assert sell_after_settle.gross_amount == round(
        sell_after_settle.fill_price * sell_after_settle.quantity, 2
    )
    assert sell_after_settle.net_cash_flow > 0
    assert sell_after_settle.total_fees == round(
        sell_after_settle.commission + sell_after_settle.stamp_tax + sell_after_settle.transfer_fee,
        2,
    )
    assert sell_after_settle.realized_pnl is not None
    assert sell_after_settle.cost_basis_consumed is not None
    assert sell_after_settle.realized_pnl == round(
        sell_after_settle.net_cash_flow - sell_after_settle.cost_basis_consumed, 2
    )


def test_cost_basis_is_rebuilt_from_ledger_after_restart(tmp_path: Path) -> None:
    first = _executor(tmp_path)
    bought = first.execute_signal(
        _buy_signal(), CXBRMarketSnapshot(price=10.0, prev_close=9.5), 100
    )
    assert bought is not None and bought.success

    restarted = _executor(tmp_path)
    restarted.settle_t_plus_1()
    sold = restarted.execute_signal(
        _sell_signal(), CXBRMarketSnapshot(price=10.5, prev_close=10.0), 100
    )
    assert sold is not None and sold.success
    assert sold.realized_pnl is not None
    assert sold.realized_pnl > 0
    summary = restarted.summarize_performance()
    assert summary["filled_orders"] == 2
    assert summary["closed_trades"] == 1
    assert summary["open_quantity"] == 0
    assert summary["realized_pnl"] == sold.realized_pnl
    assert summary["closed_win_rate"] == 1.0
    assert summary["total_fees"] > 0


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
