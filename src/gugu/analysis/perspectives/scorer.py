"""十大视角评分函数。

每个函数从特定投资大师的认知框架出发，对同一数据上下文进行独立评分。
评分范围 0-10，6分通过，4分以下致命缺陷。

设计原则：
- 每个评分函数必须可解释（reasoning 字段说明推理链）
- 数据不足时降低置信度而非猜测
- 评分逻辑基于各投资大师的核心原则，非机械套公式
"""

from __future__ import annotations

import math
from typing import Any

from gugu.analysis.perspectives.data_types import (
    PASS_THRESHOLD,
    PERSPECTIVE_BUFFETT,
    PERSPECTIVE_CHEN_JIANG_TING,
    PERSPECTIVE_DASHIXIONG,
    PERSPECTIVE_INVESTING_HABITS,
    PERSPECTIVE_LIVERMORE,
    PERSPECTIVE_LYNCH,
    PERSPECTIVE_MUNGER,
    PERSPECTIVE_QIU_GUO_LU,
    PERSPECTIVE_SOROS,
    PERSPECTIVE_TECHNICAL,
    PerspectiveScore,
    StockDataContext,
)


# ══════════════════════════════════════════════════════════════
# 视角1: 沃伦·巴菲特 — 价值投资 + 护城河 + 安全边际
# ══════════════════════════════════════════════════════════════
def score_buffett(ctx: StockDataContext) -> PerspectiveScore:
    """巴菲特视角：用合理价格买优秀企业。

    核心原则：
    - 护城河（品牌/成本/网络/转换成本）→ 长期超额收益的根源
    - 安全边际 → 买入价远低于内在价值
    - 管理层诚信 → 内部人增持正面，减持负面
    - ROE 持续 > 15% → 资本配置能力
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── 护城河评估（0-3分）──
    moat_score = 0.0
    if ctx.is_monopoly:
        moat_score += 1.5
        factors.append("行业龙头/垄断地位")
    if ctx.moat_type == "brand":
        moat_score += 1.5
        factors.append("品牌护城河")
    elif ctx.moat_type == "cost":
        moat_score += 1.2
        factors.append("成本优势护城河")
    elif ctx.moat_type == "network":
        moat_score += 1.3
        factors.append("网络效应护城河")
    elif ctx.moat_type == "switching":
        moat_score += 1.0
        factors.append("转换成本护城河")
    score += moat_score

    # ── ROE 评估（0-1.5分）──
    if ctx.roe > 20:
        score += 1.5
        factors.append(f"ROE={ctx.roe:.0f}% 卓越")
    elif ctx.roe > 15:
        score += 1.0
        factors.append(f"ROE={ctx.roe:.0f}% 良好")
    elif ctx.roe > 10:
        score += 0.5
        factors.append(f"ROE={ctx.roe:.0f}% 一般")
    elif ctx.roe > 0:
        risks.append(f"ROE={ctx.roe:.0f}% 偏低")
    else:
        score -= 1.0
        risks.append(f"ROE为负 资本配置差")

    # ── 安全边际：PE 评估（-1.5 ~ +1.5分）──
    pe = ctx.pe_dynamic if ctx.pe_dynamic > 0 else ctx.pe_static
    if pe > 0:
        if pe < 15:
            score += 1.5
            factors.append(f"PE={pe:.1f} 安全边际充足")
        elif pe < 25:
            score += 0.5
            factors.append(f"PE={pe:.1f} 合理")
        elif pe < 40:
            score -= 0.5
            risks.append(f"PE={pe:.1f} 偏高")
        elif pe < 60:
            score -= 1.0
            risks.append(f"PE={pe:.1f} 泡沫风险")
        else:
            score -= 1.5
            risks.append(f"PE={pe:.0f} 严重泡沫")

    # ── PB 评估（-1 ~ +1分）──
    if ctx.pb > 0:
        if ctx.pb < 2:
            score += 1.0
            factors.append(f"PB={ctx.pb:.2f} 破净/低估值")
        elif ctx.pb < 4:
            score += 0.3
        elif ctx.pb > 8:
            score -= 1.0
            risks.append(f"PB={ctx.pb:.1f} 偏高")

    # ── 负债率（-0.5 ~ +0.5分）──
    if ctx.debt_ratio > 0:
        if ctx.debt_ratio < 30:
            score += 0.5
            factors.append(f"负债率{ctx.debt_ratio:.0f}% 财务稳健")
        elif ctx.debt_ratio > 70:
            score -= 0.5
            risks.append(f"负债率{ctx.debt_ratio:.0f}% 偏高")

    # ── 内部人行为 ──
    if ctx.insider_buying:
        score += 0.5
        factors.append("内部人增持")
    if ctx.insider_selling:
        score -= 0.8
        risks.append("内部人减持")

    # ── 股息率 ──
    if ctx.dividend_yield > 3:
        score += 0.5
        factors.append(f"股息率{ctx.dividend_yield:.1f}% 优秀")
    elif ctx.dividend_yield > 1.5:
        score += 0.2

    # ── 置信度 ──
    confidence = _calc_confidence(ctx, ["pe_static", "roe", "pb"])

    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "巴菲特")

    return PerspectiveScore(
        perspective=PERSPECTIVE_BUFFETT,
        score=round(score, 1),
        weight=0.15,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角2: 查理·芒格 — 多元思维 + 反向思考 + 质量过滤
# ══════════════════════════════════════════════════════════════
def score_munger(ctx: StockDataContext) -> PerspectiveScore:
    """芒格视角：逆向思考 + 多元思维模型 + 质量过滤。

    核心原则：
    - 反过来想：先问会什么出错，再问什么会做对
    - 多元思维：心理学/数学/经济学/工程学交叉验证
    - 标的质量 > 价格：宁愿以合理价格买优秀企业
    - 检查清单：避免大错比追求完美重要
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── 盈利质量（0-2分）──
    if ctx.profit_growth > 30:
        score += 2.0
        factors.append(f"净利增长{ctx.profit_growth:+.0f}% 高成长")
    elif ctx.profit_growth > 15:
        score += 1.5
        factors.append(f"净利增长{ctx.profit_growth:+.0f}% 良好")
    elif ctx.profit_growth > 0:
        score += 0.5
    elif ctx.profit_growth > -10:
        score -= 0.5
        risks.append(f"净利下滑{abs(ctx.profit_growth):.0f}%")
    else:
        score -= 2.0
        risks.append(f"净利大幅下滑{abs(ctx.profit_growth):.0f}%")

    # ── 营收增长验证（0-1分）──
    if ctx.revenue_growth > 20:
        score += 1.0
        factors.append(f"营收增长{ctx.revenue_growth:+.0f}% 量价齐升")
    elif ctx.revenue_growth > 5:
        score += 0.5
    elif ctx.revenue_growth < -10:
        score -= 1.0
        risks.append(f"营收下滑{abs(ctx.revenue_growth):.0f}% 需求萎缩")

    # ── 现金流验证（芒格最看重真金白银）──
    if ctx.operating_cash_flow > 0 and ctx.free_cash_flow > 0:
        if ctx.free_cash_flow > ctx.operating_cash_flow * 0.5:
            score += 1.0
            factors.append("自由现金流充沛 盈利质量高")
        else:
            score += 0.5
            factors.append("经营现金流为正")
    elif ctx.operating_cash_flow < 0:
        score -= 1.5
        risks.append("经营现金流为负 盈利质量存疑")

    # ── 机构持仓（聪明钱背书）──
    if ctx.institutional_holding > 40:
        score += 0.5
        factors.append(f"机构持股{ctx.institutional_holding:.0f}%")
    elif ctx.institutional_holding > 0 and ctx.institutional_holding < 10:
        score -= 0.3
        risks.append(f"机构持股{ctx.institutional_holding:.0f}% 偏低")

    # ── 反向思考：检查致命风险 ──
    # PE 远高于净利增长 = 估值透支
    pe = ctx.pe_dynamic if ctx.pe_dynamic > 0 else ctx.pe_static
    if pe > 0 and ctx.profit_growth > 0:
        peg = pe / ctx.profit_growth
        if peg > 3:
            score -= 1.5
            risks.append(f"PEG={peg:.1f} 估值透支成长")
        elif peg < 1:
            score += 0.5
            factors.append(f"PEG={peg:.1f} 成长性价比佳")

    # ── 行业地位 ──
    if ctx.is_monopoly:
        score += 0.5
        factors.append("行业龙头 竞争优势强")

    confidence = _calc_confidence(ctx, ["profit_growth", "revenue_growth", "operating_cash_flow"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "芒格")

    return PerspectiveScore(
        perspective=PERSPECTIVE_MUNGER,
        score=round(score, 1),
        weight=0.10,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角3: 乔治·索罗斯 — 反身性 + 趋势判断 + 宏观共振
# ══════════════════════════════════════════════════════════════
def score_soros(ctx: StockDataContext) -> PerspectiveScore:
    """索罗斯视角：反身性理论 + 宏观共振 + 趋势判断。

    核心原则：
    - 反身性：价格影响基本面，基本面又影响价格（正/负反馈环）
    - 判断趋势的方向和强度，而非估值水平
    - 宏观环境与个股趋势共振 = 最强信号
    - 识别盛衰序列的转折点
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── 均线趋势（0-2分）──
    trend_score = _calc_ma_trend(ctx)
    score += trend_score
    if trend_score > 1.0:
        factors.append("均线多头排列 趋势向上")
    elif trend_score < -0.5:
        risks.append("均线空头排列 趋势向下")

    # ── 量价关系（反身性核心）──
    if ctx.volume_ratio > 0:
        if ctx.change_pct > 2 and ctx.volume_ratio > 1.5:
            score += 1.0
            factors.append(f"放量上涨 量价配合(量比{ctx.volume_ratio:.1f})")
        elif ctx.change_pct < -1 and ctx.volume_ratio > 1.5:
            score -= 1.0
            risks.append(f"放量下跌 恐慌抛售(量比{ctx.volume_ratio:.1f})")
        elif ctx.change_pct < 0 and ctx.volume_ratio < 0.8:
            score += 0.5
            factors.append(f"缩量回调 卖压衰竭(量比{ctx.volume_ratio:.2f})")

    # ── 资金流方向 ──
    if ctx.main_net_inflow_5d > 5:
        score += 1.0
        factors.append(f"5日主力流入{ctx.main_net_inflow_5d:.1f}亿")
    elif ctx.main_net_inflow_5d < -5:
        score -= 1.0
        risks.append(f"5日主力流出{abs(ctx.main_net_inflow_5d):.1f}亿")
    elif ctx.main_net_inflow > 1:
        score += 0.5
        factors.append(f"当日主力流入{ctx.main_net_inflow:.1f}亿")

    # ── 板块轮动共振 ──
    if ctx.hot_sector:
        score += 1.0
        factors.append("属于热门板块 资金聚焦")
    if ctx.sector_rotation_strength > 0.7:
        score += 0.5
        factors.append("板块轮动强度高")

    # ── 市场环境 ──
    if ctx.market_stage == "bull":
        score += 0.5
        factors.append("牛市环境 顺风")
    elif ctx.market_stage == "bear":
        score -= 1.0
        risks.append("熊市环境 逆风")
    elif ctx.market_stage == "crash":
        score -= 2.0
        risks.append("崩盘环境 系统性风险")

    # ── 反身性检测：价格是否透支预期 ──
    pe = ctx.pe_dynamic if ctx.pe_dynamic > 0 else ctx.pe_static
    if pe > 0 and ctx.profit_growth > 0:
        peg = pe / ctx.profit_growth
        if peg > 2.5:
            score -= 1.0
            risks.append(f"PEG={peg:.1f} 预期可能已透支")

    confidence = _calc_confidence(ctx, ["ma20", "ma60", "volume_ratio", "main_net_inflow_5d"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "索罗斯")

    return PerspectiveScore(
        perspective=PERSPECTIVE_SOROS,
        score=round(score, 1),
        weight=0.10,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角4: 彼得·林奇 — 成长股分类 + PEG + Tenbagger
# ══════════════════════════════════════════════════════════════
def score_lynch(ctx: StockDataContext) -> PerspectiveScore:
    """林奇视角：六种股票分类 + PEG估值 + Tenbagger 潜力。

    核心原则：
    - 股票类型决定投资策略（慢速成长/稳健/快速增长/周期/资产/困境反转型）
    - PEG = PE / 盈利增长率，< 1 低估，< 0.5 极佳
    - Tenbagger（十倍股）特征：小市值+高增长+合理估值
    - 机构低持仓+低覆盖 = 潜在黑马
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── PEG 评估（核心，0-3分）──
    pe = ctx.pe_dynamic if ctx.pe_dynamic > 0 else ctx.pe_static
    growth = ctx.profit_growth if ctx.profit_growth != 0 else ctx.revenue_growth
    if pe > 0 and growth > 0:
        peg = pe / growth
        if peg < 0.5:
            score += 3.0
            factors.append(f"PEG={peg:.2f} 极佳 典型低估成长股")
        elif peg < 1.0:
            score += 2.0
            factors.append(f"PEG={peg:.2f} 合理")
        elif peg < 1.5:
            score += 0.5
            factors.append(f"PEG={peg:.2f} 略高")
        elif peg < 2.5:
            score -= 0.5
            risks.append(f"PEG={peg:.1f} 偏高")
        else:
            score -= 2.0
            risks.append(f"PEG={peg:.1f} 严重高估")
    elif pe > 0 and growth < 0:
        score -= 1.5
        risks.append(f"PE={pe:.0f}但增长为负 价值陷阱")

    # ── 成长速度分类 ──
    if growth > 50:
        score += 1.0
        factors.append(f"超高速增长{growth:+.0f}%")
    elif growth > 25:
        score += 0.5
        factors.append(f"快速增长{growth:+.0f}%")
    elif growth < -20:
        score -= 1.0
        risks.append(f"增长衰退{growth:+.0f}%")

    # ── 市值空间（Tenbagger 潜力）──
    if 0 < ctx.mcap_billion < 100:
        score += 0.5
        factors.append(f"中小市值{ctx.mcap_billion:.0f}亿 空间大")
    elif ctx.mcap_billion > 2000:
        score -= 0.3
        risks.append(f"大市值{ctx.mcap_billion:.0f}亿 弹性有限")

    # ── 机构持仓（低持仓 = 潜在黑马）──
    if 0 < ctx.institutional_holding < 15:
        score += 0.5
        factors.append(f"机构低持仓{ctx.institutional_holding:.0f}% 潜在黑马")
    elif ctx.institutional_holding > 70:
        score -= 0.3
        risks.append(f"机构高持仓{ctx.institutional_holding:.0f}% 拥挤交易")

    # ── 股息再投资 ──
    if ctx.dividend_yield > 2:
        score += 0.3
        factors.append(f"股息率{ctx.dividend_yield:.1f}%")

    confidence = _calc_confidence(ctx, ["pe_dynamic", "profit_growth", "mcap_billion"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "林奇")

    return PerspectiveScore(
        perspective=PERSPECTIVE_LYNCH,
        score=round(score, 1),
        weight=0.10,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角5: 杰西·利弗莫尔 — 关键点 + 趋势跟随 + 金字塔加仓
# ══════════════════════════════════════════════════════════════
def score_livermore(ctx: StockDataContext) -> PerspectiveScore:
    """利弗莫尔视角：关键点突破 + 趋势确认 + 最小阻力方向。

    核心原则：
    - 在关键点（新高/突破/放量）入场，不在随机位置买入
    - 趋势是你的朋友——永远不要逆势操作
    - 金字塔加仓：盈利时加仓，亏损时止损
    - 止损是第一优先级——永远不要希望亏损回来
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── 趋势强度（0-2.5分）──
    trend = _calc_ma_trend(ctx)
    if trend > 1.5:
        score += 2.5
        factors.append("强趋势 均线完美多头排列")
    elif trend > 0.5:
        score += 1.5
        factors.append("趋势向上")
    elif trend < -1.5:
        score -= 2.5
        risks.append("强下降趋势 绝对回避")
    elif trend < -0.5:
        score -= 1.5
        risks.append("趋势向下")

    # ── 价格位置（关键点判断）──
    if ctx.distance_from_52w_high > -5:
        score += 1.5
        factors.append(f"距52w高点仅{ctx.distance_from_52w_high:+.0f}% 突破新高")
    elif ctx.distance_from_52w_high > -15:
        score += 0.5
    elif ctx.distance_from_52w_high < -50:
        score -= 1.0
        risks.append(f"距52w高点{ctx.distance_from_52w_high:+.0f}% 超跌")

    # ── 量价确认 ──
    if ctx.volume_ratio > 1.5 and ctx.change_pct > 2:
        score += 1.0
        factors.append(f"放量突破 量比{ctx.volume_ratio:.1f}")
    elif ctx.volume_ratio < 0.6 and abs(ctx.change_pct) < 1:
        score += 0.3
        factors.append(f"缩量整理 量比{ctx.volume_ratio:.2f}")

    # ── 最小阻力方向 ──
    if ctx.price > ctx.ma20 > ctx.ma60:
        score += 0.5
        factors.append("最小阻力向上")
    elif ctx.price < ctx.ma20 < ctx.ma60:
        score -= 1.0
        risks.append("最小阻力向下")

    # ── 止损可行性（ATR）──
    if ctx.atr14 > 0 and ctx.price > 0:
        atr_pct = ctx.atr14 / ctx.price * 100
        if 2 < atr_pct < 5:
            score += 0.3
            factors.append(f"ATR={atr_pct:.1f}% 止损空间合理")
        elif atr_pct > 8:
            score -= 0.5
            risks.append(f"ATR={atr_pct:.1f}% 波动过大 止损宽")

    confidence = _calc_confidence(ctx, ["ma20", "ma60", "volume_ratio", "high_52w"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "利弗莫尔")

    return PerspectiveScore(
        perspective=PERSPECTIVE_LIVERMORE,
        score=round(score, 1),
        weight=0.10,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角6: 邱国鹭 — 行业格局 + 估值 + 性价比
# ══════════════════════════════════════════════════════════════
def score_qiu_guo_lu(ctx: StockDataContext) -> PerspectiveScore:
    """邱国鹭视角：行业格局优先 + 估值性价比 + 胜率优先。

    核心原则：
    - 先看行业，再看个股：行业格局决定赚钱难度
    - 宁买贵的好公司，不买便宜的烂公司（性价比 ≠ 便宜）
    - 胜率 > 赔率：先求胜，再求利
    - 行业集中度提升 = 龙头受益
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── 行业格局（0-2分）──
    if ctx.is_monopoly:
        score += 2.0
        factors.append("行业龙头 集中度提升受益")
    elif ctx.hot_sector:
        score += 0.5
        factors.append("热门行业")

    # ── 行业估值对比 ──
    if ctx.pe_static > 0 and ctx.industry_pe_avg > 0:
        pe_ratio = ctx.pe_static / ctx.industry_pe_avg
        if pe_ratio < 0.8:
            score += 1.0
            factors.append(f"PE低于行业均值{pe_ratio:.0%} 相对低估")
        elif pe_ratio > 1.5:
            score -= 1.0
            risks.append(f"PE高于行业均值{pe_ratio:.0%} 相对高估")

    # ── 胜率指标：ROE 稳定性 ──
    if ctx.roe > 15:
        score += 1.0
        factors.append(f"ROE={ctx.roe:.0f}% 胜率指标强")
    elif ctx.roe < 5:
        score -= 0.5
        risks.append(f"ROE={ctx.roe:.0f}% 胜率弱")

    # ── 性价比 = 质量/价格 ──
    quality_score = 0
    if ctx.roe > 15: quality_score += 1
    if ctx.profit_growth > 15: quality_score += 1
    if ctx.is_monopoly: quality_score += 1
    if ctx.operating_cash_flow > 0: quality_score += 1

    price_score = 0
    pe = ctx.pe_dynamic if ctx.pe_dynamic > 0 else ctx.pe_static
    if 0 < pe < 20: price_score += 2
    elif pe < 30: price_score += 1
    elif pe > 50: price_score -= 1

    value_score = quality_score + price_score
    if value_score >= 4:
        score += 1.0
        factors.append(f"性价比Q={quality_score}/P={price_score} 优质优价")
    elif value_score <= 1:
        score -= 0.5
        risks.append(f"性价比Q={quality_score}/P={price_score} 偏低")

    # ── 行业分散约束（隐含）──
    if ctx.industry and "st" in ctx.industry.lower():
        score -= 1.0
        risks.append("ST行业 胜率极低")

    confidence = _calc_confidence(ctx, ["pe_static", "roe", "industry_pe_avg"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "邱国鹭")

    return PerspectiveScore(
        perspective=PERSPECTIVE_QIU_GUO_LU,
        score=round(score, 1),
        weight=0.10,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角7: 陈江挺 — 趋势 + 仓位 + 止损（炒股的智慧）
# ══════════════════════════════════════════════════════════════
def score_chen_jiang_ting(ctx: StockDataContext) -> PerspectiveScore:
    """陈江挺视角：趋势为王 + 严格止损 + 仓位管理。

    核心原则：
    - 趋势是你的朋友——永远不要和趋势作对
    - 止损是第一天条——到了止损位必须走
    - 不要摊平成本——加仓只在盈利时
    - 等待是美德——宁可错过，不可做错
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── 趋势确认（0-2.5分）──
    trend = _calc_ma_trend(ctx)
    if trend > 1.0:
        score += 2.0
        factors.append("趋势确认向上")
    elif trend > 0:
        score += 1.0
        factors.append("趋势中性偏多")
    elif trend < -1.0:
        score -= 2.0
        risks.append("趋势确认向下 不可买入")
    elif trend < 0:
        score -= 1.0
        risks.append("趋势中性偏空")

    # ── 止损可行性 ──
    if ctx.ma20 > 0 and ctx.price > 0:
        stop_dist = (ctx.price - ctx.ma20 * 0.97) / ctx.price * 100
        if 2 < stop_dist < 6:
            score += 1.0
            factors.append(f"止损距离{stop_dist:.1f}% 合理")
        elif stop_dist > 8:
            score -= 0.5
            risks.append(f"止损距离{stop_dist:.1f}% 过宽")
        elif stop_dist < 2:
            score += 0.3
            factors.append(f"止损距离{stop_dist:.1f}% 紧凑")

    # ── 仓位信号：量比确认 ──
    if ctx.volume_ratio > 0:
        if 0.8 < ctx.volume_ratio < 1.5:
            score += 0.5
            factors.append(f"量比{ctx.volume_ratio:.1f} 正常")
        elif ctx.volume_ratio > 2.5:
            score -= 0.5
            risks.append(f"量比{ctx.volume_ratio:.1f} 异常放量")

    # ── 换手率（流动性风险）──
    if ctx.turnover_pct > 15:
        score -= 1.0
        risks.append(f"换手率{ctx.turnover_pct:.0f}% 过度投机")
    elif ctx.turnover_pct > 0 and ctx.turnover_pct < 1:
        score -= 0.3
        risks.append(f"换手率{ctx.turnover_pct:.1f}% 流动性不足")

    # ── 等待价值：是否值得等 ──
    if ctx.distance_from_52w_high < -30 and trend > 0:
        score += 0.5
        factors.append("超跌+趋势转好 等待后有入场机会")

    confidence = _calc_confidence(ctx, ["ma20", "ma60", "volume_ratio", "turnover_pct"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "陈江挺")

    return PerspectiveScore(
        perspective=PERSPECTIVE_CHEN_JIANG_TING,
        score=round(score, 1),
        weight=0.10,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角8: 技术分析 — 经典技术指标综合
# ══════════════════════════════════════════════════════════════
def score_technical(ctx: StockDataContext) -> PerspectiveScore:
    """技术分析视角：经典技术指标综合评分。

    覆盖：RSI + MACD + 布林带 + 均线系统 + ATR
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── RSI（0-1.5分）──
    if ctx.rsi14 > 0:
        if 40 < ctx.rsi14 < 60:
            score += 0.5
            factors.append(f"RSI={ctx.rsi14:.0f} 中性")
        elif 30 <= ctx.rsi14 <= 40:
            score += 1.0
            factors.append(f"RSI={ctx.rsi14:.0f} 超卖区 反弹机会")
        elif 60 <= ctx.rsi14 <= 70:
            score += 0.5
            factors.append(f"RSI={ctx.rsi14:.0f} 偏强")
        elif ctx.rsi14 > 80:
            score -= 1.5
            risks.append(f"RSI={ctx.rsi14:.0f} 严重超买")
        elif ctx.rsi14 > 70:
            score -= 1.0
            risks.append(f"RSI={ctx.rsi14:.0f} 超买")
        elif ctx.rsi14 < 30:
            score += 0.5
            factors.append(f"RSI={ctx.rsi14:.0f} 深度超卖")

    # ── MACD（0-1.5分）──
    if ctx.macd_hist > 0:
        score += 1.0
        factors.append(f"MACD柱={ctx.macd_hist:.3f} 多头")
        if ctx.macd_dif > ctx.macd_dea:
            score += 0.5
            factors.append("MACD金叉确认")
    elif ctx.macd_hist < 0:
        score -= 1.0
        risks.append(f"MACD柱={ctx.macd_hist:.3f} 空头")
        if ctx.macd_dif < ctx.macd_dea:
            score -= 0.5
            risks.append("MACD死叉确认")

    # ── 布林带位置 ──
    if ctx.bollinger_upper > 0 and ctx.bollinger_lower > 0 and ctx.price > 0:
        band_width = ctx.bollinger_upper - ctx.bollinger_lower
        if band_width > 0:
            pos = (ctx.price - ctx.bollinger_lower) / band_width
            if 0.4 < pos < 0.6:
                score += 0.3
            elif pos > 0.9:
                score -= 1.0
                risks.append(f"布林带{pos:.0%} 接近上轨")
            elif pos < 0.1:
                score += 0.5
                factors.append(f"布林带{pos:.0%} 接近下轨")

    # ── 均线系统 ──
    trend = _calc_ma_trend(ctx)
    score += trend * 0.5

    confidence = _calc_confidence(ctx, ["rsi14", "macd_hist", "ma20", "ma60"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "技术分析")

    return PerspectiveScore(
        perspective=PERSPECTIVE_TECHNICAL,
        score=round(score, 1),
        weight=0.10,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角9: 投资习惯 — 行为金融 + 纪律 + 风险管理
# ══════════════════════════════════════════════════════════════
def score_investing_habits(ctx: StockDataContext) -> PerspectiveScore:
    """投资习惯视角：行为金融学 + 交易纪律 + 风险管理。

    核心原则：
    - 损失厌恶：亏损的痛苦 > 同等盈利的快乐 → 严格止损
    - 锚定效应：不要锚定在历史价格上
    - 过度自信：不要重仓单只
    - 纪律 > 预测：遵守交易纪律比预测市场重要
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── 止损纪律可行性 ──
    if ctx.ma20 > 0 and ctx.price > 0:
        stop_dist = (ctx.price - ctx.ma20 * 0.97) / ctx.price * 100
        if 3 < stop_dist < 7:
            score += 1.0
            factors.append(f"止损距离{stop_dist:.1f}% 纪律性强")
        elif stop_dist > 10:
            score -= 1.0
            risks.append(f"止损距离{stop_dist:.1f}% 纪律性差")

    # ── 仓位合理性（换手率暗示）──
    if ctx.turnover_pct > 20:
        score -= 1.5
        risks.append(f"换手率{ctx.turnover_pct:.0f}% 过度交易")
    elif ctx.turnover_pct > 10:
        score -= 0.5
        risks.append(f"换手率{ctx.turnover_pct:.0f}% 偏高")

    # ── 波动率风险 ──
    if ctx.atr14 > 0 and ctx.price > 0:
        atr_pct = ctx.atr14 / ctx.price * 100
        if atr_pct > 5:
            score -= 1.0
            risks.append(f"ATR={atr_pct:.1f}% 波动过大 仓位需降低")
        elif atr_pct < 2:
            score += 0.5
            factors.append(f"ATR={atr_pct:.1f}% 波动可控")

    # ── 锚定效应检测 ──
    if ctx.distance_from_52w_high > -3:
        score -= 0.5
        risks.append("接近52w高点 锚定追高风险")
    elif ctx.distance_from_52w_high < -40:
        score -= 0.3
        risks.append("大幅低于52w高点 抄底锚定风险")

    # ── 分散度（市值暗示分散）──
    if 50 < ctx.mcap_billion < 500:
        score += 0.5
        factors.append(f"中等市值{ctx.mcap_billion:.0f}亿 分散合理")
    elif ctx.mcap_billion > 3000:
        score += 0.3
        factors.append(f"大市值{ctx.mcap_billion:.0f}亿 天然分散")

    # ── 内部人行为纪律 ──
    if ctx.insider_selling:
        score -= 1.0
        risks.append("内部人减持 纪律负面信号")
    if ctx.insider_buying:
        score += 0.5
        factors.append("内部人增持 纪律正面信号")

    confidence = _calc_confidence(ctx, ["ma20", "atr14", "turnover_pct"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "投资习惯")

    return PerspectiveScore(
        perspective=PERSPECTIVE_INVESTING_HABITS,
        score=round(score, 1),
        weight=0.08,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 视角10: 大师兄 — 五维宏观框架（金油汇债G）
# ══════════════════════════════════════════════════════════════
def score_dashixiong(ctx: StockDataContext) -> PerspectiveScore:
    """大师兄视角：五维宏观框架 + 三层传导。

    五维：金/油/汇/债/G（股市）
    三层传导：政策层 → 资金层 → 情绪层
    """
    score = 5.0
    factors: list[str] = []
    risks: list[str] = []

    # ── 市场状态 ──
    if ctx.market_stage == "bull":
        score += 1.5
        factors.append("牛市 五维共振向上")
    elif ctx.market_stage == "sideway":
        score += 0.3
    elif ctx.market_stage == "bear":
        score -= 1.0
        risks.append("熊市 五维共振向下")
    elif ctx.market_stage == "crash":
        score -= 2.5
        risks.append("崩盘 系统性风险")

    # ── 黄金信号（避险/通胀）──
    if ctx.gold_price > 0:
        if ctx.market_regime == "risk_off":
            score += 0.5
            factors.append("黄金受宠 避险情绪")
        elif ctx.market_regime == "risk_on":
            score -= 0.3
            risks.append("黄金失宠 风险偏好上升")

    # ── 汇率影响 ──
    if ctx.usd_cny > 0:
        if ctx.usd_cny > 7.2:
            score -= 0.5
            risks.append(f"USD/CNY={ctx.usd_cny:.2f} 人民币贬值压力")
        elif ctx.usd_cny < 7.0:
            score += 0.3
            factors.append(f"USD/CNY={ctx.usd_cny:.2f} 人民币升值")

    # ── 美债收益率 ──
    if ctx.us10y_yield > 0:
        if ctx.us10y_yield > 4.5:
            score -= 0.5
            risks.append(f"美债10Y={ctx.us10y_yield:.2f}% 高利率压制")
        elif ctx.us10y_yield < 3.5:
            score += 0.3
            factors.append(f"美债10Y={ctx.us10y_yield:.2f}% 低利率友好")

    # ── 板块轮动 ──
    if ctx.hot_sector:
        score += 0.5
        factors.append("热门板块 资金聚焦")
    if ctx.sector_rotation_strength > 0.6:
        score += 0.5
        factors.append("板块轮动强 结构性机会")

    # ── 资金流 ──
    if ctx.main_net_inflow_5d > 10:
        score += 1.0
        factors.append(f"5日主力大幅流入{ctx.main_net_inflow_5d:.1f}亿")
    elif ctx.main_net_inflow_5d < -10:
        score -= 1.0
        risks.append(f"5日主力大幅流出{abs(ctx.main_net_inflow_5d):.1f}亿")

    confidence = _calc_confidence(ctx, ["market_stage", "gold_price", "usd_cny", "us10y_yield"])
    score = max(0.0, min(10.0, score))
    reasoning = _build_reasoning(factors, risks, score)
    verdict = _verdict_from_score(score, "大师兄")

    return PerspectiveScore(
        perspective=PERSPECTIVE_DASHIXIONG,
        score=round(score, 1),
        weight=0.07,
        confidence=confidence,
        reasoning=reasoning,
        key_factors=factors[:5],
        risk_flags=risks[:3],
        verdict=verdict,
    )


# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════

def _calc_ma_trend(ctx: StockDataContext) -> float:
    """计算均线趋势得分 (-2.0 ~ +2.0)。"""
    score = 0.0
    if ctx.price > 0 and ctx.ma5 > 0 and ctx.ma10 > 0 and ctx.ma20 > 0 and ctx.ma60 > 0:
        if ctx.price > ctx.ma5: score += 0.4
        if ctx.ma5 > ctx.ma10: score += 0.4
        if ctx.ma10 > ctx.ma20: score += 0.4
        if ctx.ma20 > ctx.ma60: score += 0.8  # 中期趋势权重最高
        if ctx.ma60 > ctx.ma120 > 0: score += 0.4
        if ctx.ma120 > ctx.ma250 > 0: score += 0.2
    elif ctx.price > 0 and ctx.ma20 > 0 and ctx.ma60 > 0:
        # 降级：只有 MA20/MA60
        if ctx.price > ctx.ma20: score += 0.5
        if ctx.ma20 > ctx.ma60: score += 1.0
    return max(-2.0, min(2.0, score))


def _calc_confidence(ctx: StockDataContext, required_fields: list[str]) -> float:
    """根据必需字段的存在程度计算置信度。"""
    if not required_fields:
        return 1.0
    present = 0
    for f in required_fields:
        val = getattr(ctx, f, 0)
        if val is not None and val != 0 and not (isinstance(val, float) and math.isnan(val)):
            present += 1
    return round(present / len(required_fields), 2)


def _build_reasoning(factors: list[str], risks: list[str], score: float) -> str:
    """构建推理链文本。"""
    parts = []
    if factors:
        parts.append("正面: " + "; ".join(factors[:3]))
    if risks:
        parts.append("风险: " + "; ".join(risks[:2]))
    return " | ".join(parts) if parts else f"评分={score:.1f}"


def _verdict_from_score(score: float, name: str) -> str:
    """根据分数生成一句话判定。"""
    if score >= 8:
        return f"{name}:强烈推荐"
    elif score >= 7:
        return f"{name}:推荐"
    elif score >= 6:
        return f"{name}:可考虑"
    elif score >= 5:
        return f"{name}:中性"
    elif score >= 4:
        return f"{name}:偏空"
    elif score >= 3:
        return f"{name}:回避"
    else:
        return f"{name}:强烈回避"


# ── 评分函数注册表 ──
SCORERS = {
    PERSPECTIVE_BUFFETT: score_buffett,
    PERSPECTIVE_MUNGER: score_munger,
    PERSPECTIVE_SOROS: score_soros,
    PERSPECTIVE_LYNCH: score_lynch,
    PERSPECTIVE_LIVERMORE: score_livermore,
    PERSPECTIVE_QIU_GUO_LU: score_qiu_guo_lu,
    PERSPECTIVE_CHEN_JIANG_TING: score_chen_jiang_ting,
    PERSPECTIVE_TECHNICAL: score_technical,
    PERSPECTIVE_INVESTING_HABITS: score_investing_habits,
    PERSPECTIVE_DASHIXIONG: score_dashixiong,
}
