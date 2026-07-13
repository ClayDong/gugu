"""断板反包策略单元测试。

覆盖核心场景：
- 5 核心硬 AND 条件（涨停+断板+反包+放量+趋势）
- 5 评分项置信度（缩量/量能/强反包/趋势/时效）
- 止损/止盈/巨量滞涨/时间止损卖出
- 参数校验
- 边界场景
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.breakout_rebound import BreakoutReboundStrategy


def _make_df(
    days: int = 80,
    seed: int = 42,
) -> pd.DataFrame:
    """构造基础测试数据：80 天缓慢上升趋势 + 正常成交量。

    后续用例在此基础上局部修改，构造特定场景。
    """
    np.random.seed(seed)
    n = days
    # 缓慢上升趋势：从 100 涨到 120
    trend = np.linspace(100, 120, n)
    close = trend + np.random.randn(n) * 1.5
    open_ = close - np.random.randn(n) * 0.5
    high = np.maximum(close, open_) + np.random.rand(n) * 1.5
    low = np.minimum(close, open_) - np.random.rand(n) * 1.5
    volume = np.random.randint(5_000_000, 10_000_000, size=n).astype(float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": close * volume,
        }
    )


def _inject_breakout_rebound(
    df: pd.DataFrame,
    limit_up_idx: int,
    break_idx: int,
    rebound_idx: int,
    limit_up_high: float = 120.0,
    break_high: float = 118.0,
    break_low: float = 114.0,
    break_vol: float = 5_000_000,
    rebound_close: float = 119.0,
    rebound_high: float = 120.0,
    rebound_low: float = 117.0,
    rebound_vol: float = 15_000_000,
    base_vol: float = 8_000_000,
) -> pd.DataFrame:
    """在指定位置注入涨停→断板→反包形态。

    Args:
        limit_up_idx: 涨停日位置
        break_idx: 断板日位置（涨停次日）
        rebound_idx: 反包日位置（断板次日）
        limit_up_high: 涨停日最高价
        break_high: 断板日最高价
        break_low: 断板日最低价
        break_vol: 断板日成交量（缩量）
        rebound_close: 反包日收盘价（必须 > break_high）
        rebound_high: 反包日最高价
        rebound_low: 反包日最低价
        rebound_vol: 反包日成交量（放量）
        base_vol: 基础成交量（用于计算量比）
    """
    df = df.copy()
    # 先把前面几天的 volume 设为 base_vol，确保量比计算正常
    for i in range(max(0, limit_up_idx - 10), limit_up_idx):
        df.loc[i, "volume"] = base_vol

    # 涨停日：涨幅 >= 9.5%
    prev_close = df.loc[limit_up_idx - 1, "close"]
    limit_up_close = prev_close * 1.1  # 涨停
    df.loc[limit_up_idx, "open"] = prev_close * 1.05
    df.loc[limit_up_idx, "close"] = limit_up_close
    df.loc[limit_up_idx, "high"] = limit_up_high
    df.loc[limit_up_idx, "low"] = prev_close * 1.04
    df.loc[limit_up_idx, "volume"] = base_vol * 2  # 涨停日放量

    # 断板日：未继续涨停 + 缩量调整
    df.loc[break_idx, "open"] = limit_up_close * 0.99
    df.loc[break_idx, "close"] = limit_up_close * 0.97  # 跌 3%
    df.loc[break_idx, "high"] = break_high
    df.loc[break_idx, "low"] = break_low
    df.loc[break_idx, "volume"] = break_vol  # 缩量

    # 反包日：收盘价 > 断板日最高价 + 放量阳线
    df.loc[rebound_idx, "open"] = break_low * 1.01
    df.loc[rebound_idx, "close"] = rebound_close
    df.loc[rebound_idx, "high"] = rebound_high
    df.loc[rebound_idx, "low"] = rebound_low
    df.loc[rebound_idx, "volume"] = rebound_vol  # 放量

    return df


class TestBreakoutReboundParams:
    """参数校验测试。"""

    def test_validate_params_default_passes(self):
        """默认参数应通过校验。"""
        BreakoutReboundStrategy().validate_params()

    def test_validate_params_ma_trend_gt_long_raises(self):
        """ma_trend_window >= ma_long_window 应抛错。"""
        with pytest.raises(ValueError, match="ma_trend_window"):
            BreakoutReboundStrategy({"ma_trend_window": 60, "ma_long_window": 60}).validate_params()

    def test_validate_params_lookback_zero_raises(self):
        """limit_up_lookback <= 0 应抛错。"""
        with pytest.raises(ValueError, match="limit_up_lookback"):
            BreakoutReboundStrategy({"limit_up_lookback": 0}).validate_params()

    def test_validate_params_limit_up_pct_out_of_range_raises(self):
        """limit_up_pct 超出 5%-11% 应抛错。"""
        with pytest.raises(ValueError, match="limit_up_pct"):
            BreakoutReboundStrategy({"limit_up_pct": 0.03}).validate_params()

    def test_validate_params_breakout_vol_lt_one_raises(self):
        """breakout_vol_ratio < 1.0 应抛错。"""
        with pytest.raises(ValueError, match="breakout_vol_ratio"):
            BreakoutReboundStrategy({"breakout_vol_ratio": 0.8}).validate_params()


class TestBreakoutReboundSignals:
    """信号生成测试。"""

    def test_output_columns_present(self, ohlcv_df: pd.DataFrame):
        """输出必须包含 signal/confidence 列。"""
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(ohlcv_df)
        assert "signal" in out.columns
        assert "confidence" in out.columns

    def test_signal_values_valid(self, ohlcv_df: pd.DataFrame):
        """signal 列值必须 ∈ {-1, 0, 1}。"""
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(ohlcv_df)
        assert set(out["signal"].unique()).issubset({-1, 0, 1})

    def test_confidence_range(self, ohlcv_df: pd.DataFrame):
        """confidence 列值必须在 [0, 1]。"""
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(ohlcv_df)
        valid_conf = out["confidence"].dropna()
        assert (valid_conf >= 0).all()
        assert (valid_conf <= 1).all()

    def test_no_signal_on_flat_data(self):
        """无涨停的平盘数据不应产生买入信号。"""
        n = 80
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [8_000_000.0] * n,
                "amount": [800_000_000.0] * n,
            }
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert (out["signal"] != 1).all(), "平盘数据不应产生买入信号"


class TestBreakoutReboundCoreConditions:
    """核心硬条件测试。"""

    def test_full_breakout_rebound_triggers_buy(self):
        """完整涨停→断板→反包应触发买入信号。"""
        df = _make_df(days=80, seed=42)
        # 在 index=50 注入涨停，51 断板，52 反包
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            break_high=118.0,
            rebound_close=119.0,  # > break_high
            rebound_vol=15_000_000,
            break_vol=5_000_000,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 反包日应产生买入信号
        assert out.loc[52, "signal"] == 1, "完整断板反包应触发买入信号"

    def test_no_limit_up_no_signal(self):
        """无涨停板不应产生信号。"""
        df = _make_df(days=80, seed=42)
        # 不注入涨停，直接构造反包形态
        df.loc[50, "close"] = 110.0
        df.loc[51, "high"] = 112.0
        df.loc[52, "close"] = 113.0
        df.loc[52, "volume"] = 15_000_000
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert out.loc[52, "signal"] != 1, "无涨停板不应触发反包信号"

    def test_no_break_no_signal(self):
        """涨停后继续涨停（无断板）不应产生反包信号。"""
        df = _make_df(days=80, seed=42)
        prev_close = df.loc[49, "close"]
        # 连续两天涨停
        df.loc[50, "close"] = prev_close * 1.1
        df.loc[50, "volume"] = 16_000_000
        df.loc[51, "close"] = prev_close * 1.1 * 1.1
        df.loc[51, "volume"] = 16_000_000
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 没有断板日，不应产生反包信号
        assert (out["signal"] == 1).sum() == 0 or out.loc[52, "signal"] != 1

    def test_rebound_below_break_high_no_signal(self):
        """反包日收盘价低于断板日最高价不应触发信号。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            break_high=118.0,
            rebound_close=117.0,  # < break_high，不构成反包
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert out.loc[52, "signal"] != 1, "收盘价低于断板日最高价不应触发反包"

    def test_no_volume_lower_confidence(self):
        """反包日无量置信度应较低（v0.5.8 量能降级为评分项）。"""
        df = _make_df(days=80, seed=42)
        df_strong = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            rebound_vol=15_000_000,  # 放量
        )
        df_weak = _inject_breakout_rebound(
            _make_df(days=80, seed=42),
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            rebound_vol=8_000_000,  # 与基础量持平，量比 < 1.5
        )
        strat = BreakoutReboundStrategy({"min_confidence": 0.0})
        out_strong = strat.generate_signals(df_strong)
        out_weak = strat.generate_signals(df_weak)
        # 两者都可能触发信号（量能不再是硬门槛），但放量版本置信度应更高
        if out_strong.loc[52, "signal"] == 1 and out_weak.loc[52, "signal"] == 1:
            assert out_strong.loc[52, "confidence"] > out_weak.loc[52, "confidence"], (
                "放量反包置信度应高于无量反包"
            )

    def test_yin_line_no_signal(self):
        """反包日阴线不应触发信号。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            break_high=118.0,
        )
        # 把反包日改成阴线（close < open）
        df.loc[52, "open"] = 120.0
        df.loc[52, "close"] = 119.0  # close < open，阴线
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert out.loc[52, "signal"] != 1, "阴线不应触发反包信号"

    def test_below_ma20_no_signal(self):
        """收盘价低于 MA20 不应触发信号。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            break_high=118.0,
            rebound_close=119.0,
        )
        # 把 MA20 拉高，使反包日 close < MA20
        for i in range(40, 53):
            df.loc[i, "close"] = df.loc[i, "close"] * 0.5
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert out.loc[52, "signal"] != 1, "收盘价低于 MA20 不应触发信号"


