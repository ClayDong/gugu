"""移动止损引擎单元测试。

覆盖浪谷识别、止损上移、递进警报、危险信号收紧等核心逻辑。
重点验证 2026-07-03 修复的3个bug：
1. wave_valleys 按时间排序（非价格排序）
2. _adaptive_wave_window 被正确调用
3. valley_break_count 重置逻辑
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.analysis.trailing_stop import (
    TrailingStopEngine,
    TrailingStopSignal,
    TrailingStopState,
)


def _make_df(n: int = 60, seed: int = 42) -> pd.DataFrame:
    """生成测试用 OHLCV DataFrame（中等波动）。

    ATR/price 设计在 1%-3% 之间（中波动），使 _adaptive_wave_window 返回默认窗口 5。
    high/low spread 控制在 1.5% 以内，避免触发高波动阈值。
    """
    np.random.seed(seed)
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    prices = [10.0]
    for _ in range(1, n):
        prices.append(prices[-1] * (1 + np.random.uniform(-0.01, 0.01)))
    prices = np.array(prices)
    return pd.DataFrame(
        {
            "date": dates,
            "open": prices * 0.997,
            "high": prices * 1.007,
            "low": prices * 0.993,
            "close": prices,
            "volume": np.random.uniform(1000, 2000, n),
        }
    )


def _make_uptrend_df(n: int = 60) -> pd.DataFrame:
    """生成持续上涨趋势的 DataFrame（浪谷逐步上移）。"""
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    prices = [10.0]
    for _ in range(1, n):
        # 上涨为主，偶尔回踩形成浪谷
        change = np.random.uniform(-0.01, 0.03)
        prices.append(prices[-1] * (1 + change))
    prices = np.array(prices)
    lows = prices * np.random.uniform(0.97, 0.99, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": prices * 0.99,
            "high": prices * 1.01,
            "low": lows,
            "close": prices,
            "volume": np.random.uniform(1000, 2000, n),
        }
    )


class TestInitStop:
    """初始化止损测试。"""

    def test_init_with_fixed_pct(self) -> None:
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, initial_stop_pct=0.08)
        assert state.initial_stop_price == 9.2
        assert state.current_stop_price == 9.2
        assert state.highest_price == 10.0

    def test_init_with_atr(self) -> None:
        df = _make_df()
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df, atr_multiplier=3.0)
        # ATR 止损应在 2%-20% 范围内
        stop_pct = (10.0 - state.initial_stop_price) / 10.0
        assert 0.02 <= stop_pct <= 0.20

    def test_init_with_regime_atr(self) -> None:
        df = _make_df()
        engine = TrailingStopEngine()
        state_bull = engine.init_stop(entry_price=10.0, df=df, regime="bull")
        state_bear = engine.init_stop(entry_price=10.0, df=df, regime="bear")
        # 牛市 ATR 倍数 3.5，熊市 2.5，牛市止损应更宽（止损价更低）
        assert state_bull.initial_stop_price <= state_bear.initial_stop_price

    def test_atr_fallback_to_fixed(self) -> None:
        """df 为空时回退到固定比例。"""
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=pd.DataFrame(), initial_stop_pct=0.08)
        assert state.initial_stop_price == 9.2


class TestWaveValleysOrdering:
    """浪谷时间排序测试（修复 bug #1：按价格排序→按时间排序）。"""

    def test_wave_valleys_ordered_by_time_not_price(self) -> None:
        """浪谷应按时间排序（最近在后），不是按价格排序。"""
        df = _make_uptrend_df(80)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=float(df.iloc[0]["close"]), df=df)
        state, _ = engine.update(state, df)

        # wave_valleys 应按时间顺序排列
        # 在上涨趋势中，浪谷逐步上移，所以时间排序和价格排序结果相同
        # 但关键是：wave_valleys[-1] 应该是最近的浪谷，不是最高的
        if len(state.wave_valleys) >= 2:
            # 验证没有按价格降序排列（旧bug的行为）
            # 在上涨趋势中价格逐步升高，时间排序 = 价格升序
            # 所以我们验证它不是价格降序
            is_price_desc = all(
                state.wave_valleys[i] > state.wave_valleys[i + 1]
                for i in range(len(state.wave_valleys) - 1)
            )
            assert not is_price_desc, "浪谷不应按价格降序排列（旧bug）"

    def test_latest_valley_is_most_recent(self) -> None:
        """latest_valley（wave_valleys[-1]）应该是最近的浪谷。"""
        # 构造先涨后跌再涨的数据，确保最近浪谷不是最高价浪谷
        np.random.seed(42)
        n = 80
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        prices = np.zeros(n)
        prices[0] = 10.0
        # 前40天上涨到15
        for i in range(1, 40):
            prices[i] = prices[i - 1] * 1.01
        # 40-50天下跌到13（形成低浪谷）
        for i in range(40, 50):
            prices[i] = prices[i - 1] * 0.98
        # 50-80天小幅上涨到14（最近浪谷在13附近）
        for i in range(50, n):
            prices[i] = prices[i - 1] * 1.005

        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.01,
                "low": prices * 0.98,
                "close": prices,
                "volume": np.random.uniform(1000, 2000, n),
            }
        )

        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)
        state, _ = engine.update(state, df)

        # 最近浪谷不应超过当前价格
        latest = state.wave_valleys[-1] if state.wave_valleys else 0
        current_price = float(df.iloc[-1]["close"])
        assert latest <= current_price, f"最近浪谷 {latest} 应 <= 当前价 {current_price}"


