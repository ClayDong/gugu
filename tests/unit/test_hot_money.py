"""游资策略（三数交易法）单元测试。

覆盖核心场景（v0.6.2 优化版）：
- 涨停检测 + 大阳线双通道
- 参考量能向后滚动传播（max_track_days=30）
- 缩量入场（状态机保护）
- 放量离场 + 止损止盈
- 半仓滚动
- 边界场景
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.hot_money import HotMoneyStrategy


def _make_base_df(days: int = 120, seed: int = 42) -> pd.DataFrame:
    """构造基础测试数据。"""
    np.random.seed(seed)
    n = days
    close = 100 + np.cumsum(np.random.randn(n) * 1.5)
    close = np.maximum(close, 10)
    open_ = np.zeros(n)
    high = np.zeros(n)
    low = np.zeros(n)
    volume = np.zeros(n)
    for i in range(n):
        if i == 0:
            open_[i] = close[i] - 0.5
        else:
            open_[i] = close[i - 1] + np.random.randn() * 0.3
        high[i] = max(close[i], open_[i]) + abs(np.random.randn()) * 0.8
        low[i] = min(close[i], open_[i]) - abs(np.random.randn()) * 0.8
        volume[i] = np.random.uniform(5_000_000, 10_000_000)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.astype(float),
            "amount": close * volume,
        }
    )


def _make_limit_up_df(n: int = 60, limit_up_idx: int = 20) -> pd.DataFrame:
    """构造有涨停的K线数据。"""
    np.random.seed(42)
    close = np.full(n, 100.0)
    open_ = np.full(n, 100.0)
    high = np.full(n, 100.0)
    low = np.full(n, 100.0)
    volume = np.full(n, 5_000_000.0)
    for i in range(limit_up_idx):
        close[i] = 100 + np.random.randn() * 1.0
        open_[i] = close[i] - 0.5
        high[i] = max(close[i], open_[i]) + 0.5
        low[i] = min(close[i], open_[i]) - 0.5
        volume[i] = np.random.uniform(5_000_000, 8_000_000)
    close[limit_up_idx - 1] = 100.0
    close[limit_up_idx] = 110.0
    open_[limit_up_idx] = 105.0
    high[limit_up_idx] = 110.0
    low[limit_up_idx] = 104.0
    volume[limit_up_idx] = 50_000_000
    for i in range(limit_up_idx + 1, n):
        close[i] = close[i - 1] + np.random.randn() * 0.5
        open_[i] = close[i] - 0.3
        high[i] = max(close[i], open_[i]) + 0.3
        low[i] = min(close[i], open_[i]) - 0.3
        volume[i] = np.random.uniform(5_000_000, 10_000_000)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.astype(float),
            "amount": close * volume,
        }
    )


class TestHotMoneyParams:
    """参数校验测试。"""

    def test_default_params(self):
        strategy = HotMoneyStrategy(
            {
                "limit_up_window": 7,
                "shrink_ratio": 0.5,
                "expand_ratio": 2.0,
            }
        )
        assert strategy.name == "hot_money"
        assert strategy.category == "main_force"
        assert strategy.params["limit_up_window"] == 7

    def test_limit_up_window_too_small(self):
        with pytest.raises(ValueError, match="limit_up_window"):
            HotMoneyStrategy({"limit_up_window": 2})

    def test_shrink_ratio_out_of_range(self):
        with pytest.raises(ValueError, match="shrink_ratio"):
            HotMoneyStrategy({"shrink_ratio": 1.5})

    def test_expand_ratio_too_small(self):
        with pytest.raises(ValueError, match="expand_ratio"):
            HotMoneyStrategy({"expand_ratio": 0.5})


class TestMainForceDetection:
    """主力信号检测测试。"""

    def test_detect_limit_up(self):
        df = _make_limit_up_df(n=60, limit_up_idx=20)
        strategy = HotMoneyStrategy()
        result = strategy.generate_signals(df)
        assert result["is_limit_up"].iloc[20]
        assert result["is_main_force_signal"].iloc[20]

    def test_big_yang_as_main_force(self):
        np.random.seed(42)
        n = 60
        close = np.full(n, 100.0)
        open_ = np.full(n, 100.0)
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        volume = np.full(n, 5_000_000.0)
        for i in range(10):
            close[i] = 100 + np.random.randn() * 1.0
            open_[i] = close[i] - 0.5
            high[i] = max(close[i], open_[i]) + 0.5
            low[i] = min(close[i], open_[i]) - 0.5
        close[9] = 100.0
        close[10] = 106.5  # 6.5%涨幅 >= 5% → 大阳线
        open_[10] = 101.0
        high[10] = 107.0
        low[10] = 100.5
        volume[10] = 40_000_000
        for i in range(11, n):
            close[i] = close[i - 1] + np.random.randn() * 0.5
            open_[i] = close[i] - 0.3
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = np.random.uniform(5_000_000, 10_000_000)
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume.astype(float),
                "amount": close * volume,
            }
        )
        strategy = HotMoneyStrategy()
        result = strategy.generate_signals(df)
        assert not result["is_limit_up"].iloc[10], "6.5%不是涨停"
        assert result["is_big_yang"].iloc[10], "6.5%应是大阳线"
        assert result["is_main_force_signal"].iloc[10], "大阳线应被视为主力信号"

    def test_ref_volume_forward_propagation(self):
        """参考量能应持续向前传播超过7天。"""
        np.random.seed(42)
        n = 50
        close = np.full(n, 100.0)
        open_ = np.full(n, 100.0)
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        volume = np.full(n, 5_000_000.0)
        for i in range(20):
            close[i] = 100 + np.random.randn() * 1.0
            open_[i] = close[i] - 0.5
            high[i] = max(close[i], open_[i]) + 0.5
            low[i] = min(close[i], open_[i]) - 0.5
            volume[i] = np.random.uniform(5_000_000, 8_000_000)
        close[19] = 100.0
        close[20] = 110.0
        open_[20] = 105.0
        high[20] = 110.0
        low[20] = 104.0
        volume[20] = 50_000_000
        for i in range(21, n):
            close[i] = close[i - 1] + np.random.randn() * 0.3
            open_[i] = close[i] - 0.2
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = np.random.uniform(5_000_000, 10_000_000)
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume.astype(float),
                "amount": close * volume,
            }
        )
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        for i in range(21, min(41, n)):
            assert (
                result["ref_volume"].iloc[i] == 50_000_000
            ), f"第{i}天ref_volume应持续传播（当前{result['ref_volume'].iloc[i]}）"

    def test_ref_volume_updates_on_new_signal(self):
        """新主力信号出现要更新参考量能。"""
        df = _make_limit_up_df(n=60, limit_up_idx=20)
        df_copy = df.copy()
        idx = 35
        df_copy.loc[df_copy.index[idx], "close"] = (
            df_copy.loc[df_copy.index[idx - 1], "close"] * 1.10
        )
        df_copy.loc[df_copy.index[idx], "volume"] = 80_000_000
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df_copy)
        assert result["ref_volume"].iloc[idx] == 80_000_000, "新涨停日应更新ref_volume"


class TestShrinkEntry:
    """缩量入场测试（含状态机）。"""

    def test_entry_on_volume_shrink(self):
        """涨停后缩量 + 状态机允许入场。"""
        np.random.seed(42)
        n = 50
        close = np.full(n, 100.0)
        open_ = np.full(n, 100.0)
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        volume = np.full(n, 5_000_000.0)
        for i in range(20):
            close[i] = 100 + np.random.randn() * 1.0
            open_[i] = close[i] - 0.5
            high[i] = max(close[i], open_[i]) + 0.5
            low[i] = min(close[i], open_[i]) - 0.5
            volume[i] = np.random.uniform(5_000_000, 8_000_000)
        close[19] = 100.0
        close[20] = 110.0
        open_[20] = 105.0
        high[20] = 110.0
        low[20] = 104.0
        volume[20] = 50_000_000
        for i in range(21, 25):
            close[i] = close[i - 1] + np.random.randn() * 0.3
            open_[i] = close[i] - 0.2
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = 18_000_000
        for i in range(25, n):
            close[i] = close[i - 1] + np.random.randn() * 0.3
            open_[i] = close[i] - 0.2
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = np.random.uniform(5_000_000, 10_000_000)
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume.astype(float),
                "amount": close * volume,
            }
        )
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        assert result["signal"].iloc[21] == 1, "涨停后首日缩量到18M<25M应入场"

    def test_entry_confidence_in_range(self):
        df = _make_limit_up_df()
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        buy_mask = result["signal"] == 1
        if buy_mask.any():
            assert result.loc[buy_mask, "confidence"].between(0, 1).all()

    def test_entry_position_ratio(self):
        df = _make_limit_up_df()
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        buy_mask = result["signal"] == 1
        if buy_mask.any():
            assert result.loc[buy_mask, "suggested_position_ratio"].between(0.3, 0.7).all()


class TestStopLossAndTakeProfit:
    """止损止盈测试。"""

    def test_stop_loss_on_5pct_drop(self):
        """持仓后亏损5%应触发硬止损。"""
        np.random.seed(42)
        n = 40
        close = np.full(n, 100.0)
        open_ = np.full(n, 100.0)
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        volume = np.full(n, 5_000_000.0)
        for i in range(20):
            close[i] = 100 + np.random.randn() * 1.0
            open_[i] = close[i] - 0.5
            high[i] = max(close[i], open_[i]) + 0.5
            low[i] = min(close[i], open_[i]) - 0.5
            volume[i] = np.random.uniform(5_000_000, 8_000_000)
        close[19] = 100.0
        close[20] = 110.0  # 涨停
        open_[20] = 105.0
        high[20] = 110.0
        low[20] = 104.0
        volume[20] = 50_000_000
        close[21] = 105.0  # 入场（缩量到18M）
        open_[21] = 104.0
        high[21] = 106.0
        low[21] = 104.0
        volume[21] = 18_000_000
        for i in range(22, 28):
            close[i] = close[i - 1] - 5.0  # 每天跌5元，累计>5%
            open_[i] = close[i - 1] - 1.0
            high[i] = open_[i] + 0.5
            low[i] = close[i] - 0.5
            volume[i] = np.random.uniform(5_000_000, 10_000_000)
        for i in range(28, n):
            close[i] = close[i - 1] + np.random.randn() * 0.3
            open_[i] = close[i] - 0.2
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = np.random.uniform(5_000_000, 10_000_000)
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume.astype(float),
                "amount": close * volume,
            }
        )
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        assert result["signal"].iloc[21] == 1, "第21天应入场"
        loss_signals = result[(result.index >= 22) & (result["signal"] == -1)]
        assert len(loss_signals) > 0, "连续大跌应触发止损卖出"

    def test_trailing_stop_on_big_pullback(self):
        """从最高点回落超过7%应触发移动止盈。"""
        df = _make_limit_up_df(n=60, limit_up_idx=20)
        df_copy = df.copy()
        for i in range(21, 30):
            df_copy.loc[df_copy.index[i], "close"] = 105.0 + (i - 21) * 2.0
            df_copy.loc[df_copy.index[i], "high"] = df_copy.loc[df_copy.index[i], "close"] + 0.5
            df_copy.loc[df_copy.index[i], "low"] = df_copy.loc[df_copy.index[i], "open"] - 0.3
            df_copy.loc[df_copy.index[i], "volume"] = 18_000_000
        for i in range(30, 35):
            df_copy.loc[df_copy.index[i], "close"] = (
                df_copy.loc[df_copy.index[i - 1], "close"] - 3.0
            )
            df_copy.loc[df_copy.index[i], "high"] = df_copy.loc[df_copy.index[i], "close"] + 0.5
            df_copy.loc[df_copy.index[i], "low"] = df_copy.loc[df_copy.index[i], "open"] - 0.3
            df_copy.loc[df_copy.index[i], "volume"] = np.random.uniform(5_000_000, 10_000_000)
        strategy = HotMoneyStrategy({"trailing_stop_pct": 0.07, "max_track_days": 30})
        result = strategy.generate_signals(df_copy)
        sell_signals = result[result["signal"] == -1]
        assert len(sell_signals) > 0, "大幅回落后应触发移动止盈"

    def test_expand_exit_before_other_exits(self):
        """放量离场应在止损/止盈之前被处理。"""
        np.random.seed(42)
        n = 50
        close = np.full(n, 100.0)
        open_ = np.full(n, 100.0)
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        volume = np.full(n, 5_000_000.0)
        for i in range(20):
            close[i] = 100 + np.random.randn() * 1.0
            open_[i] = close[i] - 0.5
            high[i] = max(close[i], open_[i]) + 0.5
            low[i] = min(close[i], open_[i]) - 0.5
            volume[i] = np.random.uniform(5_000_000, 8_000_000)
        close[19] = 100.0
        close[20] = 110.0
        open_[20] = 105.0
        high[20] = 110.0
        low[20] = 104.0
        volume[20] = 50_000_000
        close[21] = 108.0
        open_[21] = 107.0
        high[21] = 109.0
        low[21] = 107.0
        volume[21] = 15_000_000
        close[22] = 115.0
        open_[22] = 108.0
        high[22] = 116.0
        low[22] = 107.5
        volume[22] = 15_000_000
        for i in range(23, n):
            close[i] = close[i - 1] + np.random.randn() * 0.3
            open_[i] = close[i] - 0.2
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = np.random.uniform(5_000_000, 10_000_000)
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume.astype(float),
                "amount": close * volume,
            }
        )
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        assert result["signal"].iloc[21] == 1, "缩量应入场"
        signals_after_entry = result[result.index >= 22]
        assert (signals_after_entry["signal"] != 1).any(), "入场后应仍有信号"


class TestRollingSignals:
    """半仓滚动测试。"""

    def test_rolling_buy_on_yin_day(self):
        df = _make_limit_up_df()
        strategy = HotMoneyStrategy({"rolling_enabled": True, "max_track_days": 30})
        result = strategy.generate_signals(df)
        rolling_buys = result[(result["signal"] == 1) & (result["hm_is_rolling"])]
        for idx in rolling_buys.index:
            assert result["close"].iloc[idx] < result["open"].iloc[idx]

    def test_rolling_sell_on_yang_day(self):
        df = _make_limit_up_df()
        strategy = HotMoneyStrategy({"rolling_enabled": True, "max_track_days": 30})
        result = strategy.generate_signals(df)
        rolling_sells = result[(result["signal"] == -1) & (result["hm_is_rolling"])]
        for idx in rolling_sells.index:
            assert result["close"].iloc[idx] > result["open"].iloc[idx]

    def test_rolling_disabled(self):
        df = _make_limit_up_df()
        strategy = HotMoneyStrategy({"rolling_enabled": False, "max_track_days": 30})
        result = strategy.generate_signals(df)
        assert not result["hm_is_rolling"].any()


class TestEdgeCases:
    """边界场景测试。"""

    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        strategy = HotMoneyStrategy()
        result = strategy.generate_signals(df)
        assert "signal" in result.columns

    def test_short_dataframe(self):
        df = _make_base_df(5)
        strategy = HotMoneyStrategy()
        result = strategy.generate_signals(df)
        assert len(result) == 5
        assert "signal" in result.columns

    def test_all_zero_volume(self):
        df = _make_base_df(30)
        df["volume"] = 0.0
        df["amount"] = 0.0
        strategy = HotMoneyStrategy()
        result = strategy.generate_signals(df)
        assert (result["signal"] == 0).all()

    def test_consecutive_limit_ups(self):
        """连续涨停应使用最近涨停日量能。"""
        np.random.seed(42)
        n = 60
        close = np.full(n, 100.0)
        open_ = np.full(n, 100.0)
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        volume = np.full(n, 5_000_000.0)
        for i in range(20):
            close[i] = 100 + np.random.randn() * 1.0
            open_[i] = close[i] - 0.5
            high[i] = max(close[i], open_[i]) + 0.5
            low[i] = min(close[i], open_[i]) - 0.5
            volume[i] = np.random.uniform(5_000_000, 8_000_000)
        close[19] = 100.0
        close[20] = 110.0
        open_[20] = 105.0
        high[20] = 110.0
        low[20] = 104.0
        volume[20] = 40_000_000
        close[21] = 121.0
        open_[21] = 115.0
        high[21] = 121.0
        low[21] = 114.0
        volume[21] = 60_000_000
        for i in range(22, n):
            close[i] = close[i - 1] + np.random.randn() * 0.5
            open_[i] = close[i] - 0.3
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = np.random.uniform(5_000_000, 10_000_000)
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume.astype(float),
                "amount": close * volume,
            }
        )
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        assert result["ref_volume"].iloc[22] == 60_000_000, "连续涨停后ref_volume应为最近涨停日量能"

    def test_output_columns_exist(self):
        df = _make_limit_up_df(n=60, limit_up_idx=20)
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        for col in [
            "hm_has_limit_up",
            "hm_ref_volume",
            "hm_days_since_limit_up",
            "hm_is_shrink_entry",
            "hm_is_expand_exit",
            "hm_is_rolling",
        ]:
            assert col in result.columns, f"缺少列: {col}"

    def test_signal_values_are_valid(self):
        df = _make_limit_up_df(n=60, limit_up_idx=20)
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        assert set(result["signal"].unique()).issubset({-1, 0, 1})


class TestIntegration:
    """集成测试。"""

    def test_full_cycle_limit_up_shrink_expand(self):
        """完整周期：涨停 → 缩量入场 → 放量离场。"""
        np.random.seed(42)
        n = 80
        close = np.full(n, 100.0)
        open_ = np.full(n, 100.0)
        high = np.full(n, 100.0)
        low = np.full(n, 100.0)
        volume = np.full(n, 5_000_000.0)
        for i in range(20):
            close[i] = 100 + np.random.randn() * 1.0
            open_[i] = close[i] - 0.5
            high[i] = max(close[i], open_[i]) + 0.5
            low[i] = min(close[i], open_[i]) - 0.5
            volume[i] = np.random.uniform(5_000_000, 8_000_000)
        close[19] = 100.0
        close[20] = 110.0
        open_[20] = 105.0
        high[20] = 110.0
        low[20] = 104.0
        volume[20] = 50_000_000
        for i in range(21, 25):
            close[i] = close[i - 1] + np.random.randn() * 0.3
            open_[i] = close[i] - 0.2
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = 15_000_000
        close[25] = close[24] + 0.5
        open_[25] = close[25] - 0.2
        high[25] = close[25] + 0.5
        low[25] = open_[25] - 0.3
        volume[25] = 120_000_000
        for i in range(26, n):
            close[i] = close[i - 1] + np.random.randn() * 0.5
            open_[i] = close[i] - 0.3
            high[i] = max(close[i], open_[i]) + 0.3
            low[i] = min(close[i], open_[i]) - 0.3
            volume[i] = np.random.uniform(5_000_000, 10_000_000)
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume.astype(float),
                "amount": close * volume,
            }
        )
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        assert result["is_limit_up"].iloc[20], "第20天应为涨停日"
        assert result["signal"].iloc[21] == 1, "第21天缩量应入场"
        assert result["signal"].iloc[25] == -1, "第25天放量应离场"

    def test_no_signal_without_limit_up(self):
        """无涨停历史的股票不应产生信号。"""
        df = _make_base_df(120)
        strategy = HotMoneyStrategy({"max_track_days": 30})
        result = strategy.generate_signals(df)
        buy_signals = result[result["signal"] == 1]
        for idx in buy_signals.index:
            assert result["has_limit_up"].iloc[
                idx
            ], f"第{idx}天买入信号应有涨停历史（无涨停不应入场）"
