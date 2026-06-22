"""工具函数单元测试。"""
from __future__ import annotations

from datetime import date, datetime
from unittest import mock

from gugu.utils.calendar import _holiday_set, is_trading_day, is_trading_time


def test_is_trading_day_weekday() -> None:
    # 周一
    assert is_trading_day(date(2024, 1, 8)) is True


def test_is_trading_day_weekend() -> None:
    # 周六
    assert is_trading_day(date(2024, 1, 6)) is False


def test_is_trading_day_default() -> None:
    with mock.patch("gugu.utils.calendar.date") as mock_date:
        mock_date.today.return_value = date(2024, 1, 8)
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        assert is_trading_day() is True


def test_is_trading_time_morning() -> None:
    dt = datetime(2024, 1, 8, 10, 30)
    assert is_trading_time(dt) is True


def test_is_trading_time_noon() -> None:
    dt = datetime(2024, 1, 8, 12, 0)
    assert is_trading_time(dt) is False


def test_is_trading_time_afternoon() -> None:
    dt = datetime(2024, 1, 8, 14, 0)
    assert is_trading_time(dt) is True


def test_is_trading_time_weekend() -> None:
    dt = datetime(2024, 1, 6, 10, 30)
    assert is_trading_time(dt) is False


def test_is_trading_day_exception_fallback() -> None:
    """chinese_calendar 异常时降级到周末判断。"""
    with mock.patch("builtins.__import__", side_effect=ImportError("no module")):
        assert is_trading_day(date(2024, 1, 8)) is True
        assert is_trading_day(date(2024, 1, 6)) is False


def test_holiday_set() -> None:
    holidays = _holiday_set()
    assert len(holidays) >= 9
    this_year = date.today().year
    assert date(this_year, 10, 1) in holidays
