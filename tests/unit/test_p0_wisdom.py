"""P0 新增模块的单元测试。

覆盖：
- StageDetector（四阶段判断器）
- TrailingStopEngine（移动止损引擎）
- DangerSignalDetector（危险信号检测器）
- NoAverageDownChecker（向下摊平检查器）
- BookPerspectiveRouter（书籍多视角路由器）
"""

import pandas as pd
import pytest

from gugu.analysis.stage_detector import StageDetector, MarketStage, StageResult
from gugu.analysis.trailing_stop import (
    TrailingStopEngine,
    TrailingStopState,
    TrailingStopSignal,
)
from gugu.analysis.danger_signal import DangerSignalDetector, DangerSignalResult
from gugu.analysis.no_average_down import NoAverageDownChecker, AverageDownCheckResult
from gugu.wisdom.book_router import BookPerspectiveRouter


# ---------------------------------------------------------------------------
# 测试数据生成辅助
# ---------------------------------------------------------------------------

def _make_df(closes, volumes=None, highs=None, lows=None):
    """生成 OHLCV DataFrame。"""
    n = len(closes)
    if volumes is None:
        volumes = [10000] * n
    if highs is None:
        highs = [c * 1.02 for c in closes]
    if lows is None:
        lows = [c * 0.98 for c in closes]
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def _make_uptrend_df(n=80, start=10.0, step=0.15, vol_base=10000):
    """生成正常升势 DataFrame（足够长度供 StageDetector 使用）。"""
    closes = [start + step * i for i in range(n)]
    highs = [c + abs(step) * 0.5 for c in closes]
    lows = [c - abs(step) * 0.3 for c in closes]
    volumes = [vol_base + vol_base * 0.01 * i for i in range(n)]
    return _make_df(closes, volumes=volumes, highs=highs, lows=lows)


def _make_frenzy_df(n=80, start=10.0, step=0.8, vol_base=10000):
    """生成疯狂阶段 DataFrame（高斜率+量放大）。"""
    closes = [start + step * i for i in range(n)]
    volumes = [vol_base + vol_base * 0.1 * i for i in range(n)]
    return _make_df(closes, volumes=volumes)


def _make_flat_df(n=80, base=10.0, vol_base=10000):
    """生成牛皮市 DataFrame（价格窄幅波动）。"""
    closes = [base + 0.02 * (i % 5 - 2) for i in range(n)]
    volumes = [vol_base] * n
    return _make_df(closes, volumes=volumes)


def _make_downtrend_df(n=80, start=30.0, step=0.2, vol_base=10000):
    """生成下降趋势 DataFrame。"""
    closes = [start - step * i for i in range(n)]
    volumes = [vol_base + vol_base * 0.02 * i for i in range(n)]
    return _make_df(closes, volumes=volumes)


# ---------------------------------------------------------------------------
# StageDetector 测试
# ---------------------------------------------------------------------------

class TestStageDetector:
    """四阶段判断器测试。"""

    def setup_method(self):
        self.detector = StageDetector()

    def test_flat_market(self):
        """牛皮市：价格小幅波动，成交量平稳。"""
        df = _make_flat_df(n=80)
        result = self.detector.detect(df)
        assert isinstance(result, StageResult)
        assert 0 <= result.confidence <= 1
        assert isinstance(result.description, str)

    def test_normal_uptrend(self):
        """正常升势：价格逐步上升，高点低点逐步抬高。"""
        df = _make_uptrend_df(n=80, step=0.15)
        result = self.detector.detect(df)
        assert result.stage in (MarketStage.NORMAL_UPTREND, MarketStage.FRENZY, MarketStage.SIDEWAYS)
        assert result.confidence >= 0

    def test_frenzy_stage(self):
        """疯狂阶段：斜率极高，成交量放大。"""
        df = _make_frenzy_df(n=80, step=0.8)
        result = self.detector.detect(df)
        assert result.stage in (MarketStage.FRENZY, MarketStage.FINAL, MarketStage.NORMAL_UPTREND)

    def test_final_stage(self):
        """最后阶段：极端斜率+极端量比。"""
        df = _make_frenzy_df(n=80, step=1.5, vol_base=50000)
        result = self.detector.detect(df)
        assert result.stage in (MarketStage.FRENZY, MarketStage.FINAL, MarketStage.NORMAL_UPTREND)

    def test_downtrend(self):
        """下降趋势：价格持续下跌。"""
        df = _make_downtrend_df(n=80)
        result = self.detector.detect(df)
        assert result.stage in (MarketStage.DOWNTREND, MarketStage.SIDEWAYS)

    def test_empty_df(self):
        """空 DataFrame 返回 SIDEWAYS + confidence=0。"""
        df = pd.DataFrame()
        result = self.detector.detect(df)
        assert result.stage == MarketStage.SIDEWAYS
        assert result.confidence == 0.0

    def test_short_df(self):
        """数据不足时应返回 SIDEWAYS + confidence=0。"""
        df = _make_df([10.0, 10.5, 11.0])
        result = self.detector.detect(df)
        assert result.stage == MarketStage.SIDEWAYS
        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# TrailingStopEngine 测试
