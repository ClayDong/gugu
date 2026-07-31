"""断板反包策略单元测试（陈学长不吃面交易策略）。

覆盖核心场景：
- 选股6条件验证
- 买点3个验证
- 卖点5个验证
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


def _inject_limit_up_wash_protection(
    df: pd.DataFrame,
    limit_up_idx: int,
    wash_idx: int,
    protection_idx: int,
    limit_up_open: float = 105.0,
    limit_up_close: float = 115.5,  # 涨幅 ~10%
    wash_drop_pct: float = -0.05,  # 洗盘跌幅 5%
    wash_vol_ratio: float = 2.0,  # 洗盘放量
    protection_vol_ratio: float = 0.4,  # 护盘缩量
    base_vol: float = 8_000_000,
) -> pd.DataFrame:
    """注入涨停→洗盘→护盘形态（陈学长不吃面策略核心周期）。

    Args:
        limit_up_idx: 涨停日位置
        wash_idx: 洗盘日位置（涨停次日）
        protection_idx: 护盘日位置（洗盘次日）
        limit_up_open: 涨停日开盘价
        limit_up_close: 涨停日收盘价
        wash_drop_pct: 洗盘日跌幅（负值）
        wash_vol_ratio: 洗盘日量比
        protection_vol_ratio: 护盘日量比（缩量）
        base_vol: 基础成交量
    """
    df = df.copy()

    # 先把前面几天的 volume 设为 base_vol，确保量比计算正常
    for i in range(max(0, limit_up_idx - 10), limit_up_idx):
        df.loc[i, "volume"] = base_vol

    # 设置涨停日前一天的收盘价（确保涨幅 >= 9.5%）
    # daily_return = (close - prev_close) / prev_close >= 0.095
    # prev_close = close / 1.095
    prev_close = limit_up_close / 1.095
    df.loc[limit_up_idx - 1, "close"] = prev_close

    # ── 涨停日 ──
    df.loc[limit_up_idx, "open"] = limit_up_open
    df.loc[limit_up_idx, "close"] = limit_up_close
    df.loc[limit_up_idx, "high"] = limit_up_close * 1.005
    df.loc[limit_up_idx, "low"] = limit_up_open * 0.995
    df.loc[limit_up_idx, "volume"] = base_vol * 1.5  # 涨停日放量

    # ── 洗盘日（放量大阴线）──
    wash_open = limit_up_close * 0.98
    wash_close = limit_up_close * (1 + wash_drop_pct)
    df.loc[wash_idx, "open"] = wash_open
    df.loc[wash_idx, "close"] = wash_close
    df.loc[wash_idx, "high"] = wash_open * 1.01
    df.loc[wash_idx, "low"] = wash_close * 0.98
    df.loc[wash_idx, "volume"] = base_vol * wash_vol_ratio  # 洗盘放量

    # ── 护盘日（缩量回踩支撑位不破）──
    # 支撑位 = max(涨停开盘价, MA20)
    # 护盘日：价格接近支撑位但不破，缩量
    support_level = limit_up_open  # 简化：用涨停开盘价作为支撑
    prot_open = wash_close * 0.99
    prot_close = wash_close * 1.005  # 小阳线
    prot_low = support_level * 0.995  # 接近支撑但不破
    df.loc[protection_idx, "open"] = prot_open
    df.loc[protection_idx, "close"] = prot_close
    df.loc[protection_idx, "high"] = prot_close * 1.01
    df.loc[protection_idx, "low"] = max(prot_low, wash_close * 0.97)
    df.loc[protection_idx, "volume"] = base_vol * protection_vol_ratio  # 缩量

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

    def test_validate_params_wash_day_max_zero_raises(self):
        """wash_day_max < 1 应抛错。"""
        with pytest.raises(ValueError, match="wash_day_max"):
            BreakoutReboundStrategy({"wash_day_max": 0}).validate_params()


class TestBreakoutReboundSignals:
    """信号生成基础测试。"""

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


class TestBreakoutReboundSelectionConditions:
    """选股6条件验证测试。"""

    def test_condition1_position_low_or_mid(self):
        """条件1：位置必须在相对低位或中途。"""
        df = _make_df(days=80, seed=42)
        # 在 index=50 注入涨停（低位首板）
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 验证涨停日被正确识别
        assert out.loc[50, "is_limit_up"] == True, "涨停日应被识别"

    def test_condition2_limit_up_shrink_vol(self):
        """条件2：首板必须是缩量首板（量不能太大）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
            base_vol=8_000_000,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 涨停日量比应合理（不是异常大）
        assert out.loc[50, "vol_ratio"] < 3.0, "首板量比不应过大"

    def test_condition3_wash_day_big_yin(self):
        """条件3：首板后必须接放量大阴线洗盘。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
            wash_drop_pct=-0.05,
            wash_vol_ratio=2.0,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 洗盘日应被识别
        assert out.loc[51, "is_wash_day"] == True, "洗盘日应被识别"

    def test_condition4_protection_not_break_support(self):
        """条件4：护盘不能破关键支撑位。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
            limit_up_open=105.0,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 护盘日应接近支撑位
        assert out.loc[52, "near_support"] == True, "护盘日应接近支撑位"

    def test_condition6_no_st_no_gem(self):
        """条件6：不做ST、不做创业板（预过滤器层面测试）。"""
        from gugu.filters.breakout_rebound_prefilter import BreakoutReboundPrefilter

        prefilter = BreakoutReboundPrefilter()

        # ST股票应被排除
        result_st = prefilter.check("600519", name="ST某某", price=100.0, market_cap=100e8)
        assert result_st["pass"] == False, "ST股票应被排除"
        assert result_st["data"]["is_st"] == True

        # 创业板应被排除
        result_gem = prefilter.check("300001", name="某某科技", price=50.0, market_cap=100e8)
        assert result_gem["pass"] == False, "创业板应被排除"
        assert result_gem["data"]["is_gem"] == True


