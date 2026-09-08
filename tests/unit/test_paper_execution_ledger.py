"""PaperBroker unified execution ledger tests."""

from __future__ import annotations

import json

from gugu.execution.paper import PaperBroker
from gugu.execution.paper_ledger import ExecutionContext


def test_paper_buy_sell_writes_context_and_realized_pnl(tmp_path, monkeypatch):
    """买卖成交应保留策略身份并计算已实现盈亏。"""
    monkeypatch.setattr("gugu.config.PROJECT_ROOT", tmp_path)
    broker = PaperBroker(initial_capital=100_000, state_file=tmp_path / "state.json")
    broker.set_market_context("600519", prev_close=100.0)
    context = ExecutionContext(
        signal_id="sig-1",
        strategy="two_b_reversal",
        strategies=("two_b_reversal",),
        regime="sideways",
        data_quality="fresh_valid",
    )

    buy = broker.order("600519", "buy", 100, 100.0, execution_context=context)
    assert buy.success
    assert buy.execution_id
    broker.settle_t_plus_1()
    broker.update_price("600519", 110.0)

    sell = broker.order("600519", "sell", 100, 110.0, execution_context=context)
    assert sell.success
    assert sell.realized_pnl is not None
    assert sell.realized_pnl > 0
    assert sell.entry_execution_id == buy.execution_id

    ledger = tmp_path / "data" / "paper_execution_ledger.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["strategy"] == "two_b_reversal"
    assert rows[0]["outcome_status"] == "open"
    assert rows[1]["outcome_status"] == "closed"
    assert rows[1]["realized_pnl"] > 0
    assert rows[1]["entry_execution_id"] == rows[0]["execution_id"]


def test_paper_old_order_call_remains_compatible(tmp_path):
    """旧的 PaperBroker.order 调用不传 context 仍正常。"""
    broker = PaperBroker(initial_capital=100_000, state_file=tmp_path / "state.json")
    broker.set_market_context("600519", prev_close=100.0)
    result = broker.order("600519", "buy", 100, 100.0)
    assert result.success
    assert result.execution_id
    assert result.realized_pnl is None
