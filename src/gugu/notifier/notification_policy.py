"""Notification tiers, semantic keys, and durable archive helpers."""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX production path
    fcntl = None  # type: ignore[assignment]


class NotificationTier(StrEnum):
    """User-facing urgency tier; archive is a persistence sink, not a delay mode."""

    ACTION = "action"
    DIGEST = "digest"


_ACTION_EVENTS = {
    "signal",
    "risk_alert",
    "holdings_sell",
    "take_profit",
    "stop_loss",
    "critical_intraday",
    "critical_system",
}


def classify_tier(event_type: str, *, severity: str = "") -> NotificationTier:
    """Classify a notification conservatively by event and severity."""
    normalized_event = str(event_type).strip().lower()
    normalized_severity = str(severity).strip().lower()
    if normalized_event in _ACTION_EVENTS:
        return NotificationTier.ACTION
    if normalized_severity in {"critical", "high", "halt", "p0", "p1"}:
        return NotificationTier.ACTION
    return NotificationTier.DIGEST


def classify_signal_tier(signal: Mapping[str, Any]) -> NotificationTier:
    """Keep sells actionable while routing blocked/observational buys to Digest."""
    if str(signal.get("direction", "buy")).lower() == "sell":
        return NotificationTier.ACTION
    decision = signal.get("decision") or {}
    blocked = isinstance(decision, Mapping) and decision.get("status") == "blocked"
    if (
        signal.get("observation_only")
        or signal.get("rejected")
        or signal.get("wisdom_filtered")
        or blocked
    ):
        return NotificationTier.DIGEST
    return NotificationTier.ACTION


def semantic_dedup_key(
    event_type: str,
    *,
    symbol: str = "",
    direction: str = "",
    strategy: str = "",
    business_date: str = "",
    stage: str = "",
) -> str:
    """Build a stable key from business identity, never dynamic card text."""
    parts = [
        str(event_type).strip().lower(),
        str(symbol).strip(),
        str(direction).strip().lower(),
        str(strategy).strip(),
        str(business_date).strip(),
        str(stage).strip(),
    ]
    return ":".join(parts)


