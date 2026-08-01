"""Daily reports, performance, risk alerts, backtest, and validation formatters."""

from __future__ import annotations

from typing import Any

from gugu.data.stock_names import lookup_stock_name
from gugu.notifier.formatter import _card, _format_signals


def _format_market_summary(summary: dict[str, Any]) -> str:
    """Format market summary dict into readable markdown text."""
    if not summary:
        return ""
    total_value = summary.get("total_value", 0)
    cash = summary.get("cash", 0)
    positions_count = summary.get("positions_count", 0)
    return f"总资产: ￥{total_value:,.0f} | 现金: ￥{cash:,.0f} | 持仓: {positions_count} 只"


def _format_portfolio_summary(portfolio: dict[str, Any]) -> str:
    """Format portfolio dict into readable markdown lines with P&L."""
    if not portfolio:
        return ""
    lines: list[str] = []
    total_profit = 0.0
    total_cost = 0.0
    for sym, info in portfolio.items():
        raw = info.get("name", "") or ""
        name = raw if (raw and raw != sym) else (lookup_stock_name(sym) or raw)
        stock_label = f"{name}({sym})" if name else sym
        quantity = info.get("quantity", 0)
        profit = info.get("profit", 0)
        market_value = info.get("market_value", 0)
        cost = info.get("cost", 0)
        try:
            profit_val = float(profit or 0)
            mv_val = float(market_value or 0)
            cost_val = float(cost or 0)
        except (TypeError, ValueError):
            profit_val = 0.0
            mv_val = 0.0
            cost_val = 0.0
        total_profit += profit_val
        total_cost += cost_val
        profit_str = f"+{profit_val:,.0f}" if profit_val >= 0 else f"-{abs(profit_val):,.0f}"
        if cost_val > 0:
            profit_pct = (profit_val / cost_val) * 100
            profit_str += f" ({profit_pct:+.1f}%)"
        lines.append(f"- {stock_label}: {quantity}股 | 市值 ￥{mv_val:,.0f} | 盈亏 {profit_str}")
    if len(portfolio) > 1 and total_cost > 0:
        total_pct = (total_profit / total_cost) * 100
        total_str = f"+{total_profit:,.0f}" if total_profit >= 0 else f"-{abs(total_profit):,.0f}"
        lines.append(f"**持仓合计**: 盈亏 {total_str} ({total_pct:+.1f}%)")
    return "\n".join(lines) if lines else ""


def _format_filtered_signals(filtered: list[dict[str, Any]]) -> str:
    """格式化被过滤信号摘要。"""
    if not filtered:
        return ""
    lines = [f"**被过滤信号**（{len(filtered)} 条，仅收盘展示）"]
    for s in filtered[:8]:
        symbol = s.get("symbol", "")
        name = s.get("name", "") or lookup_stock_name(symbol) or symbol
        direction = "买入" if s.get("direction") == "buy" else "卖出"
        reason = s.get("filter_reason", "未知原因")[:40]
        lines.append(f"• {name}({symbol}) {direction} — {reason}")
    if len(filtered) > 8:
        lines.append(f"  ... 及其他 {len(filtered) - 8} 条")
    return "\n".join(lines)


def _format_source_health(health: dict[str, Any] | None) -> str:
    """格式化数据源健康摘要。"""
    if not health:
        return ""
    sources = health.get("sources", {})
    if not sources:
        return ""
    lines = ["**数据源健康**"]
    total = health.get("total_requests", 0)
    failure_rate = health.get("failure_rate", 0.0)
    degraded_min = health.get("degraded_minutes", 0.0)
    lines.append(
        f"- 今日请求: {total} 次 | 失败率: {failure_rate:.1%} | 降级时长: {degraded_min:.1f} 分钟"
    )
    for src, stats in sorted(sources.items()):
        succ = stats.get("success", 0)
        fail = stats.get("failure", 0)
        fb = stats.get("fallback_used", 0)
        deg = stats.get("degraded", 0)
        rec = stats.get("recovered", 0)
        if succ + fail + fb == 0:
            continue
        status = "✅" if fail == 0 and deg == 0 else "⚠️"
        lines.append(f"- {status} {src}: 成功{succ} 失败{fail} 降级{deg} 恢复{rec} 兜底{fb}")
    return "\n".join(lines)


