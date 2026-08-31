"""持仓时长风险检查。

该模块只负责根据建仓日期、当前盈亏和持仓时长阈值生成风控结果，
不读写熔断状态，也不执行订单，便于独立测试和复用。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from gugu.utils.log import get_logger

from .rules import RiskAction, RiskCheckResult, RiskLevel

logger = get_logger()


class HoldingDurationChecker:
    """检查亏损持仓是否达到持仓时长预警或强制清仓阈值。"""

    def __init__(
        self,
        max_holding_days_warn: int,
        max_holding_days_halt: int,
        *,
        risk_logger: Any = logger,
    ) -> None:
        self.max_holding_days_warn = max_holding_days_warn
        self.max_holding_days_halt = max_holding_days_halt
        self._logger = risk_logger

    def check(self, symbol: str, entry_date: str, current_pnl_pct: float) -> RiskCheckResult:
        """根据自然日持仓时长返回 ALLOW、WARN 或 HALT。"""
        if not entry_date:
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.ALLOW,
                message="无建仓日期",
            )

        try:
            entry = date.fromisoformat(entry_date)
            today = date.today()
            holding_days = (today - entry).days
        except (ValueError, TypeError):
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.ALLOW,
                message=f"建仓日期格式错误: {entry_date}",
            )

        if current_pnl_pct >= 0:
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.ALLOW,
                message=f"持仓 {holding_days} 天，盈利 {current_pnl_pct:+.2%}",
            )

        if holding_days >= self.max_holding_days_halt:
            self._logger.warning(
                "P2-5: {} 持仓 {} 天未盈利（{:.2%}），强制清仓（>={} 天）",
                symbol,
                holding_days,
                current_pnl_pct,
                self.max_holding_days_halt,
            )
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.HALT,
                message=(
                    f"持仓 {holding_days} 天未盈利（{current_pnl_pct:+.2%}），"
                    f"超过 {self.max_holding_days_halt} 天上限，强制清仓"
                ),
            )

        if holding_days >= self.max_holding_days_warn:
            self._logger.warning(
                "P2-5: {} 持仓 {} 天未盈利（{:.2%}），建议减半仓（>={} 天）",
                symbol,
                holding_days,
                current_pnl_pct,
                self.max_holding_days_warn,
            )
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.WARN,
                message=(
                    f"持仓 {holding_days} 天未盈利（{current_pnl_pct:+.2%}），"
                    f"超过 {self.max_holding_days_warn} 天，建议减半仓"
                ),
            )

        return RiskCheckResult(
            level=RiskLevel.L1_POSITION,
            action=RiskAction.ALLOW,
            message=f"持仓 {holding_days} 天，亏损 {current_pnl_pct:+.2%}",
        )


__all__ = ["HoldingDurationChecker"]
