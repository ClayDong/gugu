"""交易日历工具：判断交易日、节假日、交易时段。"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, time, timedelta
from functools import lru_cache

# 交易时段（A 股）
MORNING_START = time(9, 30)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
AFTERNOON_END = time(15, 0)


def is_trading_day(d: date | None = None) -> bool:
    """判断是否为 A 股交易日。

    优先使用 chinese_calendar 库（精确到每年调休），降级到周末判断。
    """
    if d is None:
        d = date.today()
    try:
        import chinese_calendar as cc

        return bool(cc.is_workday(d))
    except Exception:
        # 降级：周末 + 内置固定节假日表
        return d.weekday() < 5 and d not in _holiday_set()


def is_trading_time(now: datetime | None = None) -> bool:
    """判断当前是否在交易时段内。"""
    if now is None:
        now = datetime.now()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return (MORNING_START <= t <= MORNING_END) or (AFTERNOON_START <= t <= AFTERNOON_END)


@lru_cache(maxsize=1)
def _holiday_set() -> set[date]:
    """内置主要节日（降级用，覆盖当前年份+下一年）。

    P1-13 修复：覆盖当前年份和下一年，避免跨年运行时误判。
    """
    holidays = []
    # 固定节日（月-日）
    fixed = [(1, 1), (5, 1), (10, 1), (10, 2), (10, 3), (10, 4), (10, 5), (10, 6), (10, 7)]
    # P1-13: 覆盖当前年份和下一年
    for year in (date.today().year, date.today().year + 1):
        for m, d in fixed:
            with contextlib.suppress(ValueError):
                holidays.append(date(year, m, d))
    return set(holidays)


def refresh_holiday_cache() -> None:
    """刷新节日缓存（跨年后调用）。"""
    _holiday_set.cache_clear()


def last_trading_day(ref: date | None = None, max_lookback: int = 15) -> date:
    """返回 ref 之前的最近一个交易日（不含 ref 自身）。

    用于"T+1 日验证昨日选股"场景：周一执行时返回上周五，
    节后首日执行时返回节前最后一个交易日。
    """
    if ref is None:
        ref = date.today()
    d = ref - timedelta(days=1)
    for _ in range(max_lookback):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    # 兜底：返回自然日昨天
    return ref - timedelta(days=1)
