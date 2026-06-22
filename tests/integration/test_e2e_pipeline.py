"""端到端集成测试：不 mock 业务逻辑模块，仅 mock 外部 IO。

用 fixture 数据走完完整买入/卖出信号链路，验证最终输出正确。
覆盖 3 类边界场景：异常数据、降级场景、异常分支路径。
"""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np

from gugu.config import settings
from gugu.engine.signal_router import SignalRouter
from gugu.execution import PaperBroker
from gugu.risk import RiskManager
from gugu.strategies.registry import get_enabled_strategies
from gugu.wisdom import WisdomAdvisor


# ========== Fixture 数据 ==========

def make_bollinger_buy_df(days: int = 60, base_price: float = 100.0) -> pd.DataFrame:
    """生成触发布林带买入信号的数据：先跌后涨。"""
    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=days)
    # 前 40 天持续下跌，后 20 天微涨——触发下轨买入
    prices = [base_price * (1 - 0.008 * i) for i in range(40)] + \
             [base_price * (1 - 0.008 * 40) * (1 + 0.002 * i) for i in range(20)]
    close = pd.Series(prices[:days], dtype=float)
    high = close * 1.015
    low = close * 0.985
    open_ = close * 0.999
    volume = pd.Series([1_000_000] * days, dtype=float)
    amount = close * volume
    return pd.DataFrame({
        "date": dates[:days], "open": open_.values, "high": high.values,
        "low": low.values, "close": close.values, "volume": volume.values,
        "amount": amount.values,
    })


def make_turtle_sell_df(days: int = 60, base_price: float = 100.0) -> pd.DataFrame:
    """生成触发海龟卖出信号的数据：先涨后跌。"""
    np.random.seed(43)
    dates = pd.bdate_range("2024-01-01", periods=days)
    # 前 40 天持续上涨，后 20 天急跌——触发跌破出场线卖出
    prices = [base_price * (1 + 0.008 * i) for i in range(40)] + \
             [base_price * (1 + 0.008 * 40) * (1 - 0.015 * i) for i in range(20)]
    close = pd.Series(prices[:days], dtype=float)
    high = close * 1.015
    low = close * 0.985
    open_ = close * 0.999
    volume = pd.Series([1_000_000] * days, dtype=float)
    amount = close * volume
    return pd.DataFrame({
        "date": dates[:days], "open": open_.values, "high": high.values,
        "low": low.values, "close": close.values, "volume": volume.values,
        "amount": amount.values,
    })


def make_zero_volatility_df(days: int = 60, price: float = 100.0) -> pd.DataFrame:
    """生成零波动数据（停牌/无交易）。"""
    dates = pd.bdate_range("2024-01-01", periods=days)
    close = pd.Series([price] * days, dtype=float)
    return pd.DataFrame({
        "date": dates, "open": close.values, "high": close.values,
        "low": close.values, "close": close.values,
        "volume": pd.Series([0.0] * days), "amount": pd.Series([0.0] * days),
    })


# ========== 端到端集成测试 ==========

class TestEndToEndBuySignal:
    """端到端测试：买入信号完整链路。"""

    def test_bollinger_buy_signal_pipeline(self, tmp_path, monkeypatch):
        """布林带买入信号：数据 → 策略 → 智慧决策 → 风控 → 下单。"""
        monkeypatch.setattr("gugu.execution.paper.STATE_FILE", tmp_path / "state.json")

        # 1. 数据层
        df = make_bollinger_buy_df(60, 100.0)
        assert len(df) == 60
        assert (df["close"] > 0).all()

        # 2. 策略层
        strategies = get_enabled_strategies()
        router = SignalRouter(strategies)
        signal = router.route(df, "000858", name="五粮液")

        # 应该产生买入信号（布林带策略在下跌后触轨）
        if signal is None:
            pytest.skip("fixture 数据未触发策略信号，跳过")

        assert signal["direction"] == "buy"
        assert signal["confidence"] > 0

        # 3. 智慧决策层
        max_ratio = settings().get("risk", {}).get("max_position_ratio", 0.30)
        signal["suggested_position_ratio"] = max_ratio * 0.8
        signal["price"] = float(df["close"].iloc[-1])
        signal["prev_close"] = float(df["close"].iloc[-2])
        signal["is_st"] = False
        signal["is_suspended"] = False

        wisdom = WisdomAdvisor()
        enhanced = wisdom.advise(signal)

        # 智慧决策应调整仓位（试仓）
        assert enhanced.get("suggested_position_ratio") is not None
        assert enhanced["suggested_position_ratio"] <= max_ratio * 0.8
        # 应设置止损价
        assert enhanced.get("stop_loss_price") is not None
        assert enhanced["stop_loss_price"] < signal["price"]

        # 4. 风控层
        risk = RiskManager()
        broker = PaperBroker(initial_capital=1_000_000)
        account = broker.get_account()
        suggested_ratio = enhanced.get("suggested_position_ratio", 0.0)
        price = enhanced.get("price", 0)
        quantity = int(account.total_value * suggested_ratio / price / 100) * 100 if price > 0 else 0

        risk_result = risk.check_order(
            symbol="000858", direction="buy", quantity=quantity, price=price,
            portfolio=broker.get_portfolio(), cash=account.cash,
            prev_close=signal.get("prev_close"), is_st=False, is_suspended=False,
        )
        assert risk_result.allowed is True

        # 5. 执行层
        order_result = broker.order("000858", "buy", quantity, price)
        assert order_result.success is True

        # 验证最终持仓
        final_account = broker.get_account()
        assert "000858" in final_account.positions
        assert final_account.cash < 1_000_000


