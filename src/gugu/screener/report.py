"""尾盘选股结果 Feishu 卡片格式化。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from gugu.screener.terminal_screener import ScreeningResult


def format_screener_report(
    results: list[ScreeningResult],
    total_scanned: int,
) -> dict[str, Any]:
    """将尾盘选股结果格式化为 Feishu 卡片。

    Args:
        results: 筛选结果列表（已排序）。
        total_scanned: 扫描的总股票数。

    Returns:
        Feishu interactive card dict。
    """
    if not results:
        return _build_card(
            "尾盘选股 · 无结果",
            "yellow",
            sections=[
                f"今日扫描 {total_scanned} 只股票，未找到符合条件的标的。\n"
                "可能是市场整体偏弱，建议关注明日机会。",
            ],
            note="明策 · gugu · 尾盘选股",
        )

    # 完全通过 vs 部分通过
    passed = [r for r in results if r.passed]
    partial = [r for r in results if not r.passed]

    sections: list[str] = []

    # ── 完全通过 ──
    lines = [f"**✅ 完全通过（{len(passed)} 只）**"]
    for r in passed:
        lines.append(_format_stock_line(r))
    if lines:
        sections.append("\n".join(lines))

    # ── 部分通过 ──
    if partial:
        lines = [f"**⚠️ 部分通过（{len(partial)} 只）**"]
        for r in partial:
            lines.append(_format_stock_line(r))
        sections.append("\n".join(lines))

    # ── 汇总 ──
    total_checked = len(passed) + len(partial)
    sections.append(
        f"扫描 {total_scanned} 只 → 涨幅3-5% → 深度检查 {total_checked} 只 → "
        f"通过 {len(passed)} 只"
    )

    template = "green" if passed else "yellow"
    title = f"🔍 尾盘选股 · {_now_str()}"
    return _build_card(title, template, sections=sections, note="明策 · gugu · 尾盘选股")


def _build_card(
    title: str,
    template: str,
    sections: list[str],
    note: str = "",
) -> dict[str, Any]:
    """构建飞书卡片（独立于 formatter._card 以避免循环导入）。"""
    elements: list[dict[str, Any]] = []
    for i, section in enumerate(sections):
        if i > 0:
            elements.append({"tag": "hr"})
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": section}}
        )
    if note:
        if elements:
            elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": note}],
            }
        )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        },
    }


def _format_stock_line(r: ScreeningResult) -> str:
    """格式化单只股票信息行。"""
    cond_badges = ""
    if r.failed_conditions:
        cond_badges = " ⚠️ " + " ".join(r.failed_conditions[:3])
    vol_badge = r.volume_trend
    idx_badge = "📈" if r.beats_index else ""
    high_badge = "🔝" if r.late_session_high else ""
    return (
        f"**{r.name}({r.symbol})**  "
        f"**{r.change_pct:+.1f}%**  "
        f"¥{r.price:.2f}  "
        f"量比{r.volume_ratio:.1f}  "
        f"换手{r.turnover_pct:.1f}%  "
        f"市值{r.mcap_billion:.0f}亿  "
        f"{vol_badge} {idx_badge} {high_badge}"
        f"{cond_badges}"
    )


def _now_str() -> str:
    return datetime.now().strftime("%m-%d %H:%M")
