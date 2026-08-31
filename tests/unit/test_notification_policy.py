"""Notification tier, digest buffer, and archive tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gugu.notifier.notification_policy import (
    DigestBuffer,
    NotificationTier,
    archive_notification,
    build_digest_card,
    classify_tier,
    semantic_dedup_key,
)


def _card(title: str) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": title}}],
        },
    }


def test_classify_tier_keeps_action_events_immediate() -> None:
    assert classify_tier("signal") is NotificationTier.ACTION
    assert classify_tier("risk_alert", severity="warn") is NotificationTier.ACTION
    assert classify_tier("critical_system") is NotificationTier.ACTION
    assert classify_tier("daily_report") is NotificationTier.DIGEST
    assert classify_tier("intraday_alert", severity="info") is NotificationTier.DIGEST


def test_semantic_dedup_key_uses_business_identity() -> None:
    key = semantic_dedup_key(
        "signal",
        symbol="600519",
        direction="BUY",
        strategy="box_breakout",
        business_date="2026-08-31",
    )
    assert key == "signal:600519:buy:box_breakout:2026-08-31:"


def test_digest_buffer_groups_and_removes_only_after_success(tmp_path: Path) -> None:
    buffer = DigestBuffer(tmp_path / "digest.json")
    assert buffer.add(_card("A"), event_type="daily_report", aggregate_key="close")
    assert buffer.add(_card("B"), event_type="screener", aggregate_key="close")
    assert buffer.keys() == ["close"]
    cards = buffer.peek("close")
    assert len(cards) == 2
    digest = build_digest_card(cards)
    assert len(digest["card"]["elements"]) >= 2
    assert buffer.remove_first("close", 1) is True
    assert len(buffer.peek("close")) == 1
    assert buffer.remove("close") is True
    assert buffer.peek("close") == []


def test_digest_buffer_discards_expired_bucket(tmp_path: Path) -> None:
    path = tmp_path / "digest.json"
    buffer = DigestBuffer(path)
    assert buffer.add(_card("old"), event_type="daily_report", aggregate_key="old")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["old"]["created_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert buffer.discard_expired(max_age_hours=24) == 1
    assert buffer.keys() == []


def test_digest_buffer_rejects_missing_aggregate_key(tmp_path: Path) -> None:
    buffer = DigestBuffer(tmp_path / "digest.json")
    assert buffer.add(_card("A"), event_type="daily_report", aggregate_key="") is False
    assert not (tmp_path / "digest.json").exists()


def test_archive_is_strict_json_and_preserves_metadata(tmp_path: Path) -> None:
    path = tmp_path / "archive.jsonl"
    assert archive_notification(
        path,
        card={"value": float("inf")},
        event_type="daily_report",
        tier=NotificationTier.DIGEST,
        target="chat",
        dedup_key="report:close",
        aggregate_key="close:2026-08-31",
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["tier"] == "digest"
    assert record["dedup_key"] == "report:close"
    assert record["card"]["value"] is None
    json.dumps(record, allow_nan=False)
