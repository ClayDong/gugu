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

    P0-2 改进：每只股票增加入场区间/止损位/仓位建议/目标位/盈亏比。
    P2-1 改进：推荐精简为首选1 + 备选2 + 观察池7，降低新手决策负担。

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

    # P2-1: 三档推荐（首选1 + 备选2 + 观察池7）
    # 排序：综合评分 = 通过条件数 + 盈亏比 + 缩量程度
    def _score(r: ScreeningResult) -> float:
        rr = r.risk_reward_ratio
        shrink = max(0, 1 - r.volume_ratio)
        return r.pass_count * 1.0 + rr * 0.5 + shrink * 2.0

    passed_sorted = sorted(passed, key=_score, reverse=True)
    top_pick = passed_sorted[:1]
    secondary = passed_sorted[1:3]
    watchlist = passed_sorted[3:10]

    sections: list[str] = []

    # ── 首选（1 只）──
    if top_pick:
        lines = ["**🥇 首选（综合评分最高）**"]
        for r in top_pick:
            lines.append(_format_stock_detail(r, is_top=True))
        sections.append("\n".join(lines))

    # ── 备选（2 只）──
    if secondary:
        lines = ["**🥈 备选（次优）**"]
        for r in secondary:
            lines.append(_format_stock_detail(r, is_top=False))
        sections.append("\n".join(lines))

    # ── 观察池（最多 7 只，精简显示）──
    if watchlist:
        lines = ["**📊 观察池（仅列出，非重点推荐）**"]
        for r in watchlist:
            lines.append(_format_stock_line_compact(r))
        sections.append("\n".join(lines))

    # ── 部分通过（仍按原逻辑展示，但标注未通过条件）──
    if partial:
        lines = [f"**⚠️ 部分通过（{len(partial)} 只）**"]
        for r in partial[:5]:  # 最多展示 5 只
            lines.append(_format_stock_line_compact(r))
        sections.append("\n".join(lines))

    # ── 汇总 ──
    total_checked = len(passed) + len(partial)
    sections.append(
        f"扫描 {total_scanned} 只 → 深度检查 {total_checked} 只 → 通过 {len(passed)} 只\n"
        f"⚠️ 震荡市交易纪律：\n"
        f"  • 单股仓位≤10%，总仓位≤30%\n"
        f"  • 🛑 止损：跌破 MA20×0.97（3%缓冲）立即清仓\n"
        f"  • 💰 止盈：反弹至前高减仓 50%，全部到前高清仓\n"
        f"  • ⏰ 时间止损：持仓 5 日无进展（涨幅<2%）减半仓\n"
        f"  • 纪律 > 预测，止损不商量（陈江挺）"
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


def _format_stock_detail(r: ScreeningResult, is_top: bool = False) -> str:
    """P0-2: 格式化股票详细信息（含入场/止损/目标位/仓位）。

    用于首选和备选股票，给用户完整可执行信息。
    """
    badge = "🎯" if is_top else "  "
    cond_badges = ""
    if r.failed_conditions:
        cond_badges = " ⚠️ " + " ".join(r.failed_conditions[:3])

    # 盈亏比徽章
    rr = r.risk_reward_ratio
    rr_badge = f" | 盈亏比 {rr:.1f}✅" if rr >= 2.0 else (f" | 盈亏比 {rr:.1f}" if rr > 0 else "")

    vol_badge = r.volume_trend
    idx_badge = "📈" if r.beats_index else ""
    high_badge = "🔝" if r.late_session_high else ""

    return (
        f"{badge} **{r.name}({r.symbol})**  "
        f"**{r.change_pct:+.1f}%**  "
        f"¥{r.price:.2f}  "
        f"量比{r.volume_ratio:.1f}  "
        f"换手{r.turnover_pct:.1f}%  "
        f"市值{r.mcap_billion:.0f}亿  "
        f"{vol_badge} {idx_badge} {high_badge}{rr_badge}{cond_badges}\n"
        f"   ┌ 🎯 入场: ¥{r.entry_low:.2f} ~ ¥{r.entry_high:.2f}（MA20附近）\n"
        f"   ├ 🛑 止损: ¥{r.stop_loss_price:.2f}（破MA20×0.97）\n"
        f"   ├ 💰 目标: ¥{r.take_profit_price:.2f}（前高压力）\n"
        f"   └ 💼 仓位: {r.suggested_position*100:.0f}%（单股上限）"
    )


def _format_stock_line_compact(r: ScreeningResult) -> str:
    """P2-1: 紧凑格式（观察池用），仅显示核心信息。"""
    cond_badges = ""
    if r.failed_conditions:
        cond_badges = " ⚠️ " + " ".join(r.failed_conditions[:2])
    vol_badge = r.volume_trend
    return (
        f"• **{r.name}({r.symbol})**  "
        f"{r.change_pct:+.1f}%  "
        f"¥{r.price:.2f}  "
        f"量比{r.volume_ratio:.1f}  "
        f"止损¥{r.stop_loss_price:.2f}  "
        f"目标¥{r.take_profit_price:.2f}"
        f"{vol_badge}{cond_badges}"
    )


def _format_stock_line(r: ScreeningResult) -> str:
    """格式化单只股票信息行（兼容旧调用方）。"""
    return _format_stock_line_compact(r)


def _now_str() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


def format_range_watchlist(candidates: list[dict], market_regime: str) -> dict[str, Any]:
    """P1-1: 震荡市盘中观察列表卡片。

    让用户在盘中知道有哪些股票接近 MA20 支撑位，
    为 14:30 尾盘选股推送做心理准备。

    Args:
        candidates: 观察候选股列表，每项含 symbol/name/price/ma20/distance_pct
        market_regime: 市场状态（range/bear）

    Returns:
        Feishu interactive card dict
    """
    if not candidates:
        return _build_card(
            f"📊 盘中观察 · {_now_str()}",
            "grey",
            sections=[
                f"当前市场状态: {market_regime}\n"
                "暂无接近买点的股票，下午 14:30 将推送正式选股结果。",
            ],
            note="明策 · gugu · 盘中观察（非买入建议）",
        )

    # 按距离 MA20 远近排序（越近越优先观察）
    sorted_cands = sorted(candidates, key=lambda x: abs(x.get("distance_pct", 0)))

    lines = [f"**📊 接近买点候选（{len(sorted_cands)} 只）**"]
    for c in sorted_cands[:8]:  # 最多展示 8 只
        dist = c.get("distance_pct", 0)
        # 距离 MA20 的距离徽章
        if abs(dist) <= 1.0:
            dist_badge = "🟢 接近支撑"
        elif abs(dist) <= 3.0:
            dist_badge = "🟡 关注中"
        else:
            dist_badge = "⚪ 远离支撑"
        lines.append(
            f"• **{c.get('name', '')}({c.get('symbol', '')})**  "
            f"¥{c.get('price', 0):.2f}  "
            f"MA20 ¥{c.get('ma20', 0):.2f}  "
            f"距离 {dist:+.1f}%  {dist_badge}"
        )

    sections = [
        f"当前市场状态: **{market_regime}**（震荡市）\n"
        "⚠️ **以下为观察列表，非买入建议**\n"
        "下午 14:30 将推送正式选股结果（含入场/止损/目标位）",
        "\n".join(lines),
    ]

    return _build_card(
        f"📊 盘中观察 · {_now_str()}",
        "blue",
        sections=sections,
        note="明策 · gugu · 盘中观察（非买入建议）",
    )


def format_screener_validation(results: list[ValidationResult]) -> dict[str, Any]:
    """将尾盘选股次日验证结果格式化为 Feishu 卡片。

    P2-2 改进：
    1. 个股明细增加涨跌原因分析（大盘拖累/个股独立走势）
    2. 增加本周累计胜率统计
    3. 增加教训标注（连续亏股触发策略反思）

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
    benchmark_open = results[0].benchmark_open_change

    # 个股明细（按开盘溢价降序，最多展示 8 只）
    # P2-2: 增加涨跌原因分析
    sorted_r = sorted(results, key=lambda x: x.open_premium, reverse=True)[:8]
    detail_lines: list[str] = []
    for v in sorted_r:
        # 涨跌 emoji：🔴 涨 / 🟢 跌
        emoji = "🔴" if v.open_premium > 0 else ("🟢" if v.open_premium < 0 else "⚪")
        excess_val = v.open_premium - v.benchmark_open_change
        excess_str = f" 超额{excess_val:+.2f}%" if abs(excess_val) >= 0.01 else ""

        # P2-2: 涨跌原因分析
        reason = _analyze_premium_reason(v.open_premium, v.benchmark_open_change, excess_val)
        reason_str = f"  \n   └ 💡 {reason}" if reason else ""

        detail_lines.append(
            f"{emoji} **{v.name or v.symbol}({v.symbol})**  "
            f"开盘{v.open_premium:+.2f}%  "
            f"日内最高{v.intraday_high_premium:+.2f}%  "
            f"收盘¥{v.close_price:.2f}→开盘¥{v.open_price:.2f}"
            f"{excess_str}{reason_str}"
        )

    # P2-2: 本周累计统计
    weekly_stats = _load_weekly_validation_stats(results[0].validation_date)
    weekly_section = ""
    if weekly_stats["total"] > 0:
        weekly_win = weekly_stats["wins"]
        weekly_total = weekly_stats["total"]
        weekly_rate = weekly_win / weekly_total * 100 if weekly_total > 0 else 0
        weekly_section = (
            f"\n\n**📊 本周累计（{weekly_total} 只）**\n"
            f"胜率：{weekly_win}/{weekly_total}（{weekly_rate:.0f}%）\n"
            f"平均开盘溢价：{weekly_stats['avg_open']:+.2f}%"
        )

    # P2-2: 教训标注（连续亏股触发）
    lesson = _generate_lesson(results, weekly_stats)
    lesson_section = f"\n\n**💡 教训标注**\n{lesson}" if lesson else ""

    sections: list[str] = [
        # 汇总统计
        (
            f"**📈 汇总（{n} 只）**\n"
            f"高开率：{win_open}/{n}（{win_open/n*100:.0f}%）\n"
            f"日内冲高率：{win_high}/{n}（{win_high/n*100:.0f}%）\n"
            f"平均开盘溢价：{avg_open:+.2f}%\n"
            f"平均日内最高溢价：{avg_high:+.2f}%\n"
            f"沪深300基准开盘：{benchmark_open:+.2f}%\n"
            f"平均超额收益：{avg_excess:+.2f}%"
            f"{weekly_section}{lesson_section}"
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


def _analyze_premium_reason(open_premium: float, benchmark: float, excess: float) -> str:
    """P2-2: 分析个股开盘溢价的涨跌原因。"""
    if open_premium > 0:
        if benchmark > 0 and excess < 1.0:
            return f"大盘上涨带动（基准 {benchmark:+.2f}%），个股跟随"
        elif excess >= 1.0:
            return f"个股独立强势（超额 +{excess:.2f}%），资金认可度高"
        else:
            return "缩量回踩后放量反弹，低吸逻辑生效"
    elif open_premium < 0:
        if benchmark < 0:
            return f"大盘下跌拖累（基准 {benchmark:+.2f}%），非个股问题"
        elif excess <= -1.0:
            return f"个股独立走弱（超额 {excess:+.2f}%），可能存在利空"
        else:
            return "回踩未止跌，MA20 支撑可能失效"
    return ""


def _load_weekly_validation_stats(validation_date: str) -> dict:
    """P2-2: 加载本周累计验证统计。

    从 data/screener_validation.jsonl 读取本周（周一至今）的所有验证记录。
    """
    import json
    import os
    from datetime import datetime, timedelta

    stats = {"total": 0, "wins": 0, "avg_open": 0.0}
    validation_file = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data", "screener_validation.jsonl"
    )
    if not os.path.exists(validation_file):
        return stats

    try:
        # 计算本周一日期
        today = datetime.fromisoformat(validation_date)
        monday = today - timedelta(days=today.weekday())
        monday_str = monday.strftime("%Y-%m-%d")

        records = []
        with open(validation_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    val_date = d.get("validation_date", "")
                    if val_date >= monday_str:
                        records.append(d)
                except json.JSONDecodeError:
                    continue

        if not records:
            return stats

        wins = sum(1 for r in records if r.get("open_premium", 0) > 0)
        avg_open = sum(r.get("open_premium", 0) for r in records) / len(records)
        return {
            "total": len(records),
            "wins": wins,
            "avg_open": avg_open,
        }
    except Exception:
        return stats


def _generate_lesson(results: list, weekly_stats: dict) -> str:
    """P2-2: 根据当日和本周表现生成教训标注。"""
    n = len(results)
    win_rate = sum(1 for v in results if v.open_premium > 0) / n if n > 0 else 0
    avg_open = sum(v.open_premium for v in results) / n if n > 0 else 0

    # 连续亏损（当日胜率 < 30% 且本周胜率 < 40%）
    weekly_win_rate = weekly_stats["wins"] / weekly_stats["total"] if weekly_stats["total"] > 0 else 0

    if win_rate < 0.3 and weekly_win_rate < 0.4:
        return (
            "⚠️ 连续亏损警示：本周胜率偏低，建议：\n"
            "  1. 检查 MA20 支撑是否有效（是否需改用 MA10）\n"
            "  2. 降低仓位至 5% 以下试错\n"
            "  3. 若连续 3 天亏损，暂停推荐 1 天复盘"
        )
    elif win_rate >= 0.7:
        return "✅ 策略有效：缩量回踩低吸逻辑在当前市场环境下表现良好，可保持仓位"
    elif avg_open < -1.0:
        return (
            "📉 平均开盘溢价为负，可能原因：\n"
            "  1. 选股时点偏早，回踩未完成\n"
            "  2. 大盘低开拖累（检查基准开盘涨幅）\n"
            "  3. 建议推迟到 14:45 后再选股"
        )
    elif win_rate >= 0.5:
        return "📊 策略表现平稳，胜率符合预期，继续观察"
    return ""
