"""分析模块单元测试。

测试范围：
1. MultiPeriodRegimeDetector — 市场状态检测
2. PositionController — 仓位总控
3. PositionManager — 持仓管理
4. PerformanceAnalyzer — 绩效归因
5. StrategyPool — 策略池管理

所有测试使用合成/模拟数据，不依赖网络或数据库。
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.position_controller import PositionBudget, PositionController
from gugu.analysis.position_manager import PositionAdvice, PositionManager
from gugu.analysis.performance import PerformanceAnalyzer, PerformanceReport
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.analysis.strategy_pool import StrategyPool, StrategyWeight


# =========================================================================
# 辅助工厂函数
# =========================================================================


def make_uptrend_data(days: int = 250) -> pd.DataFrame:
    """构造一段明显上涨趋势的 OHLCV 数据。

    需要使价格显著偏离均线，确保 detector score > 0.3。
    使用大涨幅 + 低噪声确保 MA alignment 为 bullish + 高偏离度。
    """
    np.random.seed(42)
    base = 50.0
    # 分段：前 200 天缓慢上涨 + 后 50 天快速拉升，产生明显的 MA 偏离
    slow = np.linspace(0, 20, 200)
    fast = np.linspace(0, 80, 50)
    trend = np.concatenate([slow, 20 + fast])
    noise = np.random.randn(days) * 0.8
    close = base + trend + noise
    high = close + np.abs(np.random.randn(days)) * 0.4 + 0.2
    low = close - np.abs(np.random.randn(days)) * 0.4 - 0.2
    open_ = low + np.random.rand(days) * (high - low)
    volume = np.random.randint(2_000_000, 10_000_000, size=days)
    amount = close * volume
    return pd.DataFrame(
        {
            "date": pd.date_range("2023-01-01", periods=days, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


def make_downtrend_data(days: int = 250) -> pd.DataFrame:
    """构造一段明显下跌趋势的 OHLCV 数据。

    需要使价格显著偏离均线，确保 detector score < -0.3。
    """
    np.random.seed(42)
    base = 200.0
    slow = np.linspace(0, -15, 200)
    fast = np.linspace(0, -65, 50)
    trend = np.concatenate([slow, -15 + fast])
    noise = np.random.randn(days) * 0.8
    close = base + trend + noise
    high = close + np.abs(np.random.randn(days)) * 0.4 + 0.2
    low = close - np.abs(np.random.randn(days)) * 0.4 - 0.2
    open_ = low + np.random.rand(days) * (high - low)
    volume = np.random.randint(2_000_000, 10_000_000, size=days)
    amount = close * volume
    return pd.DataFrame(
        {
            "date": pd.date_range("2023-01-01", periods=days, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


def make_flat_data(days: int = 250) -> pd.DataFrame:
    """构造一段横盘震荡的 OHLCV 数据。"""
    np.random.seed(123)
    base = 100.0
    noise = np.random.randn(days) * 2  # 小幅度随机波动
    close = base + noise
    high = close + np.abs(np.random.randn(days)) * 1.5 + 0.3
    low = close - np.abs(np.random.randn(days)) * 1.5 - 0.3
    open_ = low + np.random.rand(days) * (high - low)
    volume = np.random.randint(2_000_000, 8_000_000, size=days)
    amount = close * volume
    return pd.DataFrame(
        {
            "date": pd.date_range("2023-01-01", periods=days, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


def make_crash_data(days: int = 250) -> pd.DataFrame:
    """构造一段暴跌行情数据。"""
    np.random.seed(42)
    base = 100.0
    trend = np.linspace(0, -80, days)
    high_vol_noise = np.random.randn(days) * 8
    close = base + trend + high_vol_noise
    high = close + np.abs(np.random.randn(days)) * 3 + 0.5
    low = close - np.abs(np.random.randn(days)) * 3 - 0.5
    open_ = low + np.random.rand(days) * (high - low)
    volume = np.random.randint(3_000_000, 15_000_000, size=days)
    amount = close * volume
    return pd.DataFrame(
        {
            "date": pd.date_range("2023-01-01", periods=days, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


@pytest.fixture
def uptrend_250() -> pd.DataFrame:
    """250 日上涨趋势数据。"""
    return make_uptrend_data(250)


@pytest.fixture
def downtrend_250() -> pd.DataFrame:
    """250 日下跌趋势数据。"""
    return make_downtrend_data(250)


@pytest.fixture
def flat_250() -> pd.DataFrame:
    """250 日横盘震荡数据。"""
    return make_flat_data(250)


@pytest.fixture
def crash_250() -> pd.DataFrame:
    """250 日暴跌行情数据。"""
    return make_crash_data(250)


# =========================================================================
# 辅助函数：patch data_manager 在具体模块中的引用
# =========================================================================


async def _mock_fetch_stock_history(data: pd.DataFrame, *args, **kwargs) -> pd.DataFrame:
    """模拟异步 fetch_stock_history 方法。"""
    return data


def _make_regime_detector_with_data(data: pd.DataFrame) -> MultiPeriodRegimeDetector:
    """构造一个使用模拟数据的 RegimeDetector。

    直接 Mock fetch_stock_history 方法，避免 patch 路径问题。
    """
    detector = MultiPeriodRegimeDetector()
    dm = MagicMock()
    dm.fetch_stock_history = _mock_fetch_stock_history.__get__(dm, MagicMock)
    # 绑定 data 到 partial
    import functools
    dm.fetch_stock_history = functools.partial(_mock_fetch_stock_history, data)
    detector._dm = dm
    return detector


def _make_performance_analyzer_with_data() -> PerformanceAnalyzer:
    """构造一个使用模拟 data_manager 的 PerformanceAnalyzer。"""
    import functools
    benchmark_df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=2, freq="D"),
        "close": [100, 105],
    })
    analyzer = PerformanceAnalyzer()
    dm = MagicMock()
    dm.fetch_stock_history = functools.partial(_mock_fetch_stock_history, benchmark_df)
    analyzer._dm = dm
    return analyzer


def _make_detector_raising(error: Exception) -> MultiPeriodRegimeDetector:
    """构造一个 fetch 抛异常的 RegimeDetector。"""
    async def _raise(*args, **kwargs):
        raise error
    detector = MultiPeriodRegimeDetector()
    dm = MagicMock()
    dm.fetch_stock_history = _raise
    detector._dm = dm
    return detector


# =========================================================================
# MultiPeriodRegimeDetector
# =========================================================================


class TestRegimeDetector:
    """MultiPeriodRegimeDetector 市场状态检测测试。"""

    @pytest.mark.asyncio
    async def test_base_case(self, uptrend_250: pd.DataFrame) -> None:
        """上涨趋势数据应返回 'bull' 市场状态。"""
        detector = _make_regime_detector_with_data(uptrend_250)
        result = await detector.detect()

        assert result["regime"] == "bull", f"预期 bull, 实际 {result['regime']}"
        assert result["total_position_limit"] >= 0.50, "牛市下总仓位应 >= 0.5"
        assert result["buy_signal_allowed"] is True, "牛市下应允许买入"
        assert result["sell_signal_required"] is False, "牛市下不应强制卖出"
        assert result["confidence"] > 0, "牛市判断置信度应 > 0"

    @pytest.mark.asyncio
    async def test_bear_case(self, downtrend_250: pd.DataFrame) -> None:
        """下跌趋势数据应返回 'bear' 市场状态。"""
        detector = _make_regime_detector_with_data(downtrend_250)
        result = await detector.detect()

        assert result["regime"] == "bear", f"预期 bear, 实际 {result['regime']}"
        assert result["total_position_limit"] <= 0.30, "熊市下总仓位应 <= 0.3"
        assert result["sell_signal_required"] is True, "熊市下应强制卖出"

    @pytest.mark.asyncio
    async def test_sideways(self, flat_250: pd.DataFrame) -> None:
        """横盘震荡数据应返回 'sideways' 市场状态。"""
        detector = _make_regime_detector_with_data(flat_250)
        result = await detector.detect()

        assert result["regime"] == "sideways", f"预期 sideways, 实际 {result['regime']}"
        assert result["total_position_limit"] == 0.40, "震荡市总仓位应为 0.40"

    @pytest.mark.asyncio
    async def test_empty_data(self) -> None:
        """空 DataFrame 应触发 fallback 保守模式。"""
        detector = _make_regime_detector_with_data(pd.DataFrame())
        result = await detector.detect()

        assert result["regime"] == "sideways", f"降级应为 sideways, 实际 {result['regime']}"
        assert result["total_position_limit"] == 0.40, "降级总仓位应为 0.40"
        assert result["buy_signal_allowed"] is True, "降级下应允许买入"
        assert result["sell_signal_required"] is False, "降级下不应强制卖出"
        assert result["confidence"] == 0.0, "降级置信度应为 0"

    @pytest.mark.asyncio
    async def test_fallback_on_exception(self) -> None:
        """fetch 抛出异常时应返回 fallback 结果。"""
        detector = _make_detector_raising(ValueError("API failure"))
        result = await detector.detect()

        assert result["regime"] == "sideways", "异常应降级为 sideways"
        assert "conservative" in result["reason"].lower() or "降级" in result["reason"]

    @pytest.mark.asyncio
    async def test_cache(self, uptrend_250: pd.DataFrame) -> None:
        """同一天重复调用应使用缓存。"""
        detector = _make_regime_detector_with_data(uptrend_250)

        result1 = await detector.detect()
        result2 = await detector.detect()

        assert result2["regime"] == result1["regime"]
        assert result2["total_position_limit"] == result1["total_position_limit"]


# =========================================================================
# PositionController
# =========================================================================


@dataclass
class FakeAccount:
    """模拟账户对象。"""
    total_value: float
    cash: float
    positions: list


class TestPositionController:
    """PositionController 仓位预算计算测试。"""

    def _make_regime(self, regime: str, limit: float, buy_allowed: bool = True,
                     sell_required: bool = False) -> dict:
        return {
            "regime": regime,
            "total_position_limit": limit,
            "buy_signal_allowed": buy_allowed,
            "sell_signal_required": sell_required,
        }

    @pytest.fixture(autouse=True)
    def mock_settings(self):
        """Mock settings() before each test in this class."""
        with patch("gugu.analysis.position_controller.settings") as mock:
            mock.return_value = {
                "risk": {
                    "max_position_ratio": 0.30,
                    "max_total_positions": 5,
                    "daily_loss_halt": 0.05,
                }
            }
            yield mock

    def test_bull_calculate(self) -> None:
        """牛市下应得到较高的总仓位上限。"""
        controller = PositionController()
        regime = self._make_regime("bull", 0.80)
        account = FakeAccount(total_value=1_000_000, cash=500_000, positions=[])

        budget = controller.calculate(regime=regime, account=account)

        assert budget.total_limit >= 0.70, f"牛市预期 >= 0.70, 实际 {budget.total_limit}"
        assert budget.single_limit > 0, "单股仓位上限应 > 0"
        assert budget.available_budget > 0, "可用资金应 > 0"
        assert budget.max_positions > 0, "最大持仓数应 > 0"

    def test_bear_calculate(self) -> None:
        """熊市下应得到较低的总仓位上限。"""
        controller = PositionController()
        regime = self._make_regime("bear", 0.20)
        account = FakeAccount(total_value=1_000_000, cash=900_000, positions=[])

        budget = controller.calculate(regime=regime, account=account)

        assert budget.total_limit <= 0.30, f"熊市预期 <= 0.30, 实际 {budget.total_limit}"
        assert budget.single_limit <= regime["total_position_limit"], "单股上限不应超过总上限"

    def test_halted_budget(self) -> None:
        """熔断时应将预算全部归零。"""
        controller = PositionController()
        regime = self._make_regime("bull", 0.80)
        account = FakeAccount(total_value=1_000_000, cash=500_000, positions=[])

        budget = controller.calculate(regime=regime, account=account, is_halted=True)

        assert budget.total_limit == 0.0, "熔断总上限应为 0"
        assert budget.single_limit == 0.0, "熔断单股上限应为 0"
        assert budget.available_budget == 0.0, "熔断可用预算应为 0"
        assert budget.max_positions == 0, "熔断最大持仓数应为 0"

    def test_default_values(self) -> None:
        """默认参数应产生合理的仓位预算。"""
        controller = PositionController()
        regime = self._make_regime("sideways", 0.40)
        account = FakeAccount(total_value=1_000_000, cash=600_000, positions=[])

        budget = controller.calculate(regime=regime, account=account)

        assert budget.total_limit == 0.40, f"默认震荡总仓位应为 0.40, 实际 {budget.total_limit}"
        assert budget.available_budget == 400_000, f"可用预算应为 400k, 实际 {budget.available_budget}"
        assert 0 < budget.single_limit <= 0.40, f"单股上限应 (0, 0.4], 实际 {budget.single_limit}"

    def test_accumulated_loss_halves_limit(self) -> None:
        """累计亏损 > 10% 时应使仓位减半。"""
        controller = PositionController()
        regime = self._make_regime("bull", 0.80)
        account = FakeAccount(total_value=1_000_000, cash=500_000, positions=[])

        budget = controller.calculate(regime=regime, account=account, total_pnl_pct=-0.15)

        # 0.80 * 0.5 = 0.40
        assert budget.total_limit == pytest.approx(0.40, abs=1e-4), \
            f"亏损减半预期 0.40, 实际 {budget.total_limit}"
        assert "风险系数" in budget.reason or "减半" in budget.reason

    def test_buy_not_allowed_zero_budget(self) -> None:
        """禁止买入时预算应全为 0。"""
        controller = PositionController()
        regime = self._make_regime("crash", 0.0, buy_allowed=False)
        account = FakeAccount(total_value=1_000_000, cash=500_000, positions=[])

        budget = controller.calculate(regime=regime, account=account)

        assert budget.total_limit == 0.0
        assert budget.single_limit == 0.0
        assert budget.available_budget == 0.0
        assert budget.max_positions == 0

    def test_full_positions_no_new_budget(self) -> None:
        """持仓已满时应不允许新开仓。"""
        controller = PositionController()
        regime = self._make_regime("bull", 0.80)
        # 已有 5 只持仓（等于 max_total_positions）
        account = FakeAccount(total_value=1_000_000, cash=100_000,
                              positions=["600001", "600002", "600003", "600004", "600005"])

        budget = controller.calculate(regime=regime, account=account)

        assert budget.total_limit == 0.0, "持仓已满时总仓位上限应为 0"
        assert budget.single_limit == 0.0, "持仓已满时单股上限应为 0"


# =========================================================================
# PositionManager
# =========================================================================


class TestPositionManager:
    """PositionManager 持仓管理测试。"""

    def test_register_and_get(self) -> None:
        """注册持仓后应能通过 get_position 获取。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=1000, stop_loss=9.0)

        pos = pm.get_position("000001")
        assert pos is not None
        assert pos["entry_price"] == 10.0
        assert pos["quantity"] == 1000
        assert pos["total_quantity"] == 1000

    def test_get_advice_open(self) -> None:
        """未持仓时 update 应返回 hold 且 reason 包含'无持仓'。"""
        pm = PositionManager()
        advice = pm.update("000001", current_price=10.5)

        assert advice.action == "hold"
        assert advice.entry_price == 0
        assert advice.profit_pct == 0
        assert "无持仓" in advice.reason

    def test_get_advice_add(self) -> None:
        """持仓盈利 > 3% 且未加仓过，应返回 add 建议。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=1000, stop_loss=9.0)
        # 盈利 5% 触发加仓
        advice = pm.update("000001", current_price=10.50)

        assert advice.action == "add", f"预期 add, 实际 {advice.action}"
        assert advice.suggested_quantity >= 100, "加仓数量应 >= 100 股"
        assert "加仓" in advice.reason

    def test_get_advice_no_cash(self) -> None:
        """零现金不影响 PositionManager（它不管理现金），只应正常返回持仓建议。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=1000, stop_loss=9.0)
        advice = pm.update("000001", current_price=10.10)

        assert advice.action in ("add", "hold"), f"预期 add 或 hold, 实际 {advice.action}"
        assert advice.symbol == "000001"

    def test_empty_portfolio(self) -> None:
        """无任何持仓时 get_all_positions 应返回空 dict。"""
        pm = PositionManager()
        assert pm.get_all_positions() == {}

    def test_stop_loss_triggered(self) -> None:
        """价格触及止损时应返回 close 建议。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=1000, stop_loss=9.0)

        advice = pm.update("000001", current_price=8.90)

        assert advice.action == "close", f"预期 close, 实际 {advice.action}"
        assert advice.suggested_quantity == 1000, "止损应建议平掉全部仓"
        assert "止损" in advice.reason

    def test_take_profit_first_stage(self) -> None:
        """盈利达到 10% 时应触发第一阶段止盈。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=2000, stop_loss=9.0)

        # 盈利 10%+
        advice = pm.update("000001", current_price=11.10)

        assert advice.action == "reduce", f"预期 reduce, 实际 {advice.action}"
        # 止盈 30%，2000 * 0.3 = 600，取整到 100 -> 600
        assert advice.suggested_quantity == 600, \
            f"止盈数量预期 600, 实际 {advice.suggested_quantity}"
        assert "止盈" in advice.reason

    def test_trailing_stop_activated(self) -> None:
        """盈利超过 5% 后应激活移动止损。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=1000, stop_loss=9.0)

        # 涨到 10.80（盈利 8%，超过 5% 激活阈值）— 触发移动止损上移
        # 同时会触发加仓建议（profit > 3% and add_count == 0）
        advice1 = pm.update("000001", current_price=10.80)

        # 移动止损应激活：10.80 * 0.97 = 10.476
        pos = pm.get_position("000001")
        assert pos["stop_loss"] == pytest.approx(10.476, abs=0.01), \
            f"移动止损应上移至 10.476, 实际 {pos['stop_loss']}"

        # 记录加仓，使 add_count > 0，避免下一轮返回 add
        pm.record_add("000001", add_qty=500, add_price=10.80)

        # 价格回落到 10.55（仍高于移动止损 10.476），应保持 hold
        advice2 = pm.update("000001", current_price=10.55)
        assert advice2.action == "hold", f"预期 hold, 实际 {advice2.action}"
        assert "盈利" in advice2.reason

    def test_remove_position(self) -> None:
        """移除持仓后应不再返回持仓信息。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=1000, stop_loss=9.0)
        pm.remove_position("000001")

        assert pm.get_position("000001") is None
        advice = pm.update("000001", current_price=10.0)
        assert "无持仓" in advice.reason

    def test_record_add_updates_average_price(self) -> None:
        """加仓后应重新计算持仓均价。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=1000, stop_loss=9.0)
        pm.record_add("000001", add_qty=500, add_price=12.0)

        pos = pm.get_position("000001")
        # 均价 = (10*1000 + 12*500) / 1500 = 16000 / 1500 ≈ 10.6667
        expected_avg = (10.0 * 1000 + 12.0 * 500) / 1500
        assert pos["entry_price"] == pytest.approx(expected_avg, abs=1e-4)
        assert pos["total_quantity"] == 1500
        assert pos["add_count"] == 1

    def test_record_reduce_removes_position_when_zero(self) -> None:
        """减仓至零时应自动移除持仓。"""
        pm = PositionManager()
        pm.register_position("000001", entry_price=10.0, quantity=1000, stop_loss=9.0)
        pm.record_reduce("000001", reduce_qty=1000)

        assert pm.get_position("000001") is None


# =========================================================================
# PerformanceAnalyzer
# =========================================================================


class TestPerformanceAnalyzer:
    """PerformanceAnalyzer 绩效归因测试。"""

    @pytest.mark.asyncio
    async def test_basic_stats(self) -> None:
        """简单的盈利交易应产生合理的绩效指标。"""
        trades = [
            {"pnl": 5000, "hold_days": 10, "symbol": "000001", "side": "buy", "amount": 100000},
            {"pnl": 3000, "hold_days": 8, "symbol": "000002", "side": "buy", "amount": 80000},
            {"pnl": -1000, "hold_days": 5, "symbol": "000003", "side": "buy", "amount": 50000},
        ]
        # 3 笔交易，2 赢 1 亏，总盈亏 = 5000+3000-1000 = 7000
        equity = [1_000_000, 1_020_000, 1_030_000, 1_000_000, 1_050_000]
        analyzer = _make_performance_analyzer_with_data()
        report = await analyzer.analyze(trades=trades, daily_equity=equity)

        assert report.total_trades == 3
        assert report.win_rate == pytest.approx(2 / 3, abs=0.001), \
            f"胜率应为 2/3, 实际 {report.win_rate}"
        assert report.total_return > 0, "总收益应为正"
        assert report.max_drawdown >= 0, "最大回撤应 >= 0"

    @pytest.mark.asyncio
    async def test_empty_trades(self) -> None:
        """无交易时应返回全零的绩效报告。"""
        equity = [1_000_000]
        analyzer = _make_performance_analyzer_with_data()
        report = await analyzer.analyze(trades=[], daily_equity=equity)

        assert report.total_trades == 0
        assert report.win_rate == 0
        assert report.profit_factor == 0
        assert report.total_return == 0

    @pytest.mark.asyncio
    async def test_empty_equity(self) -> None:
        """空权益序列应返回零值报告。"""
        analyzer = _make_performance_analyzer_with_data()
        report = await analyzer.analyze(trades=[], daily_equity=[])

        assert report.total_trades == 0
        assert report.total_return == 0
        assert report.sharpe == 0
        assert report.max_drawdown == 0

    @pytest.mark.asyncio
    async def test_profit_factor(self) -> None:
        """全部盈利时 profit_factor 应为 inf。"""
        trades = [
            {"pnl": 1000, "hold_days": 5, "symbol": "000001", "side": "buy", "amount": 50000},
            {"pnl": 2000, "hold_days": 3, "symbol": "000002", "side": "buy", "amount": 60000},
        ]
        equity = [1_000_000, 1_010_000, 1_020_000, 1_030_000]
        analyzer = _make_performance_analyzer_with_data()
        report = await analyzer.analyze(trades=trades, daily_equity=equity)

        assert report.profit_factor == float("inf"), "无亏损交易时 profit_factor 应为 inf"
        assert report.win_rate == 1.0

    @pytest.mark.asyncio
    async def test_negative_return(self) -> None:
        """全部亏损时应产生负的收益指标。"""
        trades = [
            {"pnl": -1000, "hold_days": 5, "symbol": "000001", "side": "buy", "amount": 50000},
            {"pnl": -2000, "hold_days": 3, "symbol": "000002", "side": "buy", "amount": 60000},
        ]
        # 权益持续下跌
        equity = [1_000_000, 990_000, 980_000, 970_000]
        analyzer = _make_performance_analyzer_with_data()
        report = await analyzer.analyze(trades=trades, daily_equity=equity)

        assert report.total_return < 0
        assert report.win_rate == 0
        # profit_factor = 0 / |-3000| = 0
        assert report.profit_factor == 0


# =========================================================================
# StrategyPool
# =========================================================================


class TestStrategyPool:
    """StrategyPool 策略池管理测试。"""

    @pytest.fixture(autouse=True)
    def mock_settings(self):
        """Mock settings() 让 StrategyPool 初始化时有已知策略列表。"""
        with patch("gugu.analysis.strategy_pool.settings") as mock:
            mock.return_value = {
                "strategy": {
                    "enabled": ["mean_revert", "breakout", "trend"],
                }
            }
            yield mock

    def test_add_strategy(self) -> None:
        """初始化的策略应存在于池中。"""
        pool = StrategyPool()
        weights = pool.get_weights()

        # 3 个策略，默认均分权重
        assert len(weights) == 3
        assert "mean_revert" in weights
        assert "breakout" in weights
        assert "trend" in weights
        for w in weights.values():
            assert w == pytest.approx(1 / 3, abs=1e-4), f"权重应均分 {1/3}"

    def test_best_strategies(self) -> None:
        """较高胜率的策略应获得更高权重。"""
        pool = StrategyPool()

        # 给 mean_revert 注入 6 次交易，胜率 83%（5 赢 1 输，在限制内）
        for i in range(6):
            pool.update_performance("mean_revert", pnl=100 if i < 5 else -50)

        # 给 breakout 注入 6 次交易，胜率 33%（2 赢 4 输，但不连续5次亏损）
        # 模式：赢-输-赢-输-输-输（2 连续亏损在末尾但未达 5）
        for pnl in [100, -50, 100, -50, -50, -50]:
            pool.update_performance("breakout", pnl=pnl)

        weights = pool.get_weights()
        assert "mean_revert" in weights
        assert "breakout" in weights
        assert weights["mean_revert"] > weights["breakout"], \
            "高胜率策略权重应大于低胜率策略"

    def test_empty_pool(self) -> None:
        """空策略池应返回空字典。"""
        with patch("gugu.analysis.strategy_pool.settings") as mock:
            mock.return_value = {"strategy": {"enabled": []}}
            pool = StrategyPool()

        assert pool.get_weights() == {}
        assert pool.get_enabled_names() == []

    def test_weight_limits(self) -> None:
        """归一化后所有权重应在 (0, 1] 范围内。"""
        pool = StrategyPool()

        # 多次更新让权重发生较大变化
        for _ in range(10):
            pool.update_performance("mean_revert", pnl=100)
            pool.update_performance("breakout", pnl=-50)
            pool.update_performance("trend", pnl=20)

        weights = pool.get_weights()
        for name, w in weights.items():
            assert 0 < w <= 1.0, f"策略 {name} 权重 {w} 应在 (0, 1] 范围内"
        assert abs(sum(weights.values()) - 1.0) < 1e-4, "所有权重之和应为 1"

    def test_weighted_fusion_buy(self) -> None:
        """多个买入信号应融合为 buy 方向。"""
        pool = StrategyPool()
        signals = [
            {"strategy_name": "mean_revert", "direction": "buy", "confidence": 0.8},
            {"strategy_name": "breakout", "direction": "buy", "confidence": 0.7},
            {"strategy_name": "trend", "direction": "sell", "confidence": 0.3},
        ]

        result = pool.weighted_fusion(signals)
        assert result["direction"] == "buy", f"预期 buy, 实际 {result['direction']}"
        assert result["confidence"] > 0.3, "融合置信度应 > 0.3"

    def test_weighted_fusion_sell(self) -> None:
        """多个卖出信号应融合为 sell 方向。"""
        pool = StrategyPool()
        signals = [
            {"strategy_name": "mean_revert", "direction": "sell", "confidence": 0.9},
            {"strategy_name": "breakout", "direction": "sell", "confidence": 0.8},
            {"strategy_name": "trend", "direction": "buy", "confidence": 0.2},
        ]

        result = pool.weighted_fusion(signals)
        assert result["direction"] == "sell", f"预期 sell, 实际 {result['direction']}"
        assert result["confidence"] > 0.3

    def test_weighted_fusion_conflict_signal(self) -> None:
        """方向冲突时融合应为 hold。"""
        pool = StrategyPool()
        signals = [
            {"strategy_name": "mean_revert", "direction": "buy", "confidence": 0.5},
            {"strategy_name": "breakout", "direction": "sell", "confidence": 0.5},
        ]

        result = pool.weighted_fusion(signals)
        assert result["direction"] == "hold", f"冲突信号预期 hold, 实际 {result['direction']}"

    def test_disable_on_consecutive_losses(self) -> None:
        """连续亏损 5 次应禁用策略。"""
        pool = StrategyPool()

        for _ in range(5):
            pool.update_performance("mean_revert", pnl=-100)

        stats = pool.get_stats()
        assert stats["mean_revert"]["enabled"] is False, "连续亏损 5 次应禁用"

    def test_recover_after_consecutive_wins(self) -> None:
        """禁用后连续盈利 3 次应恢复策略。"""
        pool = StrategyPool()

        # 先连续亏损 5 次禁用
        for _ in range(5):
            pool.update_performance("mean_revert", pnl=-100)

        # 再连续盈利 3 次恢复
        for _ in range(3):
            pool.update_performance("mean_revert", pnl=100)

        stats = pool.get_stats()
        assert stats["mean_revert"]["enabled"] is True, "连续盈利 3 次后应恢复"
# =========================================================================
# TestExecutionOptimizer
# =========================================================================


class TestExecutionOptimizer:
    """ExecutionOptimizer 执行优化器测试。"""

    def test_create_plan(self) -> None:
        """使用有效的参数创建 ExecutionPlan。"""
        from gugu.analysis.execution_optimizer import ExecutionPlan, OrderSlice

        slices = [
            OrderSlice(slice_id=1, quantity=500, target_price=10.0, time_window="09:30-09:35"),
            OrderSlice(slice_id=2, quantity=500, target_price=10.0, time_window="09:35-09:40"),
        ]
        plan = ExecutionPlan(
            symbol="000001",
            total_quantity=1000,
            slices=slices,
            avg_target_price=10.0,
            estimated_slippage=0.0001,
            estimated_impact=5.0,
            execution_method="twap",
        )

        assert plan.symbol == "000001"
        assert plan.total_quantity == 1000
        assert len(plan.slices) == 2
        assert plan.avg_target_price == 10.0
        assert plan.estimated_slippage == 0.0001
        assert plan.estimated_impact == 5.0
        assert plan.execution_method == "twap"
        for s in plan.slices:
            assert s.slice_id in (1, 2)
            assert s.order_type == "limit"

    def test_optimizer_simple(self) -> None:
        """基础优化能产生执行计划。"""
        from gugu.analysis.execution_optimizer import ExecutionOptimizer

        opt = ExecutionOptimizer()

        # direct 执行
        plan = opt.plan_execution("000001", quantity=1000, price=10.0, method="direct")
        assert plan.symbol == "000001"
        assert plan.total_quantity == 1000
        assert plan.execution_method == "direct"
        assert len(plan.slices) == 1
        assert plan.slices[0].time_window == "immediate"
        assert plan.estimated_slippage == 0.0001  # 小单 1 bp
        assert plan.estimated_impact == 5.0  # 1000 * 10 * 0.0005

        # TWAP 拆单
        plan2 = opt.plan_execution("000001", quantity=10000, price=10.0, method="twap")
        assert plan2.execution_method == "twap"
        assert len(plan2.slices) == 5
        total_sliced = sum(s.quantity for s in plan2.slices)
        assert total_sliced == plan2.total_quantity

        # VWAP 拆单
        plan3 = opt.plan_execution("000001", quantity=50000, price=10.0, method="vwap")
        assert plan3.execution_method == "vwap"
        assert len(plan3.slices) == 5

    def test_empty_portfolio(self) -> None:
        """空执行历史应返回全零质量统计。"""
        from gugu.analysis.execution_optimizer import ExecutionOptimizer

        opt = ExecutionOptimizer()
        quality = opt.get_execution_quality()

        assert quality["avg_slippage_bps"] == 0
        assert quality["fill_rate"] == 0
        assert quality["total_executions"] == 0

    def test_report_format(self) -> None:
        """ExecutionReport 应包含预期的字段。"""
        from gugu.analysis.execution_optimizer import ExecutionReport

        report = ExecutionReport(
            symbol="000001",
            ordered_quantity=1000,
            filled_quantity=950,
            avg_fill_price=10.05,
            slippage_bps=5.0,
            total_cost=10050.0,
            execution_time="09:35:00",
            status="partial",
        )

        assert report.symbol == "000001"
        assert report.ordered_quantity == 1000
        assert report.filled_quantity == 950
        assert report.avg_fill_price == 10.05
        assert report.slippage_bps == 5.0
        assert report.total_cost == 10050.0
        assert report.execution_time == "09:35:00"
        assert report.status == "partial"
        assert isinstance(report.slippage_bps, float)
        assert isinstance(report.total_cost, float)
        assert isinstance(report.ordered_quantity, int)

    def test_auto_method_selection(self) -> None:
        """auto 模式应根据金额自动选择执行方式。"""
        from gugu.analysis.execution_optimizer import ExecutionOptimizer

        opt = ExecutionOptimizer()

        # < 50000 -> direct
        plan_small = opt.plan_execution("000001", quantity=100, price=10.0, method="auto")
        assert plan_small.execution_method == "direct"

        # 50000 ~ 200000 -> twap
        plan_mid = opt.plan_execution("000001", quantity=10000, price=10.0, method="auto")
        assert plan_mid.execution_method == "twap"

        # > 200000 -> vwap
        plan_large = opt.plan_execution("000001", quantity=50000, price=10.0, method="auto")
        assert plan_large.execution_method == "vwap"

    def test_record_and_quality(self) -> None:
        """记录执行报告后，质量统计应正确反映。"""
        from gugu.analysis.execution_optimizer import ExecutionOptimizer, ExecutionReport

        opt = ExecutionOptimizer()

        report1 = ExecutionReport(
            symbol="000001", ordered_quantity=1000, filled_quantity=1000,
            avg_fill_price=10.0, slippage_bps=0.5, total_cost=10000.0,
            execution_time="09:30", status="completed",
        )
        report2 = ExecutionReport(
            symbol="000002", ordered_quantity=2000, filled_quantity=1800,
            avg_fill_price=20.1, slippage_bps=2.0, total_cost=36180.0,
            execution_time="09:45", status="partial",
        )
        opt.record_execution(report1)
        opt.record_execution(report2)

        quality = opt.get_execution_quality()
        assert quality["total_executions"] == 2
        # avg_slippage_bps = (0.5 + 2.0) * 10000 / 2 = 12500
        assert quality["avg_slippage_bps"] == 12500.0
        # fill_rate = (1000/1000 + 1800/2000) / 2 = (1.0 + 0.9) / 2 = 0.95
        assert quality["fill_rate"] == 0.95

    def test_history_cap(self) -> None:
        """执行历史超过 1000 条时应截断。"""
        from gugu.analysis.execution_optimizer import ExecutionOptimizer, ExecutionReport

        opt = ExecutionOptimizer()
        base_report = ExecutionReport(
            symbol="000001", ordered_quantity=100, filled_quantity=100,
            avg_fill_price=10.0, slippage_bps=0.1, total_cost=1000.0,
            execution_time="09:30", status="completed",
        )
        for _ in range(1100):
            opt.record_execution(base_report)

        quality = opt.get_execution_quality()
        assert quality["total_executions"] == 1000, \
            f"历史应截断至 1000 条, 实际 {quality['total_executions']}"


# =========================================================================
# TestParamOptimizer
# =========================================================================


class TestParamOptimizer:
    """ParamOptimizer 策略参数优化器测试。"""

    def test_param_range(self) -> None:
        """ParamRange 应正确验证和存储参数范围。"""
        from gugu.analysis.param_optimizer import ParamRange

        r = ParamRange(name="period", min_val=10, max_val=50, step=1, is_int=True)
        assert r.name == "period"
        assert r.min_val == 10.0
        assert r.max_val == 50.0
        assert r.step == 1.0
        assert r.is_int is True

        r2 = ParamRange(name="threshold", min_val=0.0, max_val=1.0, step=0.01, is_int=False)
        assert r2.name == "threshold"
        assert r2.is_int is False
        assert r2.step == 0.01

    def test_optimizer_initialization(self) -> None:
        """ParamOptimizer 应正确初始化参数范围。"""
        from gugu.analysis.param_optimizer import ParamOptimizer, ParamRange

        fitness = lambda params: {"sharpe": 0.5, "total_return": 0.1, "max_drawdown": 0.05}
        optimizer = ParamOptimizer(
            param_ranges=[
                ParamRange("period", 10, 50, 1),
                ParamRange("threshold", 0.0, 1.0, 0.1, is_int=False),
            ],
            fitness_func=fitness,
            population_size=20,
            generations=5,
            mutation_rate=0.1,
            crossover_rate=0.7,
            elite_count=3,
        )

        # Verify internal state via encoding/decoding
        ind = optimizer._random_individual()
        assert len(ind) == 2
        params = optimizer._decode(ind)
        assert "period" in params
        assert "threshold" in params
        assert 10 <= params["period"] <= 50
        assert 0.0 <= params["threshold"] <= 1.0

        # Encode/decode roundtrip
        params2 = {"period": 25.0, "threshold": 0.5}
        ind2 = optimizer._encode(params2)
        params3 = optimizer._decode(ind2)
        assert params3["period"] == 25
        assert params3["threshold"] == 0.5

    def test_optimize_empty(self) -> None:
        """空的参数范围应返回空的 best_params。"""
        import random
        random.seed(42)

        from gugu.analysis.param_optimizer import ParamOptimizer

        optimizer = ParamOptimizer(
            param_ranges=[],
            fitness_func=lambda params: {"sharpe": 0.5, "total_return": 0.1, "max_drawdown": 0.05},
            population_size=5,
            generations=3,
        )
        result = optimizer.optimize(verbose=False)

        assert isinstance(result.best_params, dict)
        assert result.best_params == {}
        assert len(result.history) == 3
        for entry in result.history:
            assert "generation" in entry
            assert "best_score" in entry
            assert "best_params" in entry

    def test_report_format(self) -> None:
        """OptimizationResult 应包含预期的字段。"""
        from gugu.analysis.param_optimizer import OptimizationResult

        result = OptimizationResult(
            best_params={"period": 30, "threshold": 0.6},
            best_score=0.85,
            best_sharpe=1.2,
            best_return=0.25,
            best_max_dd=0.08,
            generation=10,
            history=[
                {"generation": 1, "best_score": 0.5, "best_params": {"period": 20}},
                {"generation": 10, "best_score": 0.85, "best_params": {"period": 30}},
            ],
        )

        assert result.best_params["period"] == 30
        assert result.best_score == 0.85
        assert result.best_sharpe == 1.2
        assert result.best_return == 0.25
        assert result.best_max_dd == 0.08
        assert result.generation == 10
        assert len(result.history) == 2
        for entry in result.history:
            assert "generation" in entry
            assert "best_score" in entry

    def test_optimize_converges(self) -> None:
        """遗传算法应能收敛到较优参数。"""
        import random
        random.seed(42)

        from gugu.analysis.param_optimizer import ParamOptimizer, ParamRange

        # 适应度函数：更高的 period 得到更高的 sharpe
        def fitness(params):
            period = int(params.get("period", 10))
            return {
                "sharpe": period / 100.0,
                "total_return": 0.05,
                "max_drawdown": 0.03,
            }

        optimizer = ParamOptimizer(
            param_ranges=[ParamRange("period", 10, 50, 1)],
            fitness_func=fitness,
            population_size=10,
            generations=5,
            mutation_rate=0.2,
            crossover_rate=0.7,
            elite_count=2,
        )
        result = optimizer.optimize(verbose=False)

        # 应能找到 period >= 25 的参数
        assert result.best_params.get("period", 0) >= 25, \
            f"应收敛到大 period, 实际 {result.best_params}"
        assert result.best_score > 0.0
        assert len(result.history) == 5

    def test_decode_clamps_values(self) -> None:
        """解码应确保参数在范围内。"""
        from gugu.analysis.param_optimizer import ParamOptimizer, ParamRange

        optimizer = ParamOptimizer(
            param_ranges=[ParamRange("period", 10, 50, 1)],
            fitness_func=lambda p: {"sharpe": 0.5, "total_return": 0.1, "max_drawdown": 0.05},
            population_size=5,
            generations=1,
        )

        # 超出范围的编码应被裁剪
        params = optimizer._decode([-100.0])
        assert params["period"] == 10, f"负值应 clamp 到 10, 实际 {params['period']}"

        params2 = optimizer._decode([100.0])
        assert params2["period"] == 50, f"过大值应 clamp 到 50, 实际 {params2['period']}"


# =========================================================================
# TestSectorRotation
# =========================================================================


class TestSectorRotation:
    """SectorRotation 板块轮动检测测试。"""

    def test_get_sector(self) -> None:
        """已知股票代码应返回对应的行业类别。"""
        from gugu.analysis.sector_rotation import SW_INDUSTRY_MAP

        # 直接用 SW_INDUSTRY_MAP 验证映射关系
        assert SW_INDUSTRY_MAP["银行"] == "金融"
        assert SW_INDUSTRY_MAP["计算机"] == "科技"
        assert SW_INDUSTRY_MAP["食品饮料"] == "消费"
        assert SW_INDUSTRY_MAP["钢铁"] == "周期"
        assert SW_INDUSTRY_MAP["公用事业"] == "公用"
        assert SW_INDUSTRY_MAP["电力设备"] == "制造"

    def test_top_sectors(self) -> None:
        """top_sectors() 应按综合评分排序返回。"""
        import pandas as pd
        from gugu.analysis.sector_rotation import SectorRotation

        sr = SectorRotation()

        # 构造模拟的板块行情数据
        df = pd.DataFrame({
            "板块名称": ["银行", "计算机", "食品饮料", "钢铁"],
            "板块涨跌幅": [2.0, 5.0, -1.0, 3.0],
            "主力净流入": [5e8, 1e9, -2e8, 3e8],
        })

        scores = sr._score_sectors(df)

        # 计算机涨跌幅最大(5%)且资金流入最大(10亿) -> 总分最高
        assert scores["计算机"]["total"] > scores["银行"]["total"]
        assert scores["计算机"]["total"] > scores["食品饮料"]["total"]
        assert scores["钢铁"]["total"] > scores["食品饮料"]["total"]

        # 按 total 降序排序
        sorted_sectors = sorted(scores.items(), key=lambda x: -x[1]["total"])
        assert sorted_sectors[0][0] == "计算机"
        assert sorted_sectors[-1][0] == "食品饮料"

    def test_empty_data(self) -> None:
        """空的 DataFrame 应返回空字典。"""
        import pandas as pd
        from gugu.analysis.sector_rotation import SectorRotation

        sr = SectorRotation()
        result = sr._score_sectors(pd.DataFrame())

        assert result == {}

    def test_get_industry(self) -> None:
        """已知板块名称应能查到对应的大类。"""
        from gugu.analysis.sector_rotation import SW_INDUSTRY_MAP

        # 测试所有分类是否正确
        assert SW_INDUSTRY_MAP["电子"] == "科技"
        assert SW_INDUSTRY_MAP["通信"] == "科技"
        assert SW_INDUSTRY_MAP["国防军工"] == "科技"
        assert SW_INDUSTRY_MAP["非银金融"] == "金融"
        assert SW_INDUSTRY_MAP["房地产"] == "金融"
        assert SW_INDUSTRY_MAP["家用电器"] == "消费"
        assert SW_INDUSTRY_MAP["医药生物"] == "消费"
        assert SW_INDUSTRY_MAP["基础化工"] == "周期"
        assert SW_INDUSTRY_MAP["有色金属"] == "周期"
        assert SW_INDUSTRY_MAP["机械设备"] == "制造"
        assert SW_INDUSTRY_MAP["交通运输"] == "公用"
        assert SW_INDUSTRY_MAP["环保"] == "公用"

    def test_score_sectors_edge_cases(self) -> None:
        """极端涨跌幅应被正确归一化。"""
        import pandas as pd
        from gugu.analysis.sector_rotation import SectorRotation

        sr = SectorRotation()

        # 超大涨幅、超大资金流入
        df = pd.DataFrame({
            "板块名称": ["科技板块"],
            "板块涨跌幅": [15.0],   # 远超 5% 阈值
            "主力净流入": [5e10],   # 远超 10 亿阈值
        })
        scores = sr._score_sectors(df)
        s = scores["科技板块"]
        # pct_score 应被 cap 到 1.0
        assert s["pct_score"] == 1.0
        # flow_score 应被 cap 到 1.0
        assert s["flow_score"] == 1.0
        # total = 1.0 * 0.6 + 1.0 * 0.4 = 1.0
        assert s["total"] == 1.0

        # 超大跌幅、超大资金流出
        df2 = pd.DataFrame({
            "板块名称": ["衰退板块"],
            "板块涨跌幅": [-20.0],
            "主力净流入": [-5e10],
        })
        scores2 = sr._score_sectors(df2)
        s2 = scores2["衰退板块"]
        assert s2["pct_score"] == -1.0
        assert s2["flow_score"] == -1.0
        assert s2["total"] == -1.0

    def test_fallback_on_akshare_failure(self) -> None:
        """akshare 异常应返回 fallback 结果。"""
        from unittest.mock import patch
        from gugu.analysis.sector_rotation import SectorRotation

        sr = SectorRotation()

        with patch("gugu.analysis.sector_rotation.ak.stock_board_industry_name_em") as mock_ak:
            mock_ak.side_effect = Exception("网络连接失败")

            import asyncio
            result = asyncio.run(sr.detect())

            assert result["hot_sectors"] == []
            assert result["sector_scores"] == {}
            assert result["recommended_sectors"] == []
            assert "板块数据获取失败" in result["reason"]

    def test_fallback_on_empty_akshare_data(self) -> None:
        """akshare 返回空 DataFrame 应返回 fallback 结果。"""
        from unittest.mock import patch
        import pandas as pd
        from gugu.analysis.sector_rotation import SectorRotation

        sr = SectorRotation()

        with patch("gugu.analysis.sector_rotation.ak.stock_board_industry_name_em", return_value=pd.DataFrame()):
            import asyncio
            result = asyncio.run(sr.detect())

            assert result["hot_sectors"] == []
            assert result["sector_scores"] == {}
            assert result["recommended_sectors"] == []
            assert "板块数据获取失败" in result["reason"]

    def test_filter_stocks_by_sector_mocked(self) -> None:
        """使用 mock 的 IndustryConstraint 筛选股票。"""
        from unittest.mock import MagicMock, patch
        from gugu.analysis.sector_rotation import SectorRotation

        sr = SectorRotation()

        # Mock IndustryConstraint.get_industry — the class is imported locally
        # inside filter_stocks_by_sector, so we patch at source
        with patch("gugu.filters.industry_constraint.IndustryConstraint") as MockIndustry:
            mock_instance = MagicMock()
            def mock_get_industry(symbol):
                mapping = {"000001": "银行", "000002": "电子", "000003": "未知行业"}
                return mapping.get(symbol, "")
            mock_instance.get_industry = mock_get_industry
            MockIndustry.return_value = mock_instance

            import asyncio
            result = asyncio.run(sr.filter_stocks_by_sector(
                symbols=["000001", "000002", "000003"],
                hot_sectors=["银行", "计算机"],
            ))

            # 000001 -> 银行 (属于 hot_sectors) -> 保留
            assert "000001" in result
            # 000002 -> 电子 -> cat = "科技", hot_sectors "银行","计算机" -> "金融","科技" -> 保留
            assert "000002" in result
            # 000003 -> "未知行业" -> cat = "其他", 不匹配 hot_sectors -> 排除
            assert "000003" not in result

    def test_detect_uses_cache(self) -> None:
        """同一天重复 detect 应使用缓存避免重复网络请求。"""
        from unittest.mock import patch
        from gugu.analysis.sector_rotation import SectorRotation

        sr = SectorRotation()
        # 先设置缓存
        cached_result = {
            "hot_sectors": ["测试板块"],
            "sector_scores": {},
            "recommended_sectors": ["测试板块"],
            "categories": ["测试"],
            "reason": "缓存测试",
        }
        from datetime import date
        sr._cache = cached_result
        sr._cache_date = str(date.today())

        with patch("gugu.analysis.sector_rotation.ak.stock_board_industry_name_em") as mock_ak:
            import asyncio
            result = asyncio.run(sr.detect())
            # 缓存生效，不应调用 akshare
            mock_ak.assert_not_called()
            assert result["hot_sectors"] == ["测试板块"]
            assert result["reason"] == "缓存测试"