def format_daily_report(period: str, data: dict[str, Any]) -> dict[str, Any]:
    """Format a daily report into a Feishu card."""
    period_map = {
        "morning": ("盘前信号汇总", "grey"),
        "noon": ("午盘信号汇总", "yellow"),
        "close": ("收盘信号汇总 + 绩效", "grey"),
    }
    title_text, template = period_map.get(period, ("信号汇总", "grey"))

    market_summary = data.get("market_summary", "")
    signals = data.get("signals", [])
    portfolio_summary = data.get("portfolio_summary", "")
    performance = data.get("performance")
    regime = data.get("regime")
    risk = data.get("risk")
    trailing_stops = data.get("trailing_stops")
    filtered_signals = data.get("filtered_signals", [])
    source_health = data.get("source_health")

    sections: list[str] = []
    if market_summary:
        sections.append(f"**账户概览**\n{_format_market_summary(market_summary)}")

    if period == "close":
        if regime:
            sections.append(
                f"**市场状态**\n"
                f"- 阶段: {regime.get('regime', '?')}\n"
                f"- 总仓位上限: {regime.get('total_limit', 0):.0%}\n"
                f"- {regime.get('reason', '')[:60]}"
            )
        if risk:
            risk_lines = [f"- 熔断: {'是 ⛔' if risk.get('halted') else '否 ✅'}"]
            loss_pct = risk.get("daily_loss_pct", 0)
            if loss_pct:
                risk_lines.append(f"- 当日盈亏: {loss_pct:+.2%}")
            sections.append("**风控状态**\n" + "\n".join(risk_lines))
        if trailing_stops:
            stop_lines = []
            for s in trailing_stops:
                sym = s.get("symbol", "")
                name = lookup_stock_name(sym) or sym
                stop = s.get("current_stop", 0)
                highest = s.get("highest", 0)
                sig = s.get("signal", "hold")
                stop_lines.append(f"- {name}({sym}): 止损 {stop:.2f} / 最高 {highest:.2f} ({sig})")
            sections.append("**移动止损**\n" + "\n".join(stop_lines))

    if signals:
        sections.append(f"**今日信号**（{len(signals)} 条）\n{_format_signals(signals)}")
    if portfolio_summary:
        sections.append(f"**持仓概况**\n{_format_portfolio_summary(portfolio_summary)}")
    if performance:
        perf_text = _format_performance(performance)
        if perf_text:
            sections.append(f"**信号绩效验证**\n{perf_text}")

    if period == "close" and filtered_signals:
        filtered_text = _format_filtered_signals(filtered_signals)
        if filtered_text:
            sections.append(filtered_text)

    # v0.7.10 新增：收盘日报附带数据源健康摘要
    if period == "close":
        health_text = _format_source_health(source_health)
        if health_text:
            sections.append(health_text)

    if not sections:
        sections.append("暂无数据")

    return _card(title_text, template, sections, note="明策 · gugu · 信号汇总")


def _format_performance(perf: dict[str, Any]) -> str:
    """Format signal performance report into markdown."""
    if not perf:
        return ""
    total = perf.get("total_signals", 0)
    if total < 3:
        return perf.get("message", f"样本数不足（{total}），暂无绩效数据")

    forward_hit_rate = perf.get("forward_5d_signal_hit_rate", 0)
    avg_return = perf.get("avg_return_5d", 0)
    tracked = perf.get("tracked_count", 0)
    buy_count = perf.get("buy_count", 0)
    executed = perf.get("executed_count", 0)
    closed_trade_win_rate = perf.get("closed_trade_win_rate", 0)
    realized_pnl = perf.get("realized_pnl", 0)
    period_days = perf.get("period_days", 30)

    lines = [
        f"近 {period_days} 天信号统计: {total} 条信号, {buy_count} 条买入, {executed} 条已执行, {tracked} 条已追踪",
        f"信号5日命中率: {forward_hit_rate:.1%} | 信号5日平均收益: {avg_return:+.2%}",
        f"闭仓交易胜率: {closed_trade_win_rate:.1%} | 已实现盈亏: {realized_pnl:+.2f}",
        "（说明：前瞻命中率 = 信号发出后5日K线收盘价涨幅；闭仓胜率仅依据 outcome_status=closed 的 realized_pnl）",
    ]

    by_strategy = perf.get("by_strategy", [])
    if by_strategy:
        lines.append("**策略对比**:")
        for s in by_strategy[:5]:
            lines.append(
                f"- {s['strategy']}: {s['count']}次, "
                f"胜率{s['win_rate']:.0%}, "
                f"均收{s['avg_return']:+.2%}"
            )

    return "\n".join(lines)


