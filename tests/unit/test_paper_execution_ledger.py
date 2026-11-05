"""PaperBroker unified execution ledger tests."""

from __future__ import annotations

import json

from gugu.execution.paper import PaperBroker
from gugu.execution.paper_ledger import ExecutionContext, summarize_fills


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
    position = broker.get_position("600519")
    assert position is not None
    expected_cost = buy.price * buy.quantity + buy.commission + buy.transfer_fee
    assert position.avg_cost == expected_cost / buy.quantity
    assert position.entry_signal_id == "sig-1"
    assert position.entry_execution_id == buy.execution_id
    assert position.entry_date
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


def test_paper_partial_then_full_close_and_summary(tmp_path, monkeypatch):
    """部分平仓只计 realized fill，完整平仓才计 closed trade。"""
    monkeypatch.setattr("gugu.config.PROJECT_ROOT", tmp_path)
    broker = PaperBroker(initial_capital=200_000, state_file=tmp_path / "state.json")
    broker.set_market_context("600519", prev_close=100.0)
    context = ExecutionContext(signal_id="sig-partial", strategy="sector_momentum")
    monkeypatch.setattr(
        broker,
        "_simulate_partial_fill",
        lambda quantity, fill_price, partial_prob: (quantity, fill_price),
    )

    buy = broker.order("600519", "buy", 1_000, 100.0, execution_context=context)
    assert buy.success and buy.quantity == 1_000
    broker.settle_t_plus_1()
    first = broker.get_position("600519")
    assert first is not None
    first_sell = broker.order("600519", "sell", 400, 110.0, execution_context=context)
    assert first_sell.success
    assert first_sell.position_closed is False
    assert first_sell.position_status == "partial"

    rows = [
        json.loads(line)
        for line in (tmp_path / "data" / "paper_execution_ledger.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["outcome_status"] == "partial"
    summary = summarize_fills(rows)
    assert summary["realized_fills"] == 1
    assert summary["closed_trades"] == 0

    second_sell = broker.order("600519", "sell", 600, 110.0, execution_context=context)
    assert second_sell.success
    assert second_sell.position_closed is True
    rows = [
        json.loads(line)
        for line in (tmp_path / "data" / "paper_execution_ledger.jsonl").read_text().splitlines()
    ]
    summary = summarize_fills(rows)
    assert summary["realized_fills"] == 2
    assert summary["closed_trades"] == 1


def test_paper_old_order_call_remains_compatible(tmp_path):
    """旧的 PaperBroker.order 调用不传 context 仍正常。"""
    broker = PaperBroker(initial_capital=100_000, state_file=tmp_path / "state.json")
    broker.set_market_context("600519", prev_close=100.0)
    result = broker.order("600519", "buy", 100, 100.0)
    assert result.success
    assert result.execution_id
    assert result.realized_pnl is None


# ========== 证据等级（2026-11-05） ==========


def _ledger_rows(tmp_path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "data" / "paper_execution_ledger.jsonl").read_text().splitlines()
    ]


def test_default_evidence_class_is_forward_paper(tmp_path, monkeypatch):
    """真实运行的成交默认标 forward_paper，才可能计入前向验证。"""
    monkeypatch.delenv("GUGU_EVIDENCE_CLASS", raising=False)
    monkeypatch.setattr("gugu.config.PROJECT_ROOT", tmp_path)
    broker = PaperBroker(initial_capital=100_000, state_file=tmp_path / "state.json")
    broker.set_market_context("600519", prev_close=100.0)
    assert broker.order("600519", "buy", 100, 100.0).success

    assert _ledger_rows(tmp_path)[-1]["evidence_class"] == "forward_paper"


def test_env_overrides_evidence_class(tmp_path, monkeypatch):
    """回放脚本通过环境变量声明 replay，成交不得被当成前向证据。"""
    monkeypatch.setenv("GUGU_EVIDENCE_CLASS", "replay")
    monkeypatch.setattr("gugu.config.PROJECT_ROOT", tmp_path)
    broker = PaperBroker(initial_capital=100_000, state_file=tmp_path / "state.json")
    broker.set_market_context("600519", prev_close=100.0)
    assert broker.order("600519", "buy", 100, 100.0).success

    assert _ledger_rows(tmp_path)[-1]["evidence_class"] == "replay"


def test_unknown_evidence_class_falls_back_to_fixture(monkeypatch):
    """非法等级不静默升级为前向，统一按最低可信度 fixture 处理。"""
    from gugu.execution.paper_ledger import resolve_evidence_class

    monkeypatch.setenv("GUGU_EVIDENCE_CLASS", "trust-me-bro")
    assert resolve_evidence_class() == "fixture"
    assert resolve_evidence_class("REPLAY") == "replay"
    assert resolve_evidence_class("forward_paper") == "forward_paper"