class TestBreakoutReboundBuySignals:
    """买点3个验证测试。"""

    def test_buy1_support_bounce_shrink_vol(self):
        """买点1：护盘阶段缩量回踩支撑位。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
            protection_vol_ratio=0.4,  # 缩量
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 护盘日应产生买入信号（缩量回踩支撑位）
        assert out.loc[52, "signal"] == 1, "护盘缩量回踩应触发买入"

    def test_buy2_bottom_divergence(self):
        """买点2：底背离信号（MACD底背离 + 缩量）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        # 构造底背离：价格创新低但MACD不创新低
        # 在护盘日后几天继续下跌但MACD回升
        df.loc[53, "low"] = df.loc[52, "low"] * 0.98  # 价格创新低
        df.loc[53, "close"] = df.loc[53, "low"] * 1.01
        df.loc[53, "volume"] = 3_000_000  # 缩量
        df.loc[53, "open"] = df.loc[53, "close"] * 0.99
        df.loc[53, "high"] = df.loc[53, "close"] * 1.02

        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 验证底背离检测逻辑存在
        assert "bottom_divergence" in out.columns

    def test_buy3_effective_volume_pullback(self):
        """买点3：有效放量后的回踩。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        # 在护盘日前注入有效放量
        df.loc[49, "volume"] = 20_000_000  # 有效放量
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 验证有效放量检测逻辑存在
        assert "effective_volume" in out.columns


class TestBreakoutReboundSellSignals:
    """卖点5个验证测试。"""

    def test_sell1_rebound_success_reduce(self):
        """卖点1：反包成功→至少减仓一半。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 验证反包成功检测逻辑存在
        assert "rebound_success" in out.columns

    def test_sell2_top_divergence(self):
        """卖点2：顶背离信号（价创新高、MACD不创新高）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 验证顶背离检测逻辑存在
        assert "top_divergence" in out.columns

    def test_sell3_volume_price_divergence(self):
        """卖点3：量价背离（价格创新高但量能萎缩）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 验证量价背离检测逻辑存在
        assert "vol_shrink" in out.columns

    def test_sell4_stop_loss_below_support(self):
        """卖点4：破位止损（跌破入场点 low × 0.97）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)

        # 如果护盘日有买入信号，后续跌破应触发卖出
        if out.loc[52, "signal"] == 1:
            # 在护盘日后注入大跌
            entry_low = df.loc[52, "low"]
            df.loc[53, "close"] = entry_low * 0.95  # 跌破止损线
            df.loc[53, "open"] = entry_low * 0.98
            df.loc[53, "high"] = entry_low * 0.99
            df.loc[53, "low"] = entry_low * 0.94
            out = strat.generate_signals(df)
            assert out.loc[53, "signal"] == -1, "跌破止损线应触发卖出"

    def test_sell5_underperform_next_day(self):
        """卖点5：不及预期（次日绿开/无拉升）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 验证连续下跌检测逻辑存在
        assert "consecutive_decline" in out.columns


