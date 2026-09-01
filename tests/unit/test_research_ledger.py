"""Research ledger tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gugu.research.ledger import append_record, build_record
from gugu.utils.atomic import append_jsonl, atomic_write_json


def test_append_record_is_strict_json_and_sanitizes_non_finite_values(tmp_path: Path) -> None:
    path = tmp_path / "research_ledger.jsonl"
    result = append_record(
        {"kind": "backtest", "metric": float("inf"), "nested": {"nan": float("nan")}},
        path,
    )
    assert result == path
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["metric"] is None
    assert record["nested"]["nan"] is None
    assert record["schema_version"] == 1
    assert record["run_id"]
    assert record["recorded_at"]
    json.dumps(record, allow_nan=False)


def test_atomic_json_writers_normalize_non_finite_values(tmp_path: Path) -> None:
    json_path = tmp_path / "report.json"
    jsonl_path = tmp_path / "report.jsonl"
    atomic_write_json(json_path, {"nan": float("nan"), "inf": float("inf")})
    append_jsonl(jsonl_path, {"nan": float("nan")})
    assert json.loads(json_path.read_text(encoding="utf-8")) == {"nan": None, "inf": None}
    assert json.loads(jsonl_path.read_text(encoding="utf-8")) == {"nan": None}


def test_build_record_contains_runtime_context(tmp_path: Path) -> None:
    record = build_record(
        kind="optimization",
        entrypoint="tests",
        strategy="box_breakout",
        symbols=["600519", "000858"],
        requested_days=500,
        gate={"passed": False},
    )
    path = append_record(record, tmp_path / "ledger.jsonl")
    assert path is not None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["strategy"] == "box_breakout"
    assert loaded["symbols"] == ["600519", "000858"]
    assert loaded["symbol_count"] == 2
    assert loaded["requested_days"] == 500
    assert loaded["gate"]["passed"] is False
    assert "git_dirty" in loaded


def test_append_quarantines_invalid_historical_lines(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"ok": 1}\n{"bad": NaN}\nnot-json\n', encoding="utf-8")
    assert append_record({"kind": "backtest", "ok": 2}, path) == path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    def reject_constant(value: str) -> None:
        raise ValueError(value)

    assert all(json.loads(line, parse_constant=reject_constant) for line in lines)
    invalid = path.with_suffix(path.suffix + ".invalid")
    assert invalid.exists()
    assert len(invalid.read_text(encoding="utf-8").splitlines()) == 2


def test_concurrent_append_does_not_drop_records(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"

    def write(index: int) -> None:
        assert append_record({"kind": "backtest", "index": index}, path) == path

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(24)))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 24
    assert sorted(json.loads(line)["index"] for line in lines) == list(range(24))
