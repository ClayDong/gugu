"""内部人增减持事件研究测试（2026-11-05）。

这个脚本最容易在四个地方骗人，测试就围这四点写：

1. **前视**：数据里没有独立公告日（announcement_date 与 change_date 逐行相同），
   所以建仓必须发生在变动日**之后**的交易日开盘。测试用「把锚定日的开盘价设成
   极值」来验证取的是哪一天的开盘。
2. **样本虚高**：滚动全量快照会重复计同一事件，一次公告又因多个变动人拆成多行。
   两个都必须去重，否则 N 会大一个量级。
3. **没有同池对照**：只报绝对收益会把池子漂移当事件效应，缺对照时必须拒答。
4. **小样本**：事件数不足时不得给结论，不能拿半个数字当发现。
"""

from __future__ import annotations

import math
import random

import pandas as pd
import pytest

from scripts.insider_event_study import (
    MIN_CLUSTERS,
    MIN_EVENTS,
    analyze,
    build_rows,
    clustered_stat,
    excess_tables,
    load_events,
    main,
    placebo_p_value,
    pool_mean_by_date,
    render_markdown,
    sample_universe,
    select_events,
    summarize,
)

FLAT_INDEX_ROWS = 30


def _index(days: int = FLAT_INDEX_ROWS) -> pd.DataFrame:
    """平坦基准：开盘收盘都是 100，基准收益恒为 0，便于单独看个股腿。"""
    dates = [f"2026-07-{d:02d}" for d in range(1, days + 1)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0] * days,
            "close": [100.0] * days,
        }
    )


def _stock(days: int = FLAT_INDEX_ROWS, *, opens=None, closes=None) -> pd.DataFrame:
    index = _index(days)
    return pd.DataFrame(
        {
            "date": index["date"],
            "open": opens if opens is not None else [10.0] * days,
            "close": closes if closes is not None else [10.0] * days,
        }
    )


def _write_insider(path, rows: dict) -> None:
    pd.DataFrame(rows).to_parquet(path, index=False)


def _rows(spec: list[tuple[str, str, float]]) -> list[dict]:
    """(标的, 日期, 选股差额) → summarize 需要的行结构。"""
    out = []
    for symbol, day, gap in spec:
        out.append(
            {
                "symbol": symbol,
                "date": day,
                "event_date": day,
                "direction": "增持",
                "ratio": 0.01,
                "excess": gap,
                "pool_mean": 0.0,
                "gap": gap,
            }
        )
    return out


class TestLoadEvents:
    def test_dedups_same_event_across_snapshots(self, tmp_path):
        # 三份滚动全量快照各存了同一笔增持 → 事件数必须是 1
        for day in ("20260715", "20260716", "20260717"):
            _write_insider(
                tmp_path / f"{day}_insider_trading.parquet",
                {"code": ["600030"], "change_date": ["2026-07-10"], "direction": ["增持"]},
            )
        events = load_events(tmp_path, {"600030"})
        assert len(events) == 1
        assert events[0]["direction"] == "增持"

    def test_dedups_multiple_insiders_same_day(self, tmp_path):
        # 一次公告因 3 个变动人拆成 3 行 → 仍是 1 个事件
        _write_insider(
            tmp_path / "20260715_insider_trading.parquet",
            {
                "code": ["600030"] * 3,
                "change_date": ["2026-07-10"] * 3,
                "direction": ["增持"] * 3,
            },
        )
        assert len(load_events(tmp_path, {"600030"})) == 1

    def test_keeps_opposite_directions_separately(self, tmp_path):
        _write_insider(
            tmp_path / "20260715_insider_trading.parquet",
            {
                "code": ["600030", "600030"],
                "change_date": ["2026-07-10", "2026-07-10"],
                "direction": ["增持", "减持"],
            },
        )
        events = load_events(tmp_path, {"600030"})
        assert len(events) == 2
        assert {e["direction"] for e in events} == {"增持", "减持"}

    def test_filters_to_pool(self, tmp_path):
        _write_insider(
            tmp_path / "20260715_insider_trading.parquet",
            {
                "code": ["600030", "600031"],
                "change_date": ["2026-07-10", "2026-07-10"],
                "direction": ["增持", "增持"],
            },
        )
        events = load_events(tmp_path, {"600030"})
        assert [e["symbol"] for e in events] == ["600030"]

    def test_ignores_broken_snapshot(self, tmp_path):
        (tmp_path / "20260715_insider_trading.parquet").write_bytes(b"not parquet")
        _write_insider(
            tmp_path / "20260716_insider_trading.parquet",
            {"code": ["600030"], "change_date": ["2026-07-10"], "direction": ["增持"]},
        )
        assert len(load_events(tmp_path, {"600030"})) == 1


