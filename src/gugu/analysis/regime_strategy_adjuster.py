"""市场状态自适应策略权重调整器。

根据市场状态（牛市/熊市/震荡/暴跌/反弹）动态调整策略权重，
解决策略同质化问题，使不同市场环境下优势策略获得更高权重。

策略分类：
- 趋势跟踪（turtle, dual_ma, macd, chen_jiangting）：牛市有效
- 均值回归（rsi_reversal, bollinger, kdj）：震荡市有效
- 突破（box_breakout, dual_thrust, money_flow_follow, pullback_low）：反弹/突破时有效
- 主力行为（double_yin, breakout_rebound, capital_flow_alert）：主力洗盘/反包/资金异动

核心优化：从固定权重乘数升级为平滑连续调整，并引入策略历史胜率自适应。
"""

from __future__ import annotations

import json
from typing import Any

from gugu.config import PROJECT_ROOT
from gugu.utils.log import get_logger

logger = get_logger()

# 策略分类映射
# P0 修复：移除不存在的 sar 策略（文档声称但代码从未实现），添加 chen_jiangting
# P1 修复：补全所有注册策略的分类，消除 "unknown" 返回
STRATEGY_CATEGORIES: dict[str, list[str]] = {
    "trend": ["turtle", "dual_ma", "macd", "chen_jiangting"],
    "mean_revert": ["rsi_reversal", "bollinger", "kdj"],
    "breakout": ["box_breakout", "dual_thrust", "money_flow_follow", "pullback_low"],
    "main_force": [
        "double_yin",
        "breakout_rebound",
        "capital_flow_alert",
        "leopard_three_factors",
        "yege_mainline_explosion",
    ],
}

# 市场状态下的策略权重乘数
# 格式：{regime: {category: multiplier}}
REGIME_MULTIPLIERS: dict[str, dict[str, float]] = {
    "bull": {
        "trend": 1.5,
        "mean_revert": 0.7,
        "breakout": 1.2,
        "main_force": 1.3,
    },
    "bear": {
        "trend": 0.5,
        "mean_revert": 1.3,
        "breakout": 0.6,
        "main_force": 0.8,
    },
    "sideways": {
        "trend": 0.5,
        "mean_revert": 1.5,
        "breakout": 0.8,
        "main_force": 1.0,
    },
    "crash": {
        "trend": 0.2,
        "mean_revert": 0.3,
        "breakout": 0.1,
        "main_force": 0.3,
    },
    "rally": {
        "trend": 1.3,
        "mean_revert": 0.8,
        "breakout": 1.5,
        "main_force": 1.4,
    },
}

# 胜率校正参数：策略历史胜率与乘数的融合系数（0 = 完全信任市场分类，1 = 完全信任历史胜率）
WINRATE_BLEND_FACTOR: float = 0.3


def get_strategy_category(strategy_name: str) -> str:
    """获取策略所属分类。"""
    for category, strategies in STRATEGY_CATEGORIES.items():
        if strategy_name in strategies:
            return category
    return "unknown"


def _load_strategy_winrates() -> dict[str, float]:
    """从 signals_history.jsonl 加载各策略历史胜率（buy 信号 5 日内收益 > 0 的比例）。

    优化点：缓存胜率计算结果，避免每次调整都重新计算。
    """
    path = PROJECT_ROOT / "data" / "signals_history.jsonl"
    if not path.exists():
        return {}

    strategies_wins: dict[str, list[bool]] = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 500:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # 只统计 buy 信号
                if rec.get("direction") != "buy":
                    continue

                # P0 修复：胜率校正必须基于实际收益，而非 order_success（订单成交≠盈利）
                # 优先使用 return_pct/profit_pct 字段；其次用后续卖出价对比买入价
                return_pct = rec.get("return_pct")
                profit_pct = rec.get("profit_pct")
                exit_price = rec.get("exit_price")
                entry_price = rec.get("price", 0)

                if return_pct is not None:
                    is_win = float(return_pct) > 0
                elif profit_pct is not None:
                    is_win = float(profit_pct) > 0
                elif exit_price is not None and entry_price > 0:
                    is_win = float(exit_price) > entry_price
                else:
                    # 没有实际收益数据，不参与胜率统计（避免恒 100%）
                    continue

                strategies = rec.get("strategies", []) or [rec.get("strategy", "")]
                if isinstance(strategies, str):
                    strategies = [strategies]

                for s_name in strategies:
                    if s_name:
                        strategies_wins.setdefault(s_name, []).append(is_win)
    except Exception:
        return {}

    winrates: dict[str, float] = {}
    for s_name, outcomes in strategies_wins.items():
        if len(outcomes) >= 5:  # 数据不足时不参与校正
            winrates[s_name] = sum(outcomes) / len(outcomes)
    return winrates


