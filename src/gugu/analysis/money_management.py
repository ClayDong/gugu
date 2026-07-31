"""资金管理模块：参考 Kelly 公式 + 动态仓位管理。

核心功能：
1. Kelly 公式：基于历史胜率 + 盈亏比计算最优仓位比例
2. 半 Kelly 策略：保守版 Kelly（fractional = 0.25~0.5）
3. 金字塔加仓：盈利确认后逐层加码，每次加码量递减
4. 动态调整：根据市场状态（波动率、流动性）调整仓位

参考来源：
- 范·撒普《通向财务自由之路》：头寸规模确定
- 凯利公式：f* = (bp - q) / b = (p * (b + 1) - 1) / b
  其中 f* = 最优仓位比例, b = 盈亏比, p = 胜率, q = 1-p
"""

from __future__ import annotations

from dataclasses import dataclass

from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class KellyResult:
    """Kelly 公式计算结果。"""

    full_kelly: float  # 全 Kelly 比例（0-1）
    half_kelly: float  # 半 Kelly 比例
    quarter_kelly: float  # 1/4 Kelly（推荐首次）
    suggested_position: float  # 建议仓位比例（综合考虑后）
    win_rate: float  # 历史胜率
    profit_loss_ratio: float  # 盈亏比
    edge: float  # 优势（edge = win_rate - (1-win_rate)/profit_loss_ratio）
    is_conservative: bool  # 是否处于保守模式
    reason: str  # 决策理由


@dataclass
class PyramidingLevel:
    """金字塔加仓层级。"""

    level: int  # 第几次加仓
    trigger_profit_pct: float  # 触发加仓的盈利百分比
    ratio_of_initial: float  # 加仓量占初始仓位的比例
    stop_loss_pct: float  # 该层止损比例


