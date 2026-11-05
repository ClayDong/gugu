"""离线回测数据门面契约（2026-11-04 评审 P0-3）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from gugu.backtest.data_gate import (
    iter_cache_files,
    load_stock_history,
    parse_cache_name,
    resolve_cache_path,
    validate_stock_history,
)

ROOT = Path(__file__).resolve().parents[2]
REAL_CACHE = ROOT / "data" / "parquet_cache"


def _df(rows: int = 60, start: str = "2024-01-01") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=rows)
    close = [100.0 + i for i in range(rows)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [c - 1 for c in close],
            "high": [c + 1 for c in close],
            "low": [c - 2 for c in close],
            "close": close,
            "volume": [1_000_000] * rows,
        }
    )


class TestCacheNaming:
    def test_parses_legacy_and_adjust_suffixed_names(self) -> None:
        assert parse_cache_name(Path("600519_500d.parquet")) == ("600519", 500, "unknown")
        assert parse_cache_name(Path("000858_500d_none.parquet")) == ("000858", 500, "none")
        assert parse_cache_name(Path("random.parquet")) is None

    def test_resolve_prefers_legacy_then_none(self, tmp_path: Path) -> None:
        none_file = tmp_path / "600519_500d_none.parquet"
        _df().to_parquet(none_file)
        assert resolve_cache_path("600519", 500, tmp_path) == none_file
        legacy = tmp_path / "600519_500d.parquet"
        _df().to_parquet(legacy)
        assert resolve_cache_path("600519", 500, tmp_path) == legacy

    def test_iter_cache_files_finds_adjust_suffix(self, tmp_path: Path) -> None:
        _df().to_parquet(tmp_path / "000858_500d_none.parquet")
        _df().to_parquet(tmp_path / "600519_200d_qfq.parquet")
        (tmp_path / "noise.parquet").write_bytes(b"x")
        assert len(iter_cache_files(tmp_path, days=500)) == 1
        assert len(iter_cache_files(tmp_path)) == 2

    def test_real_cache_500d_files_are_now_discoverable(self) -> None:
        """修复前 glob('*_500d.parquet') 命中 0，现应识别到 *_500d_none.parquet。"""
        if not REAL_CACHE.is_dir():
            pytest.skip("本地无行情缓存")
        files = iter_cache_files(REAL_CACHE, days=500)
        assert files, "500d 缓存应至少被识别到一个（即便样本长度不合格）"
        assert all(parse_cache_name(f)[1] == 500 for f in files)


class TestHistoryValidation:
    def test_missing_columns_rejected(self) -> None:
        df = _df().drop(columns=["volume"])
        assert validate_stock_history(df, symbol="X", min_rows=10) is None

    def test_short_sample_rejected(self) -> None:
        assert validate_stock_history(_df(10), symbol="X", min_rows=250) is None

    def test_future_bar_rejected(self) -> None:
        assert validate_stock_history(_df(60), symbol="X", min_rows=10, asof="2024-02-01") is None

    def test_valid_history_sorted_and_typed(self) -> None:
        df = _df(30).iloc[::-1].reset_index(drop=True)
        clean = validate_stock_history(df, symbol="X", min_rows=10)
        assert clean is not None
        assert clean["date"].is_monotonic_increasing
        assert pd.api.types.is_datetime64_any_dtype(clean["date"])

    def test_load_stock_history_reads_suffixed_file(self, tmp_path: Path) -> None:
        _df(60).to_parquet(tmp_path / "600519_60d_none.parquet")
        df = load_stock_history("600519", days=60, cache_dir=tmp_path)
        assert df is not None and len(df) == 60

    def test_load_stock_history_rejects_short_sample(self, tmp_path: Path) -> None:
        _df(60).to_parquet(tmp_path / "600519_500d_none.parquet")
        assert load_stock_history("600519", days=500, cache_dir=tmp_path) is None


class TestPointInTime:
    def test_historical_bars_unchanged_between_snapshots(self, tmp_path: Path) -> None:
        """PIT 回归：T+1 快照新增一根 bar 时，T 之前的历史 close 必须逐行相同。

        前复权缓存若按最新日重算全部历史价，此断言会立即变红。
        """
        t0 = _df(60, start="2024-01-01")
        t1 = _df(61, start="2024-01-01")
        t0.to_parquet(tmp_path / "snap_t0.parquet")
        t1.to_parquet(tmp_path / "snap_t1.parquet")
        a = pd.read_parquet(tmp_path / "snap_t0.parquet")
        b = pd.read_parquet(tmp_path / "snap_t1.parquet")
        pd.testing.assert_series_equal(a["close"], b["close"].iloc[: len(a)].reset_index(drop=True))
