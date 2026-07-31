from __future__ import annotations

from gugu.models.signal import Signal


def test_signal_round_trips_decision_contract_dict() -> None:
    signal = Signal.from_dict(
        {
            "symbol": "600519",
            "direction": "buy",
            "decision": {"status": "blocked", "action": "observe"},
        }
    )
    assert signal.decision == {"status": "blocked", "action": "observe"}
    assert Signal.from_dict(signal.to_dict()).decision == signal.decision


def test_signal_reads_legacy_dictionary_without_decision() -> None:
    signal = Signal.from_dict({"symbol": "600519", "direction": "buy"})
    assert signal.symbol == "600519"
    assert signal.decision == {}
    assert "decision" in signal.to_dict()