class TestBreakoutReboundRiskManagement:
    """风控规则测试。"""

    def test_stop_loss_pct_trigger(self):
        """止损百分比触发测试。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy({"stop_loss_pct": 0.03})
        out = strat.generate_signals(df)
        # 验证止损逻辑存在
        assert "entry_low" in out.columns

    def test_take_profit_big_yang(self):
        """止盈：大阳线冲高减仓。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy({"take_profit_pct": 0.08})
        out = strat.generate_signals(df)
        # 验证大阳线检测逻辑存在
        assert "big_yang" in out.columns

    def test_stagnation_high_vol_long_upper_shadow(self):
        """巨量滞涨：量比>=2.5 + 长上影>2% + 涨幅小。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        # 注入巨量滞涨
        df.loc[53, "volume"] = 30_000_000  # 巨量
        df.loc[53, "open"] = 120.0
        df.loc[53, "close"] = 120.5  # 涨幅小
        df.loc[53, "high"] = 124.0  # 长上影 > 2%
        df.loc[53, "low"] = 119.5
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 验证滞涨检测逻辑存在
        assert "stagnation" in out.columns

    def test_time_stop_window(self):
        """时间止损：持仓超期 + 涨幅不足。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy({"time_stop_window": 5, "time_stop_threshold": 0.03})
        out = strat.generate_signals(df)
        # 验证时间止损逻辑存在
        assert "days_since_entry" in out.columns


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
        # 在 index=50 涨停，51 洗盘，但护盘在 56（超出 5 日窗口）
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=56,
        )
        strat = BreakoutReboundStrategy({"limit_up_lookback": 5})
        out = strat.generate_signals(df)
        # 超出窗口的护盘不应触发买入
        assert out.loc[56, "signal"] != 1, "超出 lookback 窗口不应触发买入"

    def test_registry_integration(self):
        """策略应能在 registry 中获取。"""
        from gugu.strategies.registry import get_strategy, list_strategies

        assert "breakout_rebound" in list_strategies()
        strat = get_strategy("breakout_rebound")
        assert strat.name == "breakout_rebound"

    def test_backtest_mode_skips_confirmation(self):
        """回测模式应跳过确认（is_backtest=True）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy({"confirm_days": 1})
        out = strat.generate_signals(df, is_backtest=True)
        # 回测模式不应报错
        assert "signal" in out.columns

    def test_sell_overrides_buy(self):
        """卖出信号应覆盖买入信号（持仓状态判断）。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        # 第52天应有买入信号（护盘缩量回踩）
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
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy({"min_confidence": 0.99})
        out = strat.generate_signals(df)
        # 高 min_confidence 应过滤弱信号
        assert out.loc[52, "signal"] != 1, "高 min_confidence 应过滤弱信号"

    def test_confidence_score_range(self):
        """置信度评分应在 [0, 1] 范围内。"""
        df = _make_df(days=80, seed=42)
        df = _inject_limit_up_wash_protection(
            df,
            limit_up_idx=50,
            wash_idx=51,
            protection_idx=52,
        )
        strat = BreakoutReboundStrategy()
        out = strat.generate_signals(df)
        valid_scores = out["confidence_score"].dropna()
        assert (valid_scores >= 0).all(), "置信度应 >= 0"
        assert (valid_scores <= 1).all(), "置信度应 <= 1"
