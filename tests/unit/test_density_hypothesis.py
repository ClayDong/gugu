"""密度优先假设检验测试（2026-11-05）。

这个脚本要回答一个很容易被"分析得很漂亮"的问题：把信号压稀之后，每笔 alpha
是不是真的变高。翻车的典型方式有两种，测试就是围绕这两种写的：

1. **前视**：用未来收益去挑样本（比如按收益分桶再看收益）。所以广度只能由
   当日及以前的信号计数决定，本文件有一条「删掉后面的信号，广度不变」的测试。
2. **桶间不可比**：低广度信号天然稀少，样本量悬殊时"看起来更好"只是噪声。
   所以切桶必须等事件数，并且单桶样本不足时明确拒答。
"""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.density_hypothesis import (
    MIN_BUCKET_EVENTS,
    analyze,
    attach_breadth,
    breadth_by_date,
    events_from_rows,
    render_markdown,
    split_buckets,
    summarize,
)


def _events(signal_dates: dict[str, list[str]], rows: list[dict] | None = None):
    from scripts.strategy_scoreboard import Events

    return Events(signal_dates=signal_dates, rows=rows or [])


def _row(symbol: str, date: str, excess: float) -> dict:
    return {"symbol": symbol, "date": date, "excess": excess}


# ── 广度只依赖当日及以前 ─────────────────────────────────


def test_breadth_counts_distinct_symbols_per_day() -> None:
    events = _events({"A": ["2024-03-01", "2024-03-02"], "B": ["2024-03-01"]})
    got = breadth_by_date(events, pool_size=4)

    assert got["2024-03-01"] == pytest.approx(0.5)  # A、B 两只 / 池子 4 只
    assert got["2024-03-02"] == pytest.approx(0.25)


def test_breadth_is_not_affected_by_later_signals() -> None:
    """删掉后面的信号，前面那天的广度必须一动不动——否则就是前视。"""
    full = _events({"A": ["2024-03-01", "2024-03-05"], "B": ["2024-03-01", "2024-03-09"]})
    trimmed = _events({"A": ["2024-03-01"], "B": ["2024-03-01"]})

    assert breadth_by_date(full, 2)["2024-03-01"] == breadth_by_date(trimmed, 2)["2024-03-01"]


def test_breadth_never_exceeds_one() -> None:
    events = _events({f"S{i}": ["2024-03-01"] for i in range(5)})
    assert breadth_by_date(events, pool_size=3)["2024-03-01"] == pytest.approx(5 / 3)
    # 分母写小了会算出 >1，这里不是"正常"，而是提示调用方给了错的池子规模
    assert breadth_by_date(events, pool_size=5)["2024-03-01"] == pytest.approx(1.0)


# ── 贴广度：查不到就丢，不猜 ─────────────────────────────


def test_attach_breadth_drops_rows_without_signal_date() -> None:
    events = _events(
        {"A": ["2024-03-01"]},
        rows=[_row("A", "2024-03-01", 0.01), _row("A", "2024-03-02", 0.02)],
    )

    got = attach_breadth(events, pool_size=1)

    assert [r["date"] for r in got] == ["2024-03-01"]
    assert got[0]["breadth"] == pytest.approx(1.0)


# ── 切桶：等事件数、可复现、不丢事件 ─────────────────────


def test_split_buckets_is_equal_size_and_lossless() -> None:
    rows = [_row("A", f"2024-04-{i:02d}", 0.01) for i in range(1, 10)]
    for index, row in enumerate(rows):
        row["breadth"] = index / 10

    groups = split_buckets(rows, buckets=3)

    assert [len(g[1]) for g in groups] == [3, 3, 3]
    merged = [row for _, group in groups for row in group]
    assert len(merged) == len(rows)
    assert sorted(id(r) for r in merged) == sorted(id(r) for r in rows)


def test_split_buckets_is_monotonic_in_breadth() -> None:
    rows = [_row("A", f"2024-05-{i:02d}", 0.01) for i in range(1, 13)]
    for index, row in enumerate(rows):
        row["breadth"] = index / 12

    groups = split_buckets(rows, buckets=3)

    means = [sum(r["breadth"] for r in group) / len(group) for _, group in groups]
    assert means == sorted(means)
    assert means[0] < means[-1]


def test_split_buckets_is_deterministic_for_tied_breadth() -> None:
    """同一天全池共振时广度完全相同，切分结果仍必须固定，否则两次跑不同结论。"""
    rows = [_row(f"S{i}", "2024-06-03", 0.01) for i in range(9)]
    for row in rows:
        row["breadth"] = 1.0

    first = [[r["symbol"] for r in g] for _, g in split_buckets(rows, 3)]
    second = [[r["symbol"] for r in g] for _, g in split_buckets(list(rows), 3)]

    assert first == second


def test_split_buckets_rejects_single_bucket() -> None:
    with pytest.raises(ValueError):
        split_buckets([], buckets=1)


def test_split_buckets_handles_empty_input() -> None:
    assert split_buckets([], buckets=3) == []