class TestExcessTables:
    def test_entry_uses_open_of_anchor_plus_lag(self):
        # 第 2 天开盘价是极值；lag=1 必须从那一天开盘买入，而不是锚定日当天
        opens = [10.0, 99.0, 10.0, 10.0]
        closes = [10.0, 10.0, 20.0, 10.0]
        tables, _ = excess_tables(
            {"600030": _stock(4, opens=opens, closes=closes)},
            _index(4),
            horizon=1,
            lag=1,
            cost=0.0,
        )
        # 锚定日 = 第 1 天 → 第 2 天开盘 99 买入 → 第 3 天收盘 20 卖出
        assert tables["600030"]["2026-07-01"] == pytest.approx(20 / 99 - 1)

    def test_lag_shifts_entry_forward(self):
        opens = [10.0, 50.0, 20.0, 10.0]
        closes = [10.0, 10.0, 10.0, 40.0]
        tables, _ = excess_tables(
            {"600030": _stock(4, opens=opens, closes=closes)},
            _index(4),
            horizon=2,
            lag=1,
            cost=0.0,
        )
        # 锚定日 07-01 → 07-02 开盘 50 买入 → 07-04 收盘 40 卖出
        assert tables["600030"]["2026-07-01"] == pytest.approx(40 / 50 - 1)

    def test_cost_is_deducted(self):
        tables, _ = excess_tables(
            {"600030": _stock(3)},
            _index(3),
            horizon=1,
            lag=1,
            cost=0.01,
        )
        # 价格不变，净超额就该正好是 −成本
        assert tables["600030"]["2026-07-01"] == pytest.approx(-0.01)

    def test_series_too_short_yields_no_anchor(self):
        tables, _ = excess_tables(
            {"600030": _stock(3)},
            _index(3),
            horizon=5,
            lag=1,
            cost=0.0,
        )
        assert tables["600030"] == {}

    def test_index_benchmark_leg_is_subtracted(self):
        index = _index(4)
        index.loc[1, "open"] = 100.0
        index.loc[2, "close"] = 110.0  # 基准 +10%
        tables, _ = excess_tables(
            {"600030": _stock(4)},
            index,
            horizon=1,
            lag=1,
            cost=0.0,
        )
        assert tables["600030"]["2026-07-01"] == pytest.approx(-0.10)


