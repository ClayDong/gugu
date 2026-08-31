"""策略能力圈标注（P2：巴菲特/芒格共识——27个策略=27个能力圈=没有能力圈）。

给每个策略标注：
- explainability: fundamental / technical / rule / ml（可解释性等级）
- confidence_level: verified / conditional / experimental / in_review（验证等级）
- position_cap: 建议仓位上限（防止"试验仓"和"验证仓"同权重）

信号卡片随附该策略的历史胜率与样本量，让使用者永远知道自己站在圈内还是圈外。
"""

from __future__ import annotations

from dataclasses import dataclass

# 可解释性等级
EXPLAINABILITY_FUNDAMENTAL = "fundamental"  # 基本面（可解释性最高）
EXPLAINABILITY_TECHNICAL = "technical"  # 技术分析（规则明确）
EXPLAINABILITY_RULE = "rule"  # 博主规则（他人观点系统化，需谨慎）
EXPLAINABILITY_ML = "ml"  # 黑箱机器学习（可解释性最低）

EXPLAINABILITY_LABELS = {
    EXPLAINABILITY_FUNDAMENTAL: "📊 基本面",
    EXPLAINABILITY_TECHNICAL: "📈 技术面",
    EXPLAINABILITY_RULE: "📋 规则/经验",
    EXPLAINABILITY_ML: "🖥️ 黑箱ML",
}

# 验证等级
VERIFIED = "verified"  # 样本外验证通过
CONDITIONAL = "conditional"  # 条件通过（有边界条件）
EXPERIMENTAL = "experimental"  # 试验中（观察）
IN_REVIEW = "in_review"  # 审查中


@dataclass
class StrategyProfile:
    """策略能力圈画像。"""

    name: str
    explainability: str = EXPLAINABILITY_TECHNICAL
    validation_status: str = IN_REVIEW
    position_cap: float = 0.20  # 建议仓位上限
    note: str = ""


# 策略画像表（代码=权威，与 manifest 同步维护）
STRATEGY_PROFILES: dict[str, StrategyProfile] = {
    # ── 趋势类（技术面，规则明确）──
    "turtle": StrategyProfile(
        "turtle", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.20, "海龟趋势跟踪"
    ),
    "dual_ma": StrategyProfile(
        "dual_ma", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.20, "双均线交叉"
    ),
    "macd": StrategyProfile("macd", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.15, "MACD 金叉死叉"),
    "ma20_trend": StrategyProfile(
        "ma20_trend", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.20, "MA20 趋势跟随"
    ),
    "ma5_momentum": StrategyProfile(
        "ma5_momentum", EXPLAINABILITY_TECHNICAL, IN_REVIEW, 0.10, "MA5 动量"
    ),
    "cloud_trend": StrategyProfile(
        "cloud_trend", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.20, "三维趋势共振"
    ),
    "weekly_combo": StrategyProfile(
        "weekly_combo", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.15, "周线多因子"
    ),
    "box_breakout": StrategyProfile(
        "box_breakout", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.20, "箱体突破"
    ),
    "breakout_rebound": StrategyProfile(
        "breakout_rebound", EXPLAINABILITY_RULE, IN_REVIEW, 0.10, "断板反包（陈学长）"
    ),
    # ── 均值回归类 ──
    "rsi_reversal": StrategyProfile(
        "rsi_reversal", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.15, "RSI 超卖反转"
    ),
    "kdj": StrategyProfile("kdj", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.15, "KDJ 反转"),
    "bollinger": StrategyProfile(
        "bollinger", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.15, "布林带回归"
    ),
    "dual_thrust": StrategyProfile(
        "dual_thrust", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.15, "Dual Thrust 突破"
    ),
    "pullback_low": StrategyProfile(
        "pullback_low", EXPLAINABILITY_TECHNICAL, IN_REVIEW, 0.10, "低吸回踩"
    ),
    # ── 资金流/主力行为类 ──
    "money_flow_follow": StrategyProfile(
        "money_flow_follow", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.15, "资金流跟随"
    ),
    "capital_flow_alert": StrategyProfile(
        "capital_flow_alert", EXPLAINABILITY_TECHNICAL, CONDITIONAL, 0.10, "资金流异动"
    ),
    "north_force_long": StrategyProfile(
        "north_force_long", EXPLAINABILITY_TECHNICAL, IN_REVIEW, 0.10, "北向/主力吸筹"
    ),
    "dragon_tiger_short": StrategyProfile(
        "dragon_tiger_short", EXPLAINABILITY_RULE, IN_REVIEW, 0.10, "龙虎榜反包"
    ),
    # hot_money 已移除（2026-08-01 样本外证伪：集中度超标不可信）
    "industry_capital_momentum": StrategyProfile(
        "industry_capital_momentum", EXPLAINABILITY_TECHNICAL, IN_REVIEW, 0.15, "行业资金动量"
    ),
    # ── 博主规则类（他人观点系统化，需谨慎）──
    "chenxuezhang": StrategyProfile(
        "chenxuezhang", EXPLAINABILITY_RULE, CONDITIONAL, 0.10, "陈学长6规则（入场端有效）"
    ),
    "linjudaye": StrategyProfile(
        "linjudaye", EXPLAINABILITY_RULE, CONDITIONAL, 0.10, "邻居大爷6规则"
    ),
    "double_yin": StrategyProfile(
        "double_yin", EXPLAINABILITY_RULE, CONDITIONAL, 0.10, "双阴钱袋子"
    ),
    "huamulan_tail_pick": StrategyProfile(
        "huamulan_tail_pick", EXPLAINABILITY_RULE, IN_REVIEW, 0.10, "花木兰尾盘选股"
    ),
    "huamulan_crash_layout": StrategyProfile(
        "huamulan_crash_layout", EXPLAINABILITY_RULE, IN_REVIEW, 0.15, "花木兰空仓等暴跌"
    ),
    "huamulan_fake_dip": StrategyProfile(
        "huamulan_fake_dip", EXPLAINABILITY_RULE, IN_REVIEW, 0.10, "花木兰假摔低吸"
    ),
    "price_volume_signal": StrategyProfile(
        "price_volume_signal", EXPLAINABILITY_RULE, IN_REVIEW, 0.10, "换手量比信号（样本外存疑）"
    ),
    "rebound_elite": StrategyProfile(
        "rebound_elite", EXPLAINABILITY_RULE, IN_REVIEW, 0.10, "断板反包精英"
    ),
    "fanbao": StrategyProfile(
        "fanbao",
        EXPLAINABILITY_RULE,
        IN_REVIEW,
        0.10,
        "小圈圈儿《反包策略》：炸板反包+高位跌停反包",
    ),
    # ── 公众号战法（2026-08-01 落地，技术面规则明确，待事件研究验证）──
    "regime_cycle_four": StrategyProfile(
        "regime_cycle_four",
        EXPLAINABILITY_TECHNICAL,
        IN_REVIEW,
        0.15,
        "四周期模型（银山谷/死亡谷）",
    ),
    "death_valley_rebound": StrategyProfile(
        "death_valley_rebound",
        EXPLAINABILITY_TECHNICAL,
        CONDITIONAL,
        0.10,
        "幻灭期修复反弹（样本外显著，2026-08-01）",
    ),
    "bollinger_oversold_rebound": StrategyProfile(
        "bollinger_oversold_rebound",
        EXPLAINABILITY_TECHNICAL,
        CONDITIONAL,
        0.10,
        "布林下轨超跌反弹（12%超跌过滤后样本外显著，2026-08-01）",
    ),
    # ── ML 类（黑箱，最低可解释性）──
    "ml_gradient_boost": StrategyProfile(
        "ml_gradient_boost",
        EXPLAINABILITY_ML,
        EXPERIMENTAL,
        0.05,
        "GradientBoosting（2026衰减，禁用中）",
    ),
    "ml_factor": StrategyProfile("ml_factor", EXPLAINABILITY_ML, EXPERIMENTAL, 0.05, "ML 多因子"),
}

