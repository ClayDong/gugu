"""公共数据模型：持仓。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    """持仓信息。

    Attributes:
        symbol: 股票代码，如 "600519"。
        quantity: 总持仓数量。
        available: T+1 可卖数量。
        avg_cost: 平均成本。
        current_price: 最新价。
        stop_loss_price: 止损价（由 wisdom 预设，0 表示未设置）。
        prev_close: 前收盘价（L3 涨跌停检查用，0 表示未知）。
        is_st: 是否 ST（影响涨跌停幅度）。
        is_suspended: 是否停牌。
        dividend_adjusted: 是否经过除权除息调整。
    """

    symbol: str
    quantity: int
    available: int
    avg_cost: float
    current_price: float = 0.0
    stop_loss_price: float = 0.0
    prev_close: float = 0.0
    is_st: bool = False
    is_suspended: bool = False
    # 移动止损状态（JSON dict，由 TrailingStopEngine 管理）
    trailing_stop: dict | None = None
    # 危险信号记录
    danger_signals: list[str] | None = None
    # P1 修复：持仓以来最低价（用于 NoAverageDownChecker 累计最大浮亏检查）
    # PaperBroker.update_price 在 price < lowest_price 时自动更新
    lowest_price: float = 0.0
    # 是否经过除权除息调整
    dividend_adjusted: bool = False

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def profit(self) -> float:
        return (self.current_price - self.avg_cost) * self.quantity

    @property
    def profit_ratio(self) -> float:
        if self.avg_cost == 0:
            return 0.0
        return (self.current_price - self.avg_cost) / self.avg_cost