class TestBuildRows:
    def _tables(self):
        return (
            {"600030": {"2026-07-01": 0.02, "2026-07-03": 0.04}},
            {"600030": ["2026-07-01", "2026-07-02", "2026-07-03"]},
            {"2026-07-01": 0.01, "2026-07-03": 0.01},
        )

    def test_snaps_non_trading_day_back_to_last_trading_day(self):
        # 事件发生在周六（缺失日）→ 锚定到之前最近的交易日，而不是被丢掉
        tables, windows, pool = self._tables()
        rows, dropped = build_rows(
            [{"symbol": "600030", "event_date": "2026-07-04", "direction": "增持", "ratio": None}],
            tables,
            windows,
            pool,
        )
        assert dropped == 0
        assert rows[0]["date"] == "2026-07-03"

    def test_gap_is_excess_minus_same_day_pool(self):
        tables, windows, pool = self._tables()
        rows, _ = build_rows(
            [{"symbol": "600030", "event_date": "2026-07-01", "direction": "增持", "ratio": None}],
            tables,
            windows,
            pool,
        )
        assert rows[0]["gap"] == pytest.approx(0.02 - 0.01)

    def test_event_before_price_history_is_dropped(self):
        tables, windows, pool = self._tables()
        rows, dropped = build_rows(
            [{"symbol": "600030", "event_date": "1994-08-10", "direction": "增持", "ratio": None}],
            tables,
            windows,
            pool,
        )
        assert (rows, dropped) == ([], 1)

    def test_event_without_forward_window_is_dropped(self):
        # 事件落在行情末端，没有 horizon 天可以算收益 → 丢弃，不是当 0
        tables, windows, pool = self._tables()
        rows, dropped = build_rows(
            [{"symbol": "600030", "event_date": "2026-07-02", "direction": "增持", "ratio": None}],
            tables,
            windows,
            pool,
        )
        assert (rows, dropped) == ([], 1)


class TestPoolMean:
    def test_averages_across_symbols_per_date(self):
        means = pool_mean_by_date(
            {
                "A": {"d1": 0.01, "d2": 0.03},
                "B": {"d1": 0.03},
            }
        )
        assert means["d1"] == pytest.approx(0.02)
        assert means["d2"] == pytest.approx(0.03)


class TestClusteredStat:
    def test_uses_cluster_means_not_events(self):
        # 同一标的的 5 笔都在 +10%，另一标的 1 笔在 0：按事件算均值 8.3%，
        # 按标的算均值 5%。后者才是这只标的真实能贡献的信息量。
        values = [0.10] * 5 + [0.0]
        keys = ["A"] * 5 + ["B"]
        result = clustered_stat(values, keys)
        assert result["clusters"] == 2
        assert result["t"] is None  # 簇太少，拒答

    def test_reports_clusters_and_p(self):
        result = clustered_stat([0.05, 0.06, 0.07], ["A", "B", "C"])
        assert result["clusters"] == MIN_CLUSTERS
        # 三个簇均值 0.06、标准差 0.01 → t = 0.06 / (0.01/√3)
        assert result["mean"] == pytest.approx(0.06)
        assert result["t"] == pytest.approx(0.06 / (0.01 / math.sqrt(3)))
        assert result["p"] is not None and result["p"] < 0.05

    def test_cluster_mean_differs_from_event_mean(self):
        # 事件加权 vs 标的等权：同一标的重复多次时两者不同，必须分别给出
        values = [0.10, 0.10, 0.10, 0.00, 0.05]
        keys = ["A", "A", "A", "B", "C"]
        event_mean = sum(values) / len(values)
        cluster_mean = clustered_stat(values, keys)["mean"]
        assert event_mean == pytest.approx(0.07)
        assert cluster_mean == pytest.approx(0.05)

    def test_zero_variance_returns_no_t(self):
        result = clustered_stat([0.02, 0.02, 0.02], ["A", "B", "C"])
        assert result["t"] is None


class TestSummarize:
    def _kwargs(self):
        return {"tables": {}, "pool_mean": {}, "rng": random.Random(7)}

    def test_refuses_below_min_events(self):
        rows = _rows([(f"S{i}", "2026-07-01", 0.01) for i in range(MIN_EVENTS - 1)])
        out = summarize(rows, "增持", 1, 5, **self._kwargs())
        assert out["sufficient"] is False
        assert "样本" in out["reason"] or str(MIN_EVENTS) in out["reason"]

    def test_reports_means_over_enough_events(self):
        spec = [(f"S{i % 6}", f"2026-07-{i + 1:02d}", 0.01 * (1 + i % 3)) for i in range(24)]
        rows = _rows(spec)
        out = summarize(rows, "增持", 1, 5, **self._kwargs())
        assert out["sufficient"] is True
        assert out["events"] == 24
        assert out["mean_gap"] == pytest.approx(sum(r["gap"] for r in rows) / 24)

    def test_refuses_when_pool_reference_missing(self):
        rows = _rows([(f"S{i % 6}", "2026-07-01", 0.01) for i in range(24)])
        rows[0]["gap"] = None
        out = summarize(rows, "增持", 1, 5, **self._kwargs())
        assert out["sufficient"] is False
        assert "对照" in out["reason"]

    def test_only_counts_requested_direction(self):
        rows = _rows([(f"S{i % 6}", "2026-07-01", 0.01) for i in range(24)])
        rows += [dict(r, direction="减持") for r in _rows([("X", "2026-07-01", 0.05)])]
        out = summarize(rows, "减持", 1, 5, **self._kwargs())
        assert out["events"] == 1
        assert out["sufficient"] is False


