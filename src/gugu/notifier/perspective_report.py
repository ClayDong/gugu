"""十大视角分析飞书报告生成器。

生成结构化的飞书卡片消息，展示：
- 综合评分 + 判定
- 各视角评分表格
- 核心优势/风险
- 操作建议 + 价位
"""

from __future__ import annotations

from typing import Any

from gugu.analysis.perspectives.data_types import PerspectiveAnalysisResult
from gugu.utils.log import get_logger

logger = get_logger()


def build_perspective_card(result: PerspectiveAnalysisResult) -> dict[str, Any]:
    """构建十大视角分析飞书卡片。"""
    # 颜色模板
    if result.composite_score >= 8:
        template = "green"
    elif result.composite_score >= 7:
        template = "blue"
    elif result.composite_score >= 6:
        template = "yellow"
    else:
        template = "red"

    lines: list[str] = []

    # ── 头部：综合评分 ──
    status_emoji = "✅" if result.passed else "❌"
    lines.append(
        f"**{result.symbol} {result.name}** | "
        f"¥{result.price:.2f}"
    )
    lines.append("")
    lines.append(
        f"### {status_emoji} 综合评分: **{result.composite_score:.1f}/10** "
        f"| {result.overall_verdict}"
    )
    lines.append("")

    # ── 各视角评分表 ──
    lines.append("**📊 十大视角评分**")
    lines.append("")
    lines.append("| 视角 | 分数 | 权重 | 判定 |")
    lines.append("|------|------|------|------|")

    perspective_names = {
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

    for s in sorted(result.scores, key=lambda x: x.score, reverse=True):
        name_cn = perspective_names.get(s.perspective, s.perspective)
        status = "✅" if s.passed else ("🔴" if s.is_fatal else "⚠️")
        bar = "█" * int(s.score) + "░" * (10 - int(s.score))
        lines.append(
            f"| {name_cn} | {s.score:.1f}{status} | {s.weight:.0%} | {s.verdict} |"
        )

    lines.append("")

    # ── 木桶效应 ──
    lines.append(
        f"**🪣 木桶短板**: {result.min_perspective}({result.min_score:.1f}分) | "
        f"通过 {result.passed_count}/10 | 致命缺陷 {result.fatal_count}个"
    )
    lines.append("")

    # ── 核心优势 ──
    if result.top_strengths:
        lines.append("**💪 核心优势**")
        for s in result.top_strengths[:3]:
            lines.append(f"- {s}")
        lines.append("")

    # ── 核心风险 ──
    if result.top_risks:
        lines.append("**⚠️ 核心风险**")
        for r in result.top_risks[:3]:
            lines.append(f"- {r}")
        lines.append("")

    # ── 操作建议 ──
    lines.append(f"**💡 操作建议**: {result.suggestion}")

    # ── 价位 ──
    if result.entry_price or result.stop_loss_price or result.target_price:
        lines.append("")
        lines.append("**📍 价位参考**")
        price_parts = []
        if result.entry_price:
            price_parts.append(f"入场: ¥{result.entry_price:.2f}")
        if result.stop_loss_price:
            price_parts.append(f"止损: ¥{result.stop_loss_price:.2f}")
        if result.target_price:
            price_parts.append(f"目标: ¥{result.target_price:.2f}")
        lines.append(" | ".join(price_parts))

        # 盈亏比
        if result.entry_price and result.stop_loss_price and result.target_price:
            risk = result.entry_price - result.stop_loss_price
            reward = result.target_price - result.entry_price
            if risk > 0:
                ratio = reward / risk
                lines.append(f"盈亏比: 1:{ratio:.1f}")

    # ── 综合推理 ──
    lines.append("")
    lines.append(f"**📝 综合推理**: {result.overall_reasoning}")

    # ── 免责声明 ──
    lines.append("")
    lines.append("---")
    lines.append(
        "*⚠️ 本分析基于量化模型自动生成，不构成投资建议。"
        "投资有风险，入市需谨慎。*"
    )

    content = "\n".join(lines)

    return {
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🔬 十大视角分析 | {result.name}",
            },
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
        ],
    }


def build_batch_summary_card(
    results: list[PerspectiveAnalysisResult],
    title: str = "📊 十大视角批量分析",
) -> dict[str, Any]:
    """构建批量分析摘要卡片。"""
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
    lines.append(f"共分析 **{len(results)}** 只股票")
    passed_count = sum(1 for r in sorted_results if r.passed)
    lines.append(f"通过 **{passed_count}** 只 | 未通过 **{len(results) - passed_count}** 只")
    lines.append("")

    # 通过列表
    passed = [r for r in sorted_results if r.passed]
    if passed:
        lines.append("**✅ 推荐买入**")
        lines.append("")
        lines.append("| 股票 | 综合分 | 最低分 | 建议 |")
        lines.append("|------|---------|---------|------|")
        for r in passed[:10]:
            lines.append(
                f"| {r.symbol} {r.name} | {r.composite_score:.1f} | "
                f"{r.min_score:.1f}({r.min_perspective[:4]}) | "
                f"{r.suggestion[:15]}... |"
            )
        lines.append("")

    # 未通过列表（前5）
    failed = [r for r in sorted_results if not r.passed]
    if failed:
        lines.append("**❌ 未通过**")
        lines.append("")
        for r in failed[:5]:
            lines.append(
                f"- {r.symbol} {r.name} | {r.composite_score:.1f}分 | "
                f"短板: {r.min_perspective}({r.min_score:.1f})"
            )

    lines.append("")
    lines.append("---")
    lines.append(
        "*⚠️ 本分析基于量化模型自动生成，不构成投资建议。"
        "投资有风险，入市需谨慎。*"
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
