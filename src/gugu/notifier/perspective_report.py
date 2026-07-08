"""十大视角分析飞书报告生成器。

生成结构化的飞书卡片消息，展示：
- 综合评分 + 判定
- 各视角评分表 + 推理链（关键因子/风险标记）
- 核心优势/风险
- 操作建议 + 价位
- 综合推理 + 术语解释

v0.5.1 修复：
- 原代码只显示视角分数和判定，没有推理链（reasoning/key_factors/risk_flags）
- 添加术语解释（ROE/PE/PB/护城河/木桶效应等），符合 project_memory 约束
- 使用 hr 分隔实现三层折叠架构（综合评分 | 视角详析 | 操作建议）
"""

from __future__ import annotations

from typing import Any

from gugu.analysis.perspectives.data_types import PerspectiveAnalysisResult
from gugu.utils.log import get_logger

logger = get_logger()


# 视角中文名映射
PERSPECTIVE_NAMES = {
    "buffett": "巴菲特",
    "munger": "芒格",
    "soros": "索罗斯",
    "lynch": "林奇",
    "livermore": "利弗莫尔",
    "qiu_guo_lu": "邱国鹭",
    "chen_jiang_ting": "陈江挺",
    "technical": "技术分析",
    "investing_habits": "投资习惯",
    "dashixiong": "大师兄",
}

# 视角核心思想（用于术语解释）
PERSPECTIVE_THOUGHT = {
    "buffett": "价值投资：护城河+安全边际+ROE",
    "munger": "多元思维+反向思考+质量过滤",
    "soros": "反身性+趋势判断+波段操作",
    "lynch": "成长股分类+PEG+生活观察",
    "livermore": "关键点+趋势跟随+纪律执行",
    "qiu_guo_lu": "行业格局+估值锚+龙头优先",
    "chen_jiang_ting": "趋势+仓位+止损纪律",
    "technical": "均线/MACD/RSI/布林带综合",
    "investing_habits": "行为金融+投资纪律+情绪控制",
    "dashixiong": "宏观五维：金/油/汇率/利率/市场状态",
}


def build_perspective_card(result: PerspectiveAnalysisResult) -> dict[str, Any]:
    """构建十大视角分析飞书卡片（详细版）。

    v0.5.1 重构：
    - 添加每个视角的推理链（reasoning + key_factors + risk_flags）
    - 添加术语解释（ROE/PE/PB/护城河/木桶效应等）
    - 使用 hr 分隔实现三层折叠架构
    - 符合 project_memory 约束：三层折叠+术语解释+新手友好
    """
    # 颜色模板
    if result.composite_score >= 8:
        template = "green"
    elif result.composite_score >= 7:
        template = "blue"
    elif result.composite_score >= 6:
        template = "yellow"
    else:
        template = "red"

    elements: list[dict[str, Any]] = []

    # ── 第一层：综合评分 + 判定 ──
    lines_header: list[str] = []
    status_emoji = "✅" if result.passed else "❌"
    lines_header.append(
        f"**{result.symbol} {result.name}** | ¥{result.price:.2f}"
    )
    lines_header.append("")
    lines_header.append(
        f"### {status_emoji} 综合评分: **{result.composite_score:.1f}/10** "
        f"| {result.overall_verdict}"
    )
    lines_header.append(
        f"📊 通过 **{result.passed_count}/10** | "
        f"🪣 木桶短板: **{result.min_perspective}({result.min_score:.1f}分)** | "
        f"🔴 致命缺陷 **{result.fatal_count}个**"
    )
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n".join(lines_header)},
    })
    elements.append({"tag": "hr"})

    # ── 第二层：各视角评分表 + 推理链 ──
    lines_scores: list[str] = []
    lines_scores.append("**📊 十大视角评分详析**")
    lines_scores.append("")

    # 评分表（按分数降序）
    sorted_scores = sorted(result.scores, key=lambda x: x.score, reverse=True)
    for s in sorted_scores:
        name_cn = PERSPECTIVE_NAMES.get(s.perspective, s.perspective)
        _thought = PERSPECTIVE_THOUGHT.get(s.perspective, "")
        status = "✅" if s.passed else ("🔴" if s.is_fatal else "⚠️")
        bar = "█" * int(s.score) + "░" * (10 - int(s.score))

        lines_scores.append(
            f"**{name_cn}** {s.score:.1f}{status} "
            f"(权重{s.weight:.0%} 置信度{s.confidence:.0%})"
        )
        lines_scores.append(f"进度: {bar}")
        lines_scores.append(f"判定: {s.verdict}")

        # 推理链（关键改进：原代码缺失）
        if s.reasoning:
            lines_scores.append(f"推理: {s.reasoning}")

        # 关键因子
        if s.key_factors:
            lines_scores.append(
                "✅ 正面: " + " | ".join(s.key_factors[:3])
            )

        # 风险标记
        if s.risk_flags:
            lines_scores.append(
                "⚠️ 风险: " + " | ".join(s.risk_flags[:3])
            )

        lines_scores.append("")

    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n".join(lines_scores)},
    })
    elements.append({"tag": "hr"})

    # ── 第三层：核心优势/风险 + 操作建议 + 价位 ──
    lines_action: list[str] = []

    # 核心优势
    if result.top_strengths:
        lines_action.append("**💪 核心优势**")
        for s in result.top_strengths[:5]:
            lines_action.append(f"- {s}")
        lines_action.append("")

    # 核心风险
    if result.top_risks:
        lines_action.append("**⚠️ 核心风险**")
        for r in result.top_risks[:5]:
            lines_action.append(f"- {r}")
        lines_action.append("")

    # 操作建议
    lines_action.append(f"**💡 操作建议**: {result.suggestion}")

    # 价位参考
    if result.entry_price or result.stop_loss_price or result.target_price:
        lines_action.append("")
        lines_action.append("**📍 价位参考**")
        price_parts = []
        if result.entry_price:
            price_parts.append(f"入场: ¥{result.entry_price:.2f}")
        if result.stop_loss_price:
            price_parts.append(f"止损: ¥{result.stop_loss_price:.2f}")
        if result.target_price:
            price_parts.append(f"目标: ¥{result.target_price:.2f}")
        lines_action.append(" | ".join(price_parts))

        # 盈亏比
        if result.entry_price and result.stop_loss_price and result.target_price:
            risk = result.entry_price - result.stop_loss_price
            reward = result.target_price - result.entry_price
            if risk > 0:
                ratio = reward / risk
                lines_action.append(f"盈亏比: 1:{ratio:.1f}")

    # 综合推理
    lines_action.append("")
    lines_action.append(f"**📝 综合推理**: {result.overall_reasoning}")

    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n".join(lines_action)},
    })
    elements.append({"tag": "hr"})

    # ── 术语解释（新手友好，符合 project_memory 约束）──
    glossary_lines = [
        "📖 **术语解释**（新手必看）",
        "",
        "**ROE（净资产收益率）** = 净利润 / 净资产，衡量公司赚钱效率，>15%为优秀",
        "**PE（市盈率）** = 股价 / 每股收益，衡量估值高低，<25为合理，>60为泡沫",
        "**PB（市净率）** = 股价 / 每股净资产，<2为低估，>8为偏高",
        "**PEG** = PE / 盈利增长率，<1为低估，衡量成长性",
        "**护城河** = 企业持续竞争优势（品牌/成本/网络/转换成本）",
        "**木桶效应** = 最弱视角决定整体评分，单一维度极低分意味致命缺陷",
        "**致命缺陷** = 单视角 <4.0分，否决买入建议",
        "**ATR（平均真实波幅）** = 衡量股价波动幅度，用于止损计算",
        "**MACD** = 趋势动量指标，金叉（DIF上穿DEA）看多，死叉看空",
        "**RSI** = 相对强弱指标，>70超买，<30超卖",
    ]
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n".join(glossary_lines)},
    })
    elements.append({"tag": "hr"})

    # ── 免责声明 ──
    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": (
                    "⚠️ 本分析基于量化模型自动生成，不构成投资建议。"
                    "投资有风险，入市需谨慎。"
                ),
            }
        ],
    })

    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🔬 十大视角分析 | {result.name}",
            },
            "template": template,
        },
        "elements": elements,
    }