class TestPlacebo:
    def test_small_p_when_event_days_stand_out(self):
        # 20 个事件日差额都是 0.3，另有 200 个平淡日期差额为 0：
        # 随机挪动 20 次几乎不可能全落在事件日上，经验 p 应接近 0
        special = [f"e{i:03d}" for i in range(20)]
        filler = [f"f{i:03d}" for i in range(200)]
        tables = {"A": {d: 0.30 for d in special} | {d: 0.0 for d in filler}}
        pool = {d: 0.0 for d in tables["A"]}
        rows = [{"symbol": "A", "date": d, "gap": 0.30, "direction": "增持"} for d in special]
        p = placebo_p_value(rows, tables, pool, random.Random(1), draws=100)
        assert p is not None and p <= 0.05

    def test_large_p_when_event_days_are_typical(self):
        # 所有日期差额一样 → 事件日和随机日无差别，经验 p 应接近 1
        days = [f"e{i:03d}" for i in range(20)] + [f"f{i:03d}" for i in range(200)]
        tables = {"A": {d: 0.05 for d in days}}
        pool = {d: 0.0 for d in days}
        rows = [{"symbol": "A", "date": d, "gap": 0.05, "direction": "增持"} for d in days[:20]]
        p = placebo_p_value(rows, tables, pool, random.Random(1), draws=100)
        assert p is not None and p > 0.5

    def test_none_when_too_few_events(self):
        p = placebo_p_value(
            [{"symbol": "A", "date": "d1", "gap": 0.1}],
            {"A": {"d1": 0.1}},
            {"d1": 0.0},
            random.Random(1),
        )
        assert p is None


class TestAnalyze:
    def _events(self, count: int) -> list[dict]:
        days = [f"2026-07-{d:02d}" for d in range(1, count + 1)]
        return [
            {"symbol": f"S{i % 4}", "event_date": day, "direction": "增持", "ratio": 0.01}
            for i, day in enumerate(days)
        ]

    def test_covers_every_direction_lag_horizon(self):
        frames = {f"S{i}": _stock() for i in range(4)}
        report = analyze(
            self._events(20),
            frames,
            _index(),
            horizons=(3, 5),
            lags=(1, 2),
            cost=0.004,
            draws=10,
        )
        assert len(report["results"]) == 2 * 2 * 2
        assert {r["lag"] for r in report["results"]} == {1, 2}
        assert {r["horizon"] for r in report["results"]} == {3, 5}

    def test_no_event_is_silently_counted_as_zero(self):
        # 价格恒定的情形下，净超额应等于 −成本，而不是 0
        frames = {f"S{i}": _stock() for i in range(4)}
        report = analyze(
            self._events(20),
            frames,
            _index(),
            horizons=(3,),
            lags=(1,),
            cost=0.01,
            draws=5,
        )
        for row in report["results"]:
            if row["sufficient"]:
                assert row["mean_excess"] == pytest.approx(-0.01, abs=1e-9)

    def test_empty_tables_do_not_crash(self):
        report = analyze(
            self._events(20),
            {},
            _index(),
            horizons=(3,),
            lags=(1,),
            cost=0.0,
            draws=5,
        )
        assert report["events_dropped"] == 20
        assert all(r["sufficient"] is False for r in report["results"])


