"""尾盘选股交易模拟器。

将 T+1 日验证数据（开盘/最高/最低/收盘）转化为可执行的策略收益。

业务逻辑（第一性原理，由 113 条验证数据推导）：
- T 日尾盘选股（信号有效：日内冲高率 86%，盘中最高超额 +2.04%）
- T+1 开盘低吸买入（68% 低开 = 好买点）
- 日内 +2% 止盈 / -3% 止损 / 收盘平仓（当日了结不隔夜）

对抗性审查：
- 止盈和止损同日都触发时（无法确定先后）→ 保守取止损，避免高估期望值
- 数据异常（开盘价 0、最高<最低）→ 返回 None 不产生交易

大盘开盘门控：
- T+1 开盘沪深300 跌幅超过阈值（默认 -0.5%）→ 放弃买入
  依据：7/30 大盘开盘 -0.74%，选中股票收盘平均 -9.58%（跌得比大盘狠 10 倍）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeResult:
    """一次模拟交易的结果。"""

    exit_reason: str  # "take_profit" | "stop_loss" | "close"
    return_pct: float  # 收益率（%）


@dataclass
class MarketGateResult:
    """大盘开盘门控结果。"""

    blocked: bool  # True = 放弃买入
    reason: str  # 阻断原因（"" 表示未阻断）


def simulate_trade(
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    take_profit_pct: float = 2.0,
    stop_loss_pct: float = 3.0,
) -> TradeResult | None:
    """模拟 T+1 开盘买入 → 日内止盈/止损/收盘平仓。

    Args:
        open_price: T+1 开盘价（买入价）
        high_price: T+1 日内最高价
        low_price: T+1 日内最低价
        close_price: T+1 收盘价
        take_profit_pct: 止盈阈值（%），默认 +2.0
        stop_loss_pct: 止损阈值（%），默认 -3.0

    Returns:
        TradeResult 或 None（数据异常不产生交易）
    """
    # 数据有效性检查
    if open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0:
        return None
    if high_price < low_price:
        return None

    tp_price = open_price * (1 + take_profit_pct / 100)
    sl_price = open_price * (1 - stop_loss_pct / 100)

    tp_hit = high_price >= tp_price
    sl_hit = low_price <= sl_price

    if tp_hit and sl_hit:
        # 同日都触发：保守取止损（无法确定先后，不高估期望值）
        return TradeResult(exit_reason="stop_loss", return_pct=-stop_loss_pct)
    if tp_hit:
        return TradeResult(exit_reason="take_profit", return_pct=take_profit_pct)
    if sl_hit:
        return TradeResult(exit_reason="stop_loss", return_pct=-stop_loss_pct)

    # 未触发：收盘平仓
    return_pct = (close_price - open_price) / open_price * 100
    return TradeResult(exit_reason="close", return_pct=round(return_pct, 2))


def check_market_gate(
    benchmark_open_change: float,
    skip_threshold: float | None = -0.5,
) -> MarketGateResult:
    """大盘开盘门控：开盘跌幅超阈值 → 放弃买入。

    Args:
        benchmark_open_change: 沪深300 开盘涨幅（%）
        skip_threshold: 阻断阈值（%），None 表示关闭门控

    Returns:
        MarketGateResult
    """
    if skip_threshold is None:
        return MarketGateResult(blocked=False, reason="")
    if benchmark_open_change < skip_threshold:
        return MarketGateResult(blocked=True, reason="market_open_drop")
    return MarketGateResult(blocked=False, reason="")