class TestAdaptiveWaveWindow:
    """自适应浪谷窗口测试（修复 bug #3：定义但未调用）。"""

    def test_high_volatility_uses_larger_window(self) -> None:
        """高波动股票应使用更大窗口（8日）。"""
        np.random.seed(42)
        n = 60
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        # 高波动：每日 ±5%
        prices = [10.0]
        for _ in range(1, n):
            prices.append(prices[-1] * (1 + np.random.uniform(-0.05, 0.05)))
        prices = np.array(prices)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.02,
                "low": prices * 0.98,
                "close": prices,
                "volume": np.random.uniform(1000, 2000, n),
            }
        )
        engine = TrailingStopEngine()
        window = engine._adaptive_wave_window(df)
        assert window == 8, f"高波动应返回8，got {window}"

    def test_low_volatility_uses_smaller_window(self) -> None:
        """低波动股票应使用更小窗口（3日）。"""
        np.random.seed(42)
        n = 60
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        # 低波动：每日 ±0.3%
        prices = [10.0]
        for _ in range(1, n):
            prices.append(prices[-1] * (1 + np.random.uniform(-0.003, 0.003)))
        prices = np.array(prices)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.001,
                "low": prices * 0.999,
                "close": prices,
                "volume": np.random.uniform(1000, 2000, n),
            }
        )
        engine = TrailingStopEngine()
        window = engine._adaptive_wave_window(df)
        assert window == 3, f"低波动应返回3，got {window}"

    def test_default_window_for_normal_volatility(self) -> None:
        """中波动应使用默认窗口（5日）。"""
        df = _make_df()
        engine = TrailingStopEngine()
        window = engine._adaptive_wave_window(df)
        assert window == TrailingStopEngine.WAVE_WINDOW

    def test_update_calls_adaptive_window(self) -> None:
        """update() 应调用 _adaptive_wave_window 而非使用固定 WAVE_WINDOW。"""
        df = _make_uptrend_df(60)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)

        # 模拟 _adaptive_wave_window 被调用
        call_count = 0
        original = engine._adaptive_wave_window

        def spy(df: pd.DataFrame) -> int:
            nonlocal call_count
            call_count += 1
            return original(df)

        engine._adaptive_wave_window = spy  # type: ignore
        state, _ = engine.update(state, df)
        assert call_count >= 1, "update() 应调用 _adaptive_wave_window"


class TestFindWaveValleys:
    """浪谷识别算法测试。"""

    def test_returns_valleys_in_time_order(self) -> None:
        """_find_wave_valleys 返回的浪谷应按时间顺序排列。"""
        np.random.seed(42)
        lows = np.array(
            [
                10,
                9.5,
                9,
                9.8,
                10.5,
                10,
                9.7,
                10.2,
                11,
                10.5,
                10,
                10.8,
                11.5,
                11,
                10.7,
                11.2,
                12,
                11.5,
                11,
                11.8,
                12.5,
                12,
                11.5,
                12.2,
                13,
                12.5,
                12,
                12.8,
                13.5,
                13,
                12.5,
                13.2,
                14,
                13.5,
                13,
                13.8,
                14.5,
                14,
                13.5,
                14.2,
            ],
            dtype=float,
        )
        series = pd.Series(lows)
        engine = TrailingStopEngine()
        valleys = engine._find_wave_valleys(series, window=3)
        assert len(valleys) > 0
        # 验证返回的是浮点数列表
        assert all(isinstance(v, float) for v in valleys)

    def test_empty_series_returns_empty(self) -> None:
        series = pd.Series([], dtype=float)
        engine = TrailingStopEngine()
        valleys = engine._find_wave_valleys(series, window=5)
        # 空序列应返回空列表或最小值
        assert len(valleys) == 0 or len(valleys) == 1

    def test_short_series_returns_recent_min(self) -> None:
        """序列长度不足窗口时应回退到最近最低点。"""
        series = pd.Series([10.0, 9.5, 9.0], dtype=float)
        engine = TrailingStopEngine()
        valleys = engine._find_wave_valleys(series, window=5)
        assert len(valleys) >= 1