class TestSampleUniverse:
    """样本外抽样：原 39 只池是当年用另一个事件筛出来的，直接拿它检验有选择偏差。"""

    def _merged(self):
        rows = []
        for i in range(12):
            for k in range(1 + i % 3):  # 每个标的 1~3 次增持
                rows.append(
                    {
                        "symbol": f"S{i:02d}",
                        "event_date": f"2026-07-{k + 1:02d}",
                        "direction": "增持",
                        "ratio": 0.01,
                    }
                )
        rows.append(
            {"symbol": "S00", "event_date": "2024-01-05", "direction": "增持", "ratio": 0.01}
        )
        return pd.DataFrame(rows)

    def test_is_deterministic_for_a_seed(self):
        merged = self._merged()
        a = sample_universe(merged, 5, seed=42)
        b = sample_universe(merged, 5, seed=42)
        assert a == b
        assert len(a) == 5

    def test_different_seed_gives_different_sample(self):
        merged = self._merged()
        assert sample_universe(merged, 5, seed=1) != sample_universe(merged, 5, seed=2)

    def test_excludes_original_pool(self):
        merged = self._merged()
        picked = sample_universe(merged, 10, seed=3, exclude={"S00", "S01"})
        assert "S00" not in picked and "S01" not in picked

    def test_skips_symbols_with_too_few_events(self):
        # 只有 1 次增持的标的抽进来几乎不会贡献样本，先筛掉
        merged = pd.DataFrame(
            [
                {"symbol": "A", "event_date": "2026-07-01", "direction": "增持", "ratio": 0.1},
                {"symbol": "A", "event_date": "2026-07-02", "direction": "增持", "ratio": 0.1},
                {"symbol": "A", "event_date": "2026-07-03", "direction": "增持", "ratio": 0.1},
                {"symbol": "B", "event_date": "2026-07-01", "direction": "增持", "ratio": 0.1},
                {"symbol": "B", "event_date": "2026-07-02", "direction": "增持", "ratio": 0.1},
                {"symbol": "C", "event_date": "2026-07-01", "direction": "增持", "ratio": 0.1},
            ]
        )
        assert set(sample_universe(merged, 50, seed=5)) == {"A", "B"}

    def test_only_counts_events_inside_since(self):
        # 窗口外的事件不该让一个实际没有近期事件的标的混进抽样
        merged = pd.DataFrame(
            [
                {"symbol": "A", "event_date": "2019-01-01", "direction": "增持", "ratio": 0.1},
                {"symbol": "A", "event_date": "2020-01-01", "direction": "增持", "ratio": 0.1},
                {"symbol": "B", "event_date": "2026-07-01", "direction": "增持", "ratio": 0.1},
                {"symbol": "B", "event_date": "2026-07-02", "direction": "增持", "ratio": 0.1},
            ]
        )
        assert sample_universe(merged, 50, seed=5, since="2026-01-01") == ["B"]

    def test_size_is_capped_by_candidates(self):
        merged = self._merged()
        assert len(sample_universe(merged, 999, seed=5)) <= 12

    def test_empty_input_returns_empty(self):
        assert sample_universe(pd.DataFrame(), 10) == []


class TestSelectEvents:
    def test_filters_and_sorts(self):
        merged = pd.DataFrame(
            [
                {"symbol": "B", "event_date": "2026-07-02", "direction": "增持", "ratio": 0.1},
                {"symbol": "A", "event_date": "2026-07-01", "direction": "增持", "ratio": 0.1},
                {"symbol": "Z", "event_date": "2026-07-01", "direction": "增持", "ratio": 0.1},
            ]
        )
        events = select_events(merged, {"A", "B"})
        assert [e["symbol"] for e in events] == ["A", "B"]

    def test_missing_ratio_becomes_none(self):
        merged = pd.DataFrame(
            [{"symbol": "A", "event_date": "2026-07-01", "direction": "增持", "ratio": None}]
        )
        assert select_events(merged, {"A"})[0]["ratio"] is None


