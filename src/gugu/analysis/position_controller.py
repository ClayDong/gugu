"""仓位总控：根据市场状态 + 账户风险 + 持仓集中度，动态计算总仓位上限。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gugu.config import settings
from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class PositionBudget:
    """仓位预算"""
    total_limit: float        # 总仓位上限（占总资产比）
    single_limit: float       # 单股仓位上限
    available_budget: float   # 可用资金
    max_positions: int        # 最大持仓数
    reason: str               # 预算理由


class PositionController:
    """仓位总控：综合市场状态、账户风险、持仓集中度，决定总仓位预算。

    决策逻辑（参考专业交易员的仓位管理）：
    1. 市场状态决定基准仓位上限（bull=80%, sideways=40%, bear=20%）
    2. 账户风险调整（累计亏损>10%时仓位减半）
    3. 持仓集中度调整（已有5只以上时降低新仓上限）
    4. 日亏熔断时仓位归零
    """

    def __init__(self) -> None:
        self._max_single = float(settings().get("risk", {}).get("max_position_ratio", 0.30))
        self._max_positions = int(settings().get("risk", {}).get("max_total_positions", 5))
        self._daily_loss_halt = float(settings().get("risk", {}).get("daily_loss_halt", 0.05))

    def calculate(
        self,
        regime: dict[str, Any],
        account: Any,  # AccountInfo dataclass
        is_halted: bool = False,
        total_pnl_pct: float = 0.0,
    ) -> PositionBudget:
        """计算当前仓位预算。

        Args:
            regime: 多周期择时结果
            account: 账户信息（total_value, cash, positions）
            is_halted: 是否日亏熔断
            total_pnl_pct: 累计盈亏比例

        Returns:
            PositionBudget: 仓位预算
        """
        # 1. 熔断时零仓位
        if is_halted:
            return PositionBudget(
                total_limit=0.0, single_limit=0.0,
                available_budget=0.0, max_positions=0,
                reason="日亏熔断，禁止新开仓"
            )

        # 2. 市场状态基准
        regime_limit = regime.get("total_position_limit", 0.40)
        buy_allowed = regime.get("buy_signal_allowed", True)
        sell_required = regime.get("sell_signal_required", False)

        if not buy_allowed:
            return PositionBudget(
                total_limit=0.0, single_limit=0.0,
                available_budget=0.0, max_positions=0,
                reason=f"市场状态 {regime.get('regime')} 禁止买入"
            )

        # 3. 账户风险调整
        risk_multiplier = 1.0
        if total_pnl_pct < -0.10:
            risk_multiplier = 0.5  # 累计亏损>10%，仓位减半
            logger.warning(f"累计亏损 {total_pnl_pct:.2%}，仓位减半")
        elif total_pnl_pct < -0.05:
            risk_multiplier = 0.7  # 累计亏损>5%，仓位打7折

        total_limit = regime_limit * risk_multiplier

        # 4. 持仓集中度调整
        current_positions = len(account.positions) if hasattr(account, 'positions') else 0
        remaining_slots = max(0, self._max_positions - current_positions)
        if remaining_slots <= 0:
            total_limit = 0.0

        # 5. 计算可用预算
        total_value = account.total_value if hasattr(account, 'total_value') else 0
        available_budget = total_value * total_limit

        # 6. 单股上限（考虑剩余仓位槽位）
        if remaining_slots > 0:
            single_limit = min(self._max_single, total_limit / remaining_slots)
        else:
            single_limit = 0.0

        reason_parts = [
            f"市场{regime.get('regime')}基准{regime_limit:.0%}",
        ]
        if risk_multiplier < 1.0:
            reason_parts.append(f"风险系数{risk_multiplier:.0%}")
        if sell_required:
            reason_parts.append("建议减仓")

        return PositionBudget(
            total_limit=round(total_limit, 4),
            single_limit=round(single_limit, 4),
            available_budget=round(available_budget, 2),
            max_positions=remaining_slots,
            reason="，".join(reason_parts),
        )