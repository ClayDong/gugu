"""策略同口径体检表测试（2026-11-05）。

这个工具的作用是"用一张表回答策略到底有没有 edge"，所以测试重点不是覆盖率，
而是**口径不能撒谎**：
- 入场必须是次日开盘（信号当日收盘才知道，当日收盘价买不进）；
- 成本必须真的扣掉；
- 基准缺失时必须丢弃事件，不能用 0 冒充；
- t 值样本不足时必须返回 nan，不能凑出一个"显著"。

背景：P0-2 原计划是"改策略让信号过 5% 涨跌幅闸门"。这组口径跑出来的结果是
`two_b_reversal` 扣成本后 5 日净超额 -0.09%（t=-1.19），收紧进场反而更差，
于是"改策略"这个方向被数据否决。工具存在的意义就是让这种否决可复现。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from scripts.strategy_scoreboard import (
    Events,
    build_stat,
    collect_events,
    main,
    net_excess_return,
    render_markdown,
    t_stat,
)


class _FixedStrategy:
    """在指定日期发买入信号的假策略，用来锁定口径。"""

    def __init__(self, signal_dates: set[str]) -> None:
        self._dates = signal_dates

    def generate_signals(self, df: pd.DataFrame, is_backtest: bool = False) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = [1 if str(d) in self._dates else 0 for d in out["date"]]
        return out


class _BoomStrategy:
    def generate_signals(self, df: pd.DataFrame, is_backtest: bool = False) -> pd.DataFrame:
        raise RuntimeError("策略内部炸了")


def _frame(dates: list[str], *, base: float = 10.0, step: float = 0.0) -> pd.DataFrame:
    """构造等长 OHLCV；open/close 按 step 线性漂移，方便手算。"""
    rows = []
    price = base
    for d in dates:
        rows.append(
            {
                "date": d,
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price * (1 + step),
                "volume": 1_000_000,
                "amount": 10_000_000,
            }
        )
        price = price * (1 + step)
    return pd.DataFrame(rows)


DATES = [f"2024-03-{d:02d}" for d in range(1, 21)]


# ── 基础统计口径 ──────────────────────────────────────────


def test_t_stat_needs_three_samples() -> None:
    assert math.isnan(t_stat([]))
    assert math.isnan(t_stat([0.01, 0.02]))


def test_t_stat_matches_manual_computation() -> None:
    values = [0.01, 0.02, 0.03, 0.04]
    mean = sum(values) / 4
    sd = statistics_stdev(values)
    assert t_stat(values) == pytest.approx(mean / (sd / math.sqrt(4)))


def statistics_stdev(values: list[float]) -> float:
    import statistics

    return statistics.stdev(values)


def test_t_stat_returns_nan_when_flat() -> None:
    """方差为 0 时没有 t 可言，不能返回 0 冒充"无效应"。"""
    assert math.isnan(t_stat([0.01, 0.01, 0.01]))


# ── 超额收益口径 ──────────────────────────────────────────


def test_net_excess_subtracts_benchmark_and_cost() -> None:
    # 股票 100 → 110，基准 100 → 105，成本 1%
    got = net_excess_return(100.0, 110.0, 100.0, 105.0, 0.01)
    expected = (110 / 100) * 0.99 - 1 - 0.05
    assert got == pytest.approx(expected)


def test_net_excess_without_cost_is_pure_difference() -> None:
    got = net_excess_return(100.0, 110.0, 100.0, 105.0, 0.0)
    assert got == pytest.approx(0.10 - 0.05)


@pytest.mark.parametrize(
    "entry,exit_,bench_in,bench_out",
    [
        (0.0, 110.0, 100.0, 105.0),
        (100.0, 0.0, 100.0, 105.0),
        (100.0, 110.0, 0.0, 105.0),
        (100.0, 110.0, 100.0, 0.0),
        (float("nan"), 110.0, 100.0, 105.0),
    ],
)
def test_net_excess_rejects_invalid_prices(entry, exit_, bench_in, bench_out) -> None:
    assert net_excess_return(entry, exit_, bench_in, bench_out, 0.004) is None


# ── 事件采集 ─────────────────────────────────────────────


def test_collect_events_enters_next_open_and_exits_at_horizon_close() -> None:
    stock = _frame(DATES, base=10.0, step=0.01)
    index = _frame(DATES, base=100.0, step=0.0)
    strategy = _FixedStrategy({DATES[5]})

    events = collect_events(
        strategy, {"X": stock}, index, horizon=3, cost_rate=0.0, start="2024-01-01", min_rows=5
    )

    # 信号在 idx 5，入场 idx 6 的 open，出场 idx 9 的 close
    expected = stock.loc[9, "close"] / stock.loc[6, "open"] - 1 - 0.0
    assert events.returns == [pytest.approx(expected)]


def test_collect_events_applies_cost() -> None:
    stock = _frame(DATES, base=10.0, step=0.01)
    index = _frame(DATES, base=100.0, step=0.0)
    strategy = _FixedStrategy({DATES[5]})

    free = collect_events(
        strategy, {"X": stock}, index, horizon=3, cost_rate=0.0, start="2024-01-01", min_rows=5
    )
    paid = collect_events(
        strategy, {"X": stock}, index, horizon=3, cost_rate=0.01, start="2024-01-01", min_rows=5
    )

    assert paid.returns[0] < free.returns[0]


def test_collect_events_counts_density_over_all_trading_days() -> None:
    stock = _frame(DATES)
    index = _frame(DATES, base=100.0, step=0.0)
    strategy = _FixedStrategy({DATES[5], DATES[6]})

    events = collect_events(
        strategy, {"X": stock}, index, horizon=2, start="2024-01-01", min_rows=5
    )

    assert events.total_days == len(DATES)
    assert events.signal_days == 2
    assert events.density == pytest.approx(2 / len(DATES))


def test_collect_events_drops_signal_when_benchmark_missing() -> None:
    stock = _frame(DATES)
    index = _frame(DATES[:6], base=100.0, step=0.0)  # 基准只到第 6 天
    strategy = _FixedStrategy({DATES[4]})

    events = collect_events(
        strategy, {"X": stock}, index, horizon=3, start="2024-01-01", min_rows=5
    )

    assert events.returns == []
    assert events.signal_days == 1


def test_collect_events_respects_start_filter() -> None:
    stock = _frame(DATES)
    index = _frame(DATES, base=100.0, step=0.0)
    strategy = _FixedStrategy({DATES[3]})

    events = collect_events(strategy, {"X": stock}, index, horizon=2, start=DATES[10], min_rows=5)

    assert events.returns == []


def test_collect_events_skips_tail_without_full_horizon() -> None:
    stock = _frame(DATES)
    index = _frame(DATES, base=100.0, step=0.0)
    strategy = _FixedStrategy({DATES[-1]})

    events = collect_events(
        strategy, {"X": stock}, index, horizon=3, start="2024-01-01", min_rows=5
    )

    assert events.returns == []


def test_collect_events_survives_strategy_exception() -> None:
    stock = _frame(DATES)
    index = _frame(DATES, base=100.0, step=0.0)

    events = collect_events(
        _BoomStrategy(), {"X": stock}, index, horizon=2, start="2024-01-01", min_rows=5
    )

    assert events.returns == []


def test_collect_events_returns_empty_without_index() -> None:
    events = collect_events(_FixedStrategy(set()), {"X": _frame(DATES)}, pd.DataFrame())
    assert events.returns == []


# ── 聚合与渲染 ───────────────────────────────────────────


def test_build_stat_reports_counts_and_mean() -> None:
    events = Events(
        per_symbol={"A": [0.01] * 10, "B": [0.02] * 10, "C": [0.03] * 10},
        signal_days=30,
        total_days=100,
    )
    stat = build_stat("demo", events)

    assert stat["samples"] == 30
    assert stat["density"] == pytest.approx(0.3)
    assert stat["mean_excess"] == pytest.approx(0.02)
    assert stat["win_rate"] == pytest.approx(1.0)


def test_build_stat_t_symbol_is_more_conservative_than_t_event() -> None:
    """按标的聚合是保守口径：簇内高度相似会把逐事件 t 抬得虚高。

    三个标的各自很稳但水平不同（0.01/0.02/0.03），逐事件看像 30 个独立样本，
    实际只有 3 个独立标的。保守 t 必须明显小于逐事件 t。
    """
    events = Events(
        per_symbol={
            "A": [0.0100, 0.0101, 0.0099, 0.0102, 0.0098] * 2,
            "B": [0.0200, 0.0201, 0.0199, 0.0202, 0.0198] * 2,
            "C": [0.0300, 0.0301, 0.0299, 0.0302, 0.0298] * 2,
        },
        signal_days=30,
        total_days=100,
    )
    stat = build_stat("demo", events)

    assert stat["t_event"] is not None
    assert stat["t_symbol"] is not None
    assert stat["t_symbol"] < stat["t_event"]


def test_build_stat_symbol_t_ignores_single_sample_clusters() -> None:
    """少于 5 个样本的标的簇不参与保守 t——防止一只票的运气被当成 edge。"""
    events = Events(
        per_symbol={"A": [0.05] * 10, "B": [0.05] * 10, "C": [0.05] * 2},
        signal_days=22,
        total_days=100,
    )
    stat = build_stat("demo", events)

    assert stat["samples"] == 22
    # 两个簇、值相同 → 标准差为 0 → 无法估 t，必须是 None 而不是编一个数
    assert stat["t_symbol"] is None


def test_build_stat_handles_no_samples() -> None:
    stat = build_stat("empty", Events())
    assert stat["samples"] == 0
    assert stat["mean_excess"] is None
    assert stat["t_symbol"] is None


def test_render_markdown_lists_every_strategy() -> None:
    stats = [
        {
            "strategy": "a",
            "density": 0.2,
            "samples": 10,
            "mean_excess": -0.001,
            "win_rate": 0.4,
            "t_event": -1.0,
            "t_symbol": -0.9,
        },
        {
            "strategy": "b",
            "density": 0.05,
            "samples": 0,
            "mean_excess": None,
            "win_rate": None,
            "t_event": None,
            "t_symbol": None,
        },
    ]
    text = render_markdown(
        stats,
        {"start": "2024-02-01", "cost": 0.004, "horizon": 5, "index": "sh000300", "symbols": 39},
    )

    assert "| a |" in text
    assert "| b |" in text
    assert "-0.10%" in text
    assert "标的数" in text


# ── 命令行契约 ───────────────────────────────────────────


def test_main_returns_error_when_pool_missing(tmp_path) -> None:
    code = main(["--pool-file", str(tmp_path / "nope.json")])
    assert code == 1


def test_main_returns_error_when_frames_unavailable(tmp_path, monkeypatch) -> None:
    pool = tmp_path / "pool.json"
    pool.write_text('{"events": [{"code": "sz000001"}]}', encoding="utf-8")

    import scripts.strategy_scoreboard as board

    monkeypatch.setattr(board, "load_index", lambda *a, **k: _frame(DATES, base=100.0))
    monkeypatch.setattr(board, "load_frames", lambda *a, **k: ({}, ["sz000001"]))

    code = main(["--pool-file", str(pool), "--cache-dir", str(tmp_path / "cache")])
    assert code == 1


def test_main_returns_error_when_index_unavailable(tmp_path, monkeypatch) -> None:
    pool = tmp_path / "pool.json"
    pool.write_text('{"events": [{"code": "sz000001"}]}', encoding="utf-8")

    import scripts.strategy_scoreboard as board

    monkeypatch.setattr(board, "load_index", lambda *a, **k: pd.DataFrame())

    code = main(["--pool-file", str(pool), "--cache-dir", str(tmp_path / "cache")])
    assert code == 1