class TestRender:
    def test_shows_refusal_rows(self):
        report = {
            "events_total": 5,
            "events_dropped": 0,
            "cost": 0.004,
            "horizons": [3],
            "lags": [1],
            "results": [
                {
                    "direction": "增持",
                    "lag": 1,
                    "horizon": 3,
                    "events": 5,
                    "clusters": 2,
                    "sufficient": False,
                    "reason": "事件数 5 < 20",
                    "fdr_significant": False,
                }
            ],
            "correction": {"n_tests": 0, "n_significant": 0},
        }
        text = render_markdown(report)
        assert "样本不足" in text
        assert "announcement_date" in text
        assert "placebo" in text


class TestMain:
    def test_missing_directory_returns_one(self, tmp_path):
        assert main(["--altalpha-dir", str(tmp_path / "nope")]) == 1

    def test_missing_pool_returns_one(self, tmp_path, capsys):
        (tmp_path / "alt").mkdir()
        assert main(["--altalpha-dir", str(tmp_path / "alt"), "--pool-file", "nope.json"]) == 1
        assert "标的池" in capsys.readouterr().err


def test_min_events_constant_is_sane():
    # 阈值本身要有意义：太小会把噪声当结论，太大任何事件研究都做不成
    assert 10 <= MIN_EVENTS <= 50
    assert math.isfinite(MIN_EVENTS)


class TestFdrFootnote:
    """报告脚注与表格必须说同一件事。

    真出过这个错：``report_correction()`` 把 ``n_significant`` 放在
    ``benjamini_hochberg`` 子字典里，脚注却按顶层键取，``.get(..., 0)`` 兜底成 0。
    于是表格逐行标「是」、脚注写「0 个」，报告自己跟自己矛盾。之前所有组合恰好
    都不显著，这个矛盾一直没暴露；一出现真信号就自打脸。
    """

    def test_reads_n_significant_from_bh_subdict(self):
        from scripts.insider_event_study import _fdr_significant_count

        correction = {
            "n_tests": 12,
            "benjamini_hochberg": {"rejected": [True, True, False], "n_significant": 2},
        }
        assert _fdr_significant_count(correction) == 2

    def test_missing_key_gives_zero_not_crash(self):
        from scripts.insider_event_study import _fdr_significant_count

        assert _fdr_significant_count({"n_tests": 3}) == 0
        assert _fdr_significant_count({}) == 0

    def test_footnote_count_matches_bold_rows(self):
        """端到端：脚注数字必须等于表格里标「是」的行数。"""
        from scripts.insider_event_study import _fdr_significant_count, render_markdown

        results = [
            {
                "direction": "增持",
                "lag": 1,
                "horizon": 5,
                "events": 100,
                "clusters": 10,
                "sufficient": True,
                "mean_excess": 0.004,
                "t_excess": 3.0,
                "mean_gap": 0.003,
                "mean_gap_cluster": 0.003,
                "t_symbol": 2.9,
                "p_symbol": 0.004,
                "placebo_p": 0.01,
                "fdr_significant": True,
            },
            {
                "direction": "减持",
                "lag": 1,
                "horizon": 5,
                "events": 100,
                "clusters": 10,
                "sufficient": True,
                "mean_excess": 0.001,
                "t_excess": 0.5,
                "mean_gap": 0.001,
                "mean_gap_cluster": 0.001,
                "t_symbol": 0.5,
                "p_symbol": 0.6,
                "placebo_p": 0.5,
                "fdr_significant": False,
            },
        ]
        correction = {
            "n_tests": 2,
            "benjamini_hochberg": {"rejected": [True, False], "n_significant": 1},
        }
        text = render_markdown(
            {
                "universe": {"kind": "oos", "size": 10, "seed": 1},
                "events_total": 200,
                "events_dropped": 0,
                "cost": 0.004,
                "results": results,
                "correction": correction,
            }
        )
        assert "校正后仍显著，1 个" in text
        assert _fdr_significant_count(correction) == text.count("| 是 |")
