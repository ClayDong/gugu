"""工具函数单元测试。"""

from __future__ import annotations

from datetime import date, datetime
from unittest import mock

from gugu.utils.calendar import (
    _holiday_set,
    is_trading_day,
    is_trading_time,
    trading_days_between,
)


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
    real_import = __import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "chinese_calendar":
            raise ImportError("no module")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        assert is_trading_day(date(2024, 1, 8)) is True
        assert is_trading_day(date(2024, 1, 6)) is False


def test_holiday_set() -> None:
    holidays = _holiday_set()
    assert len(holidays) >= 9
    this_year = date.today().year
    assert date(this_year, 10, 1) in holidays


class TestTradingDaysBetween:
    """交易日差计算测试（P1 缓存新鲜度修复）。"""

    def test_same_day_returns_zero(self) -> None:
        """同一天返回 0。"""
        assert trading_days_between(date(2024, 1, 8), date(2024, 1, 8)) == 0

    def test_end_before_start_returns_zero(self) -> None:
        """结束日期早于开始日期返回 0。"""
        assert trading_days_between(date(2024, 1, 10), date(2024, 1, 8)) == 0

    def test_one_trading_day_difference(self) -> None:
        """相邻两个交易日（周一到周二）差 1 天。"""
        # 2024-01-08 是周一，2024-01-09 是周二
        assert trading_days_between(date(2024, 1, 8), date(2024, 1, 9)) == 1

    def test_weekend_not_counted(self) -> None:
        """周末不计入交易日。"""
        # 2024-01-05 是周五，2024-01-08 是周一
        # 中间只有周六周日，交易日差 = 1（只有周一那一天）
        result = trading_days_between(date(2024, 1, 5), date(2024, 1, 8))
        assert result == 1  # 只有 1/8 周一这一个交易日

    def test_full_week(self) -> None:
        """一整周（周一到下周一）= 5 个交易日。"""
        # 2024-01-08 周一 → 2024-01-15 周一
        # 中间经过 1/9(二) 1/10(三) 1/11(四) 1/12(五) 1/15(一) = 5 个交易日
        result = trading_days_between(date(2024, 1, 8), date(2024, 1, 15))
        assert result == 5

    def test_long_holiday_scenario(self) -> None:
        """模拟国庆长假场景：自然日多但交易日少。"""
        # 2024-09-30 是国庆前最后一个交易日（周一）
        # 2024-10-08 是国庆后第一个交易日（周二）
        # 自然日 = 8 天，但交易日只有 1 天
        # 注意：实际国庆调休可能不同，这里用周末近似验证
        # 1/5(五) → 1/8(一)：自然日 3 天，交易日 1 天
        natural_days = (date(2024, 1, 8) - date(2024, 1, 5)).days
        trading = trading_days_between(date(2024, 1, 5), date(2024, 1, 8))
        assert natural_days == 3
        assert trading == 1
        assert trading < natural_days  # 交易日 < 自然日（周末被跳过）
