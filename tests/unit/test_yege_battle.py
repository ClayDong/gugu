from __future__ import annotations

from gugu.analysis.yege_battle import detect_mainline_burst, validate_next_day
from scripts.yege_event_study import summarize


def _context(**overrides):
    value = {
        "evidence_status": "available",
        "data_quality": "fresh_valid",
        "sector": "机器人",
        "message_level": "high",
        "message_source": "official",
        "message_impact_score": 0.9,
        "first_board_count": 4,
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