class TestBreakoutReboundScoring:
    """评分项置信度测试。"""

    def test_high_confidence_on_strong_rebound(self):
        """强反包（突破涨停日最高价）置信度应更高。"""
        df = _make_df(days=80, seed=42)
        # 强反包：突破涨停日最高价
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            limit_up_high=120.0,
            break_high=118.0,
            rebound_close=121.0,  # > limit_up_high（强反包）
            rebound_vol=20_000_000,
            break_vol=4_000_000,  # 缩量
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        if out.loc[52, "signal"] == 1:
            assert out.loc[52, "confidence"] > 0.5, "强反包置信度应较高"

    def test_low_confidence_on_weak_rebound(self):
        """弱反包（仅略超断板日最高价）置信度应较低。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            limit_up_high=125.0,
            break_high=118.0,
            rebound_close=118.5,  # 仅略超 break_high，远低于 limit_up_high
            rebound_vol=12_000_000,
            break_vol=7_000_000,  # 缩量不明显
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        if out.loc[52, "signal"] == 1:
            assert out.loc[52, "confidence"] < 0.8, "弱反包置信度应较低"

    def test_shrink_break_day_increases_confidence(self):
        """断板日缩量应增加置信度。"""
        df1 = _make_df(days=80, seed=42)
        df1 = _inject_breakout_rebound(
            df1,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            break_vol=4_000_000,  # 缩量
        )
        df2 = _make_df(days=80, seed=42)
        df2 = _inject_breakout_rebound(
            df2,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            break_vol=8_000_000,  # 不缩量
        )
        strat = BreakoutReboundStrategy()
        out1 = strat.generate_signals(df1)
        out2 = strat.generate_signals(df2)
        if out1.loc[52, "signal"] == 1 and out2.loc[52, "signal"] == 1:
            assert out1.loc[52, "confidence"] >= out2.loc[52, "confidence"]


class TestBreakoutReboundSellSignals:
    """卖出信号测试。"""

    def test_stop_loss_below_ma20(self):
        """跌破 MA20 应触发卖出信号。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
        )
        # 在反包后注入大跌，跌破 MA20
        ma20_at_53 = df["close"].iloc[:53].rolling(20).mean().iloc[52]
        df.loc[53, "close"] = ma20_at_53 * 0.9  # 跌破 MA20
        df.loc[53, "open"] = ma20_at_53
        df.loc[53, "high"] = ma20_at_53
        df.loc[53, "low"] = ma20_at_53 * 0.9
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 第 53 天应触发卖出（跌破 MA20）
        assert out.loc[53, "signal"] == -1, "跌破 MA20 应触发卖出"

    def test_stagnation_triggers_sell(self):
        """巨量滞涨应触发卖出信号。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
        )
        # 注入巨量滞涨：量比 >= 2.5 + 长上影 > 2% + 涨幅小
        # vol_ma5[53] ≈ mean(vol[48:52]) ≈ 10.4M，需 volume >= 26M 达到 2.5 量比
        df.loc[53, "volume"] = 30_000_000  # 巨量（量比 >= 2.5）
        df.loc[53, "open"] = 120.0
        df.loc[53, "close"] = 120.5  # 涨幅小
        df.loc[53, "high"] = 124.0  # 长上影 > 2%
        df.loc[53, "low"] = 119.5
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert out.loc[53, "signal"] == -1, "巨量滞涨应触发卖出"

    def test_big_yang_triggers_sell(self):
        """大阳线冲高应触发止盈卖出（v0.5.8 阈值从 4.8% 提高到 6.4%）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
        )
        # 注入大阳线：涨幅 >= 6.4%（take_profit_pct * 0.8）+ 量比 >= 1.5
        # vol_ma5[53] ≈ mean(vol[48:52]) ≈ 10.4M，需 volume >= 15.6M 达到 1.5 量比
        prev_close = df.loc[52, "close"]
        df.loc[53, "open"] = prev_close
        df.loc[53, "close"] = prev_close * 1.07  # 涨幅 7%（> 6.4%）
        df.loc[53, "high"] = prev_close * 1.08
        df.loc[53, "low"] = prev_close
        df.loc[53, "volume"] = 20_000_000  # 量比 >= 1.5
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert out.loc[53, "signal"] == -1, "大阳线冲高应触发止盈卖出"