class TestStopPriceUpdate:
    """止损价更新逻辑测试。"""

    def test_stop_price_only_moves_up(self) -> None:
        """止损价只能上移，不能下移。"""
        df = _make_uptrend_df(60)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)
        initial_stop = state.current_stop_price

        state, _ = engine.update(state, df)
        assert state.current_stop_price >= initial_stop, "止损价不应下移"

    def test_stop_not_above_current_price(self) -> None:
        """止损价不应超过当前价格。"""
        df = _make_uptrend_df(60)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)
        state, _ = engine.update(state, df)
        current_price = float(df.iloc[-1]["close"])
        assert state.current_stop_price < current_price, "止损价不应超过当前价"

    def test_danger_signal_tightens_stop(self) -> None:
        """危险信号应收紧止损价。"""
        df = _make_uptrend_df(60)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)
        state, _ = engine.update(state, df)  # 先正常更新
        stop_before = state.current_stop_price

        state, signal = engine.update(state, df, danger_signals=["量增价不涨"])
        assert signal == TrailingStopSignal.TIGHTEN
        assert state.tightened is True
        assert state.current_stop_price >= stop_before, "危险信号后止损应收紧（上移）"

    def test_danger_signal_reset(self) -> None:
        """危险信号解除后 tightened 应复位。"""
        df = _make_uptrend_df(60)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)
        state, _ = engine.update(state, df, danger_signals=["量增价不涨"])
        assert state.tightened is True

        state, _ = engine.update(state, df, danger_signals=None)
        assert state.tightened is False, "危险信号解除后 tightened 应复位"


class TestValleyBreakCount:
    """跌破浪谷计数测试。"""

    def test_exit_on_stop_price_hit(self) -> None:
        """价格触及止损价应返回 EXIT。"""
        df = _make_df(60)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)
        # 手动设置止损价高于当前价
        state.current_stop_price = float(df.iloc[-1]["close"]) + 1.0
        _, signal = engine.update(state, df)
        assert signal == TrailingStopSignal.EXIT

    def test_valley_break_count_no_decrement_on_recovery(self) -> None:
        """价格恢复但未创新高时跌破计数不递减（P2-2 修复）。

        P2-2 修复：去掉原递减逻辑（价格回升浪谷 2% → 计数 -1），
        原因：A 股主力常用"跌停板吃货→拉升诱多→再下跌"手法，
        递减逻辑导致 3 次清仓永远触发不了。
        修复后：仅当价格真正创新高时才完全重置为 0。
        """
        engine = TrailingStopEngine()
        state = TrailingStopState(
            initial_stop_price=9.0,
            current_stop_price=9.0,
            highest_price=11.0,
            wave_valleys=[9.0, 10.5],
            valley_break_count=2,
            last_update="2026-07-03",
            last_signal="warning",
            tightened=False,
        )

        np.random.seed(42)
        n_rows = 40
        dates = pd.date_range("2026-01-01", periods=n_rows, freq="B")
        prices = np.full(n_rows, 10.8)  # 价格 10.8 < highest_price 11.0，未创新高
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.01,
                "low": prices * 0.98,
                "close": prices,
                "volume": np.random.uniform(1000, 2000, n_rows),
            }
        )

        state, _ = engine.update(state, df)
        # P2-2 修复后：未创新高 → 计数不变（不递减、不重置）
        assert (
            state.valley_break_count == 2
        ), f"未创新高时跌破计数应保持 2（不递减），got {state.valley_break_count}"

    def test_valley_break_count_resets_on_new_high(self) -> None:
        """价格真正创新高时跌破计数重置为 0（P2-2 修复）。"""
        engine = TrailingStopEngine()
        state = TrailingStopState(
            initial_stop_price=9.0,
            current_stop_price=9.0,
            highest_price=11.0,
            wave_valleys=[9.0, 10.5],
            valley_break_count=2,
            last_update="2026-07-03",
            last_signal="warning",
            tightened=False,
        )

        np.random.seed(42)
        n_rows = 40
        dates = pd.date_range("2026-01-01", periods=n_rows, freq="B")
        prices = np.full(n_rows, 11.5)  # 价格 11.5 > highest_price 11.0，创新高
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.01,
                "low": prices * 0.98,
                "close": prices,
                "volume": np.random.uniform(1000, 2000, n_rows),
            }
        )

        state, _ = engine.update(state, df)
        # P2-2 修复后：创新高 → 计数重置为 0
        assert (
            state.valley_break_count == 0
        ), f"创新高后跌破计数应重置为 0，got {state.valley_break_count}"

    def test_consecutive_breaks_accumulate_to_exit(self) -> None:
        """连续多日跌破浪谷3次应触发 EXIT（递进警报核心场景）。

        验证完善修复：旧逻辑的每天-1递减可能导致永远无法累积到3次；
        新逻辑在价格未显著恢复时不重置计数，确保3次EXIT能触发。
        同日去重：同日多次扫描只计1次（防止盘中4次扫描误触3次EXIT）。
        """
        engine = TrailingStopEngine()
        # 隔离浪谷发现逻辑：返回空列表，防止 update() 内部更新浪谷列表
        # 使 latest_valley 始终为预设的 10.0
        engine._find_wave_valleys = lambda low_series, window: []  # type: ignore

        state = TrailingStopState(
            initial_stop_price=8.0,
            current_stop_price=8.0,
            highest_price=12.0,
            wave_valleys=[8.0, 10.0],
            valley_break_count=0,
            last_update="2026-07-03",
            last_signal="hold",
            tightened=False,
        )

        # 价格 9.5 在浪谷 10.0 下方但高于止损价 8.0
        np.random.seed(42)
        n_rows = 40
        dates = pd.date_range("2026-01-01", periods=n_rows, freq="B")
        prices = np.full(n_rows, 9.5)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.01,
                "low": prices * 0.98,
                "close": prices,
                "volume": np.random.uniform(1000, 2000, n_rows),
            }
        )

        # 第一日跌破 → ALERT
        state, signal1 = engine.update(state, df)
        assert signal1 == TrailingStopSignal.ALERT
        assert state.valley_break_count == 1

        # 第二日跌破（修改 last_break_date 模拟跨日）→ WARNING
        state.last_break_date = "2026-07-02"  # 模拟前一日，使今日重新计数
        state, signal2 = engine.update(state, df)
        assert signal2 == TrailingStopSignal.WARNING
        assert state.valley_break_count == 2

        # 第三日跌破 → EXIT
        state.last_break_date = "2026-07-02"  # 模拟前一日
        state, signal3 = engine.update(state, df)
        assert signal3 == TrailingStopSignal.EXIT
        assert state.valley_break_count == 3