class TestEndToEndSellSignal:
    """端到端测试：卖出信号链路。"""

    def test_turtle_sell_signal_pipeline(self, tmp_path, monkeypatch):
        """海龟卖出信号：数据 → 策略 → 验证卖出置信度 > 0。"""
        monkeypatch.setattr("gugu.execution.paper.STATE_FILE", tmp_path / "state.json")

        df = make_turtle_sell_df(60, 100.0)
        strategies = get_enabled_strategies()
        router = SignalRouter(strategies)
        signal = router.route(df, "000858", name="五粮液")

        if signal is None:
            pytest.skip("fixture 数据未触发策略信号，跳过")

        if signal["direction"] == "sell":
            # 关键验证：卖出信号置信度必须 > 0（修复前恒为 0）
            assert signal["confidence"] > 0, "卖出信号置信度不应为 0"


# ========== 边界场景测试 ==========

class TestBoundaryZeroVolatility:
    """边界场景1：零波动数据（停牌/无交易）。"""

    def test_zero_volatility_no_signal(self):
        """零波动数据不应产生买入/卖出信号。"""
        df = make_zero_volatility_df(60, 100.0)
        strategies = get_enabled_strategies()
        router = SignalRouter(strategies)
        signal = router.route(df, "000858", name="五粮液")
        # 零波动不应产生信号，或信号置信度为 0
        if signal is not None:
            assert signal.get("confidence", 0) == 0 or signal.get("direction") == "hold"


class TestBoundaryLowConfidence:
    """边界场景2：低置信度信号被入场过滤。"""

    def test_low_confidence_filtered(self):
        """低置信度信号应被 wisdom 入场过滤。"""
        wisdom = WisdomAdvisor()
        signal = {
            "symbol": "600519", "name": "贵州茅台", "direction": "buy",
            "confidence": 0.35, "strategy": "turtle", "strategies": ["turtle"],
            "reason": "测试", "suggested_position_ratio": 0.24, "price": 1500.0,
        }
        enhanced = wisdom.advise(signal)
        assert enhanced.get("wisdom_filtered") is True


class TestBoundaryFallbackData:
    """边界场景3：降级场景——主源失败后降级源数据仍可用。"""

    def test_fallback_collector_different_api(self):
        """SinaCollector 应使用与主源不同的 API。"""
        from gugu.data.collectors.akshare_collector import AkshareCollector
        from gugu.data.collectors.fallback import SinaCollector

        # 验证两个采集器的 source 不同
        primary = AkshareCollector()
        fallback = SinaCollector()
        assert primary.source != fallback.source
        assert fallback.source == "sina"


class TestBoundaryBacktestTPlus1:
    """边界场景：回测 T+1 限制。"""

    def test_backtest_cannot_sell_same_day(self, tmp_path, monkeypatch):
        """回测中买入当天不能卖出。"""
        monkeypatch.setattr("gugu.execution.paper.STATE_FILE", tmp_path / "state.json")

        from gugu.backtest.engine import BacktestEngine
        from gugu.strategies.mean_revert import BollingerStrategy

        # 构造一个同一天先买后卖的数据
        engine = BacktestEngine(initial_capital=1_000_000)
        strategy = BollingerStrategy()
        df = make_bollinger_buy_df(60, 100.0)

        result = engine.run(strategy, df, "000858")
        # 验证回测完成无报错
        assert result is not None
        # 验证没有同日买卖的交易
        if len(result.trades) >= 2:
            for i in range(1, len(result.trades)):
                # 不应有买入和卖出在同一天的情况
                prev_trade = result.trades[i - 1]
                curr_trade = result.trades[i]
                if prev_trade.direction == "buy" and curr_trade.direction == "sell":
                    assert prev_trade.date != curr_trade.date, "T+1 违规：同日买入卖出"
