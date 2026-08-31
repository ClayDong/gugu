"""P1 修复：参数写回解锁条件门控单元测试。

基于对抗性审查，每项条件都对应一类典型过拟合陷阱：
- 训练 Sharpe 5.01 vs 测试 Sharpe -6.28 = 典型过拟合，必须拒绝写回
- 每个窗口最优参数都不同 = 偶然性，不可写回
- 高收益但回撤 >50% = 赌徒参数，破产风险
- 系统性亏损 = 直接禁止
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts 目录加入 sys.path（非包模块）
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from optimize_params import _check_unlock_conditions  # noqa: E402


class TestUnlockConditionsAllPass:
    """4 项条件全部通过的场景。"""

    def test_all_conditions_pass_unlocked(self) -> None:
        """全部满足时应解锁写回。"""
        results = [
            {
                "window": 1,
                "best_params": {"breakout_window": 20},
                "test_sharpe": 0.5,
                "test_trades": 15,
            },
            {
                "window": 2,
                "best_params": {"breakout_window": 20},
                "test_sharpe": 0.6,
                "test_trades": 15,
            },
            {
                "window": 3,
                "best_params": {"breakout_window": 20},
                "test_sharpe": 0.4,
                "test_trades": 10,
            },
        ]  # 总交易 40 >= 30 通过
        param_votes = {"breakout_window": {"20": 3}}  # 100% 一致性

        report = _check_unlock_conditions(
            strategy_name="test",
            results=results,
            avg_sharpe=0.5,  # > 0 通过
            avg_return=0.1,  # >= 0 通过
            avg_max_dd=0.15,  # < 25% 通过
            param_votes=param_votes,
        )

        assert report["unlocked"] is True
        assert len(report["failed_checks"]) == 0
        assert all(c["passed"] for c in report["checks"])

    def test_minimal_pass_thresholds(self) -> None:
        """刚好处在阈值边界时也应通过（>0 / 80% / <25% / >=0 / >=30笔）。"""
        results = [
            {"window": i, "best_params": {"x": 10}, "test_sharpe": 0.0, "test_trades": 10}
            for i in range(4)
        ]
        # 4/4 = 100% 一致性（v0.8.1 起阈值 80%）
        param_votes = {"x": {"10": 4}}

        report = _check_unlock_conditions(
            strategy_name="test",
            results=results,
            avg_sharpe=0.001,  # 微 > 0 通过
            avg_return=0.0,  # = 0 通过（边界）
            avg_max_dd=0.249,  # 微 < 25% 通过
            param_votes=param_votes,
        )

        assert report["unlocked"] is True, f"边界值应通过: {report}"


class TestUnlockConditionsOverfit:
    """过拟合场景（对抗性审查核心案例）。"""

    def test_overfit_high_train_low_test_rejected(self) -> None:
        """训练神勇、测试亏损的过拟合应拒绝写回。

        模拟：训练 Sharpe 5.01 vs 测试 Sharpe -6.28
        """
        results = [
            {"window": 1, "best_params": {"x": 20}, "test_sharpe": -6.28},
        ]
        param_votes = {"x": {"20": 1}}

        report = _check_unlock_conditions(
            strategy_name="overfit",
            results=results,
            avg_sharpe=-6.28,  # < 0 失败
            avg_return=-0.15,  # < 0 失败
            avg_max_dd=0.45,  # > 25% 失败
            param_votes=param_votes,
        )

        assert report["unlocked"] is False
        # 至少 3 项失败（Sharpe、收益、回撤）
        assert len(report["failed_checks"]) >= 3

    def test_instable_params_rejected(self) -> None:
        """每个窗口最优参数都不同应拒绝写回。"""
        results = [
            {"window": 1, "best_params": {"x": 10}, "test_sharpe": 0.5, "test_trades": 10},
            {"window": 2, "best_params": {"x": 20}, "test_sharpe": 0.5, "test_trades": 10},
            {"window": 3, "best_params": {"x": 30}, "test_sharpe": 0.5, "test_trades": 10},
        ]
        # 3 个窗口 3 个不同的最优值，最高一致性只有 1/3 ≈ 33% < 80%（v0.8.1 阈值）
        param_votes = {"x": {"10": 1, "20": 1, "30": 1}}

        report = _check_unlock_conditions(
            strategy_name="instable",
            results=results,
            avg_sharpe=0.5,
            avg_return=0.1,
            avg_max_dd=0.15,
            param_votes=param_votes,
        )

        assert report["unlocked"] is False
        assert any("参数稳定性" in f for f in report["failed_checks"])


class TestUnlockConditionsSingleFailure:
    """单条件失败场景（验证每项检查独立生效）。"""

    def test_negative_sharpe_only_rejected(self) -> None:
        """仅 Sharpe 失败（其他通过）应拒绝。"""
        results = [
            {"window": 1, "best_params": {"x": 10}, "test_sharpe": -0.1, "test_trades": 20},
            {"window": 2, "best_params": {"x": 10}, "test_sharpe": -0.1, "test_trades": 20},
        ]
        param_votes = {"x": {"10": 2}}

        report = _check_unlock_conditions(
            strategy_name="test",
            results=results,
            avg_sharpe=-0.1,  # < 0 失败
            avg_return=0.05,  # > 0 通过
            avg_max_dd=0.15,  # < 25% 通过
            param_votes=param_votes,
        )

        assert report["unlocked"] is False
        assert len(report["failed_checks"]) == 1
        assert "样本外 Sharpe" in report["failed_checks"][0]

    def test_high_drawdown_only_rejected(self) -> None:
        """仅回撤过高（其他通过）应拒绝。"""
        results = [
            {"window": 1, "best_params": {"x": 10}, "test_sharpe": 0.5, "test_trades": 20},
            {"window": 2, "best_params": {"x": 10}, "test_sharpe": 0.5, "test_trades": 20},
        ]
        param_votes = {"x": {"10": 2}}

        report = _check_unlock_conditions(
            strategy_name="test",
            results=results,
            avg_sharpe=0.5,  # > 0 通过
            avg_return=0.05,  # > 0 通过
            avg_max_dd=0.35,  # > 25% 失败
            param_votes=param_votes,
        )

        assert report["unlocked"] is False
        assert len(report["failed_checks"]) == 1
        assert "最大回撤" in report["failed_checks"][0]

    def test_negative_return_only_rejected(self) -> None:
        """仅收益为负（其他通过）应拒绝。"""
        results = [
            {"window": 1, "best_params": {"x": 10}, "test_sharpe": 0.1, "test_trades": 20},
            {"window": 2, "best_params": {"x": 10}, "test_sharpe": 0.1, "test_trades": 20},
        ]
        param_votes = {"x": {"10": 2}}

        report = _check_unlock_conditions(
            strategy_name="test",
            results=results,
            avg_sharpe=0.1,  # > 0 通过
            avg_return=-0.05,  # < 0 失败
            avg_max_dd=0.15,  # < 25% 通过
            param_votes=param_votes,
        )

        assert report["unlocked"] is False
        assert len(report["failed_checks"]) == 1
        assert "样本外收益" in report["failed_checks"][0]


class TestUnlockConditionsTradeCount:
    """样本外交易数必须使用明确的闭合交易字段。"""

    def test_closed_trade_count_takes_precedence_over_legacy_trade_legs(self) -> None:
        """closed_trade_count 优先，不能把 trade legs 当成 round-trip。"""
        report = _check_unlock_conditions(
            strategy_name="test",
            results=[
                {
                    "window": 1,
                    "best_params": {},
                    "closed_trade_count": 30,
                    "trades": 60,
                }
            ],
            avg_sharpe=0.5,
            avg_return=0.05,
            avg_max_dd=0.10,
            param_votes={},
        )
        assert report["unlocked"] is True
        assert any("total_test_trades=30" in c["detail"] for c in report["checks"])

    def test_legacy_trade_integer_does_not_count_as_admission_sample(self) -> None:
        """历史结果仅有整数 trades 时应保守视为缺少闭合交易口径。"""
        report = _check_unlock_conditions(
            strategy_name="legacy",
            results=[{"window": 1, "best_params": {}, "trades": 60}],
            avg_sharpe=0.5,
            avg_return=0.05,
            avg_max_dd=0.10,
            param_votes={},
        )
        assert report["unlocked"] is False
        assert any("样本外交易次数 < 30" in item for item in report["failed_checks"])


class TestUnlockConditionsReportStructure:
    """解锁报告结构验证。"""

    def test_report_has_required_fields(self) -> None:
        """报告应包含 unlocked/checks/failed_checks 三个字段。"""
        report = _check_unlock_conditions(
            strategy_name="test",
            results=[{"window": 1, "best_params": {}, "test_sharpe": 0.0, "test_trades": 30}],
            avg_sharpe=0.0,
            avg_return=0.0,
            avg_max_dd=0.0,
            param_votes={},
        )

        assert "unlocked" in report
        assert "checks" in report
        assert "failed_checks" in report
        assert isinstance(report["checks"], list)
        assert len(report["checks"]) == 5  # v0.8.1: 新增样本外交易次数检查

    def test_each_check_has_required_fields(self) -> None:
        """每项检查应包含 name/passed/detail 三个字段。"""
        report = _check_unlock_conditions(
            strategy_name="test",
            results=[{"window": 1, "best_params": {}, "test_sharpe": 0.0}],
            avg_sharpe=0.0,
            avg_return=0.0,
            avg_max_dd=0.0,
            param_votes={},
        )

        for check in report["checks"]:
            assert "name" in check
            assert "passed" in check
            assert "detail" in check
            assert isinstance(check["passed"], bool)
            assert isinstance(check["detail"], str)

    def test_empty_param_votes_does_not_crash(self) -> None:
        """参数投票为空（无参数策略）不应崩溃。"""
        report = _check_unlock_conditions(
            strategy_name="test",
            results=[{"window": 1, "best_params": {}, "test_sharpe": 0.5, "test_trades": 30}],
            avg_sharpe=0.5,
            avg_return=0.05,
            avg_max_dd=0.15,
            param_votes={},
        )

        # 空参数投票视为稳定性通过（无参数可比较）
        assert report["unlocked"] is True
