"""移动止损管理器：跟踪持仓最高价，动态上移止损线。

根据《炒股的智慧》策略体系：
- 初始止损：入场价下方 ATR*2 或 8%（取较小值）
- 移动止损：每创出新高后，止损上移至最近一波回调低点下方
- 浮盈超 20% 后，保本止损（不允许持仓由盈利变亏损）
- 5日时间止损：买入后5个交易日无盈利，无条件离场
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()

# 默认参数
_DEFAULT_INITIAL_STOP_PCT = 0.08  # 初始止损幅度 8%
_DEFAULT_ATR_MULTIPLIER = 2.0  # ATR 倍数
_DEFAULT_PROFIT_PROTECT_PCT = 0.20  # 浮盈 20% 后保本止损
_TIME_STOP_DAYS = 5  # 5日时间止损


class TrailingStopManager:
    """移动止损管理器，跟踪每只持仓的动态止损线。

    用法::

        manager = TrailingStopManager()
        manager.init_position("600519", entry_price=1500.0, entry_date="2026-07-01", atr=30.0)
        result = manager.update("600519", current_price=1550.0, current_high=1560.0,
                                current_date="2026-07-02", atr=32.0)
        if result["should_stop"]:
            # 执行止损卖出
    """

    def __init__(self) -> None:
        # _positions: {symbol: {entry_price, entry_date, highest_price, atr, stop_price}}
        self._positions: dict[str, dict[str, Any]] = {}

    def init_position(
        self,
        symbol: str,
        entry_price: float,
        entry_date: str,
        atr: float = 0.0,
    ) -> None:
        """初始化一只股票的止损跟踪。

        Args:
            symbol: 股票代码
            entry_price: 入场价
            entry_date: 入场日期 (YYYY-MM-DD)
            atr: ATR 值，用于计算动态止损
        """
        # 初始止损价：取 ATR*2 和固定 8% 中较小值
        atr_stop = entry_price - atr * _DEFAULT_ATR_MULTIPLIER if atr > 0 else entry_price * (1 - _DEFAULT_INITIAL_STOP_PCT)
        pct_stop = entry_price * (1 - _DEFAULT_INITIAL_STOP_PCT)
        stop_price = min(atr_stop, pct_stop)

        self._positions[symbol] = {
            "entry_price": entry_price,
            "entry_date": entry_date,
            "highest_price": entry_price,
            "atr": atr,
            "stop_price": stop_price,
        }
        logger.info(
            f"初始化止损 {symbol}: entry={entry_price:.2f}, "
            f"stop={stop_price:.2f}, atr={atr:.2f}"
        )

    def update(
        self,
        symbol: str,
        current_price: float,
        current_high: float,
        current_date: str,
        atr: float = 0.0,
    ) -> dict[str, Any]:
        """更新止损线并判断是否触发止损。

        Args:
            symbol: 股票代码
            current_price: 当前价
            current_high: 当日最高价
            current_date: 当前日期 (YYYY-MM-DD)
            atr: 最新 ATR 值

        Returns:
            {"stop_price": float, "should_stop": bool, "reason": str}
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return {"stop_price": 0.0, "should_stop": False, "reason": "未初始化止损"}

        # 更新最高价
        if current_high > pos["highest_price"]:
            pos["highest_price"] = current_high

        # 更新 ATR
        if atr > 0:
            pos["atr"] = atr

        entry_price = pos["entry_price"]
        highest = pos["highest_price"]
        current_stop = pos["stop_price"]

        # 计算浮盈比例
        profit_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0

        # 1. 浮盈超 20%：保本止损（止损不低于入场价）
        if profit_pct >= _DEFAULT_PROFIT_PROTECT_PCT:
            new_stop = max(current_stop, entry_price)
            reason = "保本止损（浮盈超20%，锁住本金）"
        # 2. 正常移动止损：止损跟随最高价下移 ATR*2
        elif pos["atr"] > 0:
            trailing_stop = highest - pos["atr"] * _DEFAULT_ATR_MULTIPLIER
            new_stop = max(current_stop, trailing_stop)
            reason = f"移动止损（ATR跟踪，highest={highest:.2f}）"
        else:
            new_stop = current_stop
            reason = "初始止损"

        pos["stop_price"] = new_stop

        # 判断是否触发止损
        should_stop = current_price <= new_stop
        if should_stop:
            reason = f"触发止损：当前价 {current_price:.2f} <= 止损价 {new_stop:.2f}，{reason}"

        return {
            "stop_price": new_stop,
            "should_stop": should_stop,
            "reason": reason,
        }

    def check_time_stop(self, symbol: str, current_date: str) -> dict[str, Any]:
        """检查5日时间止损：买入后5个交易日无盈利则离场。

        Args:
            symbol: 股票代码
            current_date: 当前日期 (YYYY-MM-DD)

        Returns:
            {"should_stop": bool, "reason": str}
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return {"should_stop": False, "reason": "未初始化止损"}

        try:
            entry_dt = datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
            current_dt = datetime.strptime(current_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return {"should_stop": False, "reason": "日期格式错误"}

        holding_days = (current_dt - entry_dt).days

        if holding_days >= _TIME_STOP_DAYS:
            entry_price = pos["entry_price"]
            # 5日无盈利（当前止损价仍低于入场价，说明没有实质盈利）
            if pos["stop_price"] < entry_price:
                return {
                    "should_stop": True,
                    "reason": f"5日时间止损：持有 {holding_days} 日无盈利",
                }

        return {"should_stop": False, "reason": "未触发时间止损"}

    def remove(self, symbol: str) -> None:
        """清除持仓的止损跟踪记录。"""
        if symbol in self._positions:
            del self._positions[symbol]

    def get_state(self, symbol: str) -> dict[str, Any] | None:
        """获取某只股票的止损状态。"""
        return self._positions.get(symbol)

    def get_all_states(self) -> dict[str, dict[str, Any]]:
        """获取所有持仓的止损状态。"""
        return dict(self._positions)