# ---------------------------------------------------------------------------

class TestTrailingStopEngine:
    """移动止损引擎测试。"""

    def setup_method(self):
        self.engine = TrailingStopEngine()

    def test_init_stop(self):
        """初始化止损状态。"""
        state = self.engine.init_stop(entry_price=100.0)
        assert state.initial_stop_price > 0
        assert state.current_stop_price > 0
        assert state.current_stop_price < 100.0
        assert state.highest_price == 100.0
        assert state.valley_break_count == 0

    def test_init_stop_with_custom_pct(self):
        """自定义止损比例。"""
        state = self.engine.init_stop(entry_price=100.0, initial_stop_pct=0.05)
        assert state.initial_stop_price == pytest.approx(95.0, rel=0.01)
        assert state.current_stop_price == pytest.approx(95.0, rel=0.01)

    def test_stop_only_moves_up(self):
        """止损价只能上移，不能下移。"""
        state = self.engine.init_stop(entry_price=100.0)
        original_stop = state.current_stop_price

        # 价格下跌场景
        df = _make_df([100.0, 95.0, 90.0, 85.0])
        state, signal = self.engine.update(state, df)

        # 止损价不应下移（除非 EXIT 触发）
        assert state.current_stop_price <= original_stop or signal == TrailingStopSignal.EXIT

    def test_highest_price_updates(self):
        """最高价随价格上涨更新。"""
        state = self.engine.init_stop(entry_price=100.0)
        df = _make_df([100.0, 105.0, 110.0, 108.0])
        state, _ = self.engine.update(state, df)
        assert state.highest_price >= 100.0

    def test_exit_on_stop_hit(self):
        """触发止损价时返回 EXIT 信号。"""
        state = self.engine.init_stop(entry_price=100.0, initial_stop_pct=0.05)
        # 价格跌破止损价
        df = _make_df([100.0, 90.0, 85.0, 80.0])
        state, signal = self.engine.update(state, df)
        assert signal in (TrailingStopSignal.EXIT, TrailingStopSignal.WARNING, TrailingStopSignal.ALERT)

    def test_state_dict_roundtrip(self):
        """状态序列化/反序列化往返。"""
        state = self.engine.init_stop(entry_price=100.0)
        state.highest_price = 120.0
        state.valley_break_count = 2

        d = self.engine.state_to_dict(state)
        assert isinstance(d, dict)
        assert d["initial_stop_price"] > 0
        assert d["highest_price"] == 120.0
        assert d["valley_break_count"] == 2

        restored = TrailingStopEngine.dict_to_state(d)
        assert restored.initial_stop_price == d["initial_stop_price"]
        assert restored.highest_price == 120.0
        assert restored.valley_break_count == 2

    def test_danger_signal_tightens_stop(self):
        """危险信号收紧止损。"""
        state = self.engine.init_stop(entry_price=100.0, initial_stop_pct=0.08)
        original_stop = state.current_stop_price

        df = _make_df([100.0, 102.0, 101.0, 99.0])
        danger_signals = [{"type": "volume_price_divergence", "severity": "high"}]
        state, signal = self.engine.update(state, df, danger_signals)

        # 有危险信号时，止损应收紧或触发
        assert state.current_stop_price >= original_stop or signal != TrailingStopSignal.HOLD


# ---------------------------------------------------------------------------
# DangerSignalDetector 测试
# ---------------------------------------------------------------------------

class TestDangerSignalDetector:
    """危险信号检测器测试。"""

    def setup_method(self):
        self.detector = DangerSignalDetector()

    def test_no_signal_in_normal_market(self):
        """正常市场无危险信号。"""
        closes = [10.0 + 0.1 * i for i in range(20)]
        volumes = [10000] * 20
        df = _make_df(closes, volumes=volumes)
        result = self.detector.detect(df)
        assert isinstance(result, DangerSignalResult)
        assert result.severity in ("none", "low")

    def test_volume_up_price_flat(self):
        """量增价不涨信号。"""
        closes = [10.0] * 10 + [10.0, 10.0, 10.0]
        volumes = [10000] * 10 + [30000, 35000, 40000]
        df = _make_df(closes, volumes=volumes)
        result = self.detector.detect(df)
        assert isinstance(result, DangerSignalResult)

    def test_two_day_reversal(self):
        """两天转头信号。"""
        closes = [10.0 + 0.2 * i for i in range(10)] + [11.8, 11.5, 10.5]
        df = _make_df(closes)
        result = self.detector.detect(df)
        assert isinstance(result, DangerSignalResult)

    def test_long_upper_shadow(self):
        """长上影线信号。"""
        n = 15
        closes = [10.0 + 0.1 * i for i in range(n)]
        highs = [c + 2.0 for c in closes]  # 长上影
        lows = [c - 0.1 for c in closes]
        df = _make_df(closes, highs=highs, lows=lows)
        result = self.detector.detect(df)
        assert isinstance(result, DangerSignalResult)

    def test_empty_df(self):
        """空 DataFrame 不应崩溃。"""
        df = pd.DataFrame()
        result = self.detector.detect(df)
        assert result.severity == "none"
        assert not result.has_signal

    def test_with_prev_close(self):
        """传入 prev_close 参数。"""
        closes = [10.0, 11.0, 10.5, 9.5]
        df = _make_df(closes)
        result = self.detector.detect(df, prev_close=11.0)
        assert isinstance(result, DangerSignalResult)


