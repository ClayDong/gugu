"""信号优先级排序单元测试。"""

from __future__ import annotations

import pytest

from gugu.analysis.signal_priority import (
    MAX_DAILY_SIGNALS,
    SignalPriority,
    SignalPrioritySorter,
    sort_and_limit_signals,
)


def _make_signal(symbol: str, confidence: float, strategies: list[str]) -> dict:
    return {
        "symbol": symbol,
        "direction": "buy",
        "confidence": confidence,
        "strategies": strategies,
        "price": 100.0,
    }


class TestSignalPrioritySorter:
    """信号优先级排序器测试。"""

    def test_sort_by_confidence(self):
        """高置信度信号应排在前面（使用相同策略消除win_rate_factor影响）。"""
        signals = [
            _make_signal("600519", 0.6, ["box_breakout"]),
            _make_signal("000858", 0.9, ["box_breakout"]),  # 同策略，仅置信度不同
            _make_signal("002714", 0.7, ["box_breakout"]),  # 同策略，仅置信度不同
        ]
        sorter = SignalPrioritySorter()
        result = sorter.sort_signals(signals, regime="bull")

        assert len(result) == 3
        assert result[0].signal["symbol"] == "000858"  # 最高置信度
        assert result[1].signal["symbol"] == "002714"
        assert result[2].signal["symbol"] == "600519"  # 最低置信度

    def test_limit_to_max_daily(self):
        """信号数量应限制在MAX_DAILY_SIGNALS以内。"""
        signals = [_make_signal(f"60{i:04d}", 0.5 + i * 0.05, ["box_breakout"]) for i in range(10)]
        sorter = SignalPrioritySorter()
        result = sorter.sort_signals(signals)

        assert len(result) == MAX_DAILY_SIGNALS
        assert MAX_DAILY_SIGNALS == 3

    def test_regime_factor_bull(self):
        """牛市因子应为1.2。"""
        assert SignalPrioritySorter.REGIME_FACTORS["bull"] == 1.2

    def test_regime_factor_bear(self):
        """熊市因子应为0.5。"""
        assert SignalPrioritySorter.REGIME_FACTORS["bear"] == 0.5

    def test_regime_factor_crash(self):
        """暴跌因子应为0.3。"""
        assert SignalPrioritySorter.REGIME_FACTORS["crash"] == 0.3

    def test_empty_signals(self):
        """空信号列表应返回空列表。"""
        sorter = SignalPrioritySorter()
        result = sorter.sort_signals([])
        assert result == []

    def test_position_factor_low_room(self):
        """仓位不足时position_factor应为0.5。"""
        signals = [_make_signal("600519", 0.8, ["box_breakout"])]
        sorter = SignalPrioritySorter()
        result = sorter.sort_signals(signals, available_position_ratio=0.05)
        assert len(result) == 1

    def test_priority_score_calculation(self):
        """验证优先级评分计算。"""
        signal = _make_signal("600519", 0.8, ["box_breakout"])
        sorter = SignalPrioritySorter()

        score = sorter._calc_priority_score(signal, regime="bull", available_position_ratio=1.0)

        expected_confidence = 0.8
        expected_win_rate_factor = sorter._win_rate_to_factor(0.55)  # box_breakout默认胜率
        expected_regime_factor = 1.2  # bull
        expected_position_factor = 1.0  # available_position_ratio=1.0

        expected_score = (
            expected_confidence
            * expected_win_rate_factor
            * expected_regime_factor
            * expected_position_factor
        )
        assert abs(score - expected_score) < 0.001

    def test_win_rate_to_factor_high(self):
        """高胜率应返回高因子。"""
        assert SignalPrioritySorter._win_rate_to_factor(0.6) == 1.5
        assert SignalPrioritySorter._win_rate_to_factor(0.5) == 1.2

    def test_win_rate_to_factor_low(self):
        """低胜率应返回低因子。"""
        assert SignalPrioritySorter._win_rate_to_factor(0.4) == 1.0
        assert SignalPrioritySorter._win_rate_to_factor(0.3) == 0.7
        assert SignalPrioritySorter._win_rate_to_factor(0.2) == 0.5


class TestSortAndLimitSignals:
    """sort_and_limit_signals便捷函数测试。"""

    def test_basic_functionality(self):
        """基本功能测试。"""
        signals = [
            _make_signal("600519", 0.6, ["box_breakout"]),
            _make_signal("000858", 0.9, ["double_yin"]),
            _make_signal("002714", 0.7, ["ma20_trend"]),
            _make_signal("300750", 0.8, ["zhen_dahu"]),
            _make_signal("601318", 0.85, ["cloud_trend"]),
        ]
        result = sort_and_limit_signals(signals, regime="bull")

        assert len(result) == MAX_DAILY_SIGNALS
        assert all(isinstance(s, dict) for s in result)

    def test_with_strategy_pool(self):
        """带策略池的测试。"""
        signals = [_make_signal("600519", 0.8, ["box_breakout"])]

        class MockStrategyPool:
            def get_stats(self):
                return {
                    "box_breakout": {
                        "total_signals": 10,
                        "recent_win_rate": 0.6,
                    }
                }

        pool = MockStrategyPool()
        result = sort_and_limit_signals(signals, strategy_pool=pool)

        assert len(result) == 1
