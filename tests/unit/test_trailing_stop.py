"""移动止损引擎单元测试。"""
from __future__ import annotations

import pandas as pd
import pytest

from gugu.analysis.trailing_stop import (
    TrailingStopEngine,
    TrailingStopSignal,
    TrailingStopState,
)


def _df_from_close(prices: list[float]) -> pd.DataFrame:
    """从收盘价构造行情数据。"""
    return pd.DataFrame(
        {
            "open": [p * 0.99 for p in prices],
            "high": [p * 1.02 for p in prices],
            "low": [p * 0.98 for p in prices],
            "close": prices,
            "volume": [100000] * len(prices),
        }
    )


class TestTrailingStopEngine:
    """TrailingStopEngine 核心逻辑。"""

    def test_init_stop_fixed_pct(self):
        """固定比例初始化止损价。"""
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=100.0, initial_stop_pct=0.08)
        assert state.initial_stop_price == 92.0
        assert state.current_stop_price == 92.0
        assert state.highest_price == 100.0

    def test_init_stop_atr_based(self):
        """ATR 自适应初始化止损价。"""
        engine = TrailingStopEngine()
        # 创建波动数据使 ATR 可计算
        prices = [100.0 + i * 2 for i in range(20)]  # 稳定上涨
        df = _df_from_close(prices)
        state = engine.init_stop(
            entry_price=100.0,
            df=df,
            atr_multiplier=3.0,
        )
        # ATR 止损应低于入场价
        assert state.current_stop_price < 100.0
        assert state.current_stop_price > 80.0  # 不低于20%

    def test_init_stop_atr_too_small(self):
        """ATR 极小时使用最低 2% 止损保护。"""
        engine = TrailingStopEngine()
        # 构造 high=low=close 的平盘行情，ATR ≈ 0
        df = pd.DataFrame(
            {
                "open": [100.0] * 20,
                "high": [100.0] * 20,
                "low": [100.0] * 20,
                "close": [100.0] * 20,
                "volume": [100000] * 20,
            }
        )
        state = engine.init_stop(entry_price=100.0, df=df, atr_multiplier=3.0)
        # ATR = 0 → fallback 到固定比例 8%
        assert state.current_stop_price == 92.0

    def test_init_stop_fallback_no_df(self):
        """无 df 时回退到固定比例。"""
        engine = TrailingStopEngine()
        state = engine.init_stop(entry_price=100.0)
        assert state.current_stop_price == 92.0

    def test_update_hold_normal(self):
        """正常行情返回 HOLD。"""
        engine = TrailingStopEngine()
        state = TrailingStopState(
            initial_stop_price=92.0,
            current_stop_price=92.0,
            highest_price=100.0,
        )
        # 价格微涨
        prices = [100.0, 101.0, 102.0, 101.5, 103.0]
        df = _df_from_close(prices)
        new_state, signal = engine.update(state, df)
        assert signal == TrailingStopSignal.HOLD

    def test_update_exit_on_stop(self):
        """价格触及止损价返回 EXIT。"""
        engine = TrailingStopEngine()
        state = TrailingStopState(
            initial_stop_price=92.0,
            current_stop_price=92.0,
            highest_price=100.0,
            wave_valleys=[92.0],
        )
        # 价格跌破止损
        prices = [100.0, 95.0, 90.0]
        df = _df_from_close(prices)
        new_state, signal = engine.update(state, df)
        assert signal == TrailingStopSignal.EXIT

    def test_trailing_stop_moves_up(self):
        """价格上涨后止损价应上移。"""
        engine = TrailingStopEngine()
        state = TrailingStopState(
            initial_stop_price=92.0,
            current_stop_price=92.0,
            highest_price=100.0,
            wave_valleys=[92.0],
        )
        # 价格大幅上涨
        prices = [100.0, 105.0, 110.0, 108.0, 115.0]
        df = _df_from_close(prices)
        new_state, signal = engine.update(state, df)
        # 止损价应高于初始92
        assert new_state.current_stop_price > 92.0

    def test_state_serde(self):
        """状态序列化/反序列化。"""
        state = TrailingStopState(
            initial_stop_price=92.0,
            current_stop_price=95.0,
            highest_price=110.0,
            wave_valleys=[92.0, 98.0],
            valley_break_count=1,
            last_update="2024-01-01",
            last_signal="alert",
            tightened=False,
        )
        d = TrailingStopEngine.state_to_dict(state)
        restored = TrailingStopEngine.dict_to_state(d)
        assert restored.initial_stop_price == 92.0
        assert restored.current_stop_price == 95.0
        assert restored.valley_break_count == 1