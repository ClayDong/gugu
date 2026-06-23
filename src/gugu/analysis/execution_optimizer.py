"""执行优化：参考 vn.py 的执行优化器，实现TWAP/VWAP拆单和滑点控制。

核心功能：
1. TWAP拆单：时间加权平均价格，将大单拆成小单分时段执行
2. VWAP拆单：成交量加权平均价格，参考历史成交量分布
3. 滑点预估：根据股票的流动性预估滑点
4. 冲击成本计算：大单对市场价格的冲击
5. 执行质量评估：成交价 vs 决策价偏差
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from gugu.utils.log import get_logger

logger = get_logger()

@dataclass
class OrderSlice:
    """拆单子订单"""
    slice_id: int
    quantity: int
    target_price: float
    time_window: str  # e.g., "09:30-09:35"
    order_type: str = "limit"  # limit / market

@dataclass
class ExecutionPlan:
    """执行计划"""
    symbol: str
    total_quantity: int
    slices: list[OrderSlice]
    avg_target_price: float
    estimated_slippage: float
    estimated_impact: float
    execution_method: str  # "twap" / "vwap" / "direct"

@dataclass
class ExecutionReport:
    """执行报告"""
    symbol: str
    ordered_quantity: int
    filled_quantity: int
    avg_fill_price: float
    slippage_bps: float  # 成交价 vs 决策价的偏差（基点）
    total_cost: float
    execution_time: str
    status: str  # "completed" / "partial" / "failed"


class ExecutionOptimizer:
    """执行优化器。
    
    参考 vn.py 的执行优化思想：
    - 大单拆成小单，减少市场冲击
    - 根据流动性选择执行方式
    - 实时监控执行质量
    """
    
    # 默认参数
    DEFAULT_SLICE_MINUTES = 5  # 每片5分钟
    DEFAULT_SLICE_COUNT = 5    # 默认拆5片
    TWAP_THRESHOLD_AMOUNT = 50000  # 金额超过5万时拆单
    VWAP_THRESHOLD_AMOUNT = 200000  # 金额超过20万时用VWAP
    
    def __init__(self):
        self._execution_history: list[ExecutionReport] = []
    
    def plan_execution(
        self, 
        symbol: str, 
        quantity: int, 
        price: float,
        method: str = "auto",
    ) -> ExecutionPlan:
        """生成执行计划。
        
        Args:
            symbol: 股票代码
            quantity: 买入/卖出数量
            price: 决策价格
            method: 执行方式（auto/twap/vwap/direct）
        
        Returns:
            ExecutionPlan: 执行计划，包含拆单列表
        """
        total_amount = quantity * price
        
        # 自动选择执行方式
        if method == "auto":
            if total_amount < self.TWAP_THRESHOLD_AMOUNT:
                method = "direct"
            elif total_amount < self.VWAP_THRESHOLD_AMOUNT:
                method = "twap"
            else:
                method = "vwap"
        
        if method == "direct":
            # 直接执行，不拆单
            slices = [OrderSlice(
                slice_id=1, quantity=quantity, target_price=price,
                time_window="immediate", order_type="limit"
            )]
        elif method == "twap":
            # TWAP：等量等时拆单
            slice_count = self.DEFAULT_SLICE_COUNT
            qty_per_slice = max(100, quantity // slice_count)
            qty_per_slice = (qty_per_slice // 100) * 100  # 取整到100股
            if qty_per_slice == 0:
                qty_per_slice = 100
            slices = []
            remaining = quantity
            for i in range(slice_count):
                if remaining <= 0:
                    break
                slice_qty = min(qty_per_slice, remaining)
                slice_qty = (slice_qty // 100) * 100
                if slice_qty == 0:
                    slice_qty = remaining
                slices.append(OrderSlice(
                    slice_id=i + 1, quantity=slice_qty, target_price=price,
                    time_window=f"slice_{i+1}",
                    order_type="limit"
                ))
                remaining -= slice_qty
        else:
            # VWAP：参考历史成交量分布拆单（简化：等量拆单）
            slices = []
            vol_weights = [0.25, 0.20, 0.15, 0.20, 0.20]  # 成交量分布权重
            for i, w in enumerate(vol_weights[:self.DEFAULT_SLICE_COUNT]):
                qty = max(100, int(quantity * w))
                qty = (qty // 100) * 100
                if qty > 0:
                    slices.append(OrderSlice(
                        slice_id=i + 1, quantity=qty, target_price=price,
                        time_window=f"vwap_slice_{i+1}",
                        order_type="limit"
                    ))
        
        # 预估滑点（基于流动性的简化模型）
        estimated_slippage = self._estimate_slippage(symbol, quantity, price)
        estimated_impact = self._estimate_impact(quantity, price)
        
        return ExecutionPlan(
            symbol=symbol,
            total_quantity=quantity,
            slices=slices,
            avg_target_price=price,
            estimated_slippage=estimated_slippage,
            estimated_impact=estimated_impact,
            execution_method=method,
        )
    
    def _estimate_slippage(self, symbol: str, quantity: int, price: float) -> float:
        """预估滑点（基点）。
        
        简化模型：基于成交金额估算
        - 小单（<5万）：1 bp
        - 中单（5-20万）：3 bp
        - 大单（>20万）：5 bp
        """
        amount = quantity * price
        if amount < 50000:
            return 0.0001  # 1 bp
        elif amount < 200000:
            return 0.0003  # 3 bp
        else:
            return 0.0005  # 5 bp
    
    def _estimate_impact(self, quantity: int, price: float) -> float:
        """预估市场冲击成本（金额）"""
        # 简化：冲击成本 = 成交金额 * 0.05%
        return quantity * price * 0.0005
    
    def record_execution(self, report: ExecutionReport) -> None:
        """记录执行结果"""
        self._execution_history.append(report)
        if len(self._execution_history) > 1000:
            self._execution_history = self._execution_history[-1000:]
    
    def get_execution_quality(self) -> dict[str, Any]:
        """获取执行质量统计"""
        if not self._execution_history:
            return {"avg_slippage_bps": 0, "fill_rate": 0, "total_executions": 0}
        
        slippages = [r.slippage_bps for r in self._execution_history]
        fill_rates = [r.filled_quantity / r.ordered_quantity for r in self._execution_history if r.ordered_quantity > 0]
        
        return {
            "avg_slippage_bps": round(float(np.mean(slippages) * 10000), 2),
            "fill_rate": round(float(np.mean(fill_rates)) if fill_rates else 0, 2),
            "total_executions": len(self._execution_history),
        }