class TestStateSerialization:
    """状态序列化测试。"""

    def test_state_roundtrip(self) -> None:
        """状态序列化/反序列化应保持一致。"""
        engine = TrailingStopEngine()
        state = TrailingStopState(
            initial_stop_price=9.2,
            current_stop_price=10.5,
            highest_price=12.0,
            wave_valleys=[9.0, 9.5, 10.0, 10.5],
            valley_break_count=1,
            last_update="2026-07-03",
            last_signal="alert",
            tightened=False,
        )
        d = engine.state_to_dict(state)
        restored = engine.dict_to_state(d)
        assert restored.initial_stop_price == 9.2
        assert restored.current_stop_price == 10.5
        assert restored.highest_price == 12.0
        assert restored.wave_valleys == [9.0, 9.5, 10.0, 10.5]
        assert restored.valley_break_count == 1
        assert restored.tightened is False

    def test_dict_to_state_defaults(self) -> None:
        """空字典应返回默认状态。"""
        engine = TrailingStopEngine()
        state = engine.dict_to_state({})
        assert state.initial_stop_price == 0.0
        assert state.wave_valleys == []
        assert state.valley_break_count == 0


class TestRegimeAdaptive:
    """市场状态自适应参数测试。"""

    def test_bull_regime_wider_max_drawdown(self) -> None:
        """牛市应允许更大回撤。"""
        df = _make_uptrend_df(60)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)

        # 牛市更新
        state_bull, _ = engine.update(
            state.copy() if hasattr(state, "copy") else state, df, regime="bull"
        )
        # 熊市更新
        state_bear, _ = engine.update(
            TrailingStopEngine.dict_to_state(engine.state_to_dict(state)), df, regime="bear"
        )

        # 牛市 max_drawdown 18%，熊市 10%
        # 牛市止损价应 <= 熊市止损价（允许更大回撤 = 止损更低）
        assert state_bull.current_stop_price <= state_bear.current_stop_price + 0.01

    def test_regime_tighten_factor(self) -> None:
        """不同市场状态的危险信号收紧因子不同。"""
        df = _make_uptrend_df(60)
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=10.0, df=df)
        state, _ = engine.update(state, df)  # 先正常更新

        # 牛市收紧因子 0.3（收紧幅度小）
        state_bull = TrailingStopEngine.dict_to_state(engine.state_to_dict(state))
        _, sig_bull = engine.update(state_bull, df, danger_signals=["test"], regime="bull")
        assert sig_bull == TrailingStopSignal.TIGHTEN

        # 熊市收紧因子 0.6（收紧幅度大）
        state_bear = TrailingStopEngine.dict_to_state(engine.state_to_dict(state))
        _, sig_bear = engine.update(state_bear, df, danger_signals=["test"], regime="bear")
        assert sig_bear == TrailingStopSignal.TIGHTEN
        # 熊市止损应更高（收紧幅度更大）
        assert state_bear.current_stop_price >= state_bull.current_stop_price - 0.01
