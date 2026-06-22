"""交易日历工具：判断交易日、节假日、交易时段。"""
from __future__ import annotations

from datetime import date, datetime, time
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
    """内置主要节假日（降级用，覆盖主要区间）。"""
    holidays = []
    # 固定节假日（月-日）
    fixed = [(1, 1), (5, 1), (10, 1), (10, 2), (10, 3), (10, 4), (10, 5), (10, 6), (10, 7)]
    year = date.today().year
    for m, d in fixed:
        holidays.append(date(year, m, d))
    return set(holidays)
