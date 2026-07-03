"""尾盘选股结果 Feishu 卡片格式化。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gugu.screener.terminal_screener import ScreeningResult
from gugu.screener.validator import ValidationResult


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
        f"扫描 {total_scanned} 只 → 涨幅3-5% → 深度检查 {total_checked} 只 → 通过 {len(passed)} 只"
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
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": section}})
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


def format_screener_validation(results: list[ValidationResult]) -> dict[str, Any]:
    """将尾盘选股次日验证结果格式化为 Feishu 卡片。

    Args:
        results: 验证结果列表（含开盘溢价、日内最高溢价等）。

    Returns:
        Feishu interactive card dict。
    """
    if not results:
        return _build_card(
            "📊 尾盘验证 · 无记录",
            "grey",
            sections=["昨日无尾盘选股记录，或验证行情获取失败。"],
            note="明策 · gugu · 尾盘验证",
        )

    n = len(results)
    win_open = sum(1 for v in results if v.open_premium > 0)
    win_high = sum(1 for v in results if v.intraday_high_premium > 0)
    avg_open = sum(v.open_premium for v in results) / n
    avg_high = sum(v.intraday_high_premium for v in results) / n
    # 超额收益 = 个股开盘溢价 - 沪深300同日开盘涨幅
    excess = [v.open_premium - v.benchmark_open_change for v in results]
    avg_excess = sum(excess) / len(excess) if excess else 0.0

    # 个股明细（按开盘溢价降序，最多展示 8 只）
    sorted_r = sorted(results, key=lambda x: x.open_premium, reverse=True)[:8]
    detail_lines: list[str] = []
    for v in sorted_r:
        # 涨跌 emoji：🔴 涨 / 🟢 跌
        emoji = "🔴" if v.open_premium > 0 else ("🟢" if v.open_premium < 0 else "⚪")
        excess_val = v.open_premium - v.benchmark_open_change
        excess_str = f" 超额{excess_val:+.2f}%" if abs(excess_val) >= 0.01 else ""
        detail_lines.append(
            f"{emoji} **{v.name or v.symbol}({v.symbol})**  "
            f"开盘{v.open_premium:+.2f}%  "
            f"日内最高{v.intraday_high_premium:+.2f}%  "
            f"收盘¥{v.close_price:.2f}→开盘¥{v.open_price:.2f}"
            f"{excess_str}"
        )

    sections: list[str] = [
        # 汇总统计
        (
            f"**📈 汇总（{n} 只）**\n"
            f"高开率：{win_open}/{n}（{win_open/n*100:.0f}%）\n"
            f"日内冲高率：{win_high}/{n}（{win_high/n*100:.0f}%）\n"
            f"平均开盘溢价：{avg_open:+.2f}%\n"
            f"平均日内最高溢价：{avg_high:+.2f}%\n"
            f"沪深300基准开盘：{results[0].benchmark_open_change:+.2f}%\n"
            f"平均超额收益：{avg_excess:+.2f}%"
        ),
        # 个股明细
        f"**个股表现（按开盘溢价排序，最多8只）**\n" + "\n".join(detail_lines),
        # 验证日期
        f"选股日期：{results[0].screener_date} | 验证日期：{results[0].validation_date}",
    ]

    # 颜色：高开率 >= 50% 用绿色（好结果），否则黄色
    template = "green" if win_open / n >= 0.5 else "yellow"
    title = f"📊 尾盘验证 · {_now_str()}"
    return _build_card(title, template, sections=sections, note="明策 · gugu · 尾盘验证")