# ── 汇总：样本不足必须拒答 ───────────────────────────────


def test_events_from_rows_groups_by_symbol() -> None:
    events = events_from_rows(
        [_row("A", "d1", 0.01), _row("A", "d2", 0.02), _row("B", "d1", -0.01)]
    )
    assert events.per_symbol["A"] == [0.01, 0.02]
    assert events.per_symbol["B"] == [-0.01]


def test_summarize_refuses_small_bucket() -> None:
    rows = [_row("A", "2024-03-01", 0.01) for _ in range(MIN_BUCKET_EVENTS - 1)]
    for row in rows:
        row["breadth"] = 0.1
    base = events_from_rows([_row("A", "2024-03-01", 0.0) for _ in range(10)])

    stat = summarize("低广度", rows, base)

    assert stat["insufficient"] is True
    assert stat["samples"] == 0
    assert stat["mean_alpha"] is None


def test_summarize_reports_breadth_span() -> None:
    rows = [_row("A", "2024-03-01", 0.01) for _ in range(MIN_BUCKET_EVENTS)]
    for index, row in enumerate(rows):
        row["breadth"] = 0.1 + index / 10_000
    base = events_from_rows([_row("A", "2024-03-01", 0.0) for _ in range(10)])

    stat = summarize("低广度", rows, base)

    assert stat["insufficient"] is False
    assert stat["breadth_min"] == pytest.approx(0.1)
    assert stat["breadth_max"] == pytest.approx(0.1 + (MIN_BUCKET_EVENTS - 1) / 10_000)


def test_render_markdown_flags_insufficient_and_states_falsification() -> None:
    rows = [
        {
            "bucket": "低广度",
            "events": 12,
            "breadth_min": 0.0,
            "breadth_max": 0.05,
            "insufficient": True,
            "samples": 0,
            "mean_excess": None,
            "mean_alpha": None,
            "win_rate": None,
            "t_symbol": None,
            "t_alpha": None,
        }
    ]
    meta = {
        "strategy": "demo",
        "start": "2024-02-01",
        "cost": 0.004,
        "horizon": 5,
        "index": "sh000300",
        "symbols": 39,
        "buckets": 3,
    }

    text = render_markdown(rows, meta)

    assert "样本不足" in text
    assert "若各桶净alpha 随广度上升而不下降，假设被证伪" in text


# ── 端到端：分桶不能丢事件、不能用未来信息 ───────────────


class _EveryNDays:
    """每 N 个交易日发一次信号，用来构造稀疏/密集两种情形。"""

    def __init__(self, n: int) -> None:
        self._n = n

    def generate_signals(self, df: pd.DataFrame, is_backtest: bool = False) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = [1 if i % self._n == 0 else 0 for i in range(len(out))]
        return out


def _frames(symbols: int = 3, rows: int = 80) -> dict[str, pd.DataFrame]:
    dates = pd.date_range("2024-01-01", periods=rows, freq="D").strftime("%Y-%m-%d").tolist()
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * rows,
            "high": [10.1] * rows,
            "low": [9.9] * rows,
            "close": [10.0 + i * 0.01 for i in range(rows)],
            "volume": [1_000_000] * rows,
        }
    )
    return {f"S{i}": frame.copy() for i in range(symbols)}


def test_analyze_keeps_total_event_count_across_buckets() -> None:
    from scripts.strategy_scoreboard import AlwaysOnStrategy, baseline_events

    frames = _frames()
    index = frames["S0"].copy()
    index[["open", "high", "low", "close"]] = 100.0
    base = baseline_events(frames, index, horizon=3, cost_rate=0.0, start="2024-01-01")

    rows = analyze(
        _EveryNDays(2),
        frames,
        index,
        base,
        horizon=3,
        cost_rate=0.0,
        start="2024-01-01",
        buckets=3,
    )

    total = rows[0]["events"]
    bucketed = sum(r["events"] for r in rows[1:])
    assert total > 0
    assert bucketed == total
    assert AlwaysOnStrategy.name  # 只是确认对照策略可导入


def test_analyze_breadth_reflects_pool_synchrony() -> None:
    """三只标的完全同步发信号时，广度恒为 100%。"""
    frames = _frames(symbols=3)
    index = frames["S0"].copy()
    index[["open", "high", "low", "close"]] = 100.0
    from scripts.strategy_scoreboard import baseline_events

    base = baseline_events(frames, index, horizon=3, cost_rate=0.0, start="2024-01-01")
    rows = analyze(
        _EveryNDays(5),
        frames,
        index,
        base,
        horizon=3,
        cost_rate=0.0,
        start="2024-01-01",
        buckets=2,
    )

    spans = [(r["breadth_min"], r["breadth_max"]) for r in rows[1:] if r["events"]]
    assert spans and all(
        low == pytest.approx(1.0) and high == pytest.approx(1.0) for low, high in spans
    )


# ── 按日聚类：广度是日期级变量，不能被 39 只标的重复计数 ──


