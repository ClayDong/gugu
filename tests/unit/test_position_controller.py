"""仓位总控模块单元测试。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gugu.analysis.position_controller import PositionBudget, PositionController


@pytest.fixture
def controller() -> PositionController:
    return PositionController()


@pytest.fixture
def mock_account() -> MagicMock:
    acct = MagicMock()
    acct.total_value = 1_000_000
    acct.cash = 700_000
    acct.positions = {}
    return acct


class TestPositionBudget:
    """PositionBudget 数据类测试。"""

    def test_creation(self):
        budget = PositionBudget(
            total_limit=0.8, single_limit=0.3,
            available_budget=500000, max_positions=5,
            reason="test",
        )
        assert budget.total_limit == 0.8
        assert budget.single_limit == 0.3
        assert budget.available_budget == 500000
        assert budget.max_positions == 5


class TestPositionController:
    """PositionController 功能测试。"""

    def test_calculate_halted(self, controller: PositionController, mock_account: MagicMock):
        """熔断时仓位归零。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": True},
            account=mock_account, is_halted=True,
        )
        assert budget.total_limit == 0.0
        assert budget.single_limit == 0.0
        assert budget.available_budget == 0.0
        assert budget.max_positions == 0

    def test_calculate_bull_market(self, controller: PositionController, mock_account: MagicMock):
        """牛市基准仓位。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": True,
                     "regime": "bull", "sell_signal_required": False},
            account=mock_account,
        )
        assert budget.total_limit > 0
        assert budget.single_limit > 0
        assert budget.available_budget > 0

    def test_calculate_bear_market(self, controller: PositionController, mock_account: MagicMock):
        """熊市低仓位。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.2, "buy_signal_allowed": True,
                     "regime": "bear", "sell_signal_required": False},
            account=mock_account,
        )
        assert budget.total_limit <= 0.2

    def test_calculate_buy_not_allowed(self, controller: PositionController, mock_account: MagicMock):
        """禁止买入时仓位归零。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": False,
                     "regime": "crash", "sell_signal_required": True},
            account=mock_account,
        )
        assert budget.total_limit == 0.0
        assert "禁止买入" in budget.reason

    def test_calculate_risk_adjustment_large_loss(self, controller: PositionController, mock_account: MagicMock):
        """累计亏损 > 10% 时仓位减半。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": True,
                     "regime": "bull", "sell_signal_required": False},
            account=mock_account, total_pnl_pct=-0.15,
        )
        # 基准 80% * 风险系数 0.5 = 40%
        assert budget.total_limit <= 0.4

    def test_calculate_risk_adjustment_moderate_loss(self, controller: PositionController, mock_account: MagicMock):
        """累计亏损 > 5% 时仓位打7折。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": True,
                     "regime": "bull", "sell_signal_required": False},
            account=mock_account, total_pnl_pct=-0.07,
        )
        # 基准 80% * 风险系数 0.7 = 56%
        assert budget.total_limit <= 0.56

    def test_calculate_no_positions_remaining(self, controller: PositionController, mock_account: MagicMock):
        """持仓已满时总仓位为 0。"""
        mock_account.positions = {"A": MagicMock(), "B": MagicMock(), "C": MagicMock(),
                                   "D": MagicMock(), "E": MagicMock()}
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": True,
                     "regime": "bull", "sell_signal_required": False},
            account=mock_account,
        )
        assert budget.total_limit == 0.0

    def test_calculate_single_limit_minimum(self, controller: PositionController, mock_account: MagicMock):
        """单股上限不低于 5%。"""
        # 设置很多剩余槽位，让 total_limit / remaining_slots 很小
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": True,
                     "regime": "bull", "sell_signal_required": False},
            account=mock_account,
        )
        # 即使 remaining_slots 很大，single_limit 也不应低于 5%
        assert budget.single_limit >= 0.05

    def test_calculate_single_limit_not_exceed_max(self, controller: PositionController, mock_account: MagicMock):
        """单股上限不超过风控配置。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": True,
                     "regime": "bull", "sell_signal_required": False},
            account=mock_account,
        )
        # 默认 max_position_ratio = 0.3
        assert budget.single_limit <= 0.3

    def test_calculate_reason_includes_regime(self, controller: PositionController, mock_account: MagicMock):
        """理由包含市场状态信息。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.8, "buy_signal_allowed": True,
                     "regime": "bull", "sell_signal_required": False},
            account=mock_account,
        )
        assert "bull" in budget.reason or "市场" in budget.reason

    def test_calculate_sell_required_in_reason(self, controller: PositionController, mock_account: MagicMock):
        """建议减仓时理由包含提示。"""
        budget = controller.calculate(
            regime={"total_position_limit": 0.4, "buy_signal_allowed": True,
                     "regime": "bear", "sell_signal_required": True},
            account=mock_account,
        )
        assert "减仓" in budget.reason