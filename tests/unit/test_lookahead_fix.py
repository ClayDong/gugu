"""PIT 盘中隔离回归测试（2026-11-04 自证循环整改）。

原文件在测试内部自行实现 ``_should_trim()`` 并只测自己，而生产代码中根本
不存在对应逻辑——测试永远绿，线上零防线。本文件改为：

1. 直接测生产函数 ``gugu.data.pit`` 的判定与清洗语义；
2. 真实调用 ``DataManager.fetch_stock_history``，验证 PIT 清洗确实发生在
   统一返回口（实时请求 / 内存缓存 / Parquet 缓存三条路径都经此处）。

语义基线（2026-08-11 事故复盘）：
- 盘中 (<15:00) 且最后一根 bar 日期 == 今天：剔除（未完成 K 线是未来信息）；
- 盘中但数据源只到昨日已收盘 bar：保留（A 股日 K 源盘中常态，无条件
  ``iloc[:-1]`` 会让策略基于 2 个交易日前的数据计算）；
- 收盘后 (>=15:00)：完整保留。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from gugu.data.pit import should_trim_intraday_bar, trim_unfinished_intraday_bar


class _FrozenClock:
    """替换 pit.datetime，注入可复现的时刻 2026-08-11 10:30（盘中）。"""

    @classmethod
    def now(cls) -> datetime:
        return datetime(2026, 8, 11, 10, 30)


class _FrozenClockAfterClose:
    @classmethod
    def now(cls) -> datetime:
        return datetime(2026, 8, 11, 15, 5)


def _make_df(n: int = 30, end: str = "2026-08-07") -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [10.0 + i * 0.1 for i in range(n)],
            "high": [10.5 + i * 0.1 for i in range(n)],
            "low": [9.5 + i * 0.1 for i in range(n)],
            "close": [10.2 + i * 0.1 for i in range(n)],
            "volume": [1000000 + i * 1000 for i in range(n)],
        }
    )


def _append_today_bar(df: pd.DataFrame, today: str = "2026-08-11") -> pd.DataFrame:
    return pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": pd.Timestamp(today),
                        "open": 15.0,
                        "high": 15.5,
                        "low": 14.5,
                        "close": 15.2,
                        "volume": 1200000,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )


class TestIntradayPitGuard:
    """生产函数的语义契约。"""

    def test_should_trim_when_last_bar_is_today(self) -> None:
        df = _append_today_bar(_make_df(30, end="2026-08-10"))
        assert should_trim_intraday_bar(df, now_hm="10:30", today=datetime(2026, 8, 11).date())
        with patch("gugu.data.pit.datetime", _FrozenClock):
            clean = trim_unfinished_intraday_bar(df)
        assert len(clean) == 30
        assert pd.Timestamp(clean["date"].iloc[-1]).date() == datetime(2026, 8, 10).date()

    def test_keeps_last_row_when_source_only_has_yesterday(self) -> None:
        df = _make_df(30, end="2026-08-10")
        assert not should_trim_intraday_bar(df, now_hm="10:30", today=datetime(2026, 8, 11).date())
        with patch("gugu.data.pit.datetime", _FrozenClock):
            clean = trim_unfinished_intraday_bar(df)
        assert len(clean) == 30
        assert pd.Timestamp(clean["date"].iloc[-1]).date() == datetime(2026, 8, 10).date()

    def test_after_close_keeps_full_history(self) -> None:
        df = _append_today_bar(_make_df(30, end="2026-08-10"))
        assert not should_trim_intraday_bar(df, now_hm="15:05", today=datetime(2026, 8, 11).date())
        with patch("gugu.data.pit.datetime", _FrozenClockAfterClose):
            clean = trim_unfinished_intraday_bar(df)
        assert len(clean) == 31

    def test_short_df_after_drop_keeps_only_closed_bar(self) -> None:
        """剔除后样本不足时只保留已收盘 bar，样本量由 data_gate.min_rows 拒绝。"""
        df = _append_today_bar(_make_df(1, end="2026-08-10"))
        with patch("gugu.data.pit.datetime", _FrozenClock):
            clean = trim_unfinished_intraday_bar(df)
        assert len(clean) == 1
        assert pd.Timestamp(clean["date"].iloc[-1]).date() == datetime(2026, 8, 10).date()

    def test_attrs_preserved_and_trim_is_idempotent(self) -> None:
        df = _append_today_bar(_make_df(5, end="2026-08-10"))
        df.attrs["source"] = "tencent"
        with patch("gugu.data.pit.datetime", _FrozenClock):
            first = trim_unfinished_intraday_bar(df)
            second = trim_unfinished_intraday_bar(first)
        assert first.attrs["source"] == "tencent"
        assert len(first) == len(second) == 5
        assert second.attrs["source"] == "tencent"

    def test_missing_or_invalid_date_is_untouched(self) -> None:
        assert not should_trim_intraday_bar(
            pd.DataFrame({"close": [1.0]}),
            now_hm="10:30",
            today=datetime(2026, 8, 11).date(),
        )
        with patch("gugu.data.pit.datetime", _FrozenClock):
            clean = trim_unfinished_intraday_bar(pd.DataFrame({"close": [1.0]}))
        assert len(clean) == 1


class TestProductionCallSite:
    """验证清洗真的挂在 DataManager 统一返回口，而不是又一个自证测试。"""

    @pytest.mark.asyncio
    async def test_fetch_stock_history_applies_pit_guard(self) -> None:
        from gugu.data.manager import DataManager

        df = _append_today_bar(_make_df(30, end="2026-08-10"))
        df.attrs["source"] = "akshare"
        dm = DataManager.__new__(DataManager)
        dm._call_with_fallback = AsyncMock(return_value=df)
        with patch("gugu.data.pit.datetime", _FrozenClock):
            result = await dm.fetch_stock_history("sh600000", days=120)
        dm._call_with_fallback.assert_awaited_once_with("fetch_stock_history", "sh600000", 120)
        assert len(result) == 30, "盘中当日未完成 K 线必须在统一返回口被剔除"
        assert pd.Timestamp(result["date"].iloc[-1]).date() == datetime(2026, 8, 10).date()
        assert result.attrs["source"] == "akshare", "清洗不得丢弃数据血缘 attrs"

    @pytest.mark.asyncio
    async def test_fetch_stock_history_after_close_returns_full(self) -> None:
        from gugu.data.manager import DataManager

        df = _append_today_bar(_make_df(30, end="2026-08-10"))
        dm = DataManager.__new__(DataManager)
        dm._call_with_fallback = AsyncMock(return_value=df)
        with patch("gugu.data.pit.datetime", _FrozenClockAfterClose):
            result = await dm.fetch_stock_history("sh600000", days=120)
        assert len(result) == 31
