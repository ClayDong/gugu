"""信号漏斗统计测试（2026-11-05）。

漏斗脚本只做统计、不改判定逻辑，所以测试重点是「统计口径不能撒谎」：
成交/拦截/观察必须分开、拦截原因归类稳定、重复刷屏必须被看见。

背景：`data/trade_journal.jsonl` 144 条记录零成交，但这个分布此前只能人工挖。
漏斗报告首次跑出来的结果修正了一个判断——144 条里 88 条是同一只股票
（002472）在两天内被反复拦截，真正的独立信号事件远少于 144。
没有去重统计，就会把「一只票刷屏」误读成「策略信号很多但都被拦」。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.signal_funnel import (
    classify_reason,
    load_journal,
    main,
    render_markdown,
    summarize,
)


def _row(
    *,
    ts: str,
    result: str = "gate_blocked",
    reason: str = "",
    symbol: str = "600519",
    strategy: str = "box_breakout",
) -> dict:
    return {
        "ts": ts,
        "symbol": symbol,
        "strategy": strategy,
        "direction": "buy",
        "price": 0.0,
        "result": result,
        "reason": reason,
    }


# ========== 原因归类 ==========


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("今日暴涨 7.4%，不追涨杀跌", "追涨杀跌"),
        ("今日暴跌 -6.0%，不追涨杀跌", "追涨杀跌"),
        ("接力成功率0%低于60%，不接力", "接力环境"),
        ("封板率50%低于70%，不打板", "打板环境"),
        ("涨停稀少(3只)，空仓等机会", "涨停稀少-空仓"),
        ("市场环境数据不足，仅观察", "数据不足"),
        ("风控熔断中，禁止买入", "风控熔断"),
        ("持仓数已达上限 5，禁止新增买入", "仓位已满"),
        ("策略不在 verified_strategies 白名单", "策略白名单"),
        ("", "未记录原因"),
        ("某个全新的原因", "其他"),
    ],
)
def test_classify_reason(reason: str, expected: str) -> None:
    assert classify_reason(reason) == expected


# ========== 聚合口径 ==========


def test_pass_rate_and_buckets_are_separated() -> None:
    """成交、观察、拦截必须分开计数，通过率只用成交算。"""
    rows = [
        _row(ts="2026-11-05T09:35:00", result="executed"),
        _row(ts="2026-11-05T10:30:00", result="observation_only"),
        _row(ts="2026-11-05T13:05:00", result="gate_blocked", reason="今日暴涨 8%，不追涨杀跌"),
        _row(ts="2026-11-05T14:30:00", result="gate_blocked", reason="今日暴涨 9%，不追涨杀跌"),
    ]
    report = summarize(rows, days=0)

    assert report["total_events"] == 4
    assert report["executed"] == 1
    assert report["observing"] == 1
    assert report["blocked"] == 2
    assert report["pass_rate"] == pytest.approx(0.25)
    assert report["by_category"] == {"追涨杀跌": 2}


def test_zero_events_does_not_divide_by_zero() -> None:
    report = summarize([], days=0)
    assert report["total_events"] == 0
    assert report["pass_rate"] == 0.0


def test_category_only_counts_blocked_rows() -> None:
    """成交行也带 reason 时，不得混进拦截类目统计。"""
    rows = [
        _row(ts="2026-11-05T09:35:00", result="executed", reason="今日暴涨 8%，不追涨杀跌"),
        _row(ts="2026-11-05T10:30:00", result="gate_blocked", reason="接力成功率0%低于60%，不接力"),
    ]
    report = summarize(rows, days=0)
    assert report["by_category"] == {"接力环境": 1}


# ========== 重复刷屏识别 ==========


def test_duplicate_blocks_are_reported() -> None:
    """同一标的同日同原因多次 → 必须被识别为重复，而不是当成多个信号。"""
    rows = [
        _row(ts="2026-09-07T14:30:00", symbol="002472", reason="今日暴涨 7.4%，不追涨杀跌")
        for _ in range(45)
    ]
    report = summarize(rows, days=0)

    assert report["blocked"] == 45
    dups = report["duplicate_blocks"]
    assert len(dups) == 1
    assert dups[0]["symbol"] == "002472"
    assert dups[0]["count"] == 45
    assert dups[0]["category"] == "追涨杀跌"


def test_single_block_is_not_duplicate() -> None:
    rows = [_row(ts="2026-09-07T14:30:00", symbol="002472", reason="今日暴涨 7%，不追涨杀跌")]
    assert summarize(rows, days=0)["duplicate_blocks"] == []


def test_same_symbol_different_day_is_not_duplicate() -> None:
    """跨日的同标的拦截是不同事件，不能算重复。"""
    rows = [
        _row(ts="2026-09-07T14:30:00", symbol="002472", reason="今日暴涨 7%，不追涨杀跌"),
        _row(ts="2026-09-08T14:30:00", symbol="002472", reason="今日暴涨 7%，不追涨杀跌"),
    ]
    assert summarize(rows, days=0)["duplicate_blocks"] == []


# ========== 时间窗口 ==========


def test_window_filters_old_rows() -> None:
    old = (date.today() - timedelta(days=90)).isoformat()
    rows = [
        _row(ts=f"{old}T14:30:00", reason="今日暴涨 7%，不追涨杀跌"),
        _row(ts=f"{date.today().isoformat()}T14:30:00", reason="接力成功率0%低于60%，不接力"),
    ]
    report = summarize(rows, days=30)
    assert report["total_events"] == 1
    assert report["by_category"] == {"接力环境": 1}


def test_days_zero_means_all_history() -> None:
    old = (date.today() - timedelta(days=400)).isoformat()
    rows = [_row(ts=f"{old}T14:30:00", reason="今日暴涨 7%，不追涨杀跌")]
    assert summarize(rows, days=0)["total_events"] == 1


# ========== IO 与渲染 ==========


def test_load_journal_skips_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    path.write_text(
        json.dumps(_row(ts="2026-11-05T09:35:00"))
        + "\n{坏的JSON}\n"
        + json.dumps(_row(ts="2026-11-05T10:30:00")),
        encoding="utf-8",
    )
    assert len(load_journal(path)) == 2


def test_load_journal_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_journal(tmp_path / "nope.jsonl") == []


def test_render_markdown_handles_empty_window() -> None:
    md = render_markdown(summarize([], days=0))
    assert "没有任何交易事件记录" in md


def test_render_markdown_includes_all_sections() -> None:
    rows = [
        _row(ts="2026-09-07T14:30:00", symbol="002472", reason="今日暴涨 7.4%，不追涨杀跌"),
        _row(ts="2026-09-07T14:31:00", symbol="002472", reason="今日暴涨 7.4%，不追涨杀跌"),
    ]
    md = render_markdown(summarize(rows, days=0))

    assert "信号漏斗" in md
    assert "通过率" in md
    assert "拦截原因分布" in md
    assert "重复拦截" in md
    assert "按日" in md


def test_main_returns_error_when_journal_missing(tmp_path: Path, capsys) -> None:
    """账本不存在要如实返回非零，不能静默输出空报告。"""
    code = main(["--journal", str(tmp_path / "nope.jsonl")])
    assert code == 1
    assert "账本不存在" in capsys.readouterr().out


def test_main_json_output_is_parsable(tmp_path: Path, capsys) -> None:
    path = tmp_path / "j.jsonl"
    path.write_text(
        json.dumps(_row(ts="2026-11-05T09:35:00", reason="今日暴涨 7%，不追涨杀跌")),
        encoding="utf-8",
    )
    assert main(["--journal", str(path), "--days", "0", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked"] == 1
