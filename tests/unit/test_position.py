"""持仓模型测试。"""
from __future__ import annotations

import pytest

from gugu.models import Position


def test_position_market_value() -> None:
    pos = Position("600519", 1000, 1000, 1500.0, 1800.0)
    assert pos.market_value == 1_800_000.0


def test_position_profit() -> None:
    pos = Position("600519", 1000, 1000, 1500.0, 1800.0)
    assert pos.profit == 300_000.0


def test_position_profit_ratio() -> None:
    pos = Position("600519", 1000, 1000, 1500.0, 1800.0)
    assert pos.profit_ratio == pytest.approx(0.20)


def test_position_zero_avg_cost() -> None:
    pos = Position("600519", 1000, 1000, 0.0, 1800.0)
    assert pos.profit_ratio == 0.0
