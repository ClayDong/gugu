"""风控模块测试。"""
from __future__ import annotations

import pytest

from gugu.models import Position
from gugu.risk.manager import RiskManager
from gugu.risk.rules import RiskAction, RiskLevel


@pytest.fixture
def manager() -> RiskManager:
    return RiskManager(
        {
            "max_position_ratio": 0.30,
            "daily_loss_warn": 0.03,
            "daily_loss_halt": 0.05,
            "max_total_positions": 2,
            "t_plus_1": True,
        }
    )


@pytest.fixture
def empty_portfolio() -> dict[str, Position]:
    return {}


def test_buy_allow(manager: RiskManager, empty_portfolio: dict) -> None:
    res = manager.check_order(
        "600519", "buy", 100, 1500.0, empty_portfolio, cash=1_000_000
    )
    assert res.action == RiskAction.ALLOW


def test_buy_exceeds_position_limit(manager: RiskManager, empty_portfolio: dict) -> None:
    res = manager.check_order(
        "600519", "buy", 900, 1500.0, empty_portfolio, cash=1_000_000
    )
    assert res.action == RiskAction.HALT
    assert res.level == RiskLevel.L1_POSITION


def test_buy_invalid_direction(manager: RiskManager, empty_portfolio: dict) -> None:
    res = manager.check_order("600519", "hold", 100, 1500.0, empty_portfolio)
    assert res.action == RiskAction.HALT
    assert res.level == RiskLevel.L3_SYSTEM


def test_buy_invalid_quantity(manager: RiskManager, empty_portfolio: dict) -> None:
    res = manager.check_order("600519", "buy", 0, 1500.0, empty_portfolio)
    assert res.action == RiskAction.HALT


def test_buy_invalid_price(manager: RiskManager, empty_portfolio: dict) -> None:
    res = manager.check_order("600519", "buy", 100, 0.0, empty_portfolio)
    assert res.action == RiskAction.HALT


def test_buy_suspended(manager: RiskManager, empty_portfolio: dict) -> None:
    res = manager.check_order(
        "600519", "buy", 100, 1500.0, empty_portfolio, is_suspended=True
    )
    assert res.action == RiskAction.HALT
    assert "suspended" in res.message or "停牌" in res.message


def test_buy_price_limit_up(manager: RiskManager, empty_portfolio: dict) -> None:
    res = manager.check_order(
        "600519", "buy", 100, 11.0, empty_portfolio, prev_close=10.0
    )
    assert res.action == RiskAction.HALT


def test_buy_max_total_positions(manager: RiskManager) -> None:
    portfolio = {
        "000001": Position("000001", 100, 100, 10.0, 10.0),
        "000002": Position("000002", 100, 100, 10.0, 10.0),
    }
    res = manager.check_order(
        "600519", "buy", 100, 10.0, portfolio, cash=1_000_000
    )
    assert res.action == RiskAction.HALT
    assert "Max total positions" in res.message


def test_sell_no_position(manager: RiskManager, empty_portfolio: dict) -> None:
    res = manager.check_order(
        "600519", "sell", 100, 1500.0, empty_portfolio
    )
    assert res.action == RiskAction.HALT


def test_sell_exceeds_holding(manager: RiskManager) -> None:
    portfolio = {"600519": Position("600519", 100, 100, 1500.0, 1600.0)}
    res = manager.check_order("600519", "sell", 200, 1600.0, portfolio)
    assert res.action == RiskAction.HALT


def test_sell_t_plus_1(manager: RiskManager) -> None:
    portfolio = {"600519": Position("600519", 1000, 0, 1500.0, 1600.0)}
    res = manager.check_order("600519", "sell", 100, 1600.0, portfolio)
    assert res.action == RiskAction.HALT
    assert "T+1" in res.message


def test_sell_allow(manager: RiskManager) -> None:
    portfolio = {"600519": Position("600519", 1000, 1000, 1500.0, 1600.0)}
    res = manager.check_order("600519", "sell", 500, 1600.0, portfolio)
    assert res.action == RiskAction.ALLOW


def test_check_daily_loss_allow(manager: RiskManager) -> None:
    res = manager.check_daily_loss(0.01)
    assert res.action == RiskAction.ALLOW


def test_check_daily_loss_warn(manager: RiskManager) -> None:
    res = manager.check_daily_loss(0.03)
    assert res.action == RiskAction.WARN
    assert res.level == RiskLevel.L2_DAILY_LOSS


def test_check_daily_loss_halt(manager: RiskManager) -> None:
    res = manager.check_daily_loss(0.05)
    assert res.action == RiskAction.HALT
    assert manager.is_halted


def test_halt_blocks_all_orders(manager: RiskManager, empty_portfolio: dict) -> None:
    manager.check_daily_loss(0.05)
    res = manager.check_order(
        "600519", "buy", 100, 1500.0, empty_portfolio, cash=1_000_000
    )
    assert res.action == RiskAction.HALT
    assert "halt" in res.message.lower() or "熔断" in res.message


def test_reset(manager: RiskManager) -> None:
    manager.check_daily_loss(0.05)
    assert manager.is_halted
    manager.reset()
    assert not manager.is_halted


def test_is_tradable_main_board() -> None:
    manager = RiskManager()
    # 主板 ±10%，11 > 10 * 1.1 = 11.0，处于涨停，不可买
    assert not manager.is_tradable("600519", 11.0, 10.0)
    # 9 > 9.0 跌停价，可买
    assert manager.is_tradable("600519", 9.01, 10.0)


def test_is_tradable_chinese_next() -> None:
    manager = RiskManager()
    assert not manager.is_tradable("300001", 12.0, 10.0)
    assert manager.is_tradable("300001", 11.99, 10.0)


def test_is_tradable_st() -> None:
    manager = RiskManager()
    assert not manager.is_tradable("600519", 10.5, 10.0, is_st=True)


def test_risk_check_result_allowed() -> None:
    from gugu.risk.rules import RiskCheckResult

    allow = RiskCheckResult(RiskLevel.L1_POSITION, RiskAction.ALLOW, "ok")
    halt = RiskCheckResult(RiskLevel.L1_POSITION, RiskAction.HALT, "no")
    assert allow.allowed
    assert not halt.allowed
