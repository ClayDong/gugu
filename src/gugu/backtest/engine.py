"""Backtest engine with realistic A-share transaction costs.

Transaction cost model:
- Buy:  commission_rate (default 0.025%) + slippage (default 0.2%)
- Sell: commission_rate + stamp_tax (default 0.1%) + slippage

Position management respects the L1 single-position ratio from risk config.
Risk manager integration will be added in a later iteration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from gugu.config import settings
from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

from .metrics import calc_metrics

# A-share board lot: must trade in multiples of 100 shares
_BOARD_LOT = 100


@dataclass
class Trade:
    """Single trade record.

    Attributes:
        date: Trade date (pd.Timestamp / str / datetime).
        direction: "buy" or "sell".
        price: Execution price after slippage.
        quantity: Number of shares traded.
        commission: Brokerage commission.
        profit: Realized profit (only for sells, 0.0 for buys).
        stamp_tax: Stamp tax (only for sells, 0.0 for buys).
    """

    date: Any
    direction: str
    price: float
    quantity: int
    commission: float
    profit: float = 0.0
    stamp_tax: float = 0.0


@dataclass
class BacktestResult:
    """Backtest result.

    Attributes:
        symbol: Stock code.
        strategy_name: Strategy name.
        trades: List of Trade records (both buys and sells).
        equity_curve: Daily equity values indexed by date.
        metrics: Performance metrics dict from calc_metrics.
    """

    symbol: str
    strategy_name: str
    trades: list[Trade]
    equity_curve: pd.Series
    metrics: dict[str, float]


class BacktestEngine:
    """Backtest engine with realistic A-share transaction costs.

    Position sizing is capped by ``position_ratio`` (default from risk config)
    to stay consistent with the L1 single-position limit.
    Risk manager integration will be added in a later iteration.

    Example:
        >>> engine = BacktestEngine()
        >>> result = engine.run(strategy, df, symbol="600519")
        >>> print(result.metrics["total_return"])
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        commission_rate: float = 0.00025,
        stamp_tax: float = 0.001,
        slippage: float = 0.002,
        position_ratio: float | None = None,
    ) -> None:
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax = stamp_tax
        self.slippage = slippage
        # 默认与风控 L1 保持一致，避免回测与模拟盘行为脱节
        if position_ratio is None:
            position_ratio = float(
                settings().get("risk", {}).get("max_position_ratio", 0.30)
            )
        self.position_ratio = max(0.0, min(1.0, position_ratio))
        self._logger = get_logger()

    def run(self, strategy: Strategy, df: pd.DataFrame, symbol: str) -> BacktestResult:
        """Run backtest for a single symbol.

        Iterates row by row, generates signals on the expanding window
        ``df.iloc[:i+1]`` to avoid lookahead bias, and executes trades
        based on the signal at each bar.

        Args:
            strategy: Strategy instance with generate_signals method.
            df: OHLCV data with columns date, open, high, low, close, volume, amount.
            symbol: Stock code.

        Returns:
            BacktestResult with trades, equity curve, and metrics.
        """
        if len(df) == 0:
            self._logger.warning(f"Empty data for {symbol}, returning empty result")
            empty_curve = pd.Series(dtype=float)
            return BacktestResult(
                symbol=symbol,
                strategy_name=strategy.name,
                trades=[],
                equity_curve=empty_curve,
                metrics=calc_metrics(empty_curve, []),
            )

        cash = self.initial_capital
        position_qty = 0
        position_cost = 0.0  # total cost basis for current position
        trades: list[Trade] = []
        equity_values: list[float] = []
        dates: list[pd.Timestamp] = []

        for i in range(len(df)):
            row = df.iloc[i]
            current_date = row["date"]
            close = float(row["close"])

            # Generate signals on expanding window to prevent lookahead bias.
            # Note: this is O(n^2); for large datasets consider generating
            # signals once on the full df (strategies use .shift(1) so the
            # result is identical).
            signaled = strategy.generate_signals(df.iloc[: i + 1])
            signal = int(signaled["signal"].iloc[-1])

            # Execute trades based on signal
            if signal == 1 and position_qty == 0:
                # Buy with all available cash (full position)
                trade = self._execute_buy(symbol, current_date, close, cash)
                if trade is not None:
                    trades.append(trade)
                    cash -= trade.price * trade.quantity + trade.commission
                    position_qty = trade.quantity
                    position_cost = trade.price * trade.quantity + trade.commission

            elif signal == -1 and position_qty > 0:
                # Sell all shares (clear position)
                trade = self._execute_sell(symbol, current_date, close, position_qty, position_cost)
                trades.append(trade)
                proceeds = trade.price * trade.quantity - trade.commission - trade.stamp_tax
                cash += proceeds
                position_qty = 0
                position_cost = 0.0

            # Record daily equity (mark-to-market at close)
            equity = cash + position_qty * close
            equity_values.append(equity)
            dates.append(pd.Timestamp(current_date))

        equity_curve = pd.Series(equity_values, index=dates, name="equity")
        metrics = calc_metrics(equity_curve, trades)

        self._logger.info(
            f"Backtest {symbol} / {strategy.name}: "
            f"trades={len(trades)} "
            f"total_return={metrics['total_return']:.2%} "
            f"sharpe={metrics['sharpe']:.4f} "
            f"max_drawdown={metrics['max_drawdown']:.2%}"
        )

        return BacktestResult(
            symbol=symbol,
            strategy_name=strategy.name,
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
        )

    def _execute_buy(
        self,
        symbol: str,
        date: Any,
        close: float,
        cash: float,
    ) -> Trade | None:
        """Execute a buy order with slippage and commission.

        Buy price = close * (1 + slippage)
        Quantity is rounded down to the nearest board lot (100 shares).
        买入金额受 position_ratio 限制，与风控 L1 单股上限保持一致。

        Returns None if not enough cash for even one lot.
        """
        buy_price = close * (1 + self.slippage)
        cost_per_share = buy_price * (1 + self.commission_rate)
        max_value = min(cash, self.initial_capital * self.position_ratio)
        max_qty = int(max_value / cost_per_share)
        quantity = (max_qty // _BOARD_LOT) * _BOARD_LOT
        if quantity <= 0:
            self._logger.debug(
                f"SKIP BUY {symbol}: cash={cash:.2f} not enough for 1 lot at {buy_price:.2f}"
            )
            return None

        commission = buy_price * quantity * self.commission_rate
        self._logger.debug(
            f"BUY {symbol} {quantity}@{buy_price:.2f} commission={commission:.2f}"
        )
        return Trade(
            date=date,
            direction="buy",
            price=buy_price,
            quantity=quantity,
            commission=commission,
        )

    def _execute_sell(
        self,
        symbol: str,
        date: Any,
        close: float,
        quantity: int,
        cost_basis: float,
    ) -> Trade:
        """Execute a sell order with slippage, commission, and stamp tax.

        Sell price = close * (1 - slippage)
        Stamp tax is only applied to sells (A-share rule).
        """
        sell_price = close * (1 - self.slippage)
        commission = sell_price * quantity * self.commission_rate
        stamp_tax = sell_price * quantity * self.stamp_tax
        proceeds = sell_price * quantity - commission - stamp_tax
        profit = proceeds - cost_basis

        self._logger.debug(
            f"SELL {symbol} {quantity}@{sell_price:.2f} "
            f"commission={commission:.2f} tax={stamp_tax:.2f} profit={profit:.2f}"
        )
        return Trade(
            date=date,
            direction="sell",
            price=sell_price,
            quantity=quantity,
            commission=commission,
            profit=profit,
            stamp_tax=stamp_tax,
        )