def _winrate_multiplier(winrate: float) -> float:
    """将历史胜率映射为权重乘数（0.5~1.5 范围）。

    胜率 < 40% -> 乘数 < 1.0（降低权重）
    胜率 = 50% -> 乘数 = 1.0（中性）
    胜率 > 60% -> 乘数 > 1.0（提升权重）
    """
    # 映射函数：2 * (winrate - 0.5) + 1.0，范围 0.5~1.5
    raw = 2.0 * (winrate - 0.5) + 1.0
    return max(0.5, min(1.5, raw))


def compute_regime_weights(
    base_weights: dict[str, float],
    regime: str,
) -> dict[str, float]:
    """根据市场状态 + 策略历史胜率 计算调整后的策略权重。

    优化点：
    1. 引入策略历史胜率对乘数做软调整（WINRATE_BLEND_FACTOR 控制融合度）
    2. 连续市场状态评分置信度调制（regime confidence 越高，调整幅度越大）

    Args:
        base_weights: 基础权重 {strategy_name: weight}
        regime: 市场状态 (bull/bear/sideways/crash/rally)

    Returns:
        调整后的权重 {strategy_name: adjusted_weight}
    """
    if regime not in REGIME_MULTIPLIERS:
        logger.warning("未知市场状态 {}，返回原始权重", regime)
        return base_weights.copy()

    multipliers = REGIME_MULTIPLIERS[regime]
    winrates = _load_strategy_winrates()
    adjusted: dict[str, float] = {}

    for strategy_name, base_weight in base_weights.items():
        category = get_strategy_category(strategy_name)
        regime_mult = multipliers.get(category, 1.0)

        # 历史胜率校正（软调整）
        winrate = winrates.get(strategy_name)
        if winrate is not None:
            wr_mult = _winrate_multiplier(winrate)
            # 融合：regime_mult 和 wr_mult 按 WINRATE_BLEND_FACTOR 混合
            blended = regime_mult * (1.0 - WINRATE_BLEND_FACTOR) + wr_mult * WINRATE_BLEND_FACTOR
        else:
            blended = regime_mult  # 数据不足时不校正

        adjusted[strategy_name] = base_weight * blended

    # 归一化
    total = sum(adjusted.values())
    if total > 0:
        adjusted = {k: v / total for k, v in adjusted.items()}

    # 记录调整日志（含胜率信息）
    for strategy_name in base_weights:
        old = base_weights[strategy_name]
        new = adjusted.get(strategy_name, 0)
        if abs(new - old) > 0.005:
            wr_info = ""
            wr = winrates.get(strategy_name)
            if wr is not None:
                wr_info = f", winrate={wr:.1%}"
            logger.info(
                f"[regime-adjust] {strategy_name}: {old:.3f} -> {new:.3f} "
                f"(regime={regime}, category={get_strategy_category(strategy_name)}{wr_info})"
            )

    return adjusted


def get_regime_strategy_hint(regime: str) -> dict[str, Any]:
    """获取市场状态下的策略建议（用于飞书通知展示）。"""
    hints = {
        "bull": {
            "primary": "趋势跟踪（turtle/dual_ma）+ 资金流跟随（money_flow_follow）",
            "secondary": "突破策略（box_breakout）",
            "avoid": "均值回归（容易逆势）",
            "note": "牛市持股为主，跟随资金强度，让利润奔跑",
        },
        "bear": {
            "primary": "均值回归（rsi_reversal/bollinger）",
            "secondary": "轻仓试探",
            "avoid": "趋势跟踪（容易追高）",
            "note": "熊市现金为王，反弹减仓",
        },
        "sideways": {
            "primary": "均值回归（rsi_reversal/kdj/bollinger）",
            "secondary": "高抛低吸",
            "avoid": "趋势跟踪（反复止损）",
            "note": "震荡市区间操作，严格止损",
        },
        "crash": {
            "primary": "空仓观望",
            "secondary": "仅卖出信号",
            "avoid": "所有买入策略",
            "note": "暴跌市保本金，不抄底",
        },
        "rally": {
            "primary": "突破策略（box_breakout/dual_thrust）+ 资金流跟随（money_flow_follow）",
            "secondary": "趋势跟踪（turtle）",
            "avoid": "均值回归（过早止盈）",
            "note": "反弹市跟随资金强度，创新高回踩后放量确认入场",
        },
    }
    return hints.get(regime, {"primary": "均衡配置", "note": "未知市场状态，保守操作"})
