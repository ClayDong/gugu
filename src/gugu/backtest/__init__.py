"""Backtest engine for strategy validation on historical data."""
from .engine import BacktestEngine, BacktestResult
from .metrics import calc_metrics
from .report import format_report, format_report_dict

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "calc_metrics",
    "format_report",
    "format_report_dict",
]