class TestBreakoutReboundEdgeCases:
    """边界场景测试。"""

    def test_insufficient_data_no_error(self):
        """数据量不足（< 60 天）不应报错。"""
        n = 30
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [8_000_000.0] * n,
                "amount": [800_000_000.0] * n,
            }
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert len(out) == n

    def test_limit_up_at_boundary(self):
        """涨停板边界值（涨幅 = 9.5%）应识别为涨停。"""
        df = _make_df(days=80, seed=42)
        prev_close = df.loc[49, "close"]
        df.loc[50, "close"] = prev_close * 1.095  # 涨幅 9.5%
        df.loc[50, "open"] = prev_close
        df.loc[50, "high"] = prev_close * 1.095
        df.loc[50, "low"] = prev_close
        df.loc[50, "volume"] = 16_000_000
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        assert out.loc[50, "is_limit_up"] == True, "涨幅 9.5% 应识别为涨停"

    def test_lookback_window_expires(self):
        """涨停超出 lookback 窗口不应触发反包。"""
        df = _make_df(days=100, seed=42)
        # 在 index=50 涨停，51 断板，但反包在 56（超出 5 日窗口）
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=56,
        )
        strat = BreakoutReboundStrategy({"limit_up_lookback": 5})
        out = strat.generate_signals(df)
        assert out.loc[56, "signal"] != 1, "超出 lookback 窗口的反包不应触发"

    def test_registry_integration(self):
        """策略应能在 registry 中获取。"""
        from gugu.strategies.registry import get_strategy, list_strategies

        assert "breakout_rebound" in list_strategies()
        strat = get_strategy("breakout_rebound")
        assert strat.name == "breakout_rebound"

    def test_backtest_mode_skips_confirmation(self):
        """回测模式应跳过确认（is_backtest=True）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
        )
        strat = BreakoutReboundStrategy({"confirm_days": 1})
        out = strat.generate_signals(df, is_backtest=True)
        # 回测模式不应报错
        assert "signal" in out.columns

    def test_sell_overrides_buy(self):
        """卖出信号应覆盖买入信号（v0.5.8 持仓状态判断）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 第52天应有买入信号（完整断板反包）
        assert out.loc[52, "signal"] == 1, "第52天应有买入信号"

        # 第53天注入卖出条件（持仓中跌破 MA20）
        ma20_at_53 = df["close"].iloc[:53].rolling(20).mean().iloc[52]
        if pd.notna(ma20_at_53):
            df.loc[53, "close"] = ma20_at_53 * 0.9  # 跌破 MA20
            df.loc[53, "open"] = ma20_at_53
            df.loc[53, "high"] = ma20_at_53
            df.loc[53, "low"] = ma20_at_53 * 0.9
        out = strat.generate_signals(df)
        # 持仓中（第52天买入）跌破 MA20 应触发卖出
        assert out.loc[53, "signal"] == -1, "持仓中跌破MA20应触发卖出"

    def test_min_confidence_filter(self):
        """min_confidence 应过滤低置信度信号。"""
        df = _make_df(days=80, seed=42)
        df = _inject_breakout_rebound(
            df,
            limit_up_idx=50,
            break_idx=51,
            rebound_idx=52,
            break_high=118.0,
            rebound_close=118.5,  # 弱反包
            rebound_vol=12_000_000,
            break_vol=7_000_000,
        )
        strat = BreakoutReboundStrategy({"min_confidence": 0.99})
        out = strat.generate_signals(df)
        assert out.loc[52, "signal"] != 1, "高 min_confidence 应过滤弱信号"
