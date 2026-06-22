"""执行层单元测试。"""
from __future__ import annotations

import pytest

from gugu.execution import PaperBroker


@pytest.fixture
def broker(tmp_path, monkeypatch) -> PaperBroker:
    """每个测试用独立状态文件，避免持久化干扰。"""
    monkeypatch.setattr("gugu.execution.paper.STATE_FILE", tmp_path / "state.json")
    return PaperBroker(initial_capital=1_000_000)


def test_buy_and_get_account(broker):
    """测试买入后账户变化。"""
    result = broker.order("600519", "buy", 100, 1500.0)
    assert result.success
    assert result.quantity == 100
    assert result.commission > 0

    account = broker.get_account()
    assert account.cash < 1_000_000
    assert "600519" in account.positions
    assert account.positions["600519"].quantity == 100


def test_buy_invalid_quantity(broker):
    """测试非法数量被拒绝。"""
    result = broker.order("600519", "buy", 50, 1500.0)
    assert not result.success
    assert "100" in result.message


def test_buy_insufficient_cash(broker):
    """测试资金不足。"""
    result = broker.order("600519", "buy", 1_000_000, 1500.0)
    assert not result.success
    assert "资金" in result.message or "cash" in result.message.lower()


def test_sell_t_plus_1(broker):
    """测试 T+1 卖出限制。"""
    broker.order("600519", "buy", 100, 1500.0)
    result = broker.order("600519", "sell", 100, 1600.0)
    assert not result.success
    assert "T+1" in result.message or "可卖" in result.message


def test_sell_after_settle(broker):
    """T+1 结算后可卖。"""
    broker.order("600519", "buy", 100, 1500.0)
    broker.settle_t_plus_1()
    result = broker.order("600519", "sell", 100, 1600.0)
    assert result.success
    assert result.stamp_tax > 0
    assert "600519" not in broker.get_portfolio()


def test_update_price_and_profit(broker):
    """测试更新价格后盈亏。"""
    broker.order("600519", "buy", 100, 1500.0)
    broker.settle_t_plus_1()
    broker.update_price("600519", 1600.0)
    pos = broker.get_position("600519")
    assert pos is not None
    assert pos.current_price == 1600.0
    # 盈利 = (市价 - 成本) * 数量；成本含买入滑点
    expected_profit = (1600.0 - pos.avg_cost) * 100
    assert pos.profit == pytest.approx(expected_profit, rel=1e-3)


def test_trade_record(broker):
    """测试交易记录。"""
    broker.order("600519", "buy", 100, 1500.0)
    assert len(broker.trades) == 1
    assert broker.trades[0]["symbol"] == "600519"
    assert broker.trades[0]["direction"] == "buy"


def test_add_position_t_plus_1_unchanged(broker):
    """追加买入后，旧持仓 available 不变，新买入部分当日不可卖。"""
    broker.order("600519", "buy", 100, 1500.0)
    broker.settle_t_plus_1()
    pos = broker.get_position("600519")
    assert pos is not None
    assert pos.available == 100

    broker.order("600519", "buy", 200, 1500.0)
    assert pos.quantity == 300
    assert pos.available == 100  # 追加部分仍受 T+1 限制


def test_daily_start_value_recorded(broker):
    """settle_t_plus_1 应记录日初净值。"""
    broker.order("600519", "buy", 100, 1500.0)
    broker.settle_t_plus_1()
    assert broker.daily_start_value > 0
    broker.reset_daily_start_value()
    assert broker.daily_start_value == broker.get_account().total_value
