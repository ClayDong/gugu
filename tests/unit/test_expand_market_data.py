"""全市场行情扩容测试（2026-11-06）。

这个脚本最容易出的错不是崩溃，而是**悄悄少一段数据**：

1. **翻页空洞**：腾讯接口单页最多约 640 行，超出就截断成「最近 640 行」。
   如果下一轮的锚点用窗口起点而不是「上一页实际返回的最早日期」，被截断的那段
   就永远补不上，而且文件看起来完全正常。所以测试的核心是：故意让每页都截断，
   校验最终拼出来的序列与真实序列逐日一致。
2. **假完成**：文件存在不等于数据完整。行数不够要重抓，损坏文件要当没做。
3. **幸存者偏差**：清单只取在市标的会漏掉退市股，而退市股正是策略最该躲开的。
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pandas as pd
import pytest

from scripts.expand_market_data import (
    MAX_GAP_DAYS,
    MIN_ROWS_OK,
    SHORT_HISTORY_PREFIX,
    RateLimiter,
    already_done,
    em_secid,
    estimate,
    fetch_eastmoney_history,
    fetch_history,
    main,
    parse_em_kline,
    quality_check,
    requests_for,
    target_path,
)


def _bdays(start: date, end: date) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]


def _frame(dates: list[str]) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * n,
            "high": [11.0] * n,
            "low": [9.0] * n,
            "close": [10.5] * n,
            "volume": [1e6] * n,
        }
    )


def _fake_fetcher(all_dates: list[str], cap: int | None = None):
    """模拟接口：返回区间内数据，cap 给定时只保留最新 cap 行（真实截断行为）。"""
    calls: list[tuple[date, date]] = []

    def fetch(symbol, start, end, *, limiter, rows=640, retries=3):
        calls.append((start, end))
        selected = [d for d in all_dates if start.isoformat() <= d <= end.isoformat()]
        if cap is not None:
            selected = selected[-cap:]
        return _frame(selected) if selected else pd.DataFrame()

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


class TestFetchHistory:
    def test_pages_back_to_floor(self):
        today = date(2026, 11, 6)
        all_dates = _bdays(date(2019, 1, 1), today)
        fetch = _fake_fetcher(all_dates)
        frame = fetch_history("600519", 2, limiter=RateLimiter(0), today=today, fetcher=fetch)
        assert not frame.empty
        assert len(fetch.calls) >= 2
        # 覆盖到约两年，且不越过请求的边界
        assert frame["date"].min() >= (today - timedelta(days=365 * 2 + 30)).strftime("%Y-%m-%d")
        assert frame["date"].max() == today.strftime("%Y-%m-%d")

    def test_truncated_pages_leave_no_hole(self):
        # 关键测试：每页都被接口截断成最新 120 行，拼接后仍必须逐日完整
        today = date(2026, 11, 6)
        all_dates = _bdays(date(2021, 1, 4), today)
        fetch = _fake_fetcher(all_dates, cap=120)
        frame = fetch_history("600519", 3, limiter=RateLimiter(0), today=today, fetcher=fetch)

        floor = (today - timedelta(days=365 * 3 + 30)).strftime("%Y-%m-%d")
        expected = [d for d in all_dates if floor <= d <= today.strftime("%Y-%m-%d")]
        assert frame["date"].tolist() == expected

    def test_pages_never_overlap(self):
        today = date(2026, 11, 6)
        all_dates = _bdays(date(2020, 1, 1), today)
        fetch = _fake_fetcher(all_dates, cap=100)
        frame = fetch_history("600519", 3, limiter=RateLimiter(0), today=today, fetcher=fetch)
        assert not frame["date"].duplicated().any()

    def test_stops_when_api_returns_nothing(self):
        # 上市不足两年的标的：翻到上市日就没了，不能继续空转
        today = date(2026, 11, 6)
        all_dates = _bdays(date(2025, 6, 1), today)
        fetch = _fake_fetcher(all_dates)
        frame = fetch_history("301999", 5, limiter=RateLimiter(0), today=today, fetcher=fetch)
        assert frame["date"].min() == "2025-06-02"
        assert len(fetch.calls) <= 5

    def test_empty_when_no_data_at_all(self):
        today = date(2026, 11, 6)
        fetch = _fake_fetcher([])
        assert fetch_history("999999", 3, limiter=RateLimiter(0), today=today, fetcher=fetch).empty

    def test_sorted_ascending(self):
        today = date(2026, 11, 6)
        all_dates = _bdays(date(2022, 1, 3), today)
        fetch = _fake_fetcher(all_dates, cap=80)
        frame = fetch_history("600519", 2, limiter=RateLimiter(0), today=today, fetcher=fetch)
        assert pd.to_datetime(frame["date"]).is_monotonic_increasing


class TestQualityCheck:
    def test_clean_frame_passes(self):
        frame = _frame(_bdays(date(2024, 1, 1), date(2025, 6, 30)))
        assert quality_check(frame, 2) == []

    def test_empty_is_reported(self):
        assert quality_check(pd.DataFrame(), 2) == ["空数据"]

    def test_too_few_rows(self):
        frame = _frame(_bdays(date(2024, 1, 1), date(2024, 2, 1)))
        assert len(frame) < MIN_ROWS_OK
        assert any(SHORT_HISTORY_PREFIX in p for p in quality_check(frame, 5))

    def test_short_history_reports_first_bar_date(self):
        """次新股只有几十行是正常的，但必须说清「从哪天起」，否则读不出是短缺还是新股。"""
        frame = _frame(_bdays(date(2026, 9, 1), date(2026, 10, 1)))
        issues = quality_check(frame, 5)
        assert any("2026-09-01" in p for p in issues)

    def test_duplicate_dates(self):
        dates = _bdays(date(2024, 1, 1), date(2025, 6, 30))
        frame = pd.concat([_frame(dates), _frame(dates[:3])], ignore_index=True)
        assert any("日期重复" in p for p in quality_check(frame, 2))

    def test_out_of_order_dates(self):
        dates = _bdays(date(2024, 1, 1), date(2025, 6, 30))
        shuffled = frame = _frame(dates)
        shuffled.loc[0, "date"], shuffled.loc[5, "date"] = (
            shuffled.loc[5, "date"],
            shuffled.loc[0, "date"],
        )
        assert any("未升序" in p for p in quality_check(frame, 2))

    def test_large_gap_flagged(self):
        # 停牌造成的长空洞要报出来，不能当成完整
        dates = _bdays(date(2024, 1, 1), date(2024, 3, 1)) + _bdays(
            date(2024, 6, 1), date(2025, 6, 30)
        )
        problems = quality_check(_frame(dates), 2)
        assert any("空洞" in p for p in problems)

    def test_missing_column(self):
        frame = _frame(_bdays(date(2024, 1, 1), date(2025, 6, 30))).drop(columns=["close"])
        assert any("缺列 close" in p for p in quality_check(frame, 2))

    def test_non_positive_price(self):
        frame = _frame(_bdays(date(2024, 1, 1), date(2025, 6, 30)))
        frame.loc[10, "close"] = 0.0
        assert any("非法值" in p for p in quality_check(frame, 2))

    def test_gap_threshold_is_not_tiny(self):
        # 阈值太小会把正常的长假误报成漏页
        assert MAX_GAP_DAYS >= 20


class TestSplitWarnings:
    """告警要分成「历史不足」和「真问题」两类。

    全市场跑完有 281 条告警，其中 210 条只是次新股历史短。混在一个计数里，
    真正该看的「中间 70 天空洞」就被淹了。
    """

    def test_short_history_counted_separately(self):
        from scripts.expand_market_data import _split_warnings

        problems = {
            "001381": [f"{SHORT_HISTORY_PREFIX}(自 2026-11-01 起，3 行)"],
            "000040": ["中间有 70 天空洞"],
        }
        assert _split_warnings(problems) == (1, 1)

    def test_both_problems_counts_as_real(self):
        from scripts.expand_market_data import _split_warnings

        problems = {"000502": [f"{SHORT_HISTORY_PREFIX}(自 2026-01-01 起，153 行)", "日期重复"]}
        assert _split_warnings(problems) == (0, 1)

    def test_empty_dict(self):
        from scripts.expand_market_data import _split_warnings

        assert _split_warnings({}) == (0, 0)


class TestResume:
    def test_missing_file_is_not_done(self, tmp_path):
        assert already_done(tmp_path, "600519", 5) is False

    def test_short_file_is_not_done(self, tmp_path):
        path = target_path(tmp_path, "600519", 5)
        _frame(_bdays(date(2026, 1, 1), date(2026, 1, 20))).to_parquet(path, index=False)
        assert already_done(tmp_path, "600519", 5) is False

    def test_full_file_is_done(self, tmp_path):
        path = target_path(tmp_path, "600519", 5)
        _frame(_bdays(date(2024, 1, 1), date(2025, 6, 30))).to_parquet(path, index=False)
        assert already_done(tmp_path, "600519", 5) is True

    def test_corrupt_file_is_not_done(self, tmp_path):
        path = target_path(tmp_path, "600519", 5)
        path.write_bytes(b"broken")
        assert already_done(tmp_path, "600519", 5) is False

    def test_filename_encodes_years(self, tmp_path):
        assert target_path(tmp_path, "600519", 5).name == "600519_1250d_qfq.parquet"
        assert target_path(tmp_path, "600519", 10).name == "600519_2500d_qfq.parquet"


class TestEmSecid:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("600000", "1.600000"),  # 沪市主板
            ("601318", "1.601318"),
            ("688981", "1.688981"),  # 科创板
            ("000001", "0.000001"),  # 深市主板
            ("300750", "0.300750"),  # 创业板
            ("900901", "1.900901"),  # 沪市 B 股
            ("920001", "0.920001"),  # 北交所新代码段：9 开头但归深市侧
        ],
    )
    def test_prefix_by_board(self, code, expected):
        assert em_secid(code) == expected

    def test_beijing_920_is_not_treated_as_shanghai_b(self):
        """920xxx 是北交所，不能按「9 开头 = 沪市 B 股」处理，否则整段取不到。"""
        assert em_secid("920001").startswith("0.")
        assert em_secid("900901").startswith("1.")


class TestParseEmKline:
    # 东财列序：日期,开,收,高,低,量(手),额(元)——收盘在第三位，不是 OHLC 顺序
    LINE = "2024-01-02,10.09,9.87,10.10,9.82,629078,623425673.00"

    def test_close_is_third_column_not_fourth(self):
        frame = parse_em_kline([self.LINE])
        row = frame.iloc[0]
        assert row["open"] == pytest.approx(10.09)
        assert row["close"] == pytest.approx(9.87), "收盘价被当成了最高价"
        assert row["high"] == pytest.approx(10.10)
        assert row["low"] == pytest.approx(9.82)

    def test_unit_relation_recovers_raw_price(self):
        """成交额/(成交量×100) 应还原出原始均价。

        这条不变式同时防两种错：成交额按「万元」存、成交量按「股」存，都会让结果
        差 100 倍；只比较数值大小是发现不了的。
        """
        row = parse_em_kline([self.LINE]).iloc[0]
        raw_price = row["amount"] / (row["volume"] * 100)
        assert 0.5 < raw_price / row["close"] < 2.0, f"量额单位对不上：{raw_price=}"

    def test_sorted_by_date(self):
        lines = [
            "2024-01-03,1,1,1,1,1,1",
            "2024-01-02,1,1,1,1,1,1",
        ]
        dates = parse_em_kline(lines)["date"].tolist()
        assert dates == sorted(dates)

    def test_empty_returns_canonical_columns(self):
        frame = parse_em_kline([])
        assert frame.empty
        assert list(frame.columns) == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ]

    def test_short_line_skipped_instead_of_crashing(self):
        frame = parse_em_kline(["2024-01-02,1,1,1", self.LINE])
        assert len(frame) == 1


class TestFetchEastmoneyHistory:
    def _response(self, klines):
        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": {"klines": klines} if klines is not None else None}

        return _Resp()

    def test_returns_frame_from_single_request(self, monkeypatch):
        import httpx

        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(params)
            return self._response(["2024-01-02,10.09,9.87,10.10,9.82,629078,623425673.00"])

        monkeypatch.setattr(httpx, "get", fake_get)
        frame = fetch_eastmoney_history("600000", 5, limiter=RateLimiter(0.0))

        assert len(frame) == 1
        assert len(calls) == 1, "东财应当一次请求取全段，不该翻页"
        assert calls[0]["fqt"] == "1", "必须是前复权"
        assert calls[0]["klt"] == "101", "必须是日线"

    def test_missing_data_returns_empty_not_error(self, monkeypatch):
        """未上市或退市后窗口内无数据：是「无数据」，不是「抓取失败」。"""
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: self._response(None))
        assert fetch_eastmoney_history("000003", 5, limiter=RateLimiter(0.0)).empty

    def test_block_code_raises_without_retry(self, monkeypatch):
        import httpx

        from scripts.expand_market_data import BlockedError

        attempts = []

        class _Blocked:
            status_code = 501

        def fake_get(*a, **k):
            attempts.append(1)
            return _Blocked()

        monkeypatch.setattr(httpx, "get", fake_get)
        with pytest.raises(BlockedError):
            fetch_eastmoney_history("600000", 5, limiter=RateLimiter(0.0))
        assert len(attempts) == 1, "限流不该重试，重试只会把封禁期拖长"


class TestEstimate:
    def test_request_count_matches_pages(self):
        row = estimate(1000, 5, 0.5, 4, source="tencent")
        assert row["pages_per_symbol"] == math.ceil(5 / 2)
        assert row["requests"] == 1000 * 3
        assert row["hours"] == pytest.approx(1000 * 3 * 0.5 / 4 / 3600)

    def test_ten_years_needs_more_pages(self):
        assert estimate(10, 10, 0.5, 1, source="tencent")["pages_per_symbol"] == 5

    def test_eastmoney_needs_single_request_regardless_of_years(self):
        """东财一次取全段：年数只影响返回行数，不影响请求数。

        这是换源的全部理由——腾讯 5 年要 3 次请求，东财 1 次，且不用防翻页空洞。
        """
        for years in (1, 5, 10):
            row = estimate(100, years, 0.3, 1, source="eastmoney")
            assert row["requests"] == 100
            assert row["pages_per_symbol"] == 1

    def test_storage_scales_with_years(self):
        assert estimate(100, 10, 0.5, 1)["bytes"] == 2 * estimate(100, 5, 0.5, 1)["bytes"]


class TestExpandSource:
    """换源与强制重抓的行为。

    `--force` 存在的理由：换数据源后成交额口径会变（腾讯存复权后、东财存原始），
    新旧文件混在一个缓存里，按成交额筛流动性的策略会拿到系统性不同的数值。
    所以要么全量重抓统一，要么明确知道自己在混口径——这里保证前者可用。
    """

    def _fetcher(self, calls: list[str], rows: int = 300):
        def fetch(symbol, years, *, limiter, today=None):
            calls.append(symbol)
            dates = _bdays(date(2024, 1, 1), date(2025, 12, 31))[:rows]
            return _frame(dates)

        return fetch

    def test_force_refetches_completed_symbols(self, tmp_path, monkeypatch):
        from scripts import expand_market_data as mod

        done = target_path(tmp_path, "600519", 5)
        _frame(_bdays(date(2024, 1, 1), date(2025, 6, 30))).to_parquet(done, index=False)

        calls: list[str] = []
        monkeypatch.setitem(mod.FETCHERS, "eastmoney", self._fetcher(calls))

        progress = mod.expand(
            ["600519"], tmp_path, 5, source="eastmoney", force=True, verbose=False
        )
        assert calls == ["600519"], "force 应当已完成的也重抓"
        assert progress.written == 1

    def test_without_force_completed_symbols_are_skipped(self, tmp_path, monkeypatch):
        from scripts import expand_market_data as mod

        done = target_path(tmp_path, "600519", 5)
        _frame(_bdays(date(2024, 1, 1), date(2025, 6, 30))).to_parquet(done, index=False)

        calls: list[str] = []
        monkeypatch.setitem(mod.FETCHERS, "eastmoney", self._fetcher(calls))

        progress = mod.expand(["600519"], tmp_path, 5, source="eastmoney", verbose=False)
        assert calls == []
        assert progress.skipped == 1

    def test_unknown_source_rejected(self, tmp_path):
        from scripts import expand_market_data as mod

        with pytest.raises(ValueError, match="未知数据源"):
            mod.expand(["600519"], tmp_path, 5, source="不存在的源", verbose=False)

    def test_blocked_aborts_whole_batch(self, tmp_path, monkeypatch):
        from scripts import expand_market_data as mod

        def blocked(symbol, years, *, limiter, today=None):
            raise mod.BlockedError("HTTP 501")

        monkeypatch.setitem(mod.FETCHERS, "eastmoney", blocked)
        progress = mod.expand(
            ["600519", "000001", "600036"],
            tmp_path,
            5,
            workers=1,
            source="eastmoney",
            verbose=False,
        )
        assert progress.blocked is True
        assert progress.abort_reason is not None


class TestRequestsFor:
    def test_eastmoney_always_one(self):
        assert [requests_for("eastmoney", y) for y in (1, 5, 10)] == [1, 1, 1]

    def test_tencent_scales_with_years(self):
        assert requests_for("tencent", 2) == 1
        assert requests_for("tencent", 5) == 3
        assert requests_for("tencent", 10) == 5


class TestRateLimiter:
    def test_enforces_minimum_interval(self):
        limiter = RateLimiter(0.05)
        limiter.wait()
        start = pd.Timestamp.now()
        for _ in range(3):
            limiter.wait()
        elapsed = (pd.Timestamp.now() - start).total_seconds()
        assert elapsed >= 0.1


class TestMain:
    def test_missing_universe_returns_one(self, tmp_path, capsys):
        assert main(["--universe", str(tmp_path / "nope.json")]) == 1
        assert "标的清单" in capsys.readouterr().err

    def test_build_universe_exits_without_fetching(self, tmp_path, monkeypatch):
        import sys
        import types

        fake = types.ModuleType("akshare")
        fake.stock_info_a_code_name = lambda: pd.DataFrame(
            {"code": ["600519", "000001"], "name": ["贵州茅台", "平安银行"]}
        )
        fake.stock_info_sh_delist = lambda: pd.DataFrame({"公司代码": ["600001"]})
        fake.stock_info_sz_delist = lambda: pd.DataFrame({"证券代码": ["000002"]})
        monkeypatch.setitem(sys.modules, "akshare", fake)

        called = {"fetched": False}

        def boom(*args, **kwargs):
            called["fetched"] = True
            raise AssertionError("建清单不该触发抓取")

        monkeypatch.setattr("scripts.expand_market_data.expand", boom)
        universe = tmp_path / "u.json"
        assert main(["--build-universe", "--universe", str(universe)]) == 0
        assert called["fetched"] is False

        codes = json.loads(universe.read_text(encoding="utf-8"))["codes"]
        # 在市 + 退市都要在，且补零到 6 位
        assert set(codes) == {"600519", "000001", "600001", "000002"}
