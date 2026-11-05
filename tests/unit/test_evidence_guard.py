"""前向证据文件写入守卫测试（2026-11-05）。

`data/signals_history.jsonl` 与 `data/paper_execution_ledger.jsonl` 是
`check_forward_evidence.py` / `check_drift.py` 的唯一证据源。2026-11-05 实测
发现这两个文件里躺着测试夹具（合成 symbol 000001-000003、假成交 600001），
一旦被当成前向证据，整套「已验证」结论就是假的。

根因是部分模块在导入期绑定 `PROJECT_ROOT`，conftest 的 patch 影响不到。
因此改在写入点加结构性守卫，本文件守住该守卫的行为边界。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gugu.utils.evidence_guard import (
    EVIDENCE_FILENAMES,
    guard_evidence_path,
    reset_redirect_dir,
)


@pytest.fixture(autouse=True)
def _clean_redirect():
    reset_redirect_dir()
    yield
    reset_redirect_dir()


def _repo_data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def test_repo_evidence_file_is_redirected_in_test_mode(monkeypatch) -> None:
    """测试模式下写仓库 data/ 的证据文件必须被重定向到临时目录。"""
    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    target = _repo_data_dir() / "signals_history.jsonl"
    resolved = guard_evidence_path(target)

    assert resolved != target
    assert resolved.name == "signals_history.jsonl"
    assert _repo_data_dir().resolve() not in resolved.resolve().parents


def test_ledger_file_is_also_protected(monkeypatch) -> None:
    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    target = _repo_data_dir() / "paper_execution_ledger.jsonl"
    assert guard_evidence_path(target) != target


def test_temp_path_is_allowed_through(monkeypatch, tmp_path: Path) -> None:
    """测试显式传 tmp_path 时不得被二次重定向，否则断言语义全乱。"""
    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    target = tmp_path / "signals_history.jsonl"
    assert guard_evidence_path(target) == target


def test_non_evidence_file_is_untouched(monkeypatch) -> None:
    """守卫只保护证据文件，不能干扰其它运行时状态文件。"""
    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    target = _repo_data_dir() / "heartbeat.json"
    assert guard_evidence_path(target) == target


def test_production_mode_is_untouched(monkeypatch) -> None:
    """非测试模式必须原样放行，否则真实运行写不进账本。"""
    monkeypatch.delenv("GUGU_TEST_MODE", raising=False)
    target = _repo_data_dir() / "paper_execution_ledger.jsonl"
    assert guard_evidence_path(target) == target


def test_all_redirects_share_one_directory(monkeypatch) -> None:
    """同一进程内多次重定向应落到同一目录，便于排查。"""
    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    a = guard_evidence_path(_repo_data_dir() / "signals_history.jsonl")
    b = guard_evidence_path(_repo_data_dir() / "paper_execution_ledger.jsonl")
    assert a.parent == b.parent


def test_protected_filenames_cover_evidence_sources() -> None:
    """证据源清单必须覆盖门槛脚本与漏斗分析实际读取的文件。"""
    assert "signals_history.jsonl" in EVIDENCE_FILENAMES
    assert "paper_execution_ledger.jsonl" in EVIDENCE_FILENAMES
    assert "trade_journal.jsonl" in EVIDENCE_FILENAMES


# ========== 写入点接线回归（对应 2026-11-05 实际泄漏） ==========


def test_signal_tracker_write_history_does_not_touch_repo_data(monkeypatch) -> None:
    """`SignalTracker._write_history` 是实际泄漏通道，必须被守卫拦下。

    该方法的路径来自导入期绑定的 PROJECT_ROOT，conftest 的 patch 影响不到；
    修复前测试回填的前瞻收益会直接写进仓库 data/signals_history.jsonl。
    """
    from gugu.analysis.signal_tracker import SignalTracker

    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    repo_file = _repo_data_dir() / "signals_history.jsonl"
    before = repo_file.read_bytes() if repo_file.exists() else b""

    tracker = SignalTracker()
    # 强制把路径指向仓库，模拟「导入期绑定」的真实情形
    tracker._history_path = repo_file
    tracker._write_history([{"symbol": "000001", "direction": "buy"}])

    after = repo_file.read_bytes() if repo_file.exists() else b""
    assert after == before, "测试回写不得落进仓库证据文件"


def test_paper_ledger_append_does_not_touch_repo_data(monkeypatch) -> None:
    """`append_fill` 同样受守卫保护，测试成交不得写进真实账本。"""
    from gugu.execution import paper_ledger

    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    repo_file = _repo_data_dir() / "paper_execution_ledger.jsonl"
    before = repo_file.read_bytes() if repo_file.exists() else b""

    paper_ledger.append_fill({"execution_id": "x", "symbol": "600001", "direction": "buy"})

    after = repo_file.read_bytes() if repo_file.exists() else b""
    assert after == before


def test_trade_journal_does_not_touch_repo_data(monkeypatch) -> None:
    """决策审计日志同样受保护——信号漏斗分析依赖它，不能被夹具污染。"""
    from gugu.journal.trade_log import log_signal_blocked

    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    repo_file = _repo_data_dir() / "trade_journal.jsonl"
    before = repo_file.read_bytes() if repo_file.exists() else b""

    log_signal_blocked("600519", "test_strategy", "测试拦截原因")

    after = repo_file.read_bytes() if repo_file.exists() else b""
    assert after == before
