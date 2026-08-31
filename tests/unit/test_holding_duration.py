from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import Mock

from gugu.risk.holding_duration import HoldingDurationChecker
from gugu.risk.rules import RiskAction, RiskLevel


def _date_days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def test_empty_entry_date_allows_without_logging() -> None:
    logger = Mock()
    checker = HoldingDurationChecker(30, 60, risk_logger=logger)

    result = checker.check("600519", "", -0.10)

    assert result.action == RiskAction.ALLOW
    assert result.level == RiskLevel.L1_POSITION
    assert result.message == "无建仓日期"
    logger.warning.assert_not_called()


def test_invalid_entry_date_allows_without_logging() -> None:
    logger = Mock()
    checker = HoldingDurationChecker(30, 60, risk_logger=logger)

    result = checker.check("600519", "not-a-date", -0.10)

    assert result.action == RiskAction.ALLOW
    assert result.level == RiskLevel.L1_POSITION
    assert "建仓日期格式错误" in result.message
    logger.warning.assert_not_called()


def test_profitable_position_is_exempt_from_duration_threshold() -> None:
    logger = Mock()
    checker = HoldingDurationChecker(30, 60, risk_logger=logger)

    result = checker.check("600519", _date_days_ago(365), 0.01)

    assert result.action == RiskAction.ALLOW
    assert result.level == RiskLevel.L1_POSITION
    assert "盈利" in result.message
    logger.warning.assert_not_called()


def test_warn_threshold_uses_natural_days() -> None:
    logger = Mock()
    checker = HoldingDurationChecker(30, 60, risk_logger=logger)

    result = checker.check("600519", _date_days_ago(30), -0.05)

    assert result.action == RiskAction.WARN
    assert result.level == RiskLevel.L2_DAILY_LOSS
    assert "建议减半仓" in result.message
    logger.warning.assert_called_once()


def test_halt_threshold_uses_natural_days() -> None:
    logger = Mock()
    checker = HoldingDurationChecker(30, 60, risk_logger=logger)

    result = checker.check("600519", _date_days_ago(60), -0.05)

    assert result.action == RiskAction.HALT
    assert result.level == RiskLevel.L2_DAILY_LOSS
    assert "强制清仓" in result.message
    logger.warning.assert_called_once()


def test_risk_manager_delegates_with_current_thresholds() -> None:
    from gugu.risk.manager import RiskManager

    manager = RiskManager({"max_holding_days_warn": 10, "max_holding_days_halt": 20})

    assert manager._holding_duration_checker.max_holding_days_warn == 10
    assert manager._holding_duration_checker.max_holding_days_halt == 20
    result = manager.check_holding_duration("600519", _date_days_ago(20), -0.05)
    assert result.action == RiskAction.HALT
