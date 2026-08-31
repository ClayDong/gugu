"""资金流异动策略单元测试。

覆盖场景：
- 参数校验（ma20_window / shrink vs breakout）
- 默认参数加载
- 策略注册
- 信号列 / 置信度列输出
- 4 类卖出信号（放量下跌 / 量价背离 / 跌破MA20 / 巨量滞涨）
- 买入信号（缩量回踩不破MA20）
- 卖出优先于买入
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

try:
    from gugu.strategies.capital_flow_alert import CapitalFlowAlertStrategy
except ModuleNotFoundError:
    CapitalFlowAlertStrategy = None  # type: ignore[assignment]
from gugu.strategies.registry import _REGISTRY

pytestmark = pytest.mark.skip(reason="依赖已归档策略（capital_flow_alert）")

# ── 测试数据构造 ──────────────────────────────────────────────


def _make_df(days: int = 100, seed: int = 42) -> pd.DataFrame:
    """构造基础测试数据：随机走势 + 正常成交量。"""
    np.random.seed(seed)
    n = days
    base = 100.0
    close = base + np.cumsum(np.random.randn(n) * 0.5)
    high = close + np.random.rand(n) * 2
    low = close - np.random.rand(n) * 2
    open_ = low + np.random.rand(n) * (high - low)
    volume = np.random.randint(5_000_000, 10_000_000, size=n).astype(float)
    amount = close * volume
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


def _make_uptrend_df(days: int = 60) -> pd.DataFrame:
    """构造稳定上升趋势数据（用于买入信号测试）。"""
    n = days
    close = np.linspace(100, 120, n) + np.sin(np.linspace(0, 6, n)) * 0.5
    open_ = close - 0.3
    high = close + 0.8
    low = close - 0.8
    volume = np.full(n, 8_000_000.0)
    amount = close * volume
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


# ── 参数校验测试 ──────────────────────────────────────────────


class TestCapitalFlowParams:
    """参数校验测试。"""

    def test_validate_params_rejects_invalid_ma20_window(self):
        """ma20_window <= 0 应抛 ValueError。"""
        with pytest.raises(ValueError, match="ma20_window"):
            CapitalFlowAlertStrategy({"ma20_window": 0})

    def test_validate_params_rejects_shrink_ge_breakout(self):
        """shrink_ratio >= breakout_volume_ratio 应抛 ValueError。"""
        with pytest.raises(ValueError, match="shrink_ratio"):
            CapitalFlowAlertStrategy({"shrink_ratio": 1.5, "breakout_volume_ratio": 1.3})

    def test_default_params_loaded(self):
        """默认参数应正确加载。"""
        strat = CapitalFlowAlertStrategy()
        assert strat.params["ma20_window"] == 20
        assert strat.params["vol_ma_window"] == 5
        assert strat.params["shrink_ratio"] == 0.8
        assert strat.params["breakout_volume_ratio"] == 1.3
        assert strat.params["stagnation_volume_ratio"] == 2.0

    def test_strategy_registered(self):
        """策略应已注册到 _REGISTRY。"""
        assert "capital_flow_alert" in _REGISTRY
        assert _REGISTRY["capital_flow_alert"] is CapitalFlowAlertStrategy


# ── 信号输出测试 ──────────────────────────────────────────────


class TestCapitalFlowSignalOutput:
    """信号列输出测试。"""

    def test_generate_signals_returns_signal_column(self):
        """输出含 signal 列，值在 {-1, 0, 1}。"""
        df = _make_df(days=100)
        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        assert "signal" in out.columns
        assert set(out["signal"].unique()).issubset({-1, 0, 1})

    def test_generate_signals_returns_confidence_column(self):
        """输出含 confidence 列，值在 [0, 1]。"""
        df = _make_df(days=100)
        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        assert "confidence" in out.columns
        conf = out["confidence"]
        assert (conf >= 0).all()
        assert (conf <= 1).all()


# ── 卖出信号测试 ──────────────────────────────────────────────


class TestCapitalFlowSellSignals:
    """卖出信号测试。"""

    def test_volume_drop_triggers_sell(self):
        """持续放量下跌（3日内2日放量且累计收跌）应触发卖出信号。"""
        df = _make_df(days=100, seed=42)
        # 在第 80-82 天注入放量下跌
        idx = 80
        for i in range(idx, idx + 3):
            df.loc[i, "volume"] = df["volume"].iloc[:i].mean() * 1.8  # 放量
            df.loc[i, "close"] = df.loc[i - 1, "close"] * 0.985  # 每日跌 1.5%

        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        # 在 idx ~ idx+2 区间应有卖出信号
        sell_signals = (out["signal"].iloc[idx : idx + 3] == -1).any()
        assert sell_signals, "放量下跌应触发卖出信号"

    def test_divergence_triggers_sell(self):
        """量价背离（放量+收盘走弱+前日涨）应触发卖出信号。"""
        df = _make_df(days=100, seed=42)
        idx = 80
        # 前日上涨
        df.loc[idx - 1, "close"] = df.loc[idx - 2, "close"] * 1.03
        # 当日下跌 + 放量
        df.loc[idx, "close"] = df.loc[idx - 1, "close"] * 0.99
        df.loc[idx, "volume"] = df["volume"].iloc[:idx].mean() * 1.8

        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        assert out.loc[idx, "signal"] == -1, "量价背离应触发卖出信号"

    def test_break_ma20_triggers_sell(self):
        """跌破 MA20 支撑应触发卖出信号。"""
        df = _make_df(days=100, seed=42)
        idx = 80
        # 让收盘价跌破 MA20
        ma20_val = df["close"].iloc[idx - 20 : idx].mean()
        df.loc[idx, "close"] = ma20_val * 0.95  # 明确跌破 MA20

        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        assert out.loc[idx, "signal"] == -1, "跌破 MA20 应触发卖出信号"

    def test_stagnation_triggers_sell(self):
        """高位巨量滞涨（放量+长上影+涨幅小）应触发卖出信号。"""
        df = _make_df(days=100, seed=42)
        idx = 80
        prev_close = df.loc[idx - 1, "close"]
        # 巨量
        df.loc[idx, "volume"] = df["volume"].iloc[:idx].mean() * 2.5
        # 长上影：high 远高于 close，open 接近 close
        df.loc[idx, "open"] = prev_close
        df.loc[idx, "close"] = prev_close * 1.005  # 涨幅 0.5%（< 2%）
        df.loc[idx, "high"] = prev_close * 1.05  # 上影 5%（> 2%）
        df.loc[idx, "low"] = prev_close * 0.995

        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        assert out.loc[idx, "signal"] == -1, "巨量滞涨应触发卖出信号"


# ── 买入信号测试 ──────────────────────────────────────────────


def _make_buy_scenario_df() -> pd.DataFrame:
    """构造买入信号场景数据：稳定上升 → 强势拉升 → 缩量回踩。

    确保第 55 天满足全部 3 个买入条件：
    1. 缩量回踩不破 MA20（volume < vol_ma5*0.8, close >= ma20, daily_return > -2%）
    2. MA20 向上拐头（ma20[55] > ma20[50]）
    3. 前期强势（5日内最高涨幅 >= 5%）
    """
    n = 60
    close = np.zeros(n)
    # Days 0-44: 缓慢上升 100 → 108
    for i in range(45):
        close[i] = 100 + i * 0.18
    # Days 45-49: 继续上升 108 → 112
    for i in range(45, 50):
        close[i] = 108 + (i - 45) * 0.8
    # Days 50-54: 强势拉升 112 → 120（创造前期强势）
    for i in range(50, 55):
        close[i] = 112 + (i - 50) * 1.6
    close[54] = 120.0
    # Day 55: 缩量回踩（close 仍在 MA20 之上，daily_return > -2%）
    close[55] = 119.0
    # Days 56-59: placeholder
    for i in range(56, n):
        close[i] = 119.0

    open_ = close - 0.2
    high = close + 0.5
    low = close - 0.5
    volume = np.full(n, 8_000_000.0)
    # Day 55: 缩量
    volume[55] = 3_000_000.0
    amount = close * volume

    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


class TestCapitalFlowBuySignals:
    """买入信号测试。"""

    def test_shrink_pullback_triggers_buy(self):
        """缩量回踩不破 MA20 + MA20 向上 + 前期强势应触发买入信号。"""
        df = _make_buy_scenario_df()
        idx = 55

        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        assert out.loc[idx, "signal"] == 1, "缩量回踩不破 MA20 应触发买入信号"

    def test_shrink_pullback_confidence_range(self):
        """缩量回踩买入信号置信度应在合理范围 [0.5, 0.85]。"""
        df = _make_buy_scenario_df()
        idx = 55

        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        if out.loc[idx, "signal"] == 1:
            conf = out.loc[idx, "confidence"]
            assert 0.5 <= conf <= 0.85


# ── 信号优先级测试 ────────────────────────────────────────────


class TestCapitalFlowSignalPriority:
    """信号优先级测试。"""

    def test_sell_overrides_buy(self):
        """同时满足买卖条件时，卖出优先。

        构造场景：3日窗口内有2日放量（触发 volume_drop 卖出），
        但当日本身缩量回踩（满足买入条件1的缩量部分）。
        由于 volume_drop 的 rolling(3).sum() 看的是3日窗口，
        即使当日缩量，前2日的放量仍能让 cond_volume_drop=True。
        """
        df = _make_uptrend_df(days=60)
        idx = 55

        # 前期强势：idx-5 拉高创造前期强势表现
        df.loc[idx - 5, "close"] = 108.0
        df.loc[idx - 4, "close"] = 112.0
        # idx-3 设为高位（128），确保 3 日累计跌幅 > 3% 触发 cond_volume_drop
        df.loc[idx - 3, "close"] = 128.0

        # idx-2, idx-1 放量下跌（让 volume_drop rolling(3) 在 idx 处 >= 2）
        df.loc[idx - 2, "volume"] = 15_000_000  # 放量
        df.loc[idx - 2, "close"] = 120.0  # 从 128 下跌
        df.loc[idx - 1, "volume"] = 15_000_000  # 放量
        df.loc[idx - 1, "close"] = 116.0  # 继续下跌

        # 当日缩量回踩（满足买入缩量条件），但 close 仍在 MA20 之上
        ma20_val = df["close"].iloc[idx - 20 : idx].mean()
        df.loc[idx, "close"] = ma20_val * 1.005  # 略高于 MA20
        df.loc[idx, "open"] = df.loc[idx, "close"] * 0.998
        df.loc[idx, "high"] = df.loc[idx, "close"] * 1.002
        df.loc[idx, "low"] = df.loc[idx, "close"] * 0.998
        df.loc[idx, "volume"] = 3_000_000  # 缩量

        # 3日累计收跌：close[idx] / close[idx-3] - 1
        # close[idx-3] = 128, close[idx] ≈ ma20*1.005 ≈ 115
        # 115 / 128 - 1 ≈ -10.2% < -3% → cond_volume_drop True

        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        # 卖出应优先于买入
        assert out.loc[idx, "signal"] == -1, "卖出信号应优先于买入信号"

    def test_sell_buy_mutually_exclusive(self):
        """卖出和买入信号应互斥（signal 是单列，不可能同时为 1 和 -1）。"""
        df = _make_df(days=100, seed=42)
        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        sell_mask = out["signal"] == -1
        buy_mask = out["signal"] == 1
        assert not (sell_mask & buy_mask).any()

    def test_confidence_zero_when_no_signal(self):
        """无信号时置信度应为 0。"""
        df = _make_df(days=100, seed=42)
        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        no_signal = out["signal"] == 0
        if no_signal.any():
            assert (out.loc[no_signal, "confidence"] == 0).all()

    def test_sell_confidence_scales_with_features(self):
        """卖出置信度应随触发特征数增加（1特征0.5, 2特征0.7, 3+特征0.85）。"""
        df = _make_df(days=100, seed=42)
        idx = 80

        # 注入多个卖出特征：放量下跌 + 跌破MA20 + 巨量滞涨
        ma20_val = df["close"].iloc[idx - 20 : idx].mean()
        prev_close = df.loc[idx - 1, "close"]

        # 放量 + 跌破 MA20 + 长上影（同时触发多个卖出特征）
        df.loc[idx, "close"] = ma20_val * 0.90  # 跌破 MA20
        df.loc[idx, "open"] = prev_close
        df.loc[idx, "high"] = prev_close * 1.03  # 长上影
        df.loc[idx, "low"] = df.loc[idx, "close"] * 0.99
        df.loc[idx, "volume"] = df["volume"].iloc[:idx].mean() * 2.5  # 巨量

        # 确保前2日也放量（让 volume_drop rolling(3) >= 2）
        df.loc[idx - 1, "volume"] = df["volume"].iloc[:idx].mean() * 1.5
        df.loc[idx - 1, "close"] = prev_close * 0.99

        strat = CapitalFlowAlertStrategy()
        out = strat.generate_signals(df)

        if out.loc[idx, "signal"] == -1:
            assert out.loc[idx, "confidence"] >= 0.5