# 默认画像（未收录策略的兜底）
DEFAULT_PROFILE = StrategyProfile(
    "unknown", EXPLAINABILITY_TECHNICAL, IN_REVIEW, 0.10, "未收录画像"
)


def get_profile(strategy_name: str) -> StrategyProfile:
    """获取策略能力圈画像，未收录时返回兜底画像。"""
    return STRATEGY_PROFILES.get(strategy_name, DEFAULT_PROFILE)


def _runtime_validation_status(strategy_names: list[str]) -> str | None:
    """Resolve a conservative runtime role from settings, then manifest evidence."""
    try:
        from gugu.config import get_config

        strategy_cfg = get_config().strategy
        verified = set(strategy_cfg.verified_strategies)
        experimental = set(strategy_cfg.experimental_strategies)
        names = set(strategy_names)
        if names and names <= verified:
            return VERIFIED
        if names & experimental:
            return EXPERIMENTAL
    except Exception:
        pass
    return None


def annotate_signal(signal: dict) -> dict:
    """给信号添加能力圈标注。

    在 signal 中注入：
    - explainability: 可解释性等级
    - explainability_label: 中文标签
    - validation_status: 验证等级
    - position_cap: 建议仓位上限
    - capability_note: 一句话说明
    """
    strategies = signal.get("strategies") or []
    if isinstance(strategies, str):
        strategies = [s.strip() for s in strategies.split(",") if s.strip()]

    if not strategies:
        return signal

    # 取触发策略中最保守的画像（仓位上限取最小，验证等级取最低）
    profiles = [get_profile(s) for s in strategies]
    runtime_status = _runtime_validation_status(strategies)
    min_cap = min(p.position_cap for p in profiles)
    # 验证等级优先级：experimental < in_review < conditional < verified
    _severity = {EXPERIMENTAL: 0, IN_REVIEW: 1, CONDITIONAL: 2, VERIFIED: 3}
    lowest = min(profiles, key=lambda p: _severity.get(p.validation_status, 1))
    # 可解释性取最低（最保守）
    _exp_sev = {
        EXPLAINABILITY_FUNDAMENTAL: 3,
        EXPLAINABILITY_TECHNICAL: 2,
        EXPLAINABILITY_RULE: 1,
        EXPLAINABILITY_ML: 0,
    }
    lowest_exp = min(profiles, key=lambda p: _exp_sev.get(p.explainability, 1))

    signal["capability"] = {
        "explainability": lowest_exp.explainability,
        "explainability_label": EXPLAINABILITY_LABELS[lowest_exp.explainability],
        "validation_status": runtime_status or lowest.validation_status,
        "position_cap": min_cap,
        "strategy_names": strategies,
        "notes": [p.note for p in profiles if p.note],
    }
    return signal


def format_capability_line(signal: dict) -> str:
    """生成能力圈标注行（用于飞书卡片）。"""
    cap = signal.get("capability") or {}
    if not cap:
        return ""
    exp_label = cap.get("explainability_label", "")
    status = cap.get("validation_status", "")
    status_label = {
        VERIFIED: "✅ 已验证",
        CONDITIONAL: "🟡 条件通过",
        EXPERIMENTAL: "🧪 试验仓",
        IN_REVIEW: "🔍 审查中",
    }.get(status, status)
    cap_ratio = cap.get("position_cap", 0)
    return f"{exp_label} · {status_label} · 仓位上限 {cap_ratio:.0%}"
