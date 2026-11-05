"""仓位总控：根据市场状态 + 账户风险 + 持仓集中度，动态计算总仓位上限。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gugu.config import get_config
from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class PositionBudget:
    """仓位预算"""

    total_limit: float  # 总仓位上限（占总资产比）
    single_limit: float  # 单股仓位上限
    available_budget: float  # 可用资金
    max_positions: int  # 最大持仓数
    concentration_hhi: float = 0.0  # P2-2：持仓市值集中度 HHI 指数
    max_single_weight: float = 0.0  # P2-2：最大单股市值占比
    reason: str = ""  # 预算理由


class PositionController:
    """仓位总控：综合市场状态、账户风险、持仓集中度，决定总仓位预算。

    决策逻辑（参考专业交易员的仓位管理）：
    1. 市场状态决定基准仓位上限（bull=80%, sideways=40%, bear=20%）
    2. 总仓位上限检查（max_total_position_ratio，默认70%）
    3. 账户风险调整（P2-4 改用最大回撤衡量，回退兼容收益率）
    4. 持仓集中度调整（P2-2 改用市值权重 HHI 指数 + 数量双重限制）
    5. 日亏熔断时仓位归零
    """

    def __init__(self) -> None:
        rc = get_config().risk
        self._max_single = rc.max_position_ratio
        self._max_total = getattr(rc, "max_total_position_ratio", 0.70)  # P0-3 新增：总仓位上限
        self._max_positions = rc.max_total_positions
        self._daily_loss_halt = rc.daily_loss_halt
        # P2-2：集中度 HHI 阈值（0.25 = 4 只等权股的集中度，>0.25 视为偏高）
        self._concentration_hhi_warn = getattr(rc, "concentration_hhi_warn", 0.25)
        # P2-2：最大单股市值占比预警阈值（默认 25%，超过则降低单股上限）
        self._max_single_weight_warn = getattr(rc, "max_single_weight_warn", 0.25)
        # P2-4：回撤阈值（从历史高点回撤超过该比例则降仓）
        self._drawdown_halt = getattr(rc, "max_drawdown_halt", 0.15)  # 回撤 15% → 仓位减半
        self._drawdown_warn = getattr(rc, "max_drawdown_warn", 0.08)  # 回撤 8% → 仓位 7 折

    @staticmethod
    def _calc_concentration_metrics(account: Any) -> tuple[float, float]:
        """P2-2：计算持仓市值集中度指标（HHI 指数 + 最大单股占比）。

        Herfindahl-Hirschman Index (HHI) = Σ(w_i²)
        - 5 只等权股 = 5 × 0.2² = 0.2
        - 3 只等权股 = 3 × 0.333² ≈ 0.333
        - 1 只全仓 = 1.0
        通常 HHI > 0.25 视为集中度偏高。

        Returns:
            (hhi, max_single_weight): HHI 指数和最大单股占总资产比例
        """
        hhi = 0.0
        max_weight = 0.0
        try:
            total_val = float(account.total_value) if hasattr(account, "total_value") else 0.0
            positions = getattr(account, "positions", {})
            if total_val <= 0 or not positions:
                return 0.0, 0.0
            for pos in positions.values() if hasattr(positions, "values") else positions:
                market_val = 0.0
                if hasattr(pos, "market_value"):
                    market_val = float(pos.market_value)
                elif hasattr(pos, "quantity") and hasattr(pos, "current_price"):
                    market_val = float(pos.quantity) * float(pos.current_price)
                elif hasattr(pos, "avg_cost") and hasattr(pos, "quantity"):
                    # 回退到成本价估算
                    market_val = float(pos.avg_cost) * float(pos.quantity)
                if market_val > 0:
                    weight = market_val / total_val
                    hhi += weight * weight
                    max_weight = max(max_weight, weight)
        except (TypeError, ValueError, AttributeError):
            pass
        return round(hhi, 4), round(max_weight, 4)

    def calculate(
        self,
        regime: dict[str, Any],
        account: Any,  # AccountInfo dataclass
        is_halted: bool = False,
        total_pnl_pct: float = 0.0,
        max_drawdown: float | None = None,
    ) -> PositionBudget:
        """计算当前仓位预算。

        Args:
            regime: 多周期择时结果
            account: 账户信息（total_value, cash, positions）
            is_halted: 是否日亏熔断
            total_pnl_pct: 累计盈亏比例（回退兼容，优先用 max_drawdown）
            max_drawdown: P2-4 最大回撤（从历史高点的跌幅），优先使用

        Returns:
            PositionBudget: 仓位预算
        """
        # 1. 熔断时零仓位
        if is_halted:
            return PositionBudget(
                total_limit=0.0,
                single_limit=0.0,
                available_budget=0.0,
                max_positions=0,
                reason="日亏熔断，禁止新开仓",
            )

        # 2. 市场状态基准
        regime_limit = regime.get("total_position_limit", 0.40)
        buy_allowed = regime.get("buy_signal_allowed", True)
        sell_required = regime.get("sell_signal_required", False)

        if not buy_allowed:
            return PositionBudget(
                total_limit=0.0,
                single_limit=0.0,
                available_budget=0.0,
                max_positions=0,
                reason=f"市场状态 {regime.get('regime')} 禁止买入",
            )

        # 3. P0-3 新增：总仓位上限检查
        # 检查当前持仓是否已超过总仓位上限
        current_position_ratio = 0.0
        try:
            total_val = float(account.total_value) if hasattr(account, "total_value") else 0.0
            if total_val > 0:
                positions_val = (
                    float(account.positions_value) if hasattr(account, "positions_value") else 0.0
                )
                current_position_ratio = positions_val / total_val
        except (TypeError, ValueError):
            current_position_ratio = 0.0

        if current_position_ratio >= self._max_total:
            return PositionBudget(
                total_limit=0.0,
                single_limit=0.0,
                available_budget=0.0,
                max_positions=0,
                reason=f"总仓位已达 {current_position_ratio:.0%}（上限 {self._max_total:.0%}），禁止新开仓",
            )

        # 4. P2-4：账户风险调整（优先用回撤，回退到收益率）
        # 回撤比收益率更严格：从历史高点算跌幅，真实反映"亏了多少"而非"从起点赚/亏了多少"
        risk_multiplier = 1.0
        risk_reason = ""
        if max_drawdown is not None and max_drawdown > 0:
            # 回撤模式
            if max_drawdown >= self._drawdown_halt:
                risk_multiplier = 0.5
                risk_reason = f"回撤{max_drawdown:.1%}≥{self._drawdown_halt:.0%}，仓位减半"
                logger.warning("⚠️ 最大回撤 {:.2%}，触发仓位减半", max_drawdown)
            elif max_drawdown >= self._drawdown_warn:
                risk_multiplier = 0.7
                risk_reason = f"回撤{max_drawdown:.1%}≥{self._drawdown_warn:.0%}，仓位7折"
                logger.info("最大回撤 {:.2%}，仓位打 7 折", max_drawdown)
        else:
            # 回退模式：收益率
            if total_pnl_pct < -0.10:
                risk_multiplier = 0.5
                risk_reason = f"累计亏损{total_pnl_pct:.1%}，仓位减半"
                logger.warning("累计亏损 {:.2%}，仓位减半", total_pnl_pct)
            elif total_pnl_pct < -0.05:
                risk_multiplier = 0.7
                risk_reason = f"累计亏损{total_pnl_pct:.1%}，仓位7折"

        total_limit = regime_limit * risk_multiplier

        # P0-3 新增：进一步限制总仓位不超过 max_total_position_ratio
        remaining_position_room = self._max_total - current_position_ratio
        total_limit = min(total_limit, remaining_position_room)

        # 5. P2-2：持仓集中度调整（市值权重 + 数量双重检查）
        current_positions = len(account.positions) if hasattr(account, "positions") else 0
        remaining_slots = max(0, self._max_positions - current_positions)

        # P2-2：计算市值集中度指标
        hhi, max_single_weight = self._calc_concentration_metrics(account)

        # P2-2：集中度调整系数
        # HHI 越高 → 集中度越高 → 单股上限越低
        concentration_multiplier = 1.0
        concentration_reason = ""
        if hhi >= self._concentration_hhi_warn:
            # HHI 超阈值，按比例降低单股上限
            # HHI = 0.25 → 系数 0.9；HHI = 0.5 → 系数 0.7；HHI = 1.0 → 系数 0.5
            concentration_multiplier = max(0.5, 1.0 - (hhi - 0.25) * 0.8)
            concentration_reason = f"集中度HHI={hhi:.2f}，单股上限×{concentration_multiplier:.0%}"

        # P2-2：最大单股占比超限，进一步限制
        if max_single_weight >= self._max_single_weight_warn:
            # 最大股已超预警线，新仓单股上限 = max_single_weight_warn - max_single_weight
            # 防止新增仓位进一步加大集中度
            headroom = self._max_single_weight_warn - max_single_weight
            if headroom <= 0:
                concentration_multiplier = min(concentration_multiplier, 0.3)
                concentration_reason += f"，最大股占比{max_single_weight:.0%}超限，严控新仓"
            else:
                concentration_multiplier = min(
                    concentration_multiplier, headroom / self._max_single
                )
                concentration_reason += f"，最大股占比{max_single_weight:.0%}接近上限"

        if remaining_slots <= 0:
            total_limit = 0.0

        # 6. 计算可用预算
        total_value = account.total_value if hasattr(account, "total_value") else 0
        available_budget = total_value * total_limit

        # 7. 单股上限（考虑剩余仓位槽位 + 集中度调整）
        if remaining_slots > 0 and total_limit > 0:
            single_limit = min(self._max_single, total_limit / remaining_slots)
            # P2-2：应用集中度调整系数
            single_limit = single_limit * concentration_multiplier
            single_limit = max(single_limit, 0.05)  # 下限 5%
            single_limit = min(single_limit, total_limit)  # 单股不超过总预算
        else:
            single_limit = 0.0

        reason_parts = [
            f"市场{regime.get('regime')}基准{regime_limit:.0%}",
        ]
        if risk_reason:
            reason_parts.append(risk_reason)
        if concentration_reason:
            reason_parts.append(concentration_reason)
        if sell_required:
            reason_parts.append("建议减仓")

        return PositionBudget(
            total_limit=round(total_limit, 4),
            single_limit=round(single_limit, 4),
            available_budget=round(available_budget, 2),
            max_positions=remaining_slots,
            concentration_hhi=hhi,
            max_single_weight=max_single_weight,
            reason="，".join(reason_parts),
        )
