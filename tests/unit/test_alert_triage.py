"""scripts/alert_triage.py 单测（2026-09-11 告警评估员降噪改动）。

背景：修复+重启后，当日日志中修复前的 ERROR 行仍会被反复分诊，
导致每轮告警评估重复处置同一条历史告警（2026-09-11 连续 3 轮）。
新增 `_stale_error_note()` 仅做展示层标注，不改变 issues 判定。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_MOD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "alert_triage.py"
_spec = importlib.util.spec_from_file_location("alert_triage_under_test", _MOD_PATH)
assert _spec and _spec.loader
at: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(at)


@pytest.fixture()
def boot(monkeypatch: pytest.MonkeyPatch):
    def _set(value: dict | None) -> None:
        monkeypatch.setattr(at, "_read_json", lambda _path: value)

    return _set


def test_note_empty_when_no_boot_record(boot) -> None:
    boot(None)
    assert at._stale_error_note(["2026-09-11 09:36:18 | ERROR | x - boom"]) == ""


def test_note_empty_when_error_after_boot(boot) -> None:
    boot({"boot_time": "2026-09-11T12:35:22.101769"})
    line = "2026-09-11 12:40:00 | ERROR | x - boom"
    assert at._stale_error_note([line]) == ""


def test_note_empty_when_error_exactly_at_boot(boot) -> None:
    boot({"boot_time": "2026-09-11T12:35:22.101769"})
    line = "2026-09-11 12:35:22 | ERROR | x - boom"
    assert at._stale_error_note([line]) == ""


def test_note_present_when_all_errors_before_boot(boot) -> None:
    boot({"boot_time": "2026-09-11T12:35:22.101769"})
    lines = [
        "2026-09-11 09:36:18 | ERROR    | gugu.engine.scheduler_jobs:116 - 尾盘选股验证异常",
        "2026-09-11 10:00:00 | CRITICAL | gugu.x - boom",
    ]
    note = at._stale_error_note(lines)
    assert "2026-09-11 12:35:22" in note
    assert "历史残留" in note


def test_note_empty_when_one_error_after_boot(boot) -> None:
    boot({"boot_time": "2026-09-11T12:35:22.101769"})
    lines = [
        "2026-09-11 09:36:18 | ERROR | x - old",
        "2026-09-11 13:05:00 | ERROR | x - new",
    ]
    assert at._stale_error_note(lines) == ""


def test_note_ignores_untimestamped_lines(boot) -> None:
    boot({"boot_time": "2026-09-11T12:35:22.101769"})
    # Traceback 续行无时间戳 → 不参与「早于启动」判定，但也不阻断标注
    lines = ["2026-09-11 09:36:18 | ERROR | x - old", "Traceback (most recent call last):"]
    assert "历史残留" in at._stale_error_note(lines)


def test_note_empty_when_boot_time_missing_key(boot) -> None:
    boot({"pid": 28090})
    assert at._stale_error_note(["2026-09-11 09:36:18 | ERROR | x - old"]) == ""


def test_note_empty_without_timestamped_errors(boot) -> None:
    boot({"boot_time": "2026-09-11T12:35:22.101769"})
    assert at._stale_error_note(["Traceback (most recent call last):"]) == ""
