"""Research ledger tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gugu.research.ledger import append_record, build_record


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


def test_concurrent_append_does_not_drop_records(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"

    def write(index: int) -> None:
        assert append_record({"kind": "backtest", "index": index}, path) == path

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(24)))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 24
    assert sorted(json.loads(line)["index"] for line in lines) == list(range(24))
