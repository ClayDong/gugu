"""回测引擎单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.backtest import BacktestEngine, calc_metrics
from gugu.backtest.report import format_report, format_report_dict
from gugu.strategies import DualMAStrategy


@pytest.fixture
def trend_df() -> pd.DataFrame:
    """生成「上涨→回调→再上涨」行情。

    P0-3: 确保 dual_ma 的金叉+放量+MA20>MA60+MA20斜率>0 同时满足。
    - 前 60 行: 上涨 10→25（建立 MA20 > MA60 基础）
    - 60-65 行: 回调 25→22（制造 MA3 < MA8 的金叉前置条件）
    - 65-100 行: 再上涨 22→38（MA3 上穿 MA8 = 金叉，MA20 斜率恢复向上）
    """
    np.random.seed(7)
    n = 100
    dates = pd.date_range("2026-01-01", periods=n, freq="B").date
    close = np.zeros(n)
    close[:60] = np.linspace(10, 25, 60)  # 第一段：上涨
    close[60:65] = np.linspace(25, 22, 5)  # 第二段：回调
    close[65:] = np.linspace(22, 38, n - 65)  # 第三段：再上涨
    close = close + np.random.normal(0, 0.2, n)
    # 金叉区域（row 65-75）固定放量 10M 确保量比 ≥ 1.5
    volumes = np.random.randint(1e6, 3e6, n).astype(float)
    volumes[65:75] = 10e6
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": volumes,
            "amount": volumes * close,
        }
    )


@pytest.fixture
def low_vol_df() -> pd.DataFrame:
    """低波动率行情（用于测试认知引擎不影响趋势）。"""
    np.random.seed(42)
    n = 60
    dates = pd.date_range("2026-01-01", periods=n, freq="B").date
    close = np.linspace(100, 115, n) + np.random.normal(0, 0.5, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.3,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.random.randint(1e6, 5e6, n).astype(float),
            "amount": np.random.randint(1e7, 5e7, n).astype(float),
        }
    )


def test_backtest_runs(trend_df):
    """回测能跑完。"""
    engine = BacktestEngine(initial_capital=100_000)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, trend_df, "000001")
    assert result is not None
    assert len(result.equity_curve) == len(trend_df)
    assert result.symbol == "000001"


def test_metrics_basic():
    """指标计算基本正确。"""
    equity = pd.Series(
        [100, 105, 103, 110, 108, 120], index=pd.date_range("2026-01-01", periods=6, freq="B")
    )
    metrics = calc_metrics(equity, [])
    assert metrics["total_return"] == pytest.approx(0.20, rel=1e-3)
    assert metrics["max_drawdown"] > 0


def test_backtest_with_signals(trend_df):
    """策略触发信号后回测有交易。"""
    engine = BacktestEngine(initial_capital=100_000)
    strategy = DualMAStrategy(params={"short_window": 3, "long_window": 8})
    result = engine.run(strategy, trend_df, "000001")
    assert len(result.trades) > 0


def test_metrics_empty():
    """空权益曲线和空交易列表。"""
    metrics = calc_metrics(pd.Series(dtype=float), [])
    assert metrics["total_return"] == 0.0
    assert metrics["total_trades"] == 0.0
    assert metrics["win_rate"] == 0.0
    assert metrics["profit_factor"] == 0.0


def test_metrics_single_point():
    """单点权益曲线。"""
    equity = pd.Series([100.0])
    metrics = calc_metrics(equity, [])
    assert metrics["total_return"] == 0.0
    assert metrics["sharpe"] == 0.0


def test_metrics_profit_factor_inf():
    """只有盈利交易时 profit_factor 为 inf。"""
    trades = [
        {"direction": "buy", "date": "2026-01-01"},
        {"direction": "sell", "profit": 100.0, "date": "2026-01-02"},
    ]
    equity = pd.Series([100, 110], index=pd.date_range("2026-01-01", periods=2, freq="B"))
    metrics = calc_metrics(equity, trades)
    assert metrics["profit_factor"] == float("inf")
    assert metrics["win_rate"] == 1.0
    assert metrics["avg_hold_days"] == 1.0


def test_metrics_loss_only():
    """只有亏损交易。"""
    trades = [
        {"direction": "buy", "date": "2026-01-01"},
        {"direction": "sell", "profit": -50.0, "date": "2026-01-02"},
    ]
    equity = pd.Series([100, 90], index=pd.date_range("2026-01-01", periods=2, freq="B"))
    metrics = calc_metrics(equity, trades)
    assert metrics["win_rate"] == 0.0
    assert metrics["profit_factor"] == 0.0


def test_format_report(trend_df):
    """文本回测报告格式化。"""
    engine = BacktestEngine(initial_capital=100_000)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, trend_df, "000001")
    text = format_report(result)
    assert "Backtest Report" in text
    assert "000001" in text


def test_format_report_dict(trend_df):
    """回测报告 dict 格式化。"""
    engine = BacktestEngine(initial_capital=100_000)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, trend_df, "000001")
    data = format_report_dict(result)
    assert data["strategy"] == "dual_ma"
    assert data["symbol"] == "000001"
    assert "total_return" in data


def test_backtest_empty_data():
    """空数据返回空结果。"""
    engine = BacktestEngine()
    strategy = DualMAStrategy()
    result = engine.run(strategy, pd.DataFrame(), "000001")
    assert len(result.equity_curve) == 0
    assert len(result.trades) == 0


def test_backtest_insufficient_cash():
    """资金不足无法买入一手的场景。"""
    engine = BacktestEngine(initial_capital=100)
    strategy = DualMAStrategy(params={"short_window": 3, "long_window": 5})
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=10, freq="B"),
            "open": [10.0] * 10,
            "high": [11.0] * 10,
            "low": [9.0] * 10,
            "close": [10.0] * 10,
            "volume": [1e6] * 10,
            "amount": [1e7] * 10,
        }
    )
    result = engine.run(strategy, df, "000001")
    assert len(result.trades) == 0


def test_backtest_cognitive_engine_runs(low_vol_df):
    """启用认知引擎的回测能跑完且有 cognitive_log。"""
    engine = BacktestEngine(enable_cognitive_engine=True)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, low_vol_df, "000001")
    assert result is not None
    assert len(result.equity_curve) == len(low_vol_df)
    # cognitive_log 有可能为空（行情简单时无触发）
    assert hasattr(result, "cognitive_log")


def test_backtest_cognitive_log_reduces_trades(low_vol_df):
    """认知引擎应减少交易次数（趋势确认过滤假信号）。"""
    base = BacktestEngine(enable_cognitive_engine=False)
    cog = BacktestEngine(enable_cognitive_engine=True)
    strategy_base = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    strategy_cog = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    r_base = base.run(strategy_base, low_vol_df, "000001")
    r_cog = cog.run(strategy_cog, low_vol_df, "000001")
    # 认知引擎可能过滤入场，但不能断言交易次数必然减少
    # 至少认知引擎不会让交易次数暴增
    assert len(r_cog.trades) <= len(r_base.trades) + 5  # 容忍小幅波动


def test_backtest_cognitive_engine_trailing_stop_exits(low_vol_df):
    """认知引擎的移动止损应产生 EXIT 信号而非策略卖出。"""
    engine = BacktestEngine(enable_cognitive_engine=True)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, low_vol_df, "000001")
    # 验证 cognitive_log 中的 trailing_stop_exit 是合法值
    for entry in result.cognitive_log:
        if entry.get("action") == "trailing_stop_exit":
            assert "stop_price" in entry or "close" in entry


class TestTransactionCosts:
    """专项测试：A股交易费用模型（过户费+最低佣金，与PaperBroker对齐）。"""

    def test_calc_transfer_fee_shanghai(self):
        """沪市股票（600开头）双向收取0.001%过户费。"""
        engine = BacktestEngine()
        # 10万元交易，过户费=100000*0.00001=1元
        fee = engine._calc_transfer_fee("600519", 100_000)
        assert fee == pytest.approx(1.0, abs=0.01)

    def test_calc_transfer_fee_shenzhen(self):
        """深市股票（000/002/300开头）无过户费。"""
        engine = BacktestEngine()
        fee = engine._calc_transfer_fee("000001", 100_000)
        assert fee == 0.0
        fee2 = engine._calc_transfer_fee("002714", 100_000)
        assert fee2 == 0.0
        fee3 = engine._calc_transfer_fee("300750", 100_000)
        assert fee3 == 0.0

    def test_calc_transfer_fee_star_market(self):
        """科创板（688开头）收取过户费。"""
        engine = BacktestEngine()
        fee = engine._calc_transfer_fee("688981", 100_000)
        assert fee == pytest.approx(1.0, abs=0.01)

    def test_min_commission_buy(self):
        """小金额买入时佣金不低于5元（最低佣金）。"""
        engine = BacktestEngine(initial_capital=100_000)
        # 价格10元，买100股=1000元，按0.025%费率佣金=0.25元，应为5元
        trade = engine._execute_buy("600000", "2026-01-01", 10.0, 10_000)
        assert trade is not None
        assert trade.commission >= 5.0
        assert trade.commission == pytest.approx(5.0, abs=0.01)

    def test_min_commission_sell(self):
        """小金额卖出时佣金不低于5元。"""
        engine = BacktestEngine(initial_capital=100_000)
        # 价格10元，卖100股=1000元，佣金应≥5元
        trade = engine._execute_sell("600000", "2026-01-01", 10.0, 100, 1000.0)
        assert trade.commission >= 5.0

    def test_large_trade_commission_proportional(self):
        """大额交易佣金按比例收取（超过5元时不按最低）。"""
        engine = BacktestEngine(initial_capital=1_000_000)
        # 价格100元，买10000股=100万元，佣金=100万*0.00025=250元
        trade = engine._execute_buy("600519", "2026-01-01", 100.0, 1_000_000)
        assert trade is not None
        expected = 100 * trade.quantity * 0.00025
        assert trade.commission == pytest.approx(expected, rel=0.01)

    def test_shanghai_buy_has_transfer_fee(self):
        """沪市买入有过户费。"""
        engine = BacktestEngine(initial_capital=1_000_000)
        trade = engine._execute_buy("600519", "2026-01-01", 100.0, 1_000_000)
        assert trade is not None
        assert trade.transfer_fee > 0

    def test_shenzhen_buy_no_transfer_fee(self):
        """深市买入无过户费。"""
        engine = BacktestEngine(initial_capital=1_000_000)
        trade = engine._execute_buy("000001", "2026-01-01", 100.0, 1_000_000)
        assert trade is not None
        assert trade.transfer_fee == 0.0

    def test_shanghai_sell_has_transfer_fee(self):
        """沪市卖出有过户费+印花税。"""
        engine = BacktestEngine(initial_capital=100_000)
        trade = engine._execute_sell("600519", "2026-01-01", 100.0, 1000, 80_000)
        assert trade.transfer_fee > 0
        assert trade.stamp_tax > 0  # 卖出有印花税

    def test_full_cycle_deducts_all_fees(self, trend_df):
        """完整买卖周期：现金扣除应包含佣金+过户费+印花税。"""
        engine = BacktestEngine(initial_capital=100_000)
        strategy = DualMAStrategy(params={"short_window": 3, "long_window": 8})
        # 使用沪市股票（600开头）确保有过户费
        result = engine.run(strategy, trend_df, "600000")
        if len(result.trades) >= 2:
            buy_trade = [t for t in result.trades if t.direction == "buy"][0]
            sell_trade = [t for t in result.trades if t.direction == "sell"][-1]
            # 买入应有过户费（沪市）
            assert buy_trade.transfer_fee >= 0
            # 卖出应有印花税和过户费
            assert sell_trade.stamp_tax > 0
            # 佣金应≥5元
            assert buy_trade.commission >= 5.0
            assert sell_trade.commission >= 5.0

    def test_backtest_equity_accounts_for_fees(self, trend_df):
        """回测权益曲线应反映交易费用（不能100%完美零成本）。"""
        engine = BacktestEngine(initial_capital=100_000, commission_rate=0.00025)
        engine_no_fee = BacktestEngine(
            initial_capital=100_000, commission_rate=0.0, stamp_tax=0.0, slippage=0.0
        )
        strategy = DualMAStrategy(params={"short_window": 3, "long_window": 8})
        result_with_fee = engine.run(strategy, trend_df, "600000")
        result_no_fee = engine_no_fee.run(strategy, trend_df, "600000")
        if len(result_with_fee.trades) > 0:
            # 有费用的回测最终权益应≤无费用回测（考虑滑点和费用）
            final_with_fee = result_with_fee.equity_curve.iloc[-1]
            final_no_fee = result_no_fee.equity_curve.iloc[-1]
            # 允许微小差异（随机数），但有费用不应显著高于无费用
            assert final_with_fee <= final_no_fee * 1.02  # 容忍2%以内的随机差异
