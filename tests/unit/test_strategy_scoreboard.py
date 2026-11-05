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


# ── 缓存解析：能不能直接读仓库 data/parquet_cache ──────────────


def _write(path, rows: int) -> None:
    _frame(DATES[:rows] if rows <= len(DATES) else DATES).to_parquet(path)


def test_resolve_prefers_exact_symbol_file(tmp_path) -> None:
    from scripts.strategy_scoreboard import resolve_cache_file

    _write(tmp_path / "600519.parquet", 5)
    _write(tmp_path / "600519_100d_none.parquet", 10)
    assert resolve_cache_file("600519", tmp_path).name == "600519.parquet"


def test_resolve_picks_longest_dated_file(tmp_path) -> None:
    from scripts.strategy_scoreboard import resolve_cache_file

    _write(tmp_path / "600519_5d_none.parquet", 5)
    _write(tmp_path / "600519_12d_none.parquet", 12)
    _write(tmp_path / "600519_8d_qfq.parquet", 8)
    assert resolve_cache_file("600519", tmp_path).name == "600519_12d_none.parquet"


def test_resolve_handles_exchange_prefix(tmp_path) -> None:
    """标的池常写 sh600519，缓存文件名是 600519_XXXd，必须能对上。"""
    from scripts.strategy_scoreboard import resolve_cache_file

    _write(tmp_path / "600519_10d_none.parquet", 10)
    assert resolve_cache_file("sh600519", tmp_path).name == "600519_10d_none.parquet"
    assert resolve_cache_file("sz000858", tmp_path) is None


def test_resolve_returns_none_when_absent(tmp_path) -> None:
    from scripts.strategy_scoreboard import resolve_cache_file

    assert resolve_cache_file("600519", tmp_path) is None


def test_resolve_index_prefers_index_file(tmp_path) -> None:
    from scripts.strategy_scoreboard import resolve_index_file

    _write(tmp_path / "_index.parquet", 5)
    _write(tmp_path / "000300_10d_none.parquet", 10)
    assert resolve_index_file("sh000300", tmp_path).name == "_index.parquet"


def test_resolve_index_falls_back_to_dated_benchmark(tmp_path) -> None:
    from scripts.strategy_scoreboard import resolve_index_file

    _write(tmp_path / "000300_9d_none.parquet", 9)
    assert resolve_index_file("sh000300", tmp_path).name == "000300_9d_none.parquet"


# ── 对照组：同池无条件基准 ───────────────────────────────


def _long_frame(rows: int = 80, *, base: float = 10.0, step: float = 0.0) -> pd.DataFrame:
    """构造足够长（>= MIN_HISTORY_ROWS）的行情表，供 baseline_stat 使用。"""
    dates = pd.date_range("2024-01-01", periods=rows, freq="D").strftime("%Y-%m-%d").tolist()
    return _frame(dates, base=base, step=step)


def test_always_on_strategy_signals_every_row_without_mutating_input() -> None:
    from scripts.strategy_scoreboard import AlwaysOnStrategy

    frame = _frame(DATES, base=10.0, step=0.01)
    out = AlwaysOnStrategy().generate_signals(frame, is_backtest=True)

    assert list(out["signal"]) == [1] * len(DATES)
    assert "signal" not in frame.columns


def test_baseline_density_is_one_hundred_percent() -> None:
    """对照组必须是"每天都能成交"，密度 100% 才叫无条件基准。"""
    from scripts.strategy_scoreboard import baseline_stat

    stat = baseline_stat({"X": _long_frame()}, _long_frame(base=100.0), horizon=3, cost_rate=0.0)

    assert stat["density"] == pytest.approx(1.0)
    assert stat["samples"] > 0


def test_baseline_excess_is_exactly_negative_cost_when_prices_flat() -> None:
    """价格不动时，无条件超额只能等于被扣掉的那笔成本，一分不多。"""
    from scripts.strategy_scoreboard import baseline_stat

    flat = _long_frame(base=10.0, step=0.0)
    bench = _long_frame(base=100.0, step=0.0)
    stat = baseline_stat({"X": flat}, bench, horizon=3, cost_rate=0.004)

    assert stat["mean_excess"] == pytest.approx(-0.004)
    # 每个事件完全一样，方差为 0，t 值无意义 -> None，不能凑成 0
    assert stat["t_event"] is None
    assert stat["t_symbol"] is None


def test_baseline_respects_cost_rate() -> None:
    from scripts.strategy_scoreboard import baseline_stat

    flat = _long_frame(base=10.0, step=0.0)
    bench = _long_frame(base=100.0, step=0.0)

    free = baseline_stat({"X": flat}, bench, horizon=3, cost_rate=0.0)
    paid = baseline_stat({"X": flat}, bench, horizon=3, cost_rate=0.02)

    assert free["mean_excess"] == pytest.approx(0.0)
    assert paid["mean_excess"] == pytest.approx(-0.02)


