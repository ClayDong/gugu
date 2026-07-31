"""市场状态自适应策略权重调整器测试。"""

from __future__ import annotations

import pytest

from gugu.analysis.regime_strategy_adjuster import (
    REGIME_MULTIPLIERS,
    STRATEGY_CATEGORIES,
    compute_regime_weights,
    get_regime_strategy_hint,
    get_strategy_category,
)


class TestGetStrategyCategory:
    """测试策略分类识别。"""

    def test_trend_strategies(self):
        """趋势策略应正确分类。"""
        assert get_strategy_category("turtle") == "trend"
        assert get_strategy_category("dual_ma") == "trend"
        assert get_strategy_category("macd") == "trend"
        # P0 修复：sar 策略从未实现，替换为真实存在的 chen_jiangting
        assert get_strategy_category("chen_jiangting") == "trend"

    def test_mean_revert_strategies(self):
        """均值回归策略应正确分类。"""
        assert get_strategy_category("rsi_reversal") == "mean_revert"
        assert get_strategy_category("bollinger") == "mean_revert"
        assert get_strategy_category("kdj") == "mean_revert"

    def test_breakout_strategies(self):
        """突破策略应正确分类。"""
        assert get_strategy_category("box_breakout") == "breakout"
        assert get_strategy_category("dual_thrust") == "breakout"

    def test_unknown_strategy(self):
        """未知策略应返回 unknown。"""
        assert get_strategy_category("unknown_strategy") == "unknown"
        assert get_strategy_category("") == "unknown"


class TestComputeRegimeWeights:
    """测试市场状态权重计算。"""

    def test_bull_market_boosts_trend(self):
        """牛市应提升趋势策略权重。"""
        base_weights = {
            "turtle": 0.25,
            "dual_ma": 0.25,
            "rsi_reversal": 0.25,
            "bollinger": 0.25,
        }
        adjusted = compute_regime_weights(base_weights, "bull")

        # 趋势策略权重应提升
        assert adjusted["turtle"] > base_weights["turtle"]
        assert adjusted["dual_ma"] > base_weights["dual_ma"]
        # 均值回归策略权重应降低
        assert adjusted["rsi_reversal"] < base_weights["rsi_reversal"]
        assert adjusted["bollinger"] < base_weights["bollinger"]

    def test_sideways_market_boosts_mean_revert(self):
        """震荡市应提升均值回归策略权重。"""
        base_weights = {
            "turtle": 0.25,
            "dual_ma": 0.25,
            "rsi_reversal": 0.25,
            "bollinger": 0.25,
        }
        adjusted = compute_regime_weights(base_weights, "sideways")

        # 均值回归策略权重应提升
        assert adjusted["rsi_reversal"] > base_weights["rsi_reversal"]
        assert adjusted["bollinger"] > base_weights["bollinger"]
        # 趋势策略权重应降低
        assert adjusted["turtle"] < base_weights["turtle"]
        assert adjusted["dual_ma"] < base_weights["dual_ma"]

    def test_crash_market_reduces_all(self):
        """暴跌市所有策略权重乘数应最低。"""
        base_weights = {"turtle": 0.5, "rsi_reversal": 0.3, "box_breakout": 0.2}
        raw_adjusted = {
            k: base_weights[k] * REGIME_MULTIPLIERS["crash"].get(get_strategy_category(k), 1.0)
            for k in base_weights
        }

        # 不归一化的原始权重应全部低于原始值
        for name in base_weights:
            assert (
                raw_adjusted[name] < base_weights[name]
            ), f"{name}: {raw_adjusted[name]} >= {base_weights[name]}"

        # 趋势策略乘数最低（0.2），原始权重应降低最多
        trend_ratio = raw_adjusted["turtle"] / base_weights["turtle"]
        mean_revert_ratio = raw_adjusted["rsi_reversal"] / base_weights["rsi_reversal"]
        assert trend_ratio < mean_revert_ratio

    def test_rally_market_boosts_breakout(self):
        """反弹市应提升突破策略权重。"""
        base_weights = {
            "box_breakout": 0.2,
            "dual_thrust": 0.2,
            "turtle": 0.2,
            "rsi_reversal": 0.2,
            "bollinger": 0.2,
        }
        adjusted = compute_regime_weights(base_weights, "rally")

        # 突破策略权重应提升
        assert adjusted["box_breakout"] > base_weights["box_breakout"]
        assert adjusted["dual_thrust"] > base_weights["dual_thrust"]

    def test_weights_normalized(self):
        """调整后权重应归一化（总和为 1）。"""
        base_weights = {
            "turtle": 0.1,
            "dual_ma": 0.2,
            "rsi_reversal": 0.3,
            "bollinger": 0.4,
        }
        for regime in ["bull", "bear", "sideways", "crash", "rally"]:
            adjusted = compute_regime_weights(base_weights, regime)
            total = sum(adjusted.values())
            assert abs(total - 1.0) < 0.001, f"regime={regime} total={total}"

    def test_unknown_regime_returns_original(self):
        """未知市场状态应返回原始权重。"""
        base_weights = {"turtle": 0.5, "rsi_reversal": 0.5}
        adjusted = compute_regime_weights(base_weights, "unknown_regime")
        assert adjusted == base_weights

    def test_empty_weights(self):
        """空权重字典应返回空。"""
        adjusted = compute_regime_weights({}, "bull")
        assert adjusted == {}

    def test_unknown_strategy_gets_default_multiplier(self):
        """未知分类策略应使用默认乘数 1.0。"""
        base_weights = {"unknown_strat": 0.5, "turtle": 0.5}
        adjusted = compute_regime_weights(base_weights, "bull")
        # unknown_strat 权重不变（乘数 1.0），turtle 权重提升
        assert adjusted["turtle"] > adjusted["unknown_strat"]


