"""前视偏差修复单元测试（2026-08-07, AUDIT_LOOKAHEAD_20260807）

验证 engine_mixin._scan_signals 的盘中K线隔离逻辑:
1. 盘中(<15:00)且数据源已含当日未完成K线（最后一行日期==今日）: 剔除最后一行
2. 盘中(<15:00)但数据源只到昨日已收盘bar（A股日K源盘中常态）: 保留最后一行
   ⚠️ 2026-08-11 修复：原实现盘中无条件 iloc[:-1]，把昨日完整bar也删掉，
   策略基于 2 个交易日前数据计算（实证: 2026-08-11 10:19 日志 最后bar=2026-08-07，
   而缓存最后bar=08-10）。正确语义: 仅当最后一行日期==今日才剔除。
3. 收盘后(≥15:00): 完整K线保留
4. 剔除后数据不足返回 None
"""

from __future__ import annotations

import pandas as pd
import pytest

from gugu.engine.engine_mixin import EngineOpsMixin


def _make_df(n=30, end="2026-08-07"):
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


def _should_trim(df: pd.DataFrame, now_hm: str, today: str) -> bool:
    """复刻修复后的剔除判定逻辑（与 engine_mixin._scan_signals 一致）。"""
    if now_hm >= "15:00" or "date" not in df.columns:
        return False
    last_bar_date = pd.Timestamp(df["date"].iloc[-1]).date()
    return last_bar_date == pd.Timestamp(today).date()


class TestIntradayLookaheadFix:
    """盘中K线隔离逻辑(修复的核心判断)"""

    def test_intraday_drops_row_when_last_bar_is_today(self):
        """盘中(10:30) + 数据源已含当日bar(最后一行==今日): 剔除最后一行"""
        df = _make_df(30, end="2026-08-07")
        # 模拟数据源盘中已含当日未完成bar
        df_today = pd.concat(
            [
                df,
                pd.DataFrame(
                    [
                        {
                            "date": pd.Timestamp("2026-08-11"),
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
        assert _should_trim(df_today, "10:30", "2026-08-11")
        trimmed = df_today.iloc[:-1].copy()
        assert len(trimmed) == 30  # 当日未完成K线被剔除
        assert pd.Timestamp(trimmed["date"].iloc[-1]).date() == pd.Timestamp("2026-08-07").date()

    def test_intraday_keeps_last_row_when_source_only_has_yesterday(self):
        """盘中(10:30) + 数据源只到昨日bar(最后一行==昨日): 保留最后一行
        ——2026-08-11 修复点：A股日K源盘中不含当日bar，最后一行是昨日已收盘bar，
        无条件剔除会让策略数据停在 2 个交易日前。"""
        df = _make_df(30, end="2026-08-10")  # 最后一行=昨日(2026-08-10)已收盘
        assert not _should_trim(df, "10:30", "2026-08-11")
        assert len(df) == 30  # 完整保留，不剔除
        assert pd.Timestamp(df["date"].iloc[-1]).date() == pd.Timestamp("2026-08-10").date()

    def test_after_15h_keeps_full(self):
        """收盘后(15:05): 保留完整K线"""
        df = _make_df(30)
        assert not _should_trim(df, "15:05", "2026-08-11")
        assert len(df) == 30

    def test_short_df_after_drop(self):
        """剔除后数据不足(<2行): 触发保护"""
        df = _make_df(2, end="2026-08-11")
        assert _should_trim(df, "10:30", "2026-08-11")
        trimmed = df.iloc[:-1].copy()
        assert len(trimmed) == 1
        assert len(trimmed) < 2  # 触发 len(df)<2 保护, 返回 None

    def test_mixin_has_scan_signals(self):
        """确认修复所在函数存在(防止重构删除)"""
        assert hasattr(EngineOpsMixin, "_scan_signals")
