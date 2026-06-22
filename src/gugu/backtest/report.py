"""Backtest report formatters.

Provides a text report for terminal output and a dict for Feishu notification.
The dict is compatible with gugu.notifier.formatter.format_backtest_report.
"""
from __future__ import annotations

import math
from typing import Any

from .engine import BacktestResult


def format_report(result: BacktestResult) -> str:
    """Format a backtest result as a human-readable text report.

    Args:
        result: BacktestResult instance.

    Returns:
        Multi-line text report string.
    """
    m = result.metrics
    pf = _format_profit_factor(m["profit_factor"])

    lines = [
        "=" * 52,
        "  Backtest Report",
        "=" * 52,
        f"  Symbol:         {result.symbol}",
        f"  Strategy:       {result.strategy_name}",
        "-" * 52,
        f"  Total Return:   {m['total_return']:.2%}",
        f"  Annual Return:  {m['annual_return']:.2%}",
        f"  Sharpe Ratio:   {m['sharpe']:.4f}",
        f"  Max Drawdown:   {m['max_drawdown']:.2%}",
        f"  Win Rate:       {m['win_rate']:.2%}",
        f"  Profit Factor:  {pf}",
        f"  Total Trades:   {int(m['total_trades'])}",
        f"  Avg Hold Days:  {m['avg_hold_days']:.1f}",
        "=" * 52,
    ]
    return "\n".join(lines)


def format_report_dict(result: BacktestResult) -> dict[str, Any]:
    """Format a backtest result as a dict for Feishu notification.

    Compatible with gugu.notifier.formatter.format_backtest_report which
    expects keys: strategy, total_return, sharpe, max_drawdown, win_rate,
    trades_count.

    Args:
        result: BacktestResult instance.

    Returns:
        Dict with strategy name, symbol, and metric values as formatted strings.
    """
    m = result.metrics
    pf = _format_profit_factor(m["profit_factor"])

    return {
        "strategy": result.strategy_name,
        "symbol": result.symbol,
        "total_return": f"{m['total_return']:.2%}",
        "annual_return": f"{m['annual_return']:.2%}",
        "sharpe": f"{m['sharpe']:.4f}",
        "max_drawdown": f"{m['max_drawdown']:.2%}",
        "win_rate": f"{m['win_rate']:.2%}",
        "profit_factor": pf,
        "trades_count": int(m["total_trades"]),
        "avg_hold_days": f"{m['avg_hold_days']:.1f}",
    }


def _format_profit_factor(pf: float) -> str:
    """Format profit factor, handling infinity (no losing trades)."""
    if math.isinf(pf):
        return "inf"
    return f"{pf:.4f}"