class KellyCalculator:
    """Kelly 公式资金管理器。

    使用方式：
    kelly = KellyCalculator()
    result = kelly.calculate(win_rate=0.55, profit_loss_ratio=2.0)
    position = kelly.get_position_size(result, max_position_pct=0.3)
    """

    # 默认金字塔加仓层级
    DEFAULT_PYRAMID_LEVELS = [
        PyramidingLevel(level=1, trigger_profit_pct=0.05, ratio_of_initial=0.5, stop_loss_pct=0.05),
        PyramidingLevel(level=2, trigger_profit_pct=0.10, ratio_of_initial=0.3, stop_loss_pct=0.05),
        PyramidingLevel(level=3, trigger_profit_pct=0.15, ratio_of_initial=0.2, stop_loss_pct=0.05),
    ]

    # 保守系数：市场不好时使用（1/4 Kelly）
    CONSERVATIVE_FACTOR = 0.25

    def calculate(
        self,
        win_rate: float,
        profit_loss_ratio: float,
        *,
        fractional: float | None = None,
        is_conservative: bool = False,
    ) -> KellyResult:
        """计算最优仓位比例。

        核心公式：f* = (bp - q) / b = (p * (b + 1) - 1) / b

        Args:
            win_rate: 胜率（0-1），如 0.55 表示 55%
            profit_loss_ratio: 盈亏比（平均盈利/平均亏损），如 2.0
            fractional: 分式 Kelly 系数，默认保守模式=0.25，正常=0.5
            is_conservative: 是否保守模式

        Returns:
            KellyResult
        """
        # 输入校验
        win_rate = max(0.01, min(0.99, win_rate))
        profit_loss_ratio = max(0.1, profit_loss_ratio)

        # Kelly 公式
        p = win_rate
        b = profit_loss_ratio
        q = 1.0 - p
        edge = p - q / b  # 优势
        full_kelly = max(0.0, (b * p - q) / b)

        # 分式 Kelly
        factor = fractional or (self.CONSERVATIVE_FACTOR if is_conservative else 0.5)
        half_kelly = full_kelly * 0.5
        quarter_kelly = full_kelly * 0.25

        # 最终建议：使用分式 Kelly + 上限限制
        suggested = full_kelly * factor
        suggested = max(0.0, min(0.5, suggested))  # 上限 50%

        # 构建理由
        if full_kelly <= 0:
            reason = (
                f"无正期望: 胜率={win_rate:.1%}, 盈亏比={profit_loss_ratio:.1f}, "
                f"优势={edge:.3f}（需 > 0 才有正期望）"
            )
        else:
            reason = (
                f"Kelly: {full_kelly:.1%}, 半Kelly: {half_kelly:.1%}, "
                f"1/4Kelly: {quarter_kelly:.1%}, 建议: {suggested:.1%} "
                f"(胜率={win_rate:.1%}, 盈亏比={profit_loss_ratio:.1f}, "
                f"优势={edge:.3f}, 系数={factor:.1%})"
            )

        return KellyResult(
            full_kelly=full_kelly,
            half_kelly=half_kelly,
            quarter_kelly=quarter_kelly,
            suggested_position=suggested,
            win_rate=win_rate,
            profit_loss_ratio=profit_loss_ratio,
            edge=edge,
            is_conservative=is_conservative,
            reason=reason,
        )

    def get_position_size(
        self,
        kelly: KellyResult,
        max_position_pct: float = 0.3,
        regime_limit: float = 1.0,
        current_ratio: float = 0.0,
    ) -> float:
        """根据 Kelly 结果计算最终仓位比例。

        综合限制：
        1. Kelly 建议值
        2. 风控单股上限 (max_position_pct)
        3. 市场状态上限 (regime_limit)
        4. 已有仓位 (current_ratio)

        Args:
            kelly: Kelly 计算结果
            max_position_pct: 风控单股仓位上限（默认 30%）
            regime_limit: 市场状态仓位乘数（0-1）
            current_ratio: 当前已有仓位比例

        Returns:
            最终建议仓位比例（0-1）
        """
        if kelly.edge <= 0:
            return 0.0

        # 取四重限制的最小值
        remaining = max_position_pct - current_ratio
        if remaining <= 0:
            return 0.0

        position = min(
            kelly.suggested_position,
            max_position_pct * regime_limit,
            remaining,
        )
        return max(0.0, position)

    def pyramiding_add(
        self,
        current_level: int,
        current_profit_pct: float,
        initial_position_size: float,
        levels: list[PyramidingLevel] | None = None,
    ) -> tuple[float, str]:
        """金字塔加仓建议。

        Args:
            current_level: 当前加仓层级（0=初始仓位）
            current_profit_pct: 当前盈利百分比
            initial_position_size: 初始仓位数量或比例
            levels: 加仓层级配置

        Returns:
            (加仓量, 理由)
        """
        if levels is None:
            levels = self.DEFAULT_PYRAMID_LEVELS

        for level in levels:
            if level.level == current_level + 1 and current_profit_pct >= level.trigger_profit_pct:
                add_amount = initial_position_size * level.ratio_of_initial
                reason = (
                    f"金字塔加仓 L{level.level}: 盈利 {current_profit_pct:.1%} "
                    f"触发 {level.trigger_profit_pct:.0%} 阈值, "
                    f"加 {add_amount:.2f} (初始的 {level.ratio_of_initial:.0%})"
                )
                logger.info("[money-mgmt] {}", reason)
                return add_amount, reason

        return 0.0, "未满足加仓条件"

    @staticmethod
    def estimate_from_history(
        win_count: int,
        loss_count: int,
        total_profit: float,
        total_loss: float,
    ) -> tuple[float, float]:
        """从历史交易记录估算胜率和盈亏比。

        Args:
            win_count: 盈利交易次数
            loss_count: 亏损交易次数
            total_profit: 总盈利金额
            total_loss: 总亏损金额

        Returns:
            (胜率, 盈亏比)
        """
        total_trades = win_count + loss_count
        if total_trades <= 0:
            return 0.5, 1.0  # 缺省值

        win_rate = win_count / total_trades
        avg_profit = total_profit / win_count if win_count > 0 else 0.0
        avg_loss = abs(total_loss) / loss_count if loss_count > 0 else 0.0

        # 边界保护：avg_loss 为 0 时盈亏比设为较大值（避免除零）
        # 但限制上限为 10，防止单次异常数据导致 Kelly 仓位过大
        if avg_loss > 0:
            profit_loss_ratio = min(avg_profit / avg_loss, 10.0)
        elif avg_profit > 0:
            profit_loss_ratio = 10.0  # 无亏损记录，高盈亏比
        else:
            profit_loss_ratio = 1.0  # 无数据，默认值

        return win_rate, profit_loss_ratio
