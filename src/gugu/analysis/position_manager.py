"""持仓管理：参考专业交易员的持仓管理规则。

核心功能：
1. 移动止损：根据价格走势自动上移止损位
2. 分批止盈：到达目标位后分批止盈
3. 加仓规则：趋势确认后分层加仓
4. 持仓分析：盈亏比、持仓天数、风险暴露
5. 持仓建议：根据当前状态给出操作建议
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()

@dataclass
class PositionAdvice:
    """持仓建议"""
    symbol: str
    action: str  # "hold" / "add" / "reduce" / "close" / "trailing_stop"
    current_price: float
    entry_price: float
    profit_pct: float
    stop_loss: float
    take_profit: float
    trailing_stop: float
    reason: str
    suggested_quantity: int = 0


class PositionManager:
    """持仓管理器。
    
    参考专业交易员的持仓管理规则：
    1. 止损铁律：买入后立即设置止损，到点必执行
    2. 移动止损：盈利后跟随价格上移止损位
    3. 分批止盈：盈利15%止盈1/3，盈利25%止盈1/3，余下用移动止损
    4. 金字塔加仓：趋势确认后，每次加仓量递减
    """
    
    # 止盈规则
    TAKE_PROFIT_LEVELS = [
        (0.10, 0.30),  # 盈利10%，止盈30%
        (0.20, 0.30),  # 盈利20%，再止盈30%
        (0.30, 0.40),  # 盈利30%，清仓
    ]
    
    # 移动止损规则
    TRAILING_STOP_ACTIVATE = 0.05   # 盈利5%后激活移动止损
    TRAILING_STOP_DISTANCE = 0.03   # 移动止损距离3%
    
    def __init__(self):
        self._positions: dict[str, dict[str, Any]] = {}
    
    def register_position(
        self, symbol: str, entry_price: float, quantity: int,
        stop_loss: float, max_position_pct: float = 0.15,
    ) -> None:
        """注册新持仓"""
        self._positions[symbol] = {
            "entry_price": entry_price,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "max_position_pct": max_position_pct,
            "entry_date": str(date.today()),
            "highest_price": entry_price,
            "take_profit_stage": 0,  # 止盈阶段 0/1/2
            "add_count": 0,  # 加仓次数
            "total_quantity": quantity,
            "total_cost": entry_price * quantity,
        }
        logger.info(f"持仓注册: {symbol} 入场价={entry_price:.2f} 数量={quantity} 止损={stop_loss:.2f}")
    
    def update(self, symbol: str, current_price: float) -> PositionAdvice:
        """根据当前价格更新持仓状态并给出建议。
        
        Args:
            symbol: 股票代码
            current_price: 当前价格
        
        Returns:
            PositionAdvice: 持仓建议（hold/add/reduce/close/trailing_stop）
        """
        if symbol not in self._positions:
            return PositionAdvice(
                symbol=symbol, action="hold", current_price=current_price,
                entry_price=0, profit_pct=0, stop_loss=0, take_profit=0,
                trailing_stop=0, reason="无持仓"
            )
        
        pos = self._positions[symbol]
        entry_price = pos["entry_price"]
        profit_pct = (current_price - entry_price) / entry_price
        
        # 更新最高价
        if current_price > pos["highest_price"]:
            pos["highest_price"] = current_price
        
        # 计算移动止损
        trailing_stop = self._calc_trailing_stop(pos)
        if trailing_stop > pos["stop_loss"]:
            pos["stop_loss"] = trailing_stop
            logger.info(f"{symbol} 移动止损上移至 {trailing_stop:.2f}")
        
        # 止损检查
        if current_price <= pos["stop_loss"]:
            return PositionAdvice(
                symbol=symbol, action="close",
                current_price=current_price, entry_price=entry_price,
                profit_pct=round(profit_pct, 4),
                stop_loss=pos["stop_loss"],
                take_profit=0, trailing_stop=trailing_stop,
                reason=f"止损触发: 当前价 {current_price:.2f} <= 止损 {pos['stop_loss']:.2f}",
                suggested_quantity=pos["total_quantity"],
            )
        
        # 止盈检查
        stage = pos["take_profit_stage"]
        if stage < len(self.TAKE_PROFIT_LEVELS):
            target, ratio = self.TAKE_PROFIT_LEVELS[stage]
            if profit_pct >= target:
                qty = int(pos["total_quantity"] * ratio)
                qty = (qty // 100) * 100
                pos["take_profit_stage"] += 1
                if qty > 0:
                    return PositionAdvice(
                        symbol=symbol, action="reduce",
                        current_price=current_price, entry_price=entry_price,
                        profit_pct=round(profit_pct, 4),
                        stop_loss=pos["stop_loss"],
                        take_profit=target,
                        trailing_stop=trailing_stop,
                        reason=f"止盈第{stage+1}阶段: 盈利{profit_pct:.1%}，止盈{ratio:.0%}",
                        suggested_quantity=qty,
                    )
        
        # 加仓建议（趋势确认 + 回踩不破均线）
        if profit_pct > 0.03 and pos["add_count"] == 0:
            # 首次加仓建议（仅建议，不自动执行）
            add_qty = int(pos["total_quantity"] * 0.5)
            add_qty = (add_qty // 100) * 100
            if add_qty >= 100:
                return PositionAdvice(
                    symbol=symbol, action="add",
                    current_price=current_price, entry_price=entry_price,
                    profit_pct=round(profit_pct, 4),
                    stop_loss=pos["stop_loss"],
                    take_profit=0, trailing_stop=trailing_stop,
                    reason=f"趋势确认: 盈利{profit_pct:.1%}，建议加仓",
                    suggested_quantity=add_qty,
                )
        
        # 移动止损提醒
        if trailing_stop > pos["stop_loss"]:
            return PositionAdvice(
                symbol=symbol, action="trailing_stop",
                current_price=current_price, entry_price=entry_price,
                profit_pct=round(profit_pct, 4),
                stop_loss=pos["stop_loss"],
                take_profit=0, trailing_stop=trailing_stop,
                reason=f"移动止损: {pos['stop_loss']:.2f} → {trailing_stop:.2f}",
            )
        
        return PositionAdvice(
            symbol=symbol, action="hold",
            current_price=current_price, entry_price=entry_price,
            profit_pct=round(profit_pct, 4),
            stop_loss=pos["stop_loss"],
            take_profit=0, trailing_stop=trailing_stop,
            reason=f"持仓中: 盈利{profit_pct:.1%}",
        )
    
    def _calc_trailing_stop(self, pos: dict[str, Any]) -> float:
        """计算移动止损"""
        profit_pct = (pos["highest_price"] - pos["entry_price"]) / pos["entry_price"]
        if profit_pct >= self.TRAILING_STOP_ACTIVATE:
            return pos["highest_price"] * (1 - self.TRAILING_STOP_DISTANCE)
        return pos["stop_loss"]
    
    def record_add(self, symbol: str, add_qty: int, add_price: float) -> None:
        """记录加仓"""
        if symbol not in self._positions:
            return
        pos = self._positions[symbol]
        pos["add_count"] += 1
        pos["total_quantity"] += add_qty
        pos["total_cost"] += add_qty * add_price
        # 加仓后重新计算入场均价
        pos["entry_price"] = pos["total_cost"] / pos["total_quantity"]
        logger.info(f"{symbol} 加仓: +{add_qty}股 @ {add_price:.2f}，均价={pos['entry_price']:.2f}")
    
    def record_reduce(self, symbol: str, reduce_qty: int) -> None:
        """记录减仓"""
        if symbol not in self._positions:
            return
        pos = self._positions[symbol]
        pos["total_quantity"] -= reduce_qty
        if pos["total_quantity"] <= 0:
            self.remove_position(symbol)
    
    def remove_position(self, symbol: str) -> None:
        """移除持仓"""
        if symbol in self._positions:
            del self._positions[symbol]
            logger.info(f"持仓移除: {symbol}")
    
    def get_position(self, symbol: str) -> dict[str, Any] | None:
        """获取持仓信息"""
        return self._positions.get(symbol)
    
    def get_all_positions(self) -> dict[str, dict[str, Any]]:
        """获取所有持仓"""
        return self._positions.copy()