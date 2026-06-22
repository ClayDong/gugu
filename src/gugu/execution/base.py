"""交易执行接口抽象。

所有 broker（模拟盘/QMT实盘）继承此类。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from gugu.models import Position

Direction = Literal["buy", "sell"]


@dataclass
class OrderResult:
    """下单结果。"""

    success: bool
    symbol: str
    direction: Direction
    price: float
    quantity: int
    commission: float
    stamp_tax: float = 0.0
    message: str = ""


@dataclass
class AccountInfo:
    """账户信息。"""

    cash: float
    total_value: float
    positions: dict[str, Position]


class BaseBroker(ABC):
    """交易接口抽象基类。"""

    @abstractmethod
    def order(
        self, symbol: str, direction: Direction, quantity: int, price: float | None = None
    ) -> OrderResult:
        """下单。

        Args:
            symbol: 股票代码
            direction: buy/sell
            quantity: 数量（A 股 100 股整数倍）
            price: 价格，None 为市价

        Returns:
            OrderResult
        """
        ...

    @abstractmethod
    def get_position(self, symbol: str) -> Position | None:
        """获取单个持仓。"""
        ...

    @abstractmethod
    def get_portfolio(self) -> dict[str, Position]:
        """获取所有持仓。"""
        ...

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """获取账户信息。"""
        ...

    @abstractmethod
    def update_price(self, symbol: str, price: float) -> None:
        """更新持仓现价（模拟盘用，实盘自动更新）。"""
        ...
