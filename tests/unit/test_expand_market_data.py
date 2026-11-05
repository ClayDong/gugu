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
    RateLimiter,
    already_done,
    estimate,
    fetch_history,
    main,
    quality_check,
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
        assert any("行数偏少" in p for p in quality_check(frame, 5))

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


class TestEstimate:
    def test_request_count_matches_pages(self):
        row = estimate(1000, 5, 0.5, 4)
        assert row["pages_per_symbol"] == math.ceil(5 / 2)
        assert row["requests"] == 1000 * 3
        assert row["hours"] == pytest.approx(1000 * 3 * 0.5 / 4 / 3600)

    def test_ten_years_needs_more_pages(self):
        assert estimate(10, 10, 0.5, 1)["pages_per_symbol"] == 5

    def test_storage_scales_with_years(self):
        assert estimate(100, 10, 0.5, 1)["bytes"] == 2 * estimate(100, 5, 0.5, 1)["bytes"]


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
