from __future__ import annotations

from gugu.analysis.yege_battle import (
    audit_context,
    audit_event_records,
    detect_mainline_burst,
    validate_next_day,
)
from scripts.validate_yege_next_day import summarize_validations, validate_rows
from scripts.yege_event_study import add_forward_returns, dedupe_events, summarize


def _context(**overrides):
    value = {
        "evidence_status": "available",
        "data_quality": "fresh_valid",
        "sector": "机器人",
        "message_level": "high",
        "message_source": "official",
        "message_impact_score": 0.9,
        "first_board_count": 4,
        "sector_limit_up_count": 4,
        "first_board_window_minutes": 30,
        "sector_index_new_high": True,
        "sector_volume_ratio": 1.6,
        "anchor_count": 1,
        "max_anchor_market_cap_yi": 500.0,
        "anchor_negative_feedback": False,
        "old_dragon_symbol": "000001",
        "old_dragon_anomaly": True,
    }
    value.update(overrides)
    return value


def test_audit_context_distinguishes_unavailable_and_incomplete():
    assert audit_context({})["evidence_status"] == "insufficient_evidence"
    assert audit_context({"evidence_status": "unavailable"})["ready"] is False
    complete = _context()
    assert audit_context(complete)["ready"] is True


def test_audit_event_records_reports_missing_fields():
    audit = audit_event_records([{"sector": "机器人"}, {}])
    assert audit["event_count"] == 2
    assert audit["ready_count"] == 0
    assert audit["status"] == "insufficient_evidence"
    assert audit["missing_field_counts"]["message_level"] == 2


def test_mainline_burst_requires_all_evidence():
    event = detect_mainline_burst(_context(), candidate_symbol="600000", signal_date="2026-09-09")
    assert event["passed"] is False
    assert "候选股不是核心/空间龙" in event["reasons"]

    event = detect_mainline_burst(
        _context(board_role="space_dragon"), candidate_symbol="600000", signal_date="2026-09-09"
    )
    assert event["passed"] is True
    assert event["evidence_status"] == "available"


def test_burst_window_and_data_missing_fail_closed():
    event = detect_mainline_burst(
        _context(board_role="core", first_board_window_minutes=61), candidate_symbol="600000"
    )
    assert event["passed"] is False
    assert any("3-60" in reason for reason in event["reasons"])

    event = detect_mainline_burst({}, candidate_symbol="600000")
    assert event["passed"] is False
    assert event["evidence_status"] == "unavailable"


def test_event_study_dedupes_event_id_and_symbol_cooldown():
    events = [
        {"event_id": "a", "candidate_symbol": "600000", "signal_date": "2026-09-01"},
        {"event_id": "b", "candidate_symbol": "600000", "signal_date": "2026-09-02"},
        {"event_id": "a", "candidate_symbol": "600000", "signal_date": "2026-09-03"},
        {"event_id": "c", "candidate_symbol": "600001", "signal_date": "2026-09-02"},
    ]
    result = dedupe_events(events, cooldown_days=3)
    assert [event["event_id"] for event in result] == ["a", "c"]


def test_event_study_forward_returns_use_next_session_and_cost():
    result = add_forward_returns(
        {"event_id": "a", "entry_assumption": "next_session_open"},
        [100.0, 110.0, 121.0, 120.0, 130.0],
        entry_index=1,
        cost=0.004,
    )
    assert result["entry_price_source"] == "next_open"
    assert result["return_1d"] == 0.0956
    assert result["return_3d"] is not None
    assert result["return_10d"] is None


def test_event_study_summary_is_insufficient_without_sample():
    report = summarize(
        [
            {
                "validated": True,
                "entry_assumption": "next_session_open",
                "evidence_status": "available",
                "sector": "机器人",
                "return_1d": 0.02,
            }
        ]
    )
    assert report["status"] == "insufficient_evidence"
    assert report["validated_event_count"] == 1


def test_next_day_validation_separates_rule_failure_from_missing_evidence():
    event = detect_mainline_burst(
        _context(board_role="space_dragon"), candidate_symbol="600000", signal_date="2026-09-09"
    )
    event["signal_close"] = 100.0
    failed = dict(event)
    failed["passed"] = False
    result = validate_rows(
        [
            {
                **failed,
                "next_open": 100.0,
                "next_high": 102.0,
                "next_low": 99.0,
                "next_close": 101.0,
                "first_board_survival_rate": 0.8,
            }
        ]
    )
    assert result[0]["validation_status"] == "rule_failed"
    assert result[0]["validated"] is False
    report = summarize_validations(result)
    assert report["rule_failed_count"] == 1
    assert report["validated_count"] == 0


def test_next_day_validation_uses_next_open_and_rejects_missing_data():
    event = detect_mainline_burst(
        _context(board_role="space_dragon"), candidate_symbol="600000", signal_date="2026-09-09"
    )
    event["signal_close"] = 100.0
    validated = validate_next_day(
        event,
        next_open=103.0,
        next_high=108.0,
        next_low=101.0,
        next_close=106.0,
        first_board_survival_rate=0.75,
    )
    assert validated["entry_assumption"] == "next_session_open"
    assert validated["open_premium"] == 0.03
    assert validated["validated"] is True

    missing = validate_next_day(
        event,
        next_open=None,
        next_high=None,
        next_low=None,
        next_close=None,
        first_board_survival_rate=None,
    )
    assert missing["validated"] is False
    assert missing["validation_status"] == "insufficient_evidence"
