import datetime

import pandas as pd
import pytest

from gugu.data.quality import (
    DataQualityError,
    validate_sector_flow,
    validate_stock_flow,
    validate_stock_history,
)

TODAY = datetime.date.today()


def _recent_dates(n: int):
    """生成最近的 n 个日期。"""
    return pd.date_range(TODAY - datetime.timedelta(days=n - 1), periods=n)


def test_validate_stock_history_ok() -> None:
    df = pd.DataFrame(
        {
            "date": _recent_dates(5),
            "open": [10.0] * 5,
            "high": [11.0] * 5,
            "low": [9.0] * 5,
            "close": [10.5] * 5,
            "volume": [1000] * 5,
        }
    )
    out = validate_stock_history(df, "600519")
    assert len(out) == 5


def test_validate_stock_history_missing_column() -> None:
    df = pd.DataFrame({"date": [1, 2, 3]})
    with pytest.raises(DataQualityError, match="缺失列"):
        validate_stock_history(df, "600519")


def test_validate_stock_history_negative_values() -> None:
    df = pd.DataFrame(
        {
            "date": _recent_dates(3),
            "open": [10.0, 10.0, 10.0],
            "high": [11.0, 11.0, 11.0],
            "low": [9.0, 9.0, 9.0],
            "close": [10.5, -1.0, 10.5],
            "volume": [1000, 1000, 1000],
        }
    )
    out = validate_stock_history(df, "600519")
    assert len(out) == 2


def test_validate_stock_history_high_low_invalid() -> None:
    df = pd.DataFrame(
        {
            "date": _recent_dates(3),
            "open": [10.0] * 3,
            "high": [11.0, 8.0, 11.0],
            "low": [9.0] * 3,
            "close": [10.5] * 3,
            "volume": [1000] * 3,
        }
    )
    out = validate_stock_history(df, "600519")
    assert len(out) == 2


def test_validate_sector_flow_ok() -> None:
    df = pd.DataFrame(
        {
            "sector": ["白酒", "白酒", "银行"],
            "main_net": [1e8, 2e8, 3e8],
            "main_pct": [0.1, 0.2, 0.3],
        }
    )
    out = validate_sector_flow(df)
    assert len(out) == 2


def test_validate_sector_flow_missing_column() -> None:
    df = pd.DataFrame({"sector": ["白酒"]})
    with pytest.raises(DataQualityError, match="缺失列"):
        validate_sector_flow(df)


def test_validate_stock_flow_ok() -> None:
    df = pd.DataFrame(
        {
            "date": _recent_dates(3),
            "main_net": [1e6, 2e6, 3e6],
            "main_pct": [0.01, 0.02, 0.03],
        }
    )
    out = validate_stock_flow(df, "600519")
    assert len(out) == 3


def test_validate_stock_flow_without_date() -> None:
    df = pd.DataFrame({"main_net": [1e6], "main_pct": [0.01]})
    out = validate_stock_flow(df, "600519")
    assert len(out) == 1


def test_validate_stock_history_zero_price() -> None:
    """零价数据应被剔除。"""
    df = pd.DataFrame(
        {
            "date": _recent_dates(3),
            "open": [10.0, 0.0, 10.0],
            "high": [11.0, 0.0, 11.0],
            "low": [9.0, 0.0, 9.0],
            "close": [10.5, 0.0, 10.5],
            "volume": [1000, 0, 1000],
        }
    )
    out = validate_stock_history(df, "600519")
    assert len(out) == 2


def test_plausible_price_excludes_latest_value_from_ma() -> None:
    from gugu.data.quality import PlausiblePriceRule

    df = pd.DataFrame({"close": [100.0, 100.0, 1000.0], "volume": [1000] * 3})
    passed, result, _ = PlausiblePriceRule(ma_window=2, max_deviation=3.0).check(df)
    assert passed is False
    assert result.empty
