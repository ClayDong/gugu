"""数据能力审计测试（2026-11-05）。

审计脚本的价值全在"能不能拦住自我欺骗"。它本来可以输出一张漂亮表格，然后让
我看着"insider_trading 350 万行、6037 个交易日"就以为数据很充分。所以测试围绕
三个会让报告说谎的点写：

1. **退化日期**：``end_date`` 全是 0.0 时 pandas 会解析成 1970-01-01，"有日期"是假象。
   必须判为退化、回退到文件名日期，并且来源要标出来。
2. **冗余日期列**：``announcement_date`` 可能与 ``change_date`` 逐行相同。若当成独立
   公告日，就会得出"有公告日 → 无前视"的错误结论。全等必须判冗余。
3. **池内零命中**：数据再长，池子里一只都没有就是用不上。这条必须一票否决，
   不能被"不同日期很多"盖过去。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from scripts.data_capability_audit import (
    MIN_POOL_EVENTS,
    MIN_POOL_HITS,
    adequacy_label,
    audit_dataset,
    build_report,
    canonical_dataset,
    direction_feasibility,
    filename_date,
    freshness_label,
    main,
    normalize_code,
    render_markdown,
    verdict_for,
)

AS_OF = date(2026, 11, 5)


def _write_parquet(path, rows: dict) -> None:
    pd.DataFrame(rows).to_parquet(path, index=False)


def _audit(name: str, files: list, pool: set[str] | None = None):
    return audit_dataset(name, files, pool or set()).as_dict(AS_OF)


class TestCanonicalDataset:
    def test_strips_compact_date_prefix(self):
        assert canonical_dataset("20260715_dragon_tiger.parquet") == "dragon_tiger"

    def test_strips_query_suffix_kept_under_same_family(self):
        # 文件名后接查询参数时，仍必须归回同一个数据集，否则会被数成多份数据
        name = "20260715_dragon_tiger_stock_lhb_detail_em_end_date=20260715.parquet"
        assert canonical_dataset(name) == "dragon_tiger"

    def test_pure_date_filename_uses_default(self):
        # 股东户数目录用日期当文件名，剥完前缀什么都不剩 → 目录即数据集身份
        assert canonical_dataset("2026-07-28.parquet", default="holder_structure") == (
            "holder_structure"
        )

    def test_pure_date_filename_without_default_is_unknown(self):
        assert canonical_dataset("2026-07-28.parquet") == "unknown"

    def test_unknown_prefix_falls_back_to_first_token(self):
        assert canonical_dataset("20260715_custom_feed.parquet") == "custom"


class TestFilenameDate:
    def test_reads_compact_form(self):
        assert filename_date("20260715_margin_trade.parquet") == "2026-07-15"

    def test_reads_dashed_form(self):
        assert filename_date("2026-07-28.parquet") == "2026-07-28"

    def test_no_date_returns_none(self):
        assert filename_date("insider_trading.parquet") is None

    def test_invalid_calendar_date_returns_none(self):
        # 20261340 不是合法日期，不能编出一个日期来
        assert filename_date("20261340_block_trade.parquet") is None


class TestNormalizeCode:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("sh600030", "600030"),
            ("600030.SH", "600030"),
            ("600030", "600030"),
            (600030, "600030"),
            (600030.0, "600030"),  # 整型列被读成 float 时不能变成 033000
            ("sz000001", "000001"),
        ],
    )
    def test_formats(self, raw, expected):
        assert normalize_code(raw) == expected

    def test_too_short_returns_none(self):
        assert normalize_code("600") is None

    def test_empty_returns_none(self):
        assert normalize_code("") is None


class TestLabels:
    @pytest.mark.parametrize(
        "days,expected",
        [(0, "新鲜"), (7, "新鲜"), (8, "偏旧"), (30, "偏旧"), (31, "陈旧"), (90, "陈旧")],
    )
    def test_freshness_boundaries(self, days, expected):
        assert freshness_label(days, True) == expected

    def test_expired(self):
        assert freshness_label(91, True) == "过期"

    def test_no_data(self):
        assert freshness_label(None, False) == "无数据"

    @pytest.mark.parametrize(
        "count,expected",
        [
            (0, "不可用"),
            (1, "只够可行性"),
            (59, "只够可行性"),
            (60, "仅够事件研究"),
            (249, "仅够事件研究"),
            (250, "够一年"),
        ],
    )
    def test_adequacy_boundaries(self, count, expected):
        assert adequacy_label(count) == expected


class TestAuditDataset:
    def test_reads_dates_and_codes_from_columns(self, tmp_path):
        path = tmp_path / "20260715_trade.parquet"
        _write_parquet(
            path,
            {
                "trade_date": ["2026-07-14", "2026-07-15", "2026-07-15"],
                "code": ["600030", "600031", "600032"],
            },
        )
        row = _audit("demo", [path], {"600030", "600032", "999999"})
        assert row["date_column"] == "trade_date"
        assert row["date_source"] == "列"
        assert row["date_min"] == "2026-07-14"
        assert row["date_max"] == "2026-07-15"
        assert row["distinct_dates"] == 2
        assert row["codes"] == 3
        assert row["pool_hits"] == 2
        assert row["degenerate_date"] is False

    def test_zero_filled_date_column_is_degenerate_not_1970(self, tmp_path):
        # end_date 全是 0.0 → pandas 压成 1970-01-01。若不判退化，报告会写
        # "过期 20762 天"，既荒谬又掩盖了真正的问题：这一列根本没法用。
        path = tmp_path / "2026-07-28.parquet"
        _write_parquet(path, {"symbol": ["600030", "600031"], "end_date": [0.0, 0.0]})
        row = _audit("holder_structure", [path])
        assert row["degenerate_date"] is True
        assert row["date_source"] == "文件名"
        assert row["date_min"] == "2026-07-28"
        assert row["date_max"] == "2026-07-28"
        # 距基准日 100 天确实算过期——这是对的，快照就是旧的；错的是原来的 20762 天
        assert row["days_stale"] == 100
        assert row["days_stale"] < 365

    def test_filename_dates_aggregate_across_snapshots(self, tmp_path):
        for day in ("2026-07-28", "2026-07-29", "2026-07-30"):
            _write_parquet(
                tmp_path / f"{day}.parquet",
                {"symbol": ["600030"], "end_date": [0.0]},
            )
        files = sorted(tmp_path.glob("*.parquet"))
        row = _audit("holder_structure", files)
        assert row["distinct_dates"] == 3
        assert row["date_min"] == "2026-07-28"
        assert row["date_max"] == "2026-07-30"

    def test_column_dates_win_over_filename(self, tmp_path):
        path = tmp_path / "20260715_demo.parquet"
        _write_parquet(path, {"trade_date": ["2026-07-10"], "code": ["600030"]})
        row = _audit("demo", [path])
        assert row["date_source"] == "列"
        assert row["date_max"] == "2026-07-10"

    def test_duplicate_factor_reflects_rolling_snapshots(self, tmp_path):
        # 三个全量快照：总行数是单文件的 3 倍，但独立信息只有 1 份
        for idx, day in enumerate(("20260715", "20260716", "20260717")):
            _write_parquet(
                tmp_path / f"{day}_demo.parquet",
                {"trade_date": ["2026-07-10", "2026-07-11"], "code": [f"60000{idx}"] * 2},
            )
        files = sorted(tmp_path.glob("*.parquet"))
        row = _audit("demo", files)
        assert row["rows"] == 6
        assert row["duplicate_factor"] == 3.0

    def test_identical_announcement_date_is_flagged_redundant(self, tmp_path):
        path = tmp_path / "20260715_insider_trading.parquet"
        _write_parquet(
            path,
            {
                "code": ["600030", "600031"],
                "change_date": ["2026-07-10", "2026-07-11"],
                "announcement_date": ["2026-07-10", "2026-07-11"],
            },
        )
        row = _audit("insider_trading", [path])
        assert row["date_column"] == "change_date"
        assert row["redundant_date_columns"] == ["announcement_date"]

    def test_diverging_announcement_date_is_not_redundant(self, tmp_path):
        path = tmp_path / "20260715_insider_trading.parquet"
        _write_parquet(
            path,
            {
                "code": ["600030", "600031"],
                "change_date": ["2026-07-10", "2026-07-11"],
                "announcement_date": ["2026-07-12", "2026-07-13"],
            },
        )
        row = _audit("insider_trading", [path])
        assert row["redundant_date_columns"] == []
        assert row["date_columns"] == ["change_date", "announcement_date"]

    def test_partially_matching_column_is_not_redundant(self, tmp_path):
        # 有一行不同就不算冗余——报"冗余"会让人放弃一条本来有用的信息
        path = tmp_path / "20260715_insider_trading.parquet"
        _write_parquet(
            path,
            {
                "code": ["600030", "600031"],
                "change_date": ["2026-07-10", "2026-07-11"],
                "announcement_date": ["2026-07-10", "2026-07-14"],
            },
        )
        row = _audit("insider_trading", [path])
        assert row["redundant_date_columns"] == []

    def test_missing_date_values_counted(self, tmp_path):
        path = tmp_path / "20260715_margin_trade.parquet"
        _write_parquet(
            path,
            {"trade_date": ["2026-07-14", None], "code": ["600030", "000001"]},
        )
        row = _audit("margin_trade", [path])
        assert row["date_null_rows"] == 1
        assert row["date_null_ratio"] == 0.5

    def test_broken_file_is_recorded_not_fatal(self, tmp_path):
        good = tmp_path / "20260715_demo.parquet"
        _write_parquet(good, {"trade_date": ["2026-07-15"], "code": ["600030"]})
        broken = tmp_path / "20260716_demo.parquet"
        broken.write_bytes(b"not parquet")
        row = _audit("demo", [good, broken])
        assert row["files"] == 2
        assert row["rows"] == 1  # 坏文件不贡献行数，也不假装成功
        assert len(row["errors"]) == 1

    def test_no_date_anywhere_yields_unusable(self, tmp_path):
        path = tmp_path / "payload.parquet"  # 文件名也没有日期
        _write_parquet(path, {"code": ["600030"]})
        row = _audit("demo", [path])
        assert row["distinct_dates"] == 0
        assert row["adequacy"] == "不可用"
        assert row["date_source"] == "列"

    def test_pool_window_is_none_without_kline_cover(self, tmp_path):
        # 没给行情覆盖就不能算窗口内样本；None 表示"没统计"，与"统计为 0"不同
        path = tmp_path / "20260715_demo.parquet"
        _write_parquet(path, {"trade_date": ["2026-07-15"], "code": ["600030"]})
        row = _audit("demo", [path], {"600030"})
        assert row["pool_events"] == 1
        assert row["pool_events_in_window"] is None


class TestPoolSampleDeduplication:
    """样本量必须去重，否则会被重复快照和多个变动人系统性放大。"""

    def _frame(self, path, rows):
        _write_parquet(path, rows)

    def test_same_event_across_snapshots_counted_once(self, tmp_path):
        # 三份滚动全量快照各存了同一笔增持 → 样本量是 1，不是 3
        for day in ("20260715", "20260716", "20260717"):
            self._frame(
                tmp_path / f"{day}_insider_trading.parquet",
                {
                    "code": ["600030"],
                    "change_date": ["2026-07-10"],
                    "direction": ["增持"],
                },
            )
        files = sorted(tmp_path.glob("*.parquet"))
        audit = audit_dataset("insider_trading", files, {"600030"})
        assert audit.pool_events == 1

    def test_multiple_insiders_on_same_day_counted_once(self, tmp_path):
        # 一次公告因多个变动人拆成 3 行，但只发生了一个事件
        path = tmp_path / "20260715_insider_trading.parquet"
        self._frame(
            path,
            {
                "code": ["600030"] * 3,
                "change_date": ["2026-07-10"] * 3,
                "direction": ["增持"] * 3,
            },
        )
        audit = audit_dataset("insider_trading", [path], {"600030"})
        assert audit.pool_events == 1

    def test_opposite_directions_same_day_counted_separately(self, tmp_path):
        path = tmp_path / "20260715_insider_trading.parquet"
        self._frame(
            path,
            {
                "code": ["600030", "600030"],
                "change_date": ["2026-07-10", "2026-07-10"],
                "direction": ["增持", "减持"],
            },
        )
        audit = audit_dataset("insider_trading", [path], {"600030"})
        assert audit.pool_events == 2

    def test_events_outside_kline_window_excluded(self, tmp_path):
        # 事件在 1994 年，行情只到 2023 年之后 → 算不出收益，不能算可用样本
        path = tmp_path / "20260715_insider_trading.parquet"
        self._frame(
            path,
            {
                "code": ["600030", "600030"],
                "change_date": ["1994-08-10", "2026-07-10"],
                "direction": ["增持", "增持"],
            },
        )
        cover = {"600030": ("2023-07-03", "2026-11-04")}
        audit = audit_dataset("insider_trading", [path], {"600030"}, cover)
        assert audit.pool_events == 2
        assert audit.pool_events_in_window == 1

    def test_window_uses_each_symbol_own_coverage(self, tmp_path):
        # 两只标的行情起点不同，窗口必须逐标的算，不能取全池最早起点
        path = tmp_path / "20260715_insider_trading.parquet"
        self._frame(
            path,
            {
                "code": ["600030", "600031"],
                "change_date": ["2024-01-10", "2024-01-10"],
                "direction": ["增持", "增持"],
            },
        )
        cover = {
            "600030": ("2023-07-03", "2026-11-04"),
            "600031": ("2025-01-02", "2026-11-04"),
        }
        audit = audit_dataset("insider_trading", [path], {"600030", "600031"}, cover)
        assert audit.pool_events_in_window == 1


class TestVerdict:
    def _target(self, **overrides):
        base = {
            "pool_hits": 39,
            "adequacy": "够一年",
            "freshness": "偏旧",
            "distinct_dates": 6037,
            "pool_events_in_window": 82,
        }
        base.update(overrides)
        return base

    def test_full_coverage_is_doable(self):
        verdict, reason = verdict_for(self._target(), 39)
        assert verdict == "可做"
        assert reason == ""

    def test_thin_sample_beats_long_history(self):
        # 行情窗口内只有 12 条样本：数据再长也检验不出东西
        verdict, reason = verdict_for(self._target(pool_events_in_window=12), 39)
        assert verdict == "需补数据"
        assert "12" in reason

    def test_sample_unknown_does_not_block(self):
        # 未统计窗口样本（日期退化）时不该被这条规则误伤
        assert verdict_for(self._target(pool_events_in_window=None), 39)[0] == "可做"

    def test_sample_threshold_boundary(self):
        assert verdict_for(self._target(pool_events_in_window=MIN_POOL_EVENTS), 39)[0] == "可做"
        assert (
            verdict_for(self._target(pool_events_in_window=MIN_POOL_EVENTS - 1), 39)[0]
            == "需补数据"
        )

    def test_zero_pool_hits_vetoes_regardless_of_history(self):
        # 一票否决：6037 个交易日也不行
        verdict, reason = verdict_for(self._target(pool_hits=0), 39)
        assert verdict == "不可做"
        assert "零命中" in reason

    def test_few_pool_hits_means_need_more_data_not_dead(self):
        verdict, reason = verdict_for(self._target(pool_hits=2, adequacy="仅够事件研究"), 39)
        assert verdict == "需补数据"
        assert "2/39" in reason

    def test_pool_hit_threshold_boundary(self):
        assert verdict_for(self._target(pool_hits=MIN_POOL_HITS), 39)[0] == "可做"
        assert verdict_for(self._target(pool_hits=MIN_POOL_HITS - 1), 39)[0] == "需补数据"

    def test_short_history_means_need_more_data(self):
        verdict, reason = verdict_for(self._target(adequacy="只够可行性", distinct_dates=20), 39)
        assert verdict == "需补数据"
        assert "天数不足" in reason

    def test_expired_data_is_not_doable(self):
        verdict, reason = verdict_for(self._target(freshness="过期"), 39)
        assert verdict == "不可做"
        assert "过期" in reason


class TestDirectionFeasibility:
    def _row(self, **overrides):
        base = {
            "dataset": "insider_trading",
            "files": 1,
            "rows": 100,
            "date_column": "change_date",
            "date_columns": ["change_date"],
            "redundant_date_columns": [],
            "date_min": "2026-01-01",
            "date_max": "2026-10-16",
            "distinct_dates": 6037,
            "codes": 5115,
            "pool_hits": 39,
            "pool_events": 674,
            "pool_events_in_window": 82,
            "date_source": "列",
            "degenerate_date": False,
            "date_null_rows": 0,
            "date_null_ratio": 0.0,
            "duplicate_factor": 1.0,
            "days_stale": 20,
            "freshness": "偏旧",
            "adequacy": "够一年",
            "errors": [],
        }
        base.update(overrides)
        return base

    def test_missing_dataset_is_marked_not_doable(self):
        rows = direction_feasibility({}, 39)
        assert all(r["verdict"] == "不可做" for r in rows)
        assert all(r["adequacy"] == "缺数据" for r in rows)

    def test_pool_zero_hits_direction_rejected(self):
        audits = {"block_trade": self._row(pool_hits=0, distinct_dates=64)}
        rows = {r["direction"]: r for r in direction_feasibility(audits, 39)}
        assert rows["大宗交易"]["verdict"] == "不可做"
        assert "零命中" in rows["大宗交易"]["note"]

    def test_viable_direction_passes(self):
        audits = {"insider_trading": self._row()}
        rows = {r["direction"]: r for r in direction_feasibility(audits, 39)}
        assert rows["内部人交易（增减持）"]["verdict"] == "可做"


class TestRenderAndReport:
    def _build(self, tmp_path):
        alt = tmp_path / "altalpha"
        holder = tmp_path / "holder_structure"
        kline = tmp_path / "parquet_cache"
        for directory in (alt, holder, kline):
            directory.mkdir()
        _write_parquet(
            alt / "20260715_insider_trading.parquet",
            {
                "code": ["600030", "600031"],
                "change_date": ["2026-07-10", "2026-07-11"],
                "announcement_date": ["2026-07-10", "2026-07-11"],
            },
        )
        _write_parquet(
            alt / "20260715_share_unlock.parquet",
            {"code": ["600030"], "unlock_date": ["2026-12-01"]},  # 未来日期
        )
        _write_parquet(
            holder / "2026-07-28.parquet",
            {"symbol": ["600030", "600031"], "end_date": [0.0, 0.0]},
        )
        _write_parquet(
            kline / "600030_daily.parquet",
            {"date": ["2026-07-14", "2026-07-15"], "close": [10.0, 11.0]},
        )
        pool = tmp_path / "pool.json"
        pool.write_text('{"events": [{"code": "600030"}, {"code": "600031"}]}', encoding="utf-8")
        return build_report(alt, holder, kline, pool, AS_OF)

    def test_datasets_are_grouped_by_family(self, tmp_path):
        report = self._build(tmp_path)
        names = {row["dataset"] for row in report["datasets"]}
        # 日期文件名必须归到 holder_structure，而不是变成一个叫 unknown 的数据集
        assert names == {"insider_trading", "share_unlock", "holder_structure"}

    def test_degenerate_dataset_uses_filename_dates(self, tmp_path):
        report = self._build(tmp_path)
        holder = next(r for r in report["datasets"] if r["dataset"] == "holder_structure")
        assert holder["date_source"] == "文件名"
        assert holder["degenerate_date"] is True
        assert holder["days_stale"] == (AS_OF - date(2026, 7, 28)).days

    def test_future_date_triggers_lookahead_warning(self, tmp_path):
        report = self._build(tmp_path)
        assert any("晚于审计基准日" in n for n in report["notes"])
        assert any("未来信息" in n for n in report["notes"])

    def test_redundant_date_column_reported(self, tmp_path):
        report = self._build(tmp_path)
        assert any("逐行完全相同" in n for n in report["notes"])

    def test_kline_cache_summary(self, tmp_path):
        report = self._build(tmp_path)
        assert report["kline"]["files"] == 1
        assert report["kline"]["symbols"] == 1
        assert report["kline"]["latest"] == "2026-07-15"
        assert report["kline"]["pool_hits"] == 1

    def test_kline_cache_ignores_non_stock_files(self, tmp_path):
        # 基准指数 _index.parquet 有 950 行，混进来会把"个股行情长度"统计拉偏
        alt = tmp_path / "altalpha"
        holder = tmp_path / "holder_structure"
        kline = tmp_path / "parquet_cache"
        for directory in (alt, holder, kline):
            directory.mkdir()
        _write_parquet(
            kline / "600030_2d_none.parquet",
            {"date": ["2026-07-14", "2026-07-15"], "close": [10.0, 11.0]},
        )
        _write_parquet(
            kline / "_index.parquet",
            {"date": [f"2026-07-{d:02d}" for d in range(1, 11)], "close": [1.0] * 10},
        )
        pool = tmp_path / "pool.json"
        pool.write_text('{"events": [{"code": "600030"}]}', encoding="utf-8")
        report = build_report(alt, holder, kline, pool, AS_OF)
        assert report["kline"]["symbols"] == 1
        assert report["kline"]["median_rows"] == 2  # 指数那 10 行没被算进来

    def test_render_markdown_has_new_columns(self, tmp_path):
        text = render_markdown(self._build(tmp_path))
        assert "重复因子" in text
        assert "日期来源" in text
        assert "不同日期" in text
        assert "缺日期行" in text
        # 一票否决与"需补数据"的区分必须在图例里写清楚，否则会被误读
        assert "需补数据" in text
        assert "一票否决" in text
        assert "1970-01-01" in text
        # 样本量口径必须写清"去重"，否则会被行数骗
        assert "池内样本" in text
        assert "独立事件数" in text

    def test_report_json_serializable(self, tmp_path):
        import json

        report = self._build(tmp_path)
        json.dumps(report, ensure_ascii=False, default=str)


class TestMain:
    def test_missing_directory_returns_one(self, tmp_path):
        assert main(["--altalpha-dir", str(tmp_path / "nope")]) == 1

    def test_bad_as_of_returns_one(self, tmp_path, capsys):
        (tmp_path / "alt").mkdir()
        assert main(["--altalpha-dir", str(tmp_path / "alt"), "--as-of", "2026/11/05"]) == 1
        assert "YYYY-MM-DD" in capsys.readouterr().err