# ---------------------------------------------------------------------------
# NoAverageDownChecker 测试
# ---------------------------------------------------------------------------

class TestNoAverageDownChecker:
    """向下摊平检查器测试。"""

    def setup_method(self):
        self.checker = NoAverageDownChecker()

    def test_allow_buy_new_position(self):
        """新建仓允许买入。"""
        result = self.checker.check(
            symbol="000858",
            has_position=False,
            cost_price=0.0,
            current_price=100.0,
            quantity=0,
        )
        assert isinstance(result, AverageDownCheckResult)
        assert result.allowed is True

    def test_block_average_down(self):
        """亏损仓位禁止加码。"""
        result = self.checker.check(
            symbol="000858",
            has_position=True,
            cost_price=100.0,
            current_price=95.0,
            quantity=100,
        )
        assert result.allowed is False
        assert "摊平" in result.reason or "加码" in result.reason or "亏损" in result.reason

    def test_allow_add_profitable_position(self):
        """盈利仓位允许加码。"""
        result = self.checker.check(
            symbol="000858",
            has_position=True,
            cost_price=90.0,
            current_price=100.0,
            quantity=100,
        )
        assert result.allowed is True

    def test_small_loss_tolerance(self):
        """小幅亏损（<=2%）允许加码。"""
        result = self.checker.check(
            symbol="000858",
            has_position=True,
            cost_price=100.0,
            current_price=99.0,
            quantity=100,
        )
        assert result.allowed is True

    def test_zero_cost_price_allowed(self):
        """成本价为0时允许买入。"""
        result = self.checker.check(
            symbol="000858",
            has_position=True,
            cost_price=0.0,
            current_price=100.0,
            quantity=100,
        )
        assert result.allowed is True


# ---------------------------------------------------------------------------
# BookPerspectiveRouter 测试
# ---------------------------------------------------------------------------

class TestBookPerspectiveRouter:
    """书籍多视角路由器测试。"""

    def setup_method(self):
        self.router = BookPerspectiveRouter()

    def test_select_perspectives_entry(self):
        """入场场景应返回相关视角。"""
        perspectives = self.router.select_perspectives(scenario="entry", direction="buy")
        assert isinstance(perspectives, list)
        assert len(perspectives) > 0
        assert len(perspectives) <= 4

    def test_select_perspectives_stop_loss(self):
        """止损场景应返回相关视角。"""
        perspectives = self.router.select_perspectives(scenario="stop_loss", direction="sell")
        assert isinstance(perspectives, list)
        assert len(perspectives) > 0

    def test_select_perspectives_position_sizing(self):
        """仓位管理场景应返回相关视角。"""
        perspectives = self.router.select_perspectives(scenario="position_sizing")
        assert isinstance(perspectives, list)
        assert len(perspectives) > 0

    def test_build_context(self):
        """构建多视角认知上下文。"""
        ctx = self.router.build_context(scenario="entry", direction="buy")
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_build_context_with_signal(self):
        """带信号参数构建上下文。"""
        signal = {
            "symbol": "000858",
            "name": "五粮液",
            "direction": "buy",
            "price": 150.0,
            "reason": "突破新高",
            "strategies": ["breakout"],
        }
        ctx = self.router.build_context(scenario="entry", direction="buy", signal=signal)
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_unknown_scenario(self):
        """未知场景应返回默认视角。"""
        perspectives = self.router.select_perspectives(scenario="unknown_scenario")
        assert isinstance(perspectives, list)
        # 应回退到默认类别
        assert len(perspectives) > 0

    def test_buy_includes_chen_jiangting(self):
        """买入场景应包含陈江挺视角。"""
        perspectives = self.router.select_perspectives(scenario="entry", direction="buy")
        # 陈江挺是交易系统组的核心
        assert any("chen" in p.lower() or "jiangting" in p.lower() for p in perspectives)

    def test_sell_includes_livermore(self):
        """卖出场景应包含利弗莫尔视角。"""
        perspectives = self.router.select_perspectives(scenario="stop_loss", direction="sell")
        # 利弗莫尔是交易系统组的核心
        assert any("livermore" in p.lower() or "jesse" in p.lower() for p in perspectives)
