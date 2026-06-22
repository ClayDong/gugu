"""Feishu notification module for gugu trading system."""
from gugu.notifier.feishu import FeishuNotifier
from gugu.notifier.formatter import (
    format_backtest_report,
    format_daily_report,
    format_risk_alert,
    format_signal,
    format_system_error,
)

__all__ = [
    "FeishuNotifier",
    "format_signal",
    "format_daily_report",
    "format_risk_alert",
    "format_backtest_report",
    "format_system_error",
]
