"""Risk manager with three-level risk control.

This module is the single source of truth for risk enforcement.
Risk rules are iron laws and cannot be bypassed by business logic.

Levels:
- L1 single position: per-stock position ratio <= max_position_ratio (default 30%)
- L2 daily loss: warn at daily_loss_warn (default 3%), halt at daily_loss_halt (default 5%)
- L3 system: T+1 settlement, price limit (涨跌停), suspension (停牌)
"""
from __future__ import annotations

from typing import Any

from gugu.config import settings
from gugu.utils.log import get_logger

from .rules import Position, RiskAction, RiskCheckResult, RiskLevel

# Price limit ratios by board
_MAIN_BOARD_LIMIT = 0.10  # 主板 ±10%
_GEM_KCB_LIMIT = 0.20  # 创业板 / 科创板 ±20%
_ST_LIMIT = 0.05  # ST ±5%

# Symbol prefixes
_GEM_PREFIXES = ("300", "301")  # 创业板
_KCB_PREFIXES = ("688", "689")  # 科创板


class RiskManager:
    """Three-level risk manager.

    Once L2 halt is triggered, all orders are blocked until ``reset()`` is called.
    This is an iron law and cannot be bypassed.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else settings().get("risk", {})
        self.max_position_ratio: float = float(cfg.get("max_position_ratio", 0.30))
        self.daily_loss_warn: float = float(cfg.get("daily_loss_warn", 0.03))
        self.daily_loss_halt: float = float(cfg.get("daily_loss_halt", 0.05))
        self.max_total_positions: int = int(cfg.get("max_total_positions", 5))
        self.t_plus_1: bool = bool(cfg.get("t_plus_1", True))
        self._logger = get_logger()
        self._halted: bool = False

    @property
    def is_halted(self) -> bool:
        """Whether the manager is in halt state (all trading blocked)."""
        return self._halted

    def reset(self) -> None:
        """Reset halt state. Call at the start of a new trading day."""
        if self._halted:
            self._logger.info("Risk manager halt state reset")
        self._halted = False

    def check_order(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        price: float,
        portfolio: dict[str, Position],
        *,
        cash: float = 0.0,
        prev_close: float | None = None,
        is_st: bool = False,
        is_suspended: bool = False,
    ) -> RiskCheckResult:
        """Check an order against all risk rules.

        Args:
            symbol: Stock code, e.g. "600519".
            direction: "buy" or "sell".
            quantity: Order quantity in shares.
            price: Order price.
            portfolio: Current positions keyed by symbol.
            cash: Available cash, needed for position ratio calculation.
            prev_close: Previous close price, needed for price limit check.
            is_st: Whether the stock is ST (affects price limit).
            is_suspended: Whether the stock is suspended (停牌).

        Returns:
            RiskCheckResult. HALT means the order must be rejected.
        """
        # Iron law: once halted, all orders are blocked
        if self._halted:
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.HALT,
                message="Daily loss halt is active, all trading blocked. Call reset() to resume.",
            )

        direction = direction.lower().strip()
        if direction not in ("buy", "sell"):
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"Invalid direction: {direction!r}, must be 'buy' or 'sell'",
            )

        if quantity <= 0:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"Invalid quantity: {quantity}, must be positive",
            )

        if price <= 0:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"Invalid price: {price}, must be positive",
            )

        # L3: suspension check
        if is_suspended:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"{symbol} is suspended, trading not allowed",
            )

        # L3: price limit check (涨跌停) - 区分买卖方向
        if prev_close is not None and prev_close > 0 and not self.is_tradable(
            symbol, price, prev_close, is_st=is_st, direction=direction
        ):
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"{symbol} at price limit, trading not allowed",
            )

        if direction == "buy":
            return self._check_buy(symbol, quantity, price, portfolio, cash)
        return self._check_sell(symbol, quantity, price, portfolio)

    def _check_buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        portfolio: dict[str, Position],
        cash: float,
    ) -> RiskCheckResult:
        """L1 checks for buy orders: max positions and single position ratio."""
        # L1: max total positions (only when opening a new position)
        if symbol not in portfolio and len(portfolio) >= self.max_total_positions:
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.HALT,
                message=(
                    f"Max total positions ({self.max_total_positions}) reached, "
                    f"cannot open new position in {symbol}"
                ),
            )

        # L1: single position ratio limit
        current_pos = portfolio.get(symbol)
        existing_qty = current_pos.quantity if current_pos else 0

        # Post-trade position value for this symbol (valued at order price)
        new_symbol_value = (existing_qty + quantity) * price
        # Other positions valued at their current price
        other_value = sum(
            p.quantity * p.current_price for s, p in portfolio.items() if s != symbol
        )
        # Post-trade total = cash - buy_cost + new_symbol_value + other_value
        # 买入后现金减少 quantity * price（未含手续费，保守估算）
        post_trade_total = cash - quantity * price + new_symbol_value + other_value

        if post_trade_total <= 0:
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.HALT,
                message=(
                    f"Cannot determine total portfolio value ({post_trade_total}), "
                    f"pass cash= to check_order for position ratio check"
                ),
            )

        new_ratio = new_symbol_value / post_trade_total
        if new_ratio > self.max_position_ratio:
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.HALT,
                message=(
                    f"{symbol} position ratio {new_ratio:.1%} exceeds limit "
                    f"{self.max_position_ratio:.1%}"
                ),
            )

        return RiskCheckResult(
            level=RiskLevel.L1_POSITION,
            action=RiskAction.ALLOW,
            message=f"Buy {symbol} {quantity}@{price} approved, ratio={new_ratio:.1%}",
        )

    def _check_sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        portfolio: dict[str, Position],
    ) -> RiskCheckResult:
        """L3 checks for sell orders: holding and T+1."""
        position = portfolio.get(symbol)
        if position is None or position.quantity <= 0:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"No position in {symbol} to sell",
            )

        if quantity > position.quantity:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"Sell quantity {quantity} exceeds holding {position.quantity} for {symbol}",
            )

        # L3: T+1 settlement check
        if self.t_plus_1:
            sellable = position.available
            if quantity > sellable:
                return RiskCheckResult(
                    level=RiskLevel.L3_SYSTEM,
                    action=RiskAction.HALT,
                    message=(
                        f"T+1 rule: sell {quantity} exceeds sellable {sellable} "
                        f"(available) for {symbol}"
                    ),
                )

        return RiskCheckResult(
            level=RiskLevel.L3_SYSTEM,
            action=RiskAction.ALLOW,
            message=f"Sell {symbol} {quantity}@{price} approved",
        )

    def check_daily_loss(self, loss_pct: float) -> RiskCheckResult:
        """L2: Check daily loss against circuit breaker thresholds.

        Args:
            loss_pct: Daily loss as a non-negative fraction (0.03 = 3% loss).

        Returns:
            RiskCheckResult. HALT triggers a permanent block until reset().
        """
        if loss_pct < 0:
            loss_pct = 0.0

        if loss_pct >= self.daily_loss_halt:
            self._halted = True
            self._logger.warning(
                f"L2 HALT: daily loss {loss_pct:.2%} >= {self.daily_loss_halt:.2%}, "
                f"trading halted until reset()"
            )
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.HALT,
                message=(
                    f"Daily loss {loss_pct:.2%} reached halt threshold "
                    f"{self.daily_loss_halt:.2%}, trading halted"
                ),
            )

        if loss_pct >= self.daily_loss_warn:
            self._logger.warning(
                f"L2 WARN: daily loss {loss_pct:.2%} >= {self.daily_loss_warn:.2%}"
            )
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.WARN,
                message=(
                    f"Daily loss {loss_pct:.2%} reached warn threshold "
                    f"{self.daily_loss_warn:.2%}"
                ),
            )

        return RiskCheckResult(
            level=RiskLevel.L2_DAILY_LOSS,
            action=RiskAction.ALLOW,
            message=f"Daily loss {loss_pct:.2%} within limits",
        )

    def is_tradable(
        self,
        symbol: str,
        price: float,
        prev_close: float,
        *,
        is_st: bool = False,
        is_suspended: bool = False,
        direction: str = "",
    ) -> bool:
        """L3: Check if a stock is tradable (not at price limit, not suspended).

        Price limit ratios:
        - Main board (600/601/603/605/000/001/002/003): ±10%
        - ChiNext (300/301) / STAR (688/689): ±20%
        - ST stocks: ±5% (overrides board-based limit)

        Direction-aware: 涨停时允许卖出（有人排队买），跌停时允许买入（有人排队卖）。

        Args:
            symbol: Stock code.
            price: Current / order price.
            prev_close: Previous close price.
            is_st: Whether the stock is ST.
            is_suspended: Whether the stock is suspended.
            direction: "buy" or "sell". Empty string = old behavior (both blocked).

        Returns:
            True if tradable, False if at price limit or suspended.
        """
        if is_suspended:
            return False

        if prev_close <= 0:
            self._logger.warning(
                f"Cannot determine price limit for {symbol}: prev_close={prev_close}"
            )
            return True

        limit_ratio = self._price_limit_ratio(symbol, is_st)
        limit_up = round(prev_close * (1 + limit_ratio), 2)
        limit_down = round(prev_close * (1 - limit_ratio), 2)

        # 涨停：不可买入，但可以卖出
        if price >= limit_up and direction != "sell":
            return False
        # 跌停：不可卖出，但可以买入
        if price <= limit_down and direction != "buy":  # noqa: SIM103
            return False
        return True

    @staticmethod
    def _price_limit_ratio(symbol: str, is_st: bool) -> float:
        """Determine price limit ratio based on board and ST status."""
        if is_st:
            return _ST_LIMIT
        if symbol.startswith(_GEM_PREFIXES) or symbol.startswith(_KCB_PREFIXES):
            return _GEM_KCB_LIMIT
        return _MAIN_BOARD_LIMIT
