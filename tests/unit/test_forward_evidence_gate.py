"""前向证据门槛契约测试（2026-11-05）。

背景：`paper_signals: 0` 让 4 个 conditional_pass 策略永远过不了 gate_v2，
而 `check_forward_evidence.py` 自身有两个缺陷使得「即使跑满 30 笔真实成交
也永远无法通过」，同时还允许任意行冒充前向证据：

1. **日期字段错配**：账本由 `paper_ledger.build_fill_record` 写入，成交时间
   字段是 `created_at`；门槛却只读 `date`/`timestamp`/`closed_at`。结果是
   真实成交的跨日期覆盖恒为 0，门槛结构性不可达。
2. **无证据等级校验**：任何带 `realized_pnl` 的行（回放、测试夹具、手写
   JSON）都会被计入「前向已验证」，正是评审里批评的自证循环。

本文件同时覆盖这两点，并守住「0 元保本交易也是有效样本」这条边界。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_forward_evidence import evaluate_ledger


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return path


def _forward_sell(**overrides: object) -> dict:
    """构造一行合规的 forward_paper 已实现卖出。"""
    row = {
        "execution_id": "exec-1",
        "created_at": "2026-11-05T15:00:00",
        "evidence_class": "forward_paper",
        "direction": "sell",
        "symbol": "sh600000",
        "realized_pnl": 12.5,
        "outcome_status": "closed",
    }
    row.update(overrides)
    return row


def _forward_rows(n: int, *, distinct_dates: int = 10) -> list[dict]:
    return [
        _forward_sell(
            execution_id=f"exec{i:04d}",
            created_at=f"2026-11-{1 + i % distinct_dates:02d}T15:00:00",
            symbol=f"sh60000{i % 5}",
            realized_pnl=1.0 if i % 2 else -0.5,
        )
        for i in range(n)
    ]


# ========== 原有缺陷 1：日期字段必须能读到 created_at ==========


def test_created_at_is_recognized_as_trade_date(tmp_path: Path) -> None:
    """真实账本只有 created_at，跨日期覆盖必须能统计出来。

    修复前：门槛读 date/timestamp/closed_at，全部为 None → distinct_dates
    恒为 0，任何真实运行都过不了。这条用例是该缺陷的回归防线。
    """
    ledger = _write(tmp_path / "ledger.jsonl", _forward_rows(30, distinct_dates=10))
    report = evaluate_ledger(ledger)

    assert report["realized_samples"] == 30
    assert report["distinct_dates"] == 10
    assert report["passed"] is True
    assert report["reasons"] == []


def test_legacy_date_field_still_supported(tmp_path: Path) -> None:
    """兼容旧账本里显式的 date 字段，避免历史数据被一刀切废弃。

    旧格式没有 created_at，只有 date——此时必须回退到 date 取成交日。
    """
    rows = []
    for i in range(30):
        row = _forward_sell(execution_id=f"e{i}", date=f"2026-11-{1 + i % 10:02d}")
        row.pop("created_at")
        rows.append(row)
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))
    assert report["distinct_dates"] == 10


# ========== 原有缺陷 2：非前向行不得计入 ==========


@pytest.mark.parametrize(
    ("override", "label"),
    [
        ({"evidence_class": "replay"}, "回放"),
        ({"evidence_class": "fixture"}, "测试夹具"),
        ({"evidence_class": ""}, "未标注等级"),
    ],
)
def test_non_forward_evidence_is_excluded(tmp_path: Path, override: dict, label: str) -> None:
    """回放/夹具/未标注的行即使有 realized_pnl 也不得算作前向验证。"""
    rows = [_forward_sell(**override, execution_id=f"e{i}") for i in range(30)]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))

    assert report["realized_samples"] == 0, f"{label} 不得计入前向证据"
    assert report["excluded_rows"] == 30
    assert report["passed"] is False


def test_legacy_rows_without_evidence_class_are_excluded(tmp_path: Path) -> None:
    """历史账本没有 evidence_class 字段——无法证明是真实运行，一律不计入。"""
    rows = [{"symbol": f"sh60000{i}", "realized_pnl": 1.0} for i in range(30)]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))

    assert report["realized_samples"] == 0
    assert report["excluded_rows"] == 30


def test_buy_rows_are_not_realized_samples(tmp_path: Path) -> None:
    """买入不产生已实现盈亏，即使被错误填了 pnl 也不得计入。"""
    rows = [_forward_sell(direction="buy", execution_id=f"e{i}") for i in range(30)]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))
    assert report["realized_samples"] == 0


def test_rows_without_execution_id_are_excluded(tmp_path: Path) -> None:
    """execution_id 为空说明不是真实成交记录（手写占位行）。"""
    rows = [_forward_sell(execution_id="", symbol=f"sh60000{i}") for i in range(30)]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))
    assert report["realized_samples"] == 0


def test_rows_without_symbol_are_excluded(tmp_path: Path) -> None:
    rows = [_forward_sell(symbol="", execution_id=f"e{i}") for i in range(30)]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))
    assert report["realized_samples"] == 0


# ========== 边界：0 元保本交易 ==========


def test_breakeven_trade_counts_as_sample(tmp_path: Path) -> None:
    """保本平仓（pnl=0）是真实成交样本，不能用真值判断当成缺失丢掉。"""
    rows = [
        _forward_sell(
            execution_id=f"e{i}", realized_pnl=0.0, created_at=f"2026-11-{1 + i % 10:02d}T15:00:00"
        )
        for i in range(30)
    ]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))
    assert report["realized_samples"] == 30
    assert report["passed"] is True


def test_string_pnl_is_not_a_sample(tmp_path: Path) -> None:
    """realized_pnl 是字符串说明数据形态异常，不做隐式转换冒充样本。"""
    rows = [_forward_sell(realized_pnl="12.5", execution_id=f"e{i}") for i in range(30)]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))
    assert report["realized_samples"] == 0


# ========== 门槛判定与混合账本 ==========


def test_insufficient_samples_fails_with_reason(tmp_path: Path) -> None:
    ledger = _write(tmp_path / "ledger.jsonl", _forward_rows(29, distinct_dates=10))
    report = evaluate_ledger(ledger)

    assert report["passed"] is False
    assert any("29 < 要求 30" in r for r in report["reasons"])


def test_insufficient_date_coverage_fails(tmp_path: Path) -> None:
    ledger = _write(tmp_path / "ledger.jsonl", _forward_rows(30, distinct_dates=3))
    report = evaluate_ledger(ledger)

    assert report["passed"] is False
    assert any("跨日期覆盖 3" in r for r in report["reasons"])


def test_mixed_ledger_passes_and_reports_excluded(tmp_path: Path) -> None:
    """真实样本达标时，混入的回放行只作为提示，不得把结论卡成失败。"""
    rows = _forward_rows(30, distinct_dates=10) + [
        _forward_sell(evidence_class="replay", execution_id=f"r{i}") for i in range(5)
    ]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))

    assert report["passed"] is True
    assert report["realized_samples"] == 30
    assert report["excluded_rows"] == 5
    assert report["notes"], "被排除的行必须有可见提示，否则用户不知道账本里混了回放"


def test_missing_ledger_fails_explicitly(tmp_path: Path) -> None:
    report = evaluate_ledger(tmp_path / "nope.jsonl")
    assert report["passed"] is False
    assert "不存在" in report["reasons"][0]


def test_realized_pnl_is_summed(tmp_path: Path) -> None:
    rows = [
        _forward_sell(
            execution_id=f"e{i}", realized_pnl=10.0, created_at=f"2026-11-{1 + i % 10:02d}T15:00:00"
        )
        for i in range(30)
    ]
    report = evaluate_ledger(_write(tmp_path / "ledger.jsonl", rows))
    assert report["realized_pnl"] == pytest.approx(300.0)
