"""前向证据门槛测试：门槛只检查真实账本，绝不生成样本。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_forward_evidence.py"
_spec = importlib.util.spec_from_file_location("forward_evidence_under_test", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_missing_ledger_fails_closed(tmp_path: Path) -> None:
    report = mod.evaluate_ledger(tmp_path / "missing.jsonl")
    assert report["passed"] is False
    assert report["realized_samples"] == 0


def test_empty_ledger_fails(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_text("", encoding="utf-8")
    assert mod.evaluate_ledger(p)["passed"] is False


def test_unrealized_rows_do_not_count(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    _write_ledger(p, [{"realized_pnl": None} for _ in range(50)])
    report = mod.evaluate_ledger(p)
    assert report["realized_samples"] == 0
    assert report["passed"] is False


def test_sufficient_realized_samples_pass(tmp_path: Path) -> None:
    """30 笔跨 30 个交易日的真实 forward_paper 已实现卖出应通过门槛。

    2026-11-05 起门槛要求 evidence_class=forward_paper + direction=sell，
    只有一行 realized_pnl 不再是「已验证」的证据——这里用与
    paper_ledger.build_fill_record 同构的记录验证正向路径。
    """
    p = tmp_path / "ledger.jsonl"
    rows = [
        {
            "execution_id": f"exec{i:03d}",
            "created_at": f"2026-10-{day:02d}T15:00:00",
            "evidence_class": "forward_paper",
            "direction": "sell",
            "symbol": f"sh60000{i % 5}",
            "realized_pnl": 0.01 * (i + 1),
        }
        for i, day in enumerate(range(1, 31))
    ]
    _write_ledger(p, rows)
    report = mod.evaluate_ledger(p, min_samples=30, min_distinct_dates=10)
    assert report["passed"] is True
    assert report["realized_samples"] == 30
    assert report["distinct_dates"] == 30


def test_unclassified_rows_do_not_pass(tmp_path: Path) -> None:
    """没有 evidence_class 的历史行无法证明是真实运行，不得判通过。"""
    p = tmp_path / "ledger.jsonl"
    _write_ledger(
        p,
        [
            {"date": f"2026-10-{day:02d}", "symbol": "sh600000", "realized_pnl": 0.01}
            for day in range(1, 31)
        ],
    )
    report = mod.evaluate_ledger(p, min_samples=30, min_distinct_dates=10)
    assert report["passed"] is False
    assert report["realized_samples"] == 0
    assert report["excluded_rows"] == 30


def test_single_day_does_not_pass(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    _write_ledger(
        p,
        [{"date": "2026-10-01", "realized_pnl": 0.01} for _ in range(40)],
    )
    report = mod.evaluate_ledger(p, min_samples=30, min_distinct_dates=10)
    assert report["passed"] is False
    assert any("跨日期覆盖" in r for r in report["reasons"])


def test_real_repo_ledger_is_honestly_reported() -> None:
    """当前项目账本必然未达标；断言「如实报告」而非「强行通过」。"""
    ledger = Path(__file__).resolve().parents[2] / "data" / "paper_execution_ledger.jsonl"
    report = mod.evaluate_ledger(ledger)
    assert report["total_rows"] >= 0
    if not ledger.exists() or report["realized_samples"] < 30:
        assert report["passed"] is False
