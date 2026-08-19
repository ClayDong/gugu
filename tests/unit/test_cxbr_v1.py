"""CXBR V1 研究层边界和硬规则测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.research.cxbr_v1 import CXBRPosition, scan
from gugu.strategies.registry import list_strategies


def _frame(n: int = 25) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=n)
    rows: list[dict] = []
    for i, date in enumerate(dates):
        rows.append(
            {
                "date": date,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 100.0,
                "limit_up_price": 11.0,
                "turnover_pct": 5.0,
            }
        )
    # T: 非一字首板；T+1: 放量实体阴线洗盘；T+2~T+4: 缩量护盘。
    rows[20].update(open=10.0, high=11.1, low=9.9, close=11.0, volume=100.0)
    rows[21].update(open=11.0, high=11.1, low=10.1, close=10.3, volume=220.0)
    rows[22].update(open=10.4, high=10.8, low=10.35, close=10.55, volume=50.0)
    rows[23].update(open=10.55, high=10.85, low=10.45, close=10.65, volume=50.0)
    rows[24].update(open=10.65, high=10.9, low=10.5, close=10.8, volume=50.0)
    return pd.DataFrame(rows)


def _metadata() -> dict:
    return {
        "float_mv": 4_500_000_000.0,
        "is_st": False,
        "is_suspended": False,
        "listing_date": "2020-01-01",
    }


def test_cxbr_v1_is_isolated_from_runtime_registry() -> None:
    assert "cxbr_v1" not in list_strategies()


def test_scan_requires_real_hard_rule_fields_and_fails_closed() -> None:
    result = scan("600001", _frame().drop(columns=["limit_up_price"]), _metadata())
    assert result.action == "none"
    assert result.data_quality == "unavailable"
    assert "涨停" in result.reason or "字段" in result.reason


def test_scan_returns_buy_a_with_separate_stop_prices() -> None:
    result = scan("600001", _frame(), _metadata())
    assert result.action == "buy"
    assert result.stage == "entry"
    assert result.buy_point == "A"
    assert result.data_quality == "fresh_valid"
    assert result.strict_stop_price is not None
    assert result.conditional_stop_price is not None
    assert result.conditional_stop_price < result.strict_stop_price
    assert result.suggested_position == 0.20


def test_scan_rejects_stale_or_out_of_universe_metadata() -> None:
    metadata = _metadata()
    metadata["float_mv"] = 6_000_000_000.0
    result = scan("600001", _frame(), metadata)
    assert result.action == "none"
    assert result.data_quality == "unavailable"


def test_scan_rejects_non_main_board_codes() -> None:
    result = scan("300001", _frame(), _metadata())
    assert result.action == "none"
    assert result.data_quality == "unavailable"


def test_scan_rejects_new_listing_without_history_base() -> None:
    metadata = _metadata()
    metadata["listing_date"] = "2026-01-01"
    result = scan("600001", _frame(), metadata)
    assert result.action == "none"
    assert result.data_quality == "unavailable"
    assert "上市" in result.reason


def test_exit_remains_available_when_buy_scan_data_is_missing() -> None:
    frame = _frame(27)
    frame.loc[25, ["open", "high", "low", "close", "volume"]] = [10.2, 10.3, 9.8, 9.9, 200.0]
    frame.loc[26, ["open", "high", "low", "close", "volume"]] = [9.9, 10.0, 9.6, 9.8, 220.0]
    position = CXBRPosition(
        entry_date=frame.loc[24, "date"],
        entry_price=10.8,
        first_board_open=10.0,
    )
    from gugu.research.cxbr_v1 import evaluate_exit

    result = evaluate_exit(
        "600001", frame.drop(columns=["limit_up_price", "turnover_pct"]), position
    )
    assert result.action == "sell"
    assert result.stage == "exit"
    assert result.data_quality == "fresh_valid"