def build_batch_summary_card(
    results: list[PerspectiveAnalysisResult],
    title: str = "📊 十大视角批量分析",
) -> dict[str, Any]:
    """构建批量分析摘要卡片。

    v0.5.1 改进：
    - 未通过股票也显示更多信息（综合判定 + 操作建议 + 核心风险）
    - 添加统计信息（平均分、通过率、致命缺陷数）
    - 添加术语解释
    """
    if not results:
        return {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "grey",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "无分析结果"}},
            ],
        }

    # 按综合评分排序
    sorted_results = sorted(results, key=lambda r: r.composite_score, reverse=True)

    lines: list[str] = []
    passed_count = sum(1 for r in sorted_results if r.passed)
    avg_score = sum(r.composite_score for r in sorted_results) / len(sorted_results)
    total_fatal = sum(r.fatal_count for r in sorted_results)

    lines.append(f"共分析 **{len(results)}** 只股票")
    lines.append(
        f"通过 **{passed_count}** 只 | 未通过 **{len(results) - passed_count}** 只 | "
        f"平均分 **{avg_score:.1f}** | 致命缺陷 **{total_fatal}** 个"
    )
    lines.append("")

    # 通过列表
    passed = [r for r in sorted_results if r.passed]
    if passed:
        lines.append("**✅ 推荐买入**")
        lines.append("")
        for r in passed[:10]:
            lines.append(
                f"- **{r.symbol} {r.name}** | {r.composite_score:.1f}分 | "
                f"短板: {r.min_perspective}({r.min_score:.1f}) | "
                f"建议: {r.suggestion[:30]}"
            )
        lines.append("")

    # 未通过列表（前5）—— v0.5.1 改进：显示更多信息
    failed = [r for r in sorted_results if not r.passed]
    if failed:
        lines.append("**❌ 未通过**")
        lines.append("")
        for r in failed[:5]:
            lines.append(
                f"- **{r.symbol} {r.name}** | {r.composite_score:.1f}分 | "
                f"判定: {r.overall_verdict} | "
                f"短板: {r.min_perspective}({r.min_score:.1f})"
            )
            # 添加核心风险（最多2条）
            if r.top_risks:
                for risk in r.top_risks[:2]:
                    lines.append(f"  - ⚠️ {risk}")
            # 添加操作建议
            if r.suggestion:
                lines.append(f"  - 💡 {r.suggestion}")
        lines.append("")

    lines.append("---")
    lines.append(
        "*⚠️ 本分析基于量化模型自动生成，不构成投资建议。"
        "投资有风险，入市需谨慎。*"
    )
    lines.append("")
    lines.append(
        "*📖 详细分析见后续推送的个股卡片（含视角评分表、推理链、价位参考、术语解释）*"
    )

    content = "\n".join(lines)

    template = "green" if passed_count > len(results) * 0.3 else "yellow"

    return {
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
        ],
    }
