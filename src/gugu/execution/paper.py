"""模拟盘 broker。

内存中维护现金、持仓、交易记录。模拟真实交易成本。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from gugu.config import settings
from gugu.execution.base import AccountInfo, BaseBroker, Direction, OrderResult
from gugu.models import Position
from gugu.utils.log import get_logger

logger = get_logger()


class PaperBroker(BaseBroker):
    """模拟盘 broker。"""

    def __init__(
        self,
        initial_capital: float | None = None,
        commission_rate: float | None = None,
        stamp_tax: float | None = None,
        slippage: float | None = None,
    ) -> None:
        cfg = settings().get("execution", {}).get("paper", {})
        self._cash = float(initial_capital or cfg.get("initial_capital", 1_000_000))
        self._commission_rate = float(commission_rate or cfg.get("commission_rate", 0.00025))
        self._stamp_tax = float(stamp_tax or cfg.get("stamp_tax", 0.001))
        self._slippage = float(slippage or cfg.get("slippage", 0.002))
        self._positions: dict[str, Position] = {}
        self._trades: list[dict[str, Any]] = []

    def order(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        price: float | None = None,
    ) -> OrderResult:
        """下单。模拟成交，扣手续费/印花税/滑点。"""
        symbol = symbol.strip().zfill(6)
        if quantity <= 0 or quantity % 100 != 0:
            return OrderResult(
                False, symbol, direction, 0, 0, 0, message="数量必须为 100 的正整数倍"
            )

        # 获取现价
        cur_price = price
        if cur_price is None:
            pos = self._positions.get(symbol)
            cur_price = pos.current_price if pos else 0.0
        if cur_price <= 0:
            return OrderResult(False, symbol, direction, 0, 0, 0, message="无可用价格")

        if direction == "buy":
            fill_price = cur_price * (1 + self._slippage)
            commission = fill_price * quantity * self._commission_rate
            total_cost = fill_price * quantity + commission
            if total_cost > self._cash:
                return OrderResult(
                    False, symbol, direction, 0, 0, 0, message="资金不足"
                )

            self._cash -= total_cost
            pos = self._positions.get(symbol)
            if pos:
                new_qty = pos.quantity + quantity
                pos.avg_cost = (
                    (pos.avg_cost * pos.quantity + fill_price * quantity) / new_qty
                )
                pos.quantity = new_qty
                # T+1：新买的当日不可卖
                pos.available = pos.available
            else:
                self._positions[symbol] = Position(
                    symbol=symbol,
                    quantity=quantity,
                    available=0,  # T+1
                    avg_cost=fill_price,
                    current_price=cur_price,
                )

            result = OrderResult(
                True, symbol, direction, fill_price, quantity, commission, 0, "买入成功"
            )
            logger.info(
                f"[模拟盘] 买入 {symbol} {quantity}股 @ {fill_price:.2f} 佣金 {commission:.2f}"
            )

        elif direction == "sell":
            pos = self._positions.get(symbol)
            if not pos or pos.available < quantity:
                return OrderResult(
                    False, symbol, direction, 0, 0, 0, message="可卖数量不足（T+1）"
                )

            fill_price = cur_price * (1 - self._slippage)
            commission = fill_price * quantity * self._commission_rate
            stamp_tax = fill_price * quantity * self._stamp_tax
            proceeds = fill_price * quantity - commission - stamp_tax

            self._cash += proceeds
            pos.quantity -= quantity
            pos.available -= quantity
            if pos.quantity <= 0:
                del self._positions[symbol]

            result = OrderResult(
                True,
                symbol,
                direction,
                fill_price,
                quantity,
                commission,
                stamp_tax,
                "卖出成功",
            )
            logger.info(
                f"[模拟盘] 卖出 {symbol} {quantity}股 @ {fill_price:.2f} "
                f"佣金 {commission:.2f} 印花税 {stamp_tax:.2f}"
            )
        else:
            return OrderResult(
                False, symbol, direction, 0, 0, 0, message=f"未知方向: {direction}"
            )

        self._trades.append(
            {
                "date": date.today().isoformat(),
                "symbol": symbol,
                "direction": direction,
                "price": result.price,
                "quantity": quantity,
                "commission": result.commission,
                "stamp_tax": result.stamp_tax,
            }
        )
        return result

    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol.strip().zfill(6))

    def get_portfolio(self) -> dict[str, Position]:
        return dict(self._positions)

    def get_account(self) -> AccountInfo:
        positions_value = sum(p.market_value for p in self._positions.values())
        return AccountInfo(
            cash=self._cash,
            total_value=self._cash + positions_value,
            positions=dict(self._positions),
        )

    def update_price(self, symbol: str, price: float) -> None:
        symbol = symbol.strip().zfill(6)
        pos = self._positions.get(symbol)
        if pos:
            pos.current_price = price

    def update_prices(self, prices: dict[str, float]) -> None:
        """批量更新现价。"""
        for sym, price in prices.items():
            self.update_price(sym, price)

    def settle_t_plus_1(self) -> None:
        """T+1 结算：每日开盘前调用，持仓全部变为可卖。"""
        for pos in self._positions.values():
            pos.available = pos.quantity

    @property
    def trades(self) -> list[dict[str, Any]]:
        """历史交易记录。"""
        return list(self._trades)
