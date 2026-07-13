"""资金管理模块单元测试。"""

from __future__ import annotations

import pytest

from gugu.analysis.money_management import KellyCalculator, KellyResult


class TestKellyCalculator:
    """Kelly 公式计算器测试。"""

    def test_calculate_basic(self):
        """基本 Kelly 计算。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.6, profit_loss_ratio=2.0)
        assert isinstance(result, KellyResult)
        assert result.full_kelly > 0
        assert result.half_kelly > 0
        assert result.quarter_kelly > 0
        assert result.suggested_position > 0
        assert result.edge > 0

    def test_calculate_no_edge(self):
        """无正期望时建议仓位为 0。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.3, profit_loss_ratio=1.0)
        assert result.full_kelly == 0.0
        assert result.suggested_position == 0.0
        assert "无正期望" in result.reason

    def test_calculate_conservative(self):
        """保守模式使用 1/4 Kelly。"""
        kelly = KellyCalculator()
        result_normal = kelly.calculate(win_rate=0.6, profit_loss_ratio=2.0, is_conservative=False)
        result_conservative = kelly.calculate(
            win_rate=0.6, profit_loss_ratio=2.0, is_conservative=True
        )
        assert result_conservative.suggested_position < result_normal.suggested_position

    def test_calculate_custom_fractional(self):
        """自定义分式系数。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.6, profit_loss_ratio=2.0, fractional=0.3)
        assert result.suggested_position > 0

    def test_calculate_win_rate_clamped(self):
        """胜率被限制在 [0.01, 0.99]。"""
        kelly = KellyCalculator()
        r1 = kelly.calculate(win_rate=0.0, profit_loss_ratio=2.0)
        r2 = kelly.calculate(win_rate=1.0, profit_loss_ratio=2.0)
        # win_rate=0 → clamp to 0.01 → edge=-0.485 → full_kelly=0
        assert r1.full_kelly == 0.0
        assert r1.win_rate == 0.01
        # win_rate=1 → clamp to 0.99 → edge=0.985 → full_kelly>0
        assert r2.full_kelly > 0
        assert r2.win_rate == 0.99

    def test_calculate_profit_loss_ratio_clamped(self):
        """盈亏比被限制 >= 0.1。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.6, profit_loss_ratio=0.0)
        assert result.full_kelly >= 0

    def test_calculate_suggested_capped_at_50pct(self):
        """建议仓位上限 50%。"""
        kelly = KellyCalculator()
        # 极高胜率+极高盈亏比 → Kelly 建议 > 50%
        result = kelly.calculate(win_rate=0.8, profit_loss_ratio=5.0)
        assert result.suggested_position <= 0.5

    def test_get_position_size_no_edge(self):
        """无正期望时返回 0。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.3, profit_loss_ratio=1.0)
        size = kelly.get_position_size(result, max_position_pct=0.3)
        assert size == 0.0

    def test_get_position_size_with_edge(self):
        """有正期望时返回合理值。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.6, profit_loss_ratio=2.0)
        size = kelly.get_position_size(result, max_position_pct=0.3)
        assert 0 < size <= 0.3

    def test_get_position_size_regime_limit(self):
        """市场状态限制应生效。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.6, profit_loss_ratio=2.0)
        size_full = kelly.get_position_size(result, max_position_pct=0.3, regime_limit=1.0)
        size_limited = kelly.get_position_size(result, max_position_pct=0.3, regime_limit=0.5)
        assert size_limited <= size_full

    def test_get_position_size_existing_position(self):
        """已有仓位应减少可用额度。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.6, profit_loss_ratio=2.0)
        size_no_pos = kelly.get_position_size(result, max_position_pct=0.3, current_ratio=0.0)
        size_with_pos = kelly.get_position_size(result, max_position_pct=0.3, current_ratio=0.2)
        assert size_with_pos <= size_no_pos

    def test_get_position_size_remaining_full(self):
        """已有仓位达到上限时返回 0。"""
        kelly = KellyCalculator()
        result = kelly.calculate(win_rate=0.6, profit_loss_ratio=2.0)
        size = kelly.get_position_size(result, max_position_pct=0.3, current_ratio=0.3)
        assert size == 0.0

    def test_pyramiding_add_first_level(self):
        """第一层加仓条件满足时返回加仓量。"""
        kelly = KellyCalculator()
        amount, reason = kelly.pyramiding_add(
            current_level=0,
            current_profit_pct=0.06,
            initial_position_size=100,
        )
        assert amount == 50.0  # 初始的 50%
        assert "金字塔加仓" in reason

    def test_pyramiding_add_not_triggered(self):
        """未满足加仓条件返回 0。"""
        kelly = KellyCalculator()
        amount, reason = kelly.pyramiding_add(
            current_level=0,
            current_profit_pct=0.02,
            initial_position_size=100,
        )
        assert amount == 0.0
        assert "未满足" in reason

    def test_pyramiding_add_second_level(self):
        """第二层加仓。"""
        kelly = KellyCalculator()
        amount, reason = kelly.pyramiding_add(
            current_level=1,
            current_profit_pct=0.12,
            initial_position_size=100,
        )
        assert amount == 30.0  # 初始的 30%

    def test_pyramiding_add_third_level(self):
        """第三层加仓。"""
        kelly = KellyCalculator()
        amount, reason = kelly.pyramiding_add(
            current_level=2,
            current_profit_pct=0.18,
            initial_position_size=100,
        )
        assert amount == 20.0  # 初始的 20%

    def test_pyramiding_add_custom_levels(self):
        """自定义加仓层级。"""
        from gugu.analysis.money_management import PyramidingLevel

        kelly = KellyCalculator()
        custom_levels = [
            PyramidingLevel(
                level=1, trigger_profit_pct=0.03, ratio_of_initial=0.8, stop_loss_pct=0.03
            ),
        ]
        amount, reason = kelly.pyramiding_add(
            current_level=0,
            current_profit_pct=0.04,
            initial_position_size=100,
            levels=custom_levels,
        )
        assert amount == 80.0

    def test_estimate_from_history_basic(self):
        """从历史记录估算。"""
        win_rate, pl_ratio = KellyCalculator.estimate_from_history(
            win_count=60,
            loss_count=40,
            total_profit=30000,
            total_loss=10000,
        )
        assert win_rate == 0.6
        assert pl_ratio > 1.0

    def test_estimate_from_history_no_trades(self):
        """无交易记录返回缺省值。"""
        win_rate, pl_ratio = KellyCalculator.estimate_from_history(0, 0, 0, 0)
        assert win_rate == 0.5
        assert pl_ratio == 1.0

    def test_estimate_from_history_no_losses(self):
        """无亏损记录时盈亏比设为上限。"""
        win_rate, pl_ratio = KellyCalculator.estimate_from_history(10, 0, 5000, 0)
        assert win_rate == 1.0
        assert pl_ratio == 10.0  # 上限

    def test_estimate_from_history_no_wins(self):
        """无盈利记录时盈亏比为 0。"""
        win_rate, pl_ratio = KellyCalculator.estimate_from_history(0, 10, 0, 5000)
        assert win_rate == 0.0
        assert pl_ratio == 0.0

    def test_estimate_from_history_pl_ratio_capped(self):
        """盈亏比上限 10。"""
        win_rate, pl_ratio = KellyCalculator.estimate_from_history(
            win_count=5,
            loss_count=1,
            total_profit=100000,
            total_loss=100,
        )
        assert pl_ratio <= 10.0