def _sanitize(value: Any) -> Any:
    """Convert notification payloads to strict JSON-compatible values."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        with contextlib.suppress(Exception):
            return _sanitize(value.item())
    return value


class DigestBuffer:
    """Persist and aggregate deferred digest cards by business key."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def add(
        self,
        card: dict[str, Any],
        *,
        event_type: str,
        aggregate_key: str,
        target: str = "",
        dedup_key: str = "",
    ) -> bool:
        """Add one card to a durable aggregate bucket."""
        if not aggregate_key:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        try:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    buckets: dict[str, dict[str, Any]] = {}
                    if self.path.exists():
                        raw = json.loads(self.path.read_text(encoding="utf-8"))
                        if isinstance(raw, dict):
                            buckets = raw
                    bucket = buckets.setdefault(
                        aggregate_key,
                        {
                            "aggregate_key": aggregate_key,
                            "event_type": event_type,
                            "target": target,
                            "created_at": datetime.now(UTC).isoformat(),
                            "cards": [],
                        },
                    )
                    cards = bucket.setdefault("cards", [])
                    dedup_keys = bucket.setdefault("dedup_keys", [])
                    if dedup_key and dedup_key in dedup_keys:
                        return True
                    cards.append(_sanitize(card))
                    if dedup_key:
                        dedup_keys.append(dedup_key)
                    content = json.dumps(buckets, ensure_ascii=False, allow_nan=False, indent=2)
                    fd, tmp_name = tempfile.mkstemp(
                        dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as handle:
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(tmp_name, self.path)
                    except Exception:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp_name)
                        raise
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return True
        except Exception:
            return False

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def discard_expired(self, *, max_age_hours: int = 24) -> int:
        """Discard old buckets so stale reports cannot be sent on a later day."""
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        removed = 0
        for aggregate_key in self.keys():
            cards = self.peek(aggregate_key)
            if not cards:
                continue
            # Buckets created by older versions may lack timestamps; keep them for one flush.
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                created_at = raw[aggregate_key].get("created_at", "")
                if datetime.fromisoformat(created_at) < cutoff and self.remove(aggregate_key):
                    removed += 1
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
        return removed

    def keys(self) -> list[str]:
        """List currently buffered aggregate keys."""
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        try:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                try:
                    if not self.path.exists():
                        return []
                    buckets = json.loads(self.path.read_text(encoding="utf-8"))
                    return list(buckets) if isinstance(buckets, dict) else []
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            return []

    def peek(self, aggregate_key: str) -> list[dict[str, Any]]:
        """Read one aggregate bucket without removing it."""
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        try:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                try:
                    if not self.path.exists():
                        return []
                    buckets = json.loads(self.path.read_text(encoding="utf-8"))
                    bucket = buckets.get(aggregate_key, {}) if isinstance(buckets, dict) else {}
                    cards = bucket.get("cards", []) if isinstance(bucket, dict) else []
                    return list(cards) if isinstance(cards, list) else []
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            return []

    def remove_first(self, aggregate_key: str, count: int) -> bool:
        """Remove only a successfully delivered prefix of an aggregate bucket."""
        if count <= 0:
            return False
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        try:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    if not self.path.exists():
                        return False
                    buckets = json.loads(self.path.read_text(encoding="utf-8"))
                    if not isinstance(buckets, dict) or aggregate_key not in buckets:
                        return False
                    bucket = buckets[aggregate_key]
                    cards = bucket.get("cards", []) if isinstance(bucket, dict) else []
                    if not isinstance(cards, list):
                        return False
                    if len(cards) <= count:
                        buckets.pop(aggregate_key)
                    else:
                        bucket["cards"] = cards[count:]
                        dedup_keys = bucket.get("dedup_keys", [])
                        if isinstance(dedup_keys, list):
                            bucket["dedup_keys"] = dedup_keys[count:]
                    content = json.dumps(buckets, ensure_ascii=False, allow_nan=False, indent=2)
                    fd, tmp_name = tempfile.mkstemp(
                        dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as handle:
                            handle.write(content if buckets else "{}\n")
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(tmp_name, self.path)
                    except Exception:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp_name)
                        raise
                    return True
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            return False

    def remove(self, aggregate_key: str) -> bool:
        """Remove one aggregate bucket atomically after successful delivery."""
        return self.remove_first(aggregate_key, count=1_000_000_000)

    def drain(self, aggregate_key: str) -> list[dict[str, Any]]:
        """Compatibility helper: read then remove one aggregate bucket."""
        cards = self.peek(aggregate_key)
        if cards and self.remove_first(aggregate_key, len(cards)):
            return cards
        return []


def build_digest_card(
    cards: list[dict[str, Any]], *, title: str = "📋 gugu 汇总提醒"
) -> dict[str, Any]:
    """Combine card body elements into one bounded Feishu card."""
    elements: list[dict[str, Any]] = []
    for card in cards:
        payload = card.get("card", card)
        if not isinstance(payload, dict):
            continue
        header = payload.get("header", {})
        if isinstance(header, dict):
            source_title = header.get("title", {})
            if isinstance(source_title, dict) and source_title.get("content"):
                elements.append(
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"**{source_title['content']}**"},
                    }
                )
        source_elements = payload.get("elements", [])
        if isinstance(source_elements, list):
            elements.extend(source_elements[:8])
        if len(elements) >= 40:
            break
    if not elements:
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "暂无汇总内容"}}]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements[:40],
        },
    }


def archive_notification(
    path: str | Path,
    *,
    card: dict[str, Any],
    event_type: str,
    tier: NotificationTier | str,
    target: str = "",
    dedup_key: str = "",
    aggregate_key: str = "",
) -> bool:
    """Persist one full notification envelope before external delivery.

    Archive failures never block delivery. A process-level file lock keeps concurrent
    scheduler/retry writers from interleaving records; append-only storage avoids
    silently dropping old evidence.
    """
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    record = _sanitize(
        {
            "schema_version": 1,
            "archived_at": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "tier": str(tier),
            "target": target,
            "dedup_key": dedup_key,
            "aggregate_key": aggregate_key,
            "card": card,
        }
    )
    try:
        line = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with target_path.open("a", encoding="utf-8") as archive_file:
            if fcntl is not None:
                fcntl.flock(archive_file.fileno(), fcntl.LOCK_EX)
            try:
                archive_file.write(line + "\n")
                archive_file.flush()
                os.fsync(archive_file.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(archive_file.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            target_path.chmod(0o600)
        return True
    except Exception:
        return False