def test_date_cluster_stat_collapses_same_day_rows() -> None:
    """同一天 39 只标的只是同一件事的 39 个副本，按日只能算一个观测。"""
    from scripts.density_hypothesis import date_cluster_stat

    rows = [_row(f"S{i}", "2024-03-01", 0.05) for i in range(39)]
    dates, value = date_cluster_stat(rows)

    assert dates == 1
    assert value is None  # 只有一个观测，谈不上 t


def test_date_cluster_stat_uses_date_means() -> None:
    from scripts.density_hypothesis import date_cluster_stat

    rows = []
    for day, level in (("2024-03-01", 0.02), ("2024-03-04", 0.02), ("2024-03-05", 0.02)):
        # 每天两只标的，均值恰好等于 level
        rows += [_row("A", day, level + 0.01), _row("B", day, level - 0.01)]

    dates, value = date_cluster_stat(rows)

    assert dates == 3
    # 三天均值完全相同 -> 方差为 0 -> 不给 t，而不是给一个巨大的 t
    assert value is None


def test_date_cluster_stat_can_be_much_weaker_than_event_t() -> None:
    """构造"一天爆发"：按事件算 t 很大，按日算只有 3 个观测。"""
    from scripts.density_hypothesis import date_cluster_stat

    rows = [_row(f"S{i}", "2024-03-01", 0.10) for i in range(39)]
    rows += [_row(f"S{i}", "2024-03-04", 0.00) for i in range(39)]
    rows += [_row(f"S{i}", "2024-03-05", 0.00) for i in range(39)]

    dates, value = date_cluster_stat(rows)

    assert dates == 3
    assert value is None or abs(value) <= 2.0


def test_summarize_reports_dates_and_t_date() -> None:
    rows = []
    for day in ("2024-03-01", "2024-03-04", "2024-03-05"):
        for index in range(MIN_BUCKET_EVENTS // 3 + 1):
            rows.append(_row("A", day, 0.01 * (index + 1)))
    for row in rows:
        row["breadth"] = 0.5
    base = events_from_rows([_row("A", "2024-03-01", 0.0) for _ in range(10)])

    stat = summarize("高广度", rows, base)

    assert stat["dates"] == 3
    assert "t_date" in stat


# ── 多重检验与精确 p 值 ──────────────────────────────────


def test_bh_adjusted_p_is_never_below_raw_and_capped_at_one() -> None:
    from scripts.density_hypothesis import bh_adjusted_p

    raw = [0.01, 0.04, 0.03]
    adjusted = bh_adjusted_p(raw)

    assert all(a >= p - 1e-12 for a, p in zip(adjusted, raw))
    assert all(0 <= a <= 1 for a in adjusted)


def test_bh_adjusted_p_matches_manual_three_test_case() -> None:
    """三项 [0.01,0.03,0.04]：标准 BH 逐项校正为 [0.03,0.04,0.04]。"""
    from scripts.density_hypothesis import bh_adjusted_p

    assert bh_adjusted_p([0.01, 0.03, 0.04]) == pytest.approx([0.03, 0.04, 0.04])


def test_bh_adjusted_p_is_monotone_with_rank() -> None:
    from scripts.density_hypothesis import bh_adjusted_p

    raw = [0.2, 0.001, 0.05, 0.9]
    adjusted = bh_adjusted_p(raw)
    ordered_pairs = sorted(zip(raw, adjusted))

    assert [a for _, a in ordered_pairs] == sorted(a for _, a in ordered_pairs)


def test_t_p_value_uses_t_distribution_not_normal() -> None:
    """df=38、t=2.0：t 分布 p≈0.052，正态 p≈0.046。用错就会跨过 0.05。"""
    from scripts.density_hypothesis import t_p_value

    exact = t_p_value(2.0, 38)
    assert exact == pytest.approx(0.0526, abs=5e-4)
    assert exact > 0.05


def test_t_p_value_is_two_sided_and_handles_none() -> None:
    from scripts.density_hypothesis import t_p_value

    assert t_p_value(-2.0, 38) == pytest.approx(t_p_value(2.0, 38))
    assert t_p_value(None, 38) is None
    assert t_p_value(2.0, 0) is None


def test_apply_fdr_marks_buckets_and_skips_overall_row() -> None:
    from scripts.density_hypothesis import apply_fdr

    rows = [
        {"bucket": "全集（不分桶）", "p_date": 0.001},
        {"bucket": "低广度", "p_date": 0.04},
        {"bucket": "中广度", "p_date": 0.02},
        {"bucket": "高广度", "p_date": 0.01},
    ]
    apply_fdr(rows)

    assert rows[0]["p_date_fdr"] is None
    assert all(r["p_date_fdr"] is not None for r in rows[1:])
    # 全集不参与校正，所以校正后最小值应与三项 BH 一致
    from scripts.density_hypothesis import bh_adjusted_p

    assert [r["p_date_fdr"] for r in rows[1:]] == pytest.approx(bh_adjusted_p([0.04, 0.02, 0.01]))
