from __future__ import annotations

from typing import get_args

from gugu.models.decision import (
    DecisionAction,
    DecisionContract,
    DecisionStatus,
    DataQuality,
    evaluate_entry_gate,
)


def test_contract_defaults_to_candidate_observation() -> None:
    decision = DecisionContract(symbol="600519", decision_type="entry")
    assert decision.action == "observe"
    assert decision.status == "candidate"
    assert decision.data_quality == "unavailable"
    assert decision.allowed_to_trade is False


def test_contract_round_trips_legacy_safe_fields() -> None:
    decision = DecisionContract(
        symbol="600519",
        action="blocked",
        status="blocked",
        data_quality="partial",
        block_reasons=["核心数据缺失"],
    )
    restored = DecisionContract.from_dict(decision.to_dict())
    assert restored.symbol == "600519"
    assert restored.status == "blocked"
    assert restored.block_reasons == ["核心数据缺失"]


def test_signal_gate_preserves_legacy_dict_and_blocks_without_perspective() -> None:
    decision = evaluate_entry_gate(
        {
            "symbol": "600519",
            "direction": "buy",
            "data_quality": "fresh",
        }
    )
    assert decision.action == "observe"
    assert decision.status == "blocked"
    assert "缺少十大视角评分" in decision.block_reasons


def test_signal_gate_blocks_bad_data_quality_before_perspective() -> None:
    decision = evaluate_entry_gate(
        {"symbol": "600519", "direction": "buy", "data_quality": "stale"},
        {"passed": True},
    )
    assert decision.status == "blocked"
    assert decision.data_quality == "stale"
    assert "数据状态不是 fresh_valid" in decision.block_reasons


def test_signal_gate_accepts_fresh_valid_alias() -> None:
    decision = evaluate_entry_gate(
        {"symbol": "600519", "direction": "buy", "data_quality": "fresh_valid"},
        {"passed": True, "error_count": 0},
    )
    assert decision.status == "approved"


def test_signal_gate_blocks_perspective_error_or_failure() -> None:
    failed = evaluate_entry_gate(
        {"symbol": "600519", "direction": "buy", "data_quality": "fresh"},
        {"passed": False},
    )
    errored = evaluate_entry_gate(
        {"symbol": "600519", "direction": "buy", "data_quality": "fresh"},
        {"passed": True, "error_count": 1},
    )
    status_error = evaluate_entry_gate(
        {"symbol": "600519", "direction": "buy", "data_quality": "fresh"},
        {"passed": True, "status": "error"},
    )
    assert failed.status == "blocked"
    assert "十大视角未通过统一门禁" in failed.block_reasons
    assert errored.status == "blocked"
    assert "存在视角评分异常" in errored.block_reasons
    assert status_error.status == "blocked"
    assert "存在视角评分异常" in status_error.block_reasons


def test_signal_gate_approves_only_fresh_passing_buy() -> None:
    decision = evaluate_entry_gate(
        {"symbol": "600519", "direction": "buy", "data_quality": "fresh"},
        {"passed": True, "error_count": 0},
    )
    assert decision.action == "buy"
    assert decision.status == "approved"
    assert decision.allowed_to_trade is True


def test_public_literal_aliases_are_importable() -> None:
    assert "fresh" in get_args(DataQuality)
    assert "approved" in get_args(DecisionStatus)
    assert "observe" in get_args(DecisionAction)