def test_baseline_picks_up_pool_drift_against_benchmark() -> None:
    """池子涨得比基准快时无条件超额必须为正——这正是"beta 冒充 alpha"的来源。"""
    from scripts.strategy_scoreboard import baseline_stat

    pool = _long_frame(base=10.0, step=0.01)  # 每日 +1%
    bench = _long_frame(base=100.0, step=0.0)  # 基准不动
    stat = baseline_stat({"X": pool}, bench, horizon=3, cost_rate=0.0)

    assert stat["mean_excess"] > 0


def test_baseline_ignores_symbols_shorter_than_min_history() -> None:
    from scripts.strategy_scoreboard import baseline_stat

    frames = {"LONG": _long_frame(80), "SHORT": _frame(DATES[:10], base=10.0)}
    bench = _long_frame(base=100.0)
    only_long = baseline_stat({"LONG": _long_frame(80)}, bench, horizon=3, cost_rate=0.0)

    stat = baseline_stat(frames, bench, horizon=3, cost_rate=0.0)
    assert stat["samples"] == only_long["samples"]
    assert stat["density"] == pytest.approx(1.0)


def test_baseline_without_benchmark_returns_empty_not_zero() -> None:
    from scripts.strategy_scoreboard import baseline_stat

    stat = baseline_stat({"X": _long_frame()}, pd.DataFrame(), horizon=3)

    assert stat["samples"] == 0
    assert stat["mean_excess"] is None


def test_render_markdown_shows_baseline_row_and_caveat() -> None:
    stats = [
        {
            "strategy": "demo",
            "density": 0.3,
            "samples": 100,
            "mean_excess": 0.001,
            "win_rate": 0.5,
            "t_event": 1.0,
            "t_symbol": 0.5,
        }
    ]
    baseline = {
        "strategy": "同池无条件基准",
        "density": 1.0,
        "samples": 1000,
        "mean_excess": -0.0004,
        "win_rate": 0.43,
        "t_event": -0.96,
        "t_symbol": -0.37,
    }
    text = render_markdown(stats, {"baseline": baseline})

    assert "（对照）同池无条件基准" in text
    assert "100%" in text
    assert "beta" in text and "alpha" in text


def test_render_markdown_omits_baseline_when_absent() -> None:
    text = render_markdown([], {"symbols": 0})

    assert "（对照）" not in text


def test_render_markdown_omits_baseline_when_no_samples() -> None:
    baseline = {
        "strategy": "同池无条件基准",
        "density": 0.0,
        "samples": 0,
        "mean_excess": None,
        "win_rate": None,
        "t_event": None,
        "t_symbol": None,
    }
    text = render_markdown([], {"baseline": baseline})

    assert "（对照）" not in text


# ── 净 alpha：扣掉池子漂移后的配对检验 ──────────────────


def _events(mapping: dict[str, list[float]]) -> "object":
    from scripts.strategy_scoreboard import Events

    return Events(per_symbol=mapping)


def test_net_alpha_pairs_by_symbol_not_by_event() -> None:
    """配对单位必须是标的：一只标的信号再多，也不能一票否决其他标的。

    逐事件平均会是 (0.01*10 + 0.02*10 + 0.03*200)/220 ≈ 2.8%，被 C 的样本量
    拖偏；逐标的平均是 (1% + 2% + 3%)/3 = 2%。
    """
    from scripts.strategy_scoreboard import net_alpha

    rule = _events({"A": [0.01] * 10, "B": [0.02] * 10, "C": [0.03] * 200})
    base = _events({"A": [0.0] * 10, "B": [0.0] * 10, "C": [0.0] * 10})

    got = net_alpha(rule, base)

    assert got["pairs"] == 3
    assert got["mean_alpha"] == pytest.approx(0.02)


def test_net_alpha_is_zero_when_rule_equals_baseline() -> None:
    from scripts.strategy_scoreboard import net_alpha

    same = {
        "A": [0.01, 0.02, 0.03, 0.04, 0.05],
        "B": [0.02, 0.01, 0.0, 0.03, 0.01],
        "C": [-0.01, -0.02, 0.0, 0.01, 0.02],
    }
    got = net_alpha(_events(same), _events(same))

    assert got["pairs"] == 3
    assert got["mean_alpha"] == pytest.approx(0.0)
    # 差值全为 0，没有方差，t 不能报成 0 冒充"确认无效"
    assert got["t_alpha"] is None


