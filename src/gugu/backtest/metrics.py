"""Backtest performance metrics.

Calculates risk/return metrics from an equity curve and a list of trades.
All metrics return plain Python floats for JSON compatibility.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# Trading days per year for A-share market
_TRADING_DAYS = 252


def calc_metrics(equity_curve: pd.Series, trades: list[Any]) -> dict[str, float]:
    """Calculate backtest performance metrics.

    Args:
        equity_curve: Daily equity values indexed by date.
        trades: List of trade records (dicts or dataclasses) with fields
                direction ("buy"/"sell"), price, quantity, commission, profit, date.

    Returns:
        Dict with total_return, annual_return, sharpe, max_drawdown, calmar_ratio,
        win_rate, profit_factor, total_trades, avg_hold_days.
    """
    returns = (
        equity_curve.pct_change().dropna() if len(equity_curve) > 1 else pd.Series(dtype=float)
    )
    max_dd = _max_drawdown(equity_curve)
    ann_ret = _annual_return(equity_curve)
    return {
        "total_return": _total_return(equity_curve),
        "annual_return": ann_ret,
        "sharpe": _sharpe(returns),
        "max_drawdown": max_dd,
        "calmar_ratio": _calmar_ratio(ann_ret, max_dd),
        "sortino_ratio": _sortino_ratio(returns, periods=_TRADING_DAYS, rf_rate=0.02),
        "win_rate": _win_rate(trades),
        "profit_factor": _profit_factor(trades),
        "total_trades": float(len(trades)),
        "avg_hold_days": _avg_hold_days(trades),
    }


def _sharpe(returns: pd.Series, periods: int = _TRADING_DAYS, rf_rate: float = 0.02) -> float:
    """Annualized Sharpe ratio with risk-free rate.

    Args:
        returns: Daily returns series.
        periods: Annualization factor, default 252 trading days.
        rf_rate: Annual risk-free rate (default 2% for A-share market).

    Returns:
        Sharpe ratio, 0.0 if not computable.
    """
    if len(returns) < 2:
        return 0.0
    std = float(returns.std())
    if std == 0 or np.isnan(std):
        return 0.0
    mean = float(returns.mean())
    if np.isnan(mean):
        return 0.0
    # 日化无风险利率
    daily_rf = rf_rate / periods
    excess_mean = mean - daily_rf
    return float(np.sqrt(periods) * excess_mean / std)


def _max_drawdown(equity: pd.Series) -> float:
    """Maximum drawdown as a positive fraction.

    Args:
        equity: Equity curve series.

    Returns:
        Max drawdown as a positive number (e.g. 0.20 for 20% drawdown).
        Returns 0.0 for empty or always-rising curves.
    """
    if len(equity) == 0:
        return 0.0
    equity = equity.dropna()
    if len(equity) == 0:
        return 0.0
    peak = equity.cummax()
    drawdown = (equity - peak) / peak
    max_dd = float(drawdown.min())
    if np.isnan(max_dd):
        return 0.0
    return -max_dd


def _total_return(equity: pd.Series) -> float:
    """Total return over the entire equity curve."""
    if len(equity) == 0:
        return 0.0
    initial = float(equity.iloc[0])
    final = float(equity.iloc[-1])
    if initial == 0:
        return 0.0
    return (final - initial) / initial


def _annual_return(equity: pd.Series, periods: int = _TRADING_DAYS) -> float:
    """Annualized return using compound rate."""
    if len(equity) < 2:
        return 0.0
    initial = float(equity.iloc[0])
    final = float(equity.iloc[-1])
    if initial <= 0 or final <= 0:
        return 0.0
    n = len(equity)
    return float((final / initial) ** (periods / n) - 1)


def _win_rate(trades: list[Any]) -> float:
    """Win rate based on closed (sell) trades."""
    sells = [t for t in trades if _get(t, "direction") == "sell"]
    if not sells:
        return 0.0
    wins = sum(1 for t in sells if float(_get(t, "profit", 0.0) or 0.0) > 0)
    return wins / len(sells)


def _profit_factor(trades: list[Any]) -> float:
    """Profit factor = gross profit / gross loss.

    Returns:
        Ratio of gross profit to gross loss. Returns inf if no losing trades
        but has winning trades, 0.0 if no trades or no winning trades.
    """
    sells = [t for t in trades if _get(t, "direction") == "sell"]
    if not sells:
        return 0.0
    profits = [float(_get(t, "profit", 0.0) or 0.0) for t in sells]
    gross_profit = sum(p for p in profits if p > 0)
    gross_loss = abs(sum(p for p in profits if p < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _avg_hold_days(trades: list[Any]) -> float:
    """Average holding days, pairing each sell with its preceding buy."""
    if not trades:
        return 0.0
    last_buy_date: pd.Timestamp | None = None
    total_days = 0.0
    sell_count = 0
    for t in trades:
        direction = _get(t, "direction")
        date = _get(t, "date")
        if direction == "buy":
            last_buy_date = _to_timestamp(date)
        elif direction == "sell" and last_buy_date is not None:
            sell_date = _to_timestamp(date)
            if sell_date is not None:
                total_days += (sell_date - last_buy_date).days
                sell_count += 1
            last_buy_date = None
    if sell_count == 0:
        return 0.0
    return total_days / sell_count


def _get(trade: Any, field: str, default: Any = None) -> Any:
    """Get a field from a dict or dataclass trade record."""
    if isinstance(trade, dict):
        return trade.get(field, default)
    return getattr(trade, field, default)


def _to_timestamp(date: Any) -> pd.Timestamp | None:
    """Safely convert a date value to pd.Timestamp."""
    if date is None:
        return None
    try:
        return pd.Timestamp(date)
    except (ValueError, TypeError):
        return None


def _calmar_ratio(annual_return: float, max_drawdown: float) -> float:
    """Calmar ratio = annual return / max drawdown.

    Measures risk-adjusted return relative to maximum drawdown.
    A higher Calmar ratio indicates better return per unit of drawdown risk.

    Returns:
        0.0 if max_drawdown is 0 or negative (no drawdown means undefined).
    """
    if max_drawdown <= 0 or annual_return == 0:
        return 0.0
    return annual_return / max_drawdown


def _sortino_ratio(
    returns: pd.Series,
    periods: int = _TRADING_DAYS,
    rf_rate: float = 0.02,
) -> float:
    """Sortino ratio — like Sharpe but uses only downside deviation.

    Downside deviation only penalizes negative returns, making it a better
    measure for strategies with asymmetric return distributions.

    Returns:
        0.0 if not computable.
    """
    if len(returns) < 2:
        return 0.0
    daily_rf = rf_rate / periods
    excess = returns - daily_rf
    # Only negative deviations
    downside = returns[returns < 0]
    if len(downside) == 0:
        return 0.0
    downside_std = float(downside.std())
    if downside_std == 0 or np.isnan(downside_std):
        return 0.0
    mean_excess = float(excess.mean())
    if np.isnan(mean_excess):
        return 0.0
    return float(np.sqrt(periods) * mean_excess / downside_std)