def format_performance(perf: dict[str, Any]) -> dict[str, Any]:
    """Format a standalone performance report card."""
    if not perf:
        return _card("📈 信号绩效验证", "grey", ["暂无数据"])

    text = _format_performance(perf)
    if not text:
        return _card("📈 信号绩效验证", "grey", ["暂无数据"])

    total = perf.get("total_signals", 0)
    forward_hit_rate = perf.get("forward_5d_signal_hit_rate", 0)
    template = "green" if forward_hit_rate >= 0.5 else "red"

    return _card(
        title="📈 信号绩效验证",
        template=template,
        sections=[text],
        note="gugu · 信号绩效验证",
        subtitle=f"命中率 {forward_hit_rate:.1%} | 样本 {total}",
    )


def format_risk_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Format a risk alert into a Feishu card."""
    level = alert.get("level", "warn")
    is_halt = level == "halt"
    template = "red" if is_halt else "yellow"
    level_text = "熔断" if is_halt else "预警"

    message = alert.get("message", "")
    suggestion = alert.get("suggestion", "")

    title = f"风控{level_text} · gugu"

    sections = [
        f"**级别**：{level_text}\n**详情**：{message}",
        f"**建议动作**：{suggestion}",
    ]

    return _card(title, template, sections, note="明策 · gugu · 风控告警")


def format_backtest_report(report: dict[str, Any]) -> dict[str, Any]:
    """Format a backtest report into a Feishu card."""
    total_return = report.get("total_return", 0)
    try:
        return_val = float(total_return)
    except (TypeError, ValueError):
        return_val = 0.0
    template = "red" if return_val >= 0 else "green"

    strategy = report.get("strategy", "")
    sharpe = report.get("sharpe", 0)
    max_drawdown = report.get("max_drawdown", 0)
    win_rate = report.get("win_rate", 0)
    trades_count = report.get("trades_count", 0)
    annual_return = report.get("annual_return", 0)
    profit_factor = report.get("profit_factor", 0)
    avg_hold_days = report.get("avg_hold_days", 0)
    symbol = report.get("symbol", "")
    name = lookup_stock_name(symbol) or symbol

    title = f"回测报告 · {strategy}"
    if symbol:
        title += f" · {name}({symbol})"

    if symbol:
        header = f"**策略**：{strategy}\n**标的**：{name}({symbol})"
    else:
        header = f"**策略**：{strategy}\n**标的**：多股聚合"
    sections = [
        f"{header}\n**总收益**：{total_return}\n**年化收益**：{annual_return}",
        f"**夏普比率**：{sharpe}\n**最大回撤**：{max_drawdown}\n"
        f"**胜率**：{win_rate}\n**盈亏比**：{profit_factor}",
        f"**交易次数**：{trades_count}\n**平均持仓**：{avg_hold_days} 天",
    ]

    return _card(title, template, sections, note="明策 · gugu · 回测报告")


def format_validation_report(report: dict[str, Any]) -> dict[str, Any]:
    """Format a strategy validation/gate report into a Feishu card."""
    strategy_name = report.get("strategy_name", "")
    gate_status = report.get("gate_status", "unknown")
    metrics = report.get("metrics", {})
    issues = report.get("issues", [])
    recs = report.get("recommendations", [])

    status_colors = {"passed": "red", "failed": "green", "partial": "yellow", "unknown": "grey"}
    template = status_colors.get(gate_status, "grey")

    title = f"策略验证 · {strategy_name}"
    if gate_status == "passed":
        title += " ✅"
    elif gate_status == "failed":
        title += " ❌"

    sections = [f"**策略**：{strategy_name}\n**门禁状态**：{gate_status}"]

    if metrics:
        metric_lines = []
        for k, v in metrics.items():
            metric_lines.append(f"{k}: {v}")
        sections.append("**核心指标**：\n" + "\n".join(metric_lines))

    if issues:
        sections.append("**发现问题**：\n" + "\n".join(f"- {i}" for i in issues[:5]))
        if len(issues) > 5:
            sections[-1] += f"\n...及 {len(issues) - 5} 项其他问题"

    if recs:
        sections.append("**优化建议**：\n" + "\n".join(f"- {r}" for r in recs[:3]))
        if len(recs) > 3:
            sections[-1] += f"\n...及 {len(recs) - 3} 项其他建议"

    return _card(title, template, sections, note="明策 · gugu · 策略验证")


def format_system_error(error: dict[str, Any]) -> dict[str, Any]:
    """Format a system error into a Feishu card."""
    from gugu.notifier.formatter_special import _sanitize_message

    module = error.get("module", "")
    message = error.get("message", "")
    suggestion = error.get("suggestion", "")

    message = _sanitize_message(message)

    title = f"系统异常 · {module}"

    sections = [
        f"**模块**：{module}\n**异常**：{message}",
        f"**恢复建议**：{suggestion}",
    ]

    return _card(title, "red", sections, note="明策 · gugu · 异常告警")


def build_signal_performance_card(report: dict[str, Any]) -> dict[str, Any]:
    """Build a signal performance report into a Feishu card."""
    total = report.get("total_signals", 0)

    if total < 3 or "message" in report:
        msg = report.get("message", f"样本数不足（{total}），暂无绩效数据")
        return _card(
            title="📈 信号绩效验证",
            template="grey",
            sections=[msg],
            note="gugu · 信号绩效验证",
        )

    forward_hit_rate = report.get("forward_5d_signal_hit_rate", report.get("win_rate", 0))
    avg_return = report.get("avg_return_5d", 0)
    closed_trade_win_rate = report.get("closed_trade_win_rate", 0)
    realized_pnl = report.get("realized_pnl", 0)
    buy_count = report.get("buy_count", 0)
    sell_count = report.get("sell_count", 0)
    executed = report.get("executed_count", 0)
    tracked = report.get("tracked_count", 0)
    period_days = report.get("period_days", 30)
    generated_at = report.get("generated_at", "")

    template = "green" if forward_hit_rate >= 0.5 else "red"

    overview_lines = [
        f"**统计区间**：近 {period_days} 天",
        f"**信号总数**：{total}（买 {buy_count} / 卖 {sell_count} / 已执行 {executed}）",
        f"**追踪样本**：{tracked}",
        f"**前瞻5日命中率**：{forward_hit_rate:.1%}",
        f"**前瞻5日平均收益**：{avg_return:+.2%}",
        f"**已平仓交易胜率**：{closed_trade_win_rate:.1%}",
        f"**已实现盈亏**：{realized_pnl:+.2f}",
    ]
    sections = ["\n".join(overview_lines)]

    by_strategy = report.get("by_strategy", [])
    if by_strategy:
        strategy_lines = ["**📊 策略绩效对比**"]
        for s in by_strategy[:8]:
            strategy_lines.append(
                f"- {s.get('strategy', '?')}: "
                f"{s.get('count', 0)}次 | "
                f"胜率{s.get('win_rate', 0):.0%} | "
                f"均收{s.get('avg_return', 0):+.2%} | "
                f"最佳{s.get('best_return', 0):+.2%} / 最差{s.get('worst_return', 0):+.2%}"
            )
        sections.append("\n".join(strategy_lines))

    recent = report.get("recent_signals", [])
    if recent:
        recent_lines = ["**🕒 最近信号追踪**"]
        for sig in recent[:5]:
            symbol = sig.get("symbol", "")
            name = lookup_stock_name(symbol) or sig.get("name", "") or symbol
            direction = sig.get("direction", "")
            dir_icon = "🔴" if direction == "buy" else "🟢"
            entry = sig.get("entry_price", "?")
            ret_5d = sig.get("return_5d")
            ret_str = f"{ret_5d:+.2%}" if isinstance(ret_5d, int | float) else "N/A"
            err = sig.get("error")
            if err:
                ret_str = f"追踪失败({err})"
            recent_lines.append(f"{dir_icon} {name}({symbol}) 入场{entry} → 5日{ret_str}")
        sections.append("\n".join(recent_lines))

    note = f"gugu · 信号绩效验证 | {generated_at}"
    return _card(
        title="📈 信号绩效验证",
        template=template,
        sections=sections,
        note=note,
        subtitle=f"前瞻命中率 {forward_hit_rate:.1%} | 已平仓胜率 {closed_trade_win_rate:.1%} | 样本 {tracked}",
    )


def build_signal_daily_validation_card(report: dict[str, Any]) -> dict[str, Any]:
    """Build a T+1 signal daily validation report card."""
    total = report.get("total_signals", 0)
    if total < 3 or "message" in report:
        msg = report.get("message", f"样本数不足（{total}），暂无次日验证")
        return _card(
            title="📊 信号次日验证",
            template="grey",
            sections=[msg],
            note="gugu · 信号次日验证",
        )

    valid = report.get("valid_count", 0)
    win_rate = report.get("win_rate", 0)
    avg_close_return = report.get("avg_close_return", 0)
    avg_drawdown = report.get("avg_max_drawdown", 0)
    stop_hit_rate = report.get("stop_hit_rate", 0)
    target_date = report.get("date", "")

    template = "green" if win_rate >= 0.5 else "red"

    overview_lines = [
        f"**信号日期**：{target_date}",
        f"**买入信号数**：{total}（可追踪 {valid}）",
        f"**T+1 收盘胜率**：{win_rate:.1%}",
        f"**T+1 平均收盘收益**：{avg_close_return:+.2%}",
        f"**T+1 平均盘中回撤**：{avg_drawdown:+.2%}",
        f"**止损命中率**：{stop_hit_rate:.1%}",
    ]
    sections = ["\n".join(overview_lines)]

    by_strategy = report.get("by_strategy", [])
    if by_strategy:
        strategy_lines = ["**📊 策略 T+1 表现**"]
        for s in by_strategy[:8]:
            strategy_lines.append(
                f"- {s.get('strategy', '?')}: "
                f"{s.get('count', 0)}次 | "
                f"胜率{s.get('win_rate', 0):.0%} | "
                f"均收{s.get('avg_return', 0):+.2%}"
            )
        sections.append("\n".join(strategy_lines))

    signals = report.get("signals", [])
    if signals:
        detail_lines = ["**🕒 信号明细**"]
        for sig in signals[:5]:
            symbol = sig.get("symbol", "")
            name = lookup_stock_name(symbol) or sig.get("name", "") or symbol
            entry = sig.get("entry_price", "?")
            close_ret = sig.get("close_return")
            dd = sig.get("max_drawdown")
            stop_hit = "🛑" if sig.get("stop_loss_hit") else ""
            ret_str = f"{close_ret:+.2%}" if isinstance(close_ret, int | float) else "N/A"
            dd_str = f"{dd:+.2%}" if isinstance(dd, int | float) else "N/A"
            detail_lines.append(
                f"- {name}({symbol}): 入场{entry} → 收盘{ret_str} | 回撤{dd_str} {stop_hit}"
            )
        sections.append("\n".join(detail_lines))

    generated_at = report.get("generated_at", "")
    return _card(
        title="📊 信号次日验证",
        template=template,
        sections=sections,
        note=f"gugu · 信号次日验证 | {generated_at}",
        subtitle=f"T+1 胜率 {win_rate:.1%} | 样本 {valid}",
    )


def build_signal_weekly_card(report: dict[str, Any]) -> dict[str, Any]:
    """Build a weekly signal performance summary card."""
    total = report.get("total_signals", 0)
    if total < 3 or "message" in report:
        msg = report.get("message", f"样本数不足（{total}），暂无周报")
        return _card(
            title="📈 信号绩效周报",
            template="grey",
            sections=[msg],
            note="gugu · 信号绩效周报",
        )

    valid = report.get("valid_count", 0)
    win_rate = report.get("win_rate", 0)
    avg_return = report.get("avg_return_5d", 0)
    best = report.get("best_return", 0)
    worst = report.get("worst_return", 0)
    start_date = report.get("start_date", "")
    end_date = report.get("end_date", "")

    template = "green" if win_rate >= 0.5 else "red"

    overview_lines = [
        f"**统计区间**：{start_date} ~ {end_date}",
        f"**买入信号数**：{total}（可追踪 {valid}）",
        f"**5日胜率**：{win_rate:.1%}",
        f"**5日平均收益**：{avg_return:+.2%}",
        f"**最佳/最差**：{best:+.2%} / {worst:+.2%}",
    ]
    sections = ["\n".join(overview_lines)]

    daily_summary = report.get("daily_summary", [])
    if daily_summary:
        daily_lines = ["**📅 分日信号密度**"]
        for d in daily_summary:
            daily_lines.append(
                f"- {d.get('date', '?')}: "
                f"{d.get('count', 0)}条 | "
                f"胜率{d.get('win_rate', 0):.0%} | "
                f"均收{d.get('avg_return', 0):+.2%}"
            )
        sections.append("\n".join(daily_lines))

    by_strategy = report.get("by_strategy", [])
    if by_strategy:
        strategy_lines = ["**📊 策略贡献**"]
        for s in by_strategy[:6]:
            strategy_lines.append(
                f"- {s.get('strategy', '?')}: "
                f"{s.get('count', 0)}次 | "
                f"胜率{s.get('win_rate', 0):.0%} | "
                f"均收{s.get('avg_return', 0):+.2%}"
            )
        sections.append("\n".join(strategy_lines))

    generated_at = report.get("generated_at", "")
    return _card(
        title="📈 信号绩效周报",
        template=template,
        sections=sections,
        note=f"gugu · 信号绩效周报 | {generated_at}",
        subtitle=f"5日胜率 {win_rate:.1%} | 样本 {valid}",
    )
