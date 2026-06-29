"""向下摊平检查器：禁止在亏损仓位上加码。

这是多本股书的共识铁律：
- 陈江挺："绝不向下摊平。犯了错，不是老老实实地认错，重新开始，
  而是抱着侥幸心理向下摊平——这是破产的捷径。"
- 利弗莫尔："不要摊平亏损，在亏损的仓位上绝对不要加码。
  这是我一生中违背最多但也最深刻的教训。"
- 海龟交易法则："我绝不会在亏损之后加大仓位试图'捞回来'。
  这是交易者最常见的自杀行为。"

实现逻辑：
- 检查当前持仓是否处于亏损状态
- 如果亏损，阻止任何买入信号
- 提供明确的拒绝理由
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class AverageDownCheckResult:
    """向下摊平检查结果。"""
    allowed: bool          # 是否允许加仓
    reason: str            # 检查理由
    current_cost: float    # 当前成本价
    current_price: float   # 当前现价
    profit_pct: float      # 浮动盈亏比例


class NoAverageDownChecker:
    """向下摊平检查器。

    在买入信号执行前检查：如果该股票已有持仓且处于亏损状态，
    则拒绝加仓（向下摊平）。

    例外情况：
    - 无持仓时正常买入（不存在摊平问题）
    - 持仓盈利时正常加仓（顺势加仓，不是摊平）
    - 持仓微亏（<2%）时允许加仓（在止损范围内，不算摊平）
    """

    # 微亏容忍阈值：亏损<2%时不阻止加仓
    MICRO_LOSS_THRESHOLD = -0.02

    def check(
        self,
        symbol: str,
        has_position: bool,
        cost_price: float,
        current_price: float,
        quantity: int = 0,
    ) -> AverageDownCheckResult:
        """检查是否允许加仓。

        Args:
            symbol: 股票代码
            has_position: 是否已有持仓
            cost_price: 持仓成本价
            current_price: 当前现价
            quantity: 持仓数量

        Returns:
            AverageDownCheckResult 检查结果
        """
        # 无持仓 → 允许买入
        if not has_position or quantity <= 0:
            return AverageDownCheckResult(
                allowed=True,
                reason="无持仓，正常买入",
                current_cost=0.0,
                current_price=current_price,
                profit_pct=0.0,
            )

        # 计算浮动盈亏
        if cost_price <= 0:
            return AverageDownCheckResult(
                allowed=True,
                reason="成本价异常，允许买入",
                current_cost=cost_price,
                current_price=current_price,
                profit_pct=0.0,
            )

        profit_pct = (current_price - cost_price) / cost_price

        # 盈利中 → 允许加仓（顺势加仓）
        if profit_pct > 0:
            return AverageDownCheckResult(
                allowed=True,
                reason=f"持仓盈利 {profit_pct:.2%}，允许顺势加仓",
                current_cost=cost_price,
                current_price=current_price,
                profit_pct=profit_pct,
            )

        # 微亏（< 2%）→ 允许加仓
        if profit_pct >= self.MICRO_LOSS_THRESHOLD:
            return AverageDownCheckResult(
                allowed=True,
                reason=f"持仓微亏 {profit_pct:.2%}（在容忍范围内），允许加仓",
                current_cost=cost_price,
                current_price=current_price,
                profit_pct=profit_pct,
            )

        # 明确亏损 → 拒绝加仓
        logger.warning(
            f"[no-avg-down] {symbol} 持仓亏损 {profit_pct:.2%}，"
            f"拒绝向下摊平（成本={cost_price:.2f}, 现价={current_price:.2f}）"
        )
        return AverageDownCheckResult(
            allowed=False,
            reason=(
                f"持仓亏损 {profit_pct:.2%}，禁止向下摊平。"
                f"陈江挺/利弗莫尔/海龟法则共识：在亏损仓位上加码是破产的捷径。"
                f"应止损或等待盈利后再加仓。"
            ),
            current_cost=cost_price,
            current_price=current_price,
            profit_pct=profit_pct,
        )
