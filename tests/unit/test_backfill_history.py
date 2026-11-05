"""补长历史缓存测试（2026-11-05）。

这个脚本会往 `data/parquet_cache/` 写文件，所以测试重点是**不许搞坏已有数据**：
- 已存在更长的同标的数据时必须跳过，不能覆盖成更短的；
- 拉取失败必须如实标失败，不能写空文件冒充；
- 当日半根 bar 默认剔除，避免带进回测；
- 文件名用真实行数，否则 `min_rows=天数` 的检查会把长历史误判成短样本。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts.backfill_history import (
    cache_filename,
    existing_max_rows,
    load_pool,
    main,
    plan_symbol,
    sanitize_history,
)

DATES = [f"2024-04-{d:02d}" for d in range(1, 16)]


def _frame(rows: int = 10, *, start: str = "2024-05-01") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=rows, freq="D"),
            "open": [10.0] * rows,
            "high": [10.5] * rows,
            "low": [9.5] * rows,
            "close": [10.2] * rows,
            "volume": [1_000_000] * rows,
        }
    )


# ── 命名 ─────────────────────────────────────────────────


def test_cache_filename_uses_real_row_count() -> None:
    """命名必须写真实行数，写虚数会被 validate_stock_history 判成短样本。"""
    assert cache_filename("600519", 801) == "600519_801d_none.parquet"
    assert cache_filename("519", 800) == "000519_800d_none.parquet"


# ── 清洗 ─────────────────────────────────────────────────


def test_sanitize_drops_duplicate_dates_keeping_last() -> None:
    df = _frame(5)
    dup = pd.concat([df, df.iloc[[2]].assign(close=11.0)], ignore_index=True)
    clean = sanitize_history(dup)
    assert clean is not None
    assert len(clean) == 5
    assert clean.loc[clean["date"] == pd.Timestamp("2024-05-03"), "close"].iloc[0] == pytest.approx(
        11.0
    )


def test_sanitize_sorts_by_date() -> None:
    clean = sanitize_history(_frame(6).iloc[::-1])
    assert clean is not None
    assert clean["date"].is_monotonic_increasing


def test_sanitize_drops_non_positive_prices() -> None:
    df = _frame(4)
    df.loc[1, "close"] = 0.0
    df.loc[2, "low"] = -1.0
    clean = sanitize_history(df)
    assert clean is not None
    assert len(clean) == 2


def test_sanitize_drops_missing_columns() -> None:
    assert sanitize_history(pd.DataFrame({"date": ["2024-01-01"], "close": [1.0]})) is None


def test_sanitize_returns_none_on_empty() -> None:
    assert sanitize_history(pd.DataFrame()) is None
    assert sanitize_history(None) is None


def test_sanitize_can_drop_specific_date() -> None:
    df = _frame(3)  # 05-01 ~ 05-03
    clean = sanitize_history(df, drop_date="2024-05-03")
    assert clean is not None
    assert len(clean) == 2
    assert clean["date"].max() == pd.Timestamp("2024-05-02")


# ── 写入计划 ─────────────────────────────────────────────


def test_plan_writes_new_long_history(tmp_path: Path) -> None:
    plan = plan_symbol("600519", _frame(10), tmp_path, drop_date=None)
    assert plan.action == "write"
    assert plan.rows == 10
    assert plan.target is not None and plan.target.name == "600519_10d_none.parquet"


def test_plan_skips_when_existing_is_longer(tmp_path: Path) -> None:
    """这是最重要的一条：补数脚本绝不能把长历史覆盖成短历史。"""
    _frame(20).to_parquet(tmp_path / "600519_20d_none.parquet")
    plan = plan_symbol("600519", _frame(10), tmp_path, drop_date=None)
    assert plan.action == "skip_existing"
    assert "不缩短" in plan.reason


def test_plan_writes_when_existing_is_shorter(tmp_path: Path) -> None:
    _frame(5).to_parquet(tmp_path / "600519_5d_none.parquet")
    plan = plan_symbol("600519", _frame(10), tmp_path, drop_date=None)
    assert plan.action == "write"


def test_plan_ignores_other_symbols_when_comparing(tmp_path: Path) -> None:
    _frame(50).to_parquet(tmp_path / "000858_50d_none.parquet")
    plan = plan_symbol("600519", _frame(10), tmp_path, drop_date=None)
    assert plan.action == "write"


def test_plan_reports_no_data_instead_of_writing_placeholder(tmp_path: Path) -> None:
    assert plan_symbol("600519", None, tmp_path, drop_date=None).action == "no_data"
    empty = pd.DataFrame()
    assert plan_symbol("600519", empty, tmp_path, drop_date=None).action == "no_data"


def test_existing_max_rows_reads_across_adjust_suffixes(tmp_path: Path) -> None:
    _frame(5).to_parquet(tmp_path / "600519_5d_none.parquet")
    _frame(12).to_parquet(tmp_path / "600519_12d_qfq.parquet")
    assert existing_max_rows("600519", tmp_path) == 12


def test_existing_max_rows_zero_when_absent(tmp_path: Path) -> None:
    assert existing_max_rows("600519", tmp_path) == 0


# ── 标的池 ───────────────────────────────────────────────


def test_load_pool_strips_prefix(tmp_path: Path) -> None:
    pool = tmp_path / "pool.json"
    pool.write_text(
        '{"events": [{"code": "sh600519"}, {"code": "sz000858"}, {"code": "bad"}]}',
        encoding="utf-8",
    )
    assert load_pool(pool) == ["000858", "600519"]


def test_load_pool_missing_file(tmp_path: Path) -> None:
    assert load_pool(tmp_path / "nope.json") == []


# ── 命令行契约 ───────────────────────────────────────────


def test_main_errors_without_symbols(tmp_path: Path) -> None:
    code = main(
        ["--pool-file", str(tmp_path / "nope.json"), "--cache-dir", str(tmp_path), "--index", ""]
    )
    assert code == 1


def test_main_returns_error_when_every_fetch_fails(tmp_path: Path, monkeypatch) -> None:
    """全部拉不到就必须报失败，不能返回成功让人以为补上了。"""
    import scripts.backfill_history as backfill

    monkeypatch.setattr(
        "gugu.analysis.event_study_data.fetch_stock_klines",
        lambda symbol, days: (_ for _ in ()).throw(RuntimeError("blocked")),
    )
    code = backfill.main(
        ["--symbols", "600519", "--cache-dir", str(tmp_path / "cache"), "--index", ""]
    )
    assert code == 1


def test_main_writes_file_and_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    import scripts.backfill_history as backfill

    frame = _frame(10)
    monkeypatch.setattr(
        "gugu.analysis.event_study_data.fetch_stock_klines",
        lambda symbol, days: frame.copy(),
    )
    cache = tmp_path / "cache"
    argv = ["--symbols", "600519", "--cache-dir", str(cache), "--index", ""]
    assert backfill.main(argv) == 0
    written = list(cache.glob("*.parquet"))
    assert len(written) == 1 and written[0].name == "600519_10d_none.parquet"

    # 第二次运行不得重复写入更短/同长的文件
    assert backfill.main(argv) == 0
    assert len(list(cache.glob("*.parquet"))) == 1


def test_main_backfills_benchmark_index(tmp_path: Path, monkeypatch) -> None:
    """基准指数必须一起补：指数只有 200 行时，800 行窗口算不出超额。"""
    import scripts.backfill_history as backfill

    frame = _frame(10)
    monkeypatch.setattr(
        "gugu.analysis.event_study_data.fetch_stock_klines", lambda symbol, days: frame.copy()
    )
    monkeypatch.setattr(
        "gugu.analysis.event_study_data.fetch_index_klines", lambda code, days: frame.copy()
    )
    cache = tmp_path / "cache"
    code = backfill.main(["--symbols", "600519", "--cache-dir", str(cache), "--index", "sh000300"])
    assert code == 0
    names = sorted(p.name for p in cache.glob("*.parquet"))
    assert names == ["000300_10d_none.parquet", "600519_10d_none.parquet"]