def test_net_alpha_skips_symbols_with_thin_rule_samples() -> None:
    """单标的规则事件不足 5 笔时不参与配对——不拿一笔交易代表一只标的。"""
    from scripts.strategy_scoreboard import net_alpha

    rule = _events({"A": [0.05]})  # 只有 1 笔
    base = _events({"A": [0.0] * 10})

    got = net_alpha(rule, base)

    assert got["pairs"] == 0
    assert got["mean_alpha"] is None
    assert got["t_alpha"] is None
    assert got["positive_ratio"] is None


def test_net_alpha_reports_none_below_three_pairs() -> None:
    from scripts.strategy_scoreboard import net_alpha

    rule = _events({"A": [0.01] * 6, "B": [0.02] * 6})
    base = _events({"A": [0.0] * 6, "B": [0.0] * 6})

    got = net_alpha(rule, base)

    assert got["pairs"] == 2
    assert got["mean_alpha"] is None
    assert got["t_alpha"] is None


def test_net_alpha_ignores_symbol_missing_from_baseline() -> None:
    """基准里没有这只标的，就没有对照组，不能拿别的标的凑。"""
    from scripts.strategy_scoreboard import net_alpha

    rule = _events({"A": [0.01] * 6, "Z": [0.09] * 6})
    base = _events({"A": [0.0] * 6})

    got = net_alpha(rule, base)

    assert got["pairs"] == 1
    assert got["mean_alpha"] is None


def test_net_alpha_reports_positive_ratio_and_sign() -> None:
    from scripts.strategy_scoreboard import net_alpha

    rule = _events({"A": [0.03] * 6, "B": [0.03] * 6, "C": [-0.01] * 6})
    base = _events({"A": [0.0] * 6, "B": [0.0] * 6, "C": [0.0] * 6})

    got = net_alpha(rule, base)

    assert got["mean_alpha"] == pytest.approx((0.03 + 0.03 - 0.01) / 3)
    assert got["positive_ratio"] == pytest.approx(2 / 3)
    assert got["t_alpha"] is not None


def test_baseline_events_stat_matches_baseline_stat() -> None:
    from scripts.strategy_scoreboard import AlwaysOnStrategy, baseline_events, baseline_stat

    frames = {"X": _long_frame(80, base=10.0, step=0.005)}
    bench = _long_frame(base=100.0, step=0.002)

    direct = baseline_stat(frames, bench, horizon=2, cost_rate=0.001)
    via = build_stat(
        AlwaysOnStrategy.name, baseline_events(frames, bench, horizon=2, cost_rate=0.001)
    )

    assert direct == via


def test_render_markdown_shows_net_alpha_columns() -> None:
    stats = [
        {
            "strategy": "demo",
            "density": 0.3,
            "samples": 100,
            "mean_excess": 0.001,
            "win_rate": 0.5,
            "t_event": 1.0,
            "t_symbol": 0.5,
            "pairs": 39,
            "mean_alpha": 0.0012,
            "t_alpha": 0.4,
            "positive_ratio": 0.55,
        }
    ]

    text = render_markdown(stats, {"symbols": 39})

    assert "净alpha" in text
    assert "选股alpha" in text
    assert "+0.12%" in text
    # 夹具没给 t_selection，这一列必须是破折号而不是 0
    row = next(line for line in text.splitlines() if line.startswith("| demo |"))
    assert row.count("—") >= 1


def test_render_markdown_marks_missing_net_alpha() -> None:
    stats = [
        {
            "strategy": "demo",
            "density": 0.3,
            "samples": 100,
            "mean_excess": 0.001,
            "win_rate": 0.5,
            "t_event": 1.0,
            "t_symbol": 0.5,
            "pairs": 2,
            "mean_alpha": None,
            "t_alpha": None,
            "positive_ratio": None,
        }
    ]

    text = render_markdown(stats, {"symbols": 39})
    row = next(line for line in text.splitlines() if line.startswith("| demo |"))

    assert row.count("—") >= 2


def test_default_cache_matches_backfill_tool() -> None:
    """体检表必须读补数工具写的那个目录。

    2026-11-05 实际踩到：体检表默认 data/kline_cache，补数工具写
    data/parquet_cache，于是 `--strategies ...` 不带 --cache-dir 时会静默
    回退联网、被限流后报「基准不可用」——数据明明在本地。这类漂移不报错，
    只给出错误的"没数据"，所以钉一条测试。
    """
    import scripts.backfill_history as backfill
    import scripts.strategy_scoreboard as board

    assert board.DEFAULT_CACHE == backfill.DEFAULT_CACHE
    assert board.DEFAULT_CACHE.name == "parquet_cache"
