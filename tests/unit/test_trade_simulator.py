"""尾盘选股交易模拟器单元测试（TDD: RED）。

业务逻辑（第一性原理，由 113 条验证数据推导）：
- T 日尾盘选股（信号有效：日内冲高率 86%）
- T+1 开盘低吸买入（68% 低开 = 好买点）
- 日内 +2% 止盈 / -3% 止损 / 收盘平仓（当日了结不隔夜）
- 大盘开盘跌超阈值 → 放弃买入（7/30 大盘 -0.74%，选中股 -9.58%）

对抗性审查：
- 止盈和止损同日都触发时（无法确定先后）→ 保守取止损，避免高估期望值
- 开盘价为 0（停牌/数据异常）→ 返回 None 不产生交易
"""

from __future__ import annotations

import pytest

from gugu.screener.trade_simulator import (
    TradeResult,
    simulate_trade,
    check_market_gate,
)


class TestSimulateTrade:
    """交易模拟：开盘买入 → 止盈/止损/收盘平仓。"""

    def test_take_profit_only(self):
        """日内最高触及 +2% 止盈线 → 止盈离场。"""
        r = simulate_trade(
            open_price=10.0,
            high_price=10.30,  # +3%，超过止盈线
            low_price=9.90,  # 未破止损线
            close_price=9.95,
            take_profit_pct=2.0,
            stop_loss_pct=3.0,
        )
        assert r is not None
        assert r.exit_reason == "take_profit"
        assert r.return_pct == pytest.approx(2.0)

    def test_stop_loss_only(self):
        """日内最低破 -3% 止损线 → 止损离场。"""
        r = simulate_trade(
            open_price=10.0,
            high_price=10.10,
            low_price=9.60,  # -4%，跌破止损线
            close_price=9.70,
            take_profit_pct=2.0,
            stop_loss_pct=3.0,
        )
        assert r is not None
        assert r.exit_reason == "stop_loss"
        assert r.return_pct == pytest.approx(-3.0)

    def test_close_at_eod(self):
        """止盈止损都未触发 → 收盘平仓。"""
        r = simulate_trade(
            open_price=10.0,
            high_price=10.10,  # +1%，未到止盈
            low_price=9.90,  # -1%，未破止损
            close_price=10.05,  # +0.5%
            take_profit_pct=2.0,
            stop_loss_pct=3.0,
        )
        assert r is not None
        assert r.exit_reason == "close"
        assert r.return_pct == pytest.approx(0.5)

    def test_both_triggered_conservative_stop_loss(self):
        """止盈止损同日都触发 → 保守取止损（无法确定先后，不高估期望值）。"""
        r = simulate_trade(
            open_price=10.0,
            high_price=10.50,  # +5%，触发止盈
            low_price=9.50,  # -5%，触发止损
            close_price=10.20,
            take_profit_pct=2.0,
            stop_loss_pct=3.0,
        )
        assert r is not None
        assert r.exit_reason == "stop_loss"
        assert r.return_pct == pytest.approx(-3.0)

    def test_exact_boundary_hit(self):
        """精确触及止盈线（+2.0%）→ 止盈。"""
        r = simulate_trade(
            open_price=10.0,
            high_price=10.20,  # 精确 +2.0%
            low_price=9.85,
            close_price=10.00,
            take_profit_pct=2.0,
            stop_loss_pct=3.0,
        )
        assert r is not None
        assert r.exit_reason == "take_profit"

    def test_zero_open_price_returns_none(self):
        """开盘价为 0（停牌/数据异常）→ 不产生交易。"""
        r = simulate_trade(
            open_price=0.0,
            high_price=10.0,
            low_price=9.0,
            close_price=9.5,
            take_profit_pct=2.0,
            stop_loss_pct=3.0,
        )
        assert r is None

    def test_invalid_ohlc_returns_none(self):
        """最高 < 最低（数据异常）→ 不产生交易。"""
        r = simulate_trade(
            open_price=10.0,
            high_price=9.5,  # 异常：high < low
            low_price=10.5,
            close_price=10.0,
            take_profit_pct=2.0,
            stop_loss_pct=3.0,
        )
        assert r is None

    def test_custom_params(self):
        """自定义止盈/止损参数生效。"""
        r = simulate_trade(
            open_price=20.0,
            high_price=20.50,  # +2.5%，超过自定义止盈 +2%
            low_price=19.40,  # -3%，超过自定义止损 -5%？不：-3% > -5%
            close_price=19.80,
            take_profit_pct=2.0,
            stop_loss_pct=5.0,
        )
        assert r is not None
        assert r.exit_reason == "take_profit"
        assert r.return_pct == pytest.approx(2.0)


class TestCheckMarketGate:
    """大盘开盘门控：大盘跌超阈值 → 放弃买入。"""

    def test_market_gate_blocked(self):
        """大盘开盘跌 -0.8%（超过 -0.5% 阈值）→ 阻断买入。"""
        gate = check_market_gate(
            benchmark_open_change=-0.8,
            skip_threshold=-0.5,
        )
        assert gate.blocked is True
        assert gate.reason == "market_open_drop"

    def test_market_gate_allowed(self):
        """大盘开盘 +0.3% → 正常买入。"""
        gate = check_market_gate(
            benchmark_open_change=0.3,
            skip_threshold=-0.5,
        )
        assert gate.blocked is False
        assert gate.reason == ""

    def test_market_gate_threshold_boundary(self):
        """大盘开盘精确 -0.5% → 不阻断（严格大于阈值才阻断）。"""
        gate = check_market_gate(
            benchmark_open_change=-0.5,
            skip_threshold=-0.5,
        )
        assert gate.blocked is False

    def test_market_gate_threshold_disabled(self):
        """阈值为 None → 门控关闭。"""
        gate = check_market_gate(
            benchmark_open_change=-2.0,
            skip_threshold=None,
        )
        assert gate.blocked is False

    def test_trade_result_dataclass(self):
        """TradeResult 字段完整性。"""
        r = TradeResult(exit_reason="close", return_pct=0.5)
        assert r.exit_reason == "close"
        assert r.return_pct == 0.5