class TestGetRegimeStrategyHint:
    """测试市场状态策略建议。"""

    def test_bull_hint(self):
        """牛市建议应包含趋势跟踪。"""
        hint = get_regime_strategy_hint("bull")
        assert "趋势" in hint["primary"]
        assert "牛市" in hint["note"]

    def test_bear_hint(self):
        """熊市建议应包含现金为王。"""
        hint = get_regime_strategy_hint("bear")
        assert "现金" in hint["note"] or "熊市" in hint["note"]

    def test_crash_hint(self):
        """暴跌建议应建议空仓。"""
        hint = get_regime_strategy_hint("crash")
        assert "空仓" in hint["primary"] or "卖出" in hint["primary"]

    def test_sideways_hint(self):
        """震荡市建议应包含均值回归。"""
        hint = get_regime_strategy_hint("sideways")
        assert "均值回归" in hint["primary"] or "高抛低吸" in hint["secondary"]

    def test_rally_hint(self):
        """反弹市建议应包含突破策略。"""
        hint = get_regime_strategy_hint("rally")
        assert "突破" in hint["primary"]

    def test_unknown_regime_hint(self):
        """未知市场状态应返回保守建议。"""
        hint = get_regime_strategy_hint("unknown")
        assert "保守" in hint["note"] or "均衡" in hint["primary"]


class TestRegimeMultipliers:
    """测试市场状态乘数配置完整性。"""

    def test_all_regimes_have_multipliers(self):
        """所有市场状态都应有乘数配置。"""
        expected_regimes = {"bull", "bear", "sideways", "crash", "rally"}
        assert set(REGIME_MULTIPLIERS.keys()) == expected_regimes

    def test_all_regimes_have_all_categories(self):
        """所有市场状态都应有四类策略的乘数。"""
        expected_categories = {"trend", "mean_revert", "breakout", "main_force"}
        for regime, multipliers in REGIME_MULTIPLIERS.items():
            assert (
                set(multipliers.keys()) == expected_categories
            ), f"regime={regime} missing categories"

    def test_crash_multipliers_are_lowest(self):
        """暴跌市的乘数应是最低的。"""
        crash_multipliers = REGIME_MULTIPLIERS["crash"]
        for regime, multipliers in REGIME_MULTIPLIERS.items():
            if regime == "crash":
                continue
            for category in crash_multipliers:
                assert (
                    crash_multipliers[category] <= multipliers[category]
                ), f"crash {category} should be <= {regime} {category}"
