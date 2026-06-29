"""Feishu interactive card formatters for gugu notifications.

Color semantics:
- green: buy / profit
- red: sell / loss
- yellow: warning
- grey: info
"""
from __future__ import annotations

from typing import Any

# 内置股票名称映射（fallback，当信号中 name 缺失或异常时使用）
_STOCK_NAMES: dict[str, str] = {
    "600519": "贵州茅台", "300750": "宁德时代", "000858": "五粮液",
    "601318": "中国平安", "000333": "美的集团", "300059": "东方财富",
    "600030": "中信证券", "000776": "广发证券", "603259": "药明康德",
    "600600": "青岛啤酒", "002625": "光启技术", "600674": "川投能源",
    "688396": "华润微", "601238": "广汽集团", "600460": "士兰微",
    "000977": "浪潮信息", "002049": "紫光国微", "300033": "同花顺",
    "600026": "中远海能", "600150": "中国船舶", "600489": "中金黄金",
    "600584": "长电科技", "601899": "紫金矿业", "603019": "中科曙光",
    "603799": "华友钴业", "600036": "招商银行", "601398": "工商银行",
    "601939": "建设银行", "000538": "云南白药",
}


def _resolve_name(signal: dict[str, Any]) -> str:
    """从信号字典解析股票中文名称，支持多层 fallback。

    优先级：signal["name"]（有效时）> _STOCK_NAMES 映射 > signal["symbol"]
    同时校验 name 不能等于代码本身（防止 API 返回脏数据）。
    """
    symbol = signal.get("symbol", "")
    name = signal.get("name", "") or ""
    # 如果 name 为空或等于代码（API 脏数据），用内置映射
    if not name or name == symbol:
        name = _STOCK_NAMES.get(symbol, "")
    # 仍然为空，回退为代码
    return name or symbol


def _card(
    title: str,
    template: str,
    sections: list[str],
    note: str = "",
) -> dict[str, Any]:
    """Build a Feishu interactive card message.

    Args:
        title: Card header title.
        template: Header color template (green/red/yellow/grey).
        sections: List of markdown content blocks, separated by hr.
        note: Optional footer note text.

    Returns:
        Feishu interactive card message dict.
    """
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


def _format_signals(signals: list[dict[str, Any]]) -> str:
    """Format signals list into markdown lines."""
    if not signals:
        return "无"
    lines: list[str] = []
    for s in signals:
        direction = str(s.get("direction", "")).lower()
        symbol = s.get("symbol", "")
        name = _resolve_name(s)
        stock_label = f"{name}({symbol})" if name else symbol
        action = {"buy": "买入", "sell": "卖出"}.get(direction, direction)
        lines.append(f"- {action} {stock_label}")
    return "\n".join(lines) if lines else "无"


def _format_wisdom(wisdom: dict[str, Any]) -> str:
    """Format wisdom advice dict into readable markdown lines."""
    if not wisdom:
        return ""
    label_map = {
        "entry_check": "入场建议",
        "stop_loss": "止损建议",
        "position_sizing": "仓位建议",
        "profit_taking": "止盈建议",
        "trailing_stop": "追踪止损",
        "psychology_check": "心态检查",
    }
    lines: list[str] = []
    for key, val in wisdom.items():
        if not val:
            continue
        label = label_map.get(key, key)
        text = val if isinstance(val, str) else str(val)
        # truncate at 200 chars to keep card short
        if len(text) > 200:
            text = text[:200] + "..."
        lines.append(f"- **{label}**：{text}")
    return "\n".join(lines) if lines else ""


def _format_wisdom_decision(decision: Any) -> str:
    """Format wisdom decision dict into readable markdown lines."""
    if not decision or not isinstance(decision, dict):
        return ""
    lines: list[str] = []
    if decision.get("entry_filtered"):
        lines.append(f"- ⚠️ **入场过滤**：{decision.get('filter_reason', '低置信度')}")
    if decision.get("adjusted_position_ratio") is not None:
        ratio = decision["adjusted_position_ratio"]
        strategy = decision.get("position_strategy", "")
        strategy_text = {"trial": "试仓", "add": "加码", "full": "满仓"}.get(strategy, strategy)
        lines.append(f"- 📊 **仓位调整**：{ratio:.2%}（{strategy_text}）")
    if decision.get("stop_loss_price") is not None:
        stop_price = decision["stop_loss_price"]
        stop_pct = decision.get("stop_loss_pct", 0)
        lines.append(f"- 🛑 **止损预设**：￥{stop_price:.2f}（-{stop_pct:.0%}）")
    return "\n".join(lines) if lines else ""


def _format_decision_chain(chain: list[dict]) -> str:
    """Format decision_chain into compact markdown lines.

    Shows each filter step with pass/fail status, result, and key details.
    """
    if not chain:
        return ""
    lines: list[str] = []
    for step in chain:
        name = step.get("name", "")
        result = step.get("result", "")
        passed = step.get("passed", True)
        status_icon = "✅" if passed else "❌"
        desc = step.get("description", step.get("reason", ""))
        detail = f" — {desc[:60]}" if desc else ""
        lines.append(f"{status_icon} **{name}**: {result}{detail}")
    return "\n".join(lines)


def format_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Format a trade signal into a Feishu card.

    Args:
        signal: Dict with keys symbol, name, direction("buy"/"sell"),
                strategy, reason, suggested_position, price, wisdom, wisdom_decision.

    Returns:
        Feishu interactive card message dict.
    """
    direction = str(signal.get("direction", "buy")).lower()
    is_buy = direction == "buy"
    template = "green" if is_buy else "red"
    action = "买入" if is_buy else "卖出"

    symbol = signal.get("symbol", "")
    name = _resolve_name(signal)
    strategies = signal.get("strategies", [])
    strategy = signal.get("strategy", "") or (
        ",".join(str(s) for s in strategies) if strategies else "未提供"
    )
    reason = signal.get("reason", "") or ""
    confidence = signal.get("confidence") or 0.0  # None → 0.0, keeps 0.0 as-is
    position = signal.get("suggested_position", "")
    if not position and signal.get("suggested_position_ratio") is not None:
        ratio = float(signal["suggested_position_ratio"])
        position = f"{ratio:.0%}"
    price = signal.get("price") or 0.0
    wisdom = signal.get("wisdom", {})
    wisdom_decision = signal.get("wisdom_decision", {})

    # 信号强度标签
    strength = ""
    if wisdom_decision.get("entry_filtered"):
        strength = "⚠️ 已过滤"
    elif signal.get("sector_check", {}).get("is_hot") and signal.get("multi_period", {}).get("weekly_aligned"):
        strength = "🟢 强信号"
    elif signal.get("weekly_misaligned") or signal.get("sector_check", {}).get("is_cold"):
        strength = "🟡 弱信号"
    else:
        strength = "🔵 中信号" if confidence >= 0.6 else "🟡 弱信号"

    # entry-filtered signals use yellow card
    if wisdom_decision.get("entry_filtered"):
        template = "yellow"
        action = f"{action}（已过滤）"

    title = f"{action}信号 · {name}({symbol})"
    if strength and not wisdom_decision.get("entry_filtered"):
        title = f"{strength} {title}"

    sections = [
        f"**股票**：{name}({symbol})\n**方向**：{action}\n**当前价**：{price}\n**信号强度**：{strength}",
        f"**触发策略**：{strategy}\n**建议仓位**：{position or '未提供'}\n**触发理由**：{reason}",
    ]

    # if the signal has an order result, display actual fill details (U-01 fix)
    order_result = signal.get("order_result")
    if order_result:
        success = order_result.get("success", False)
        qty = order_result.get("quantity", 0)
        fill_price = order_result.get("price", 0)
        commission = order_result.get("commission", 0)
        amount = qty * fill_price
        status_label = "✅ 成交" if success else "❌ 失败"
        sections.append(
            f"**实际下单**：{status_label}\n"
            f"**数量**：{qty}股\n"
            f"**成交价**：￥{fill_price:.2f}\n"
            f"**金额**：￥{amount:.2f}\n"
            f"**佣金**：￥{commission:.2f}\n"
            f"**备注**：{order_result.get('message', '')}"
        )

    # 决策链路摘要（A-06 修复：从 decision_chain 渲染每一层）
    decision_chain_text = _format_decision_chain(signal.get("decision_chain", []))
    if decision_chain_text:
        sections.append(f"**决策链路**\n{decision_chain_text}")

    decision_text = _format_wisdom_decision(wisdom_decision)
    if decision_text:
        sections.append(f"**智慧决策**\n{decision_text}")

    wisdom_text = _format_wisdom(wisdom)
    if wisdom_text:
        sections.append(f"**交易智慧参考**\n{wisdom_text}")

    return _card(title, template, sections, note="明策 · gugu · 信号通知")


def _format_market_summary(summary: dict[str, Any]) -> str:
    """Format market summary dict into readable markdown text."""
    if not summary:
        return ""
    total_value = summary.get("total_value", 0)
    cash = summary.get("cash", 0)
    positions_count = summary.get("positions_count", 0)
    return (
        f"总资产: ￥{total_value:,.0f} | "
        f"现金: ￥{cash:,.0f} | "
        f"持仓: {positions_count} 只"
    )


def _format_sector_top(sector_top: list[dict[str, Any]]) -> str:
    """Format sector flow list into readable markdown lines."""
    if not sector_top:
        return ""
    lines: list[str] = []
    for item in sector_top:
        sector = item.get("sector", "")
        main_net = item.get("main_net", 0)
        main_pct = item.get("main_pct", 0)
        try:
            net_val = float(main_net or 0)
            pct_val = float(main_pct or 0)
        except (TypeError, ValueError):
            net_val = 0.0
            pct_val = 0.0
        lines.append(
            f"- {sector}: 主力净流入 ￥{net_val/1e8:,.2f}亿 ({pct_val:+.2%})"
        )
    return "\n".join(lines) if lines else ""


def _format_portfolio_summary(portfolio: dict[str, Any]) -> str:
    """Format portfolio dict into readable markdown lines."""
    if not portfolio:
        return ""
    lines: list[str] = []
    for sym, info in portfolio.items():
        raw = info.get("name", "") or ""
        name = raw if (raw and raw != sym) else _STOCK_NAMES.get(sym, raw)
        stock_label = f"{name}({sym})" if name else sym
        quantity = info.get("quantity", 0)
        profit = info.get("profit", 0)
        market_value = info.get("market_value", 0)
        try:
            profit_val = float(profit or 0)
            mv_val = float(market_value or 0)
        except (TypeError, ValueError):
            profit_val = 0.0
            mv_val = 0.0
        profit_str = f"+{profit_val:,.0f}" if profit_val >= 0 else f"-{abs(profit_val):,.0f}"
        lines.append(
            f"- {stock_label}: {quantity}股 | 市值 ￥{mv_val:,.0f} | 盈亏 {profit_str}"
        )
    return "\n".join(lines) if lines else ""


def format_daily_report(period: str, data: dict[str, Any]) -> dict[str, Any]:
    """Format a daily report into a Feishu card.

    聚焦信号汇总 + 绩效验证，弱化传统日报。

    Args:
        period: "morning" / "noon" / "close".
        data: Dict with keys market_summary, signals, portfolio_summary,
              performance, regime, risk, trailing_stops.

    Returns:
        Feishu interactive card message dict.
    """
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

    sections: list[str] = []
    if market_summary:
        sections.append(f"**账户概览**\n{_format_market_summary(market_summary)}")

    # 收盘报告增加市场状态 + 风控状态 + 移动止损（A-06 修复）
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
                stop = s.get("current_stop", 0)
                highest = s.get("highest", 0)
                sig = s.get("signal", "hold")
                stop_lines.append(
                    f"- {sym}: 止损 {stop:.2f} / 最高 {highest:.2f} ({sig})"
                )
            sections.append("**移动止损**\n" + "\n".join(stop_lines))

    if signals:
        sections.append(f"**今日信号**（{len(signals)} 条）\n{_format_signals(signals)}")
    if portfolio_summary:
        sections.append(f"**持仓概况**\n{_format_portfolio_summary(portfolio_summary)}")
    if performance:
        perf_text = _format_performance(performance)
        if perf_text:
            sections.append(f"**信号绩效验证**\n{perf_text}")

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

    win_rate = perf.get("win_rate", 0)
    avg_return = perf.get("avg_return_5d", 0)
    tracked = perf.get("tracked_count", 0)
    buy_count = perf.get("buy_count", 0)
    executed = perf.get("executed_count", 0)
    period_days = perf.get("period_days", 30)

    lines = [
        f"近 {period_days} 天: {total} 信号, "
        f"{buy_count} 买, {executed} 已执行, {tracked} 可追踪",
        f"5日命中率: {win_rate:.1%} | 平均收益: {avg_return:+.2%}",
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


def format_risk_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Format a risk alert into a Feishu card.

    Args:
        alert: Dict with keys level("warn"/"halt"), message, suggestion.

    Returns:
        Feishu interactive card message dict.
    """
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
    """Format a backtest report into a Feishu card.

    Args:
        report: Dict with keys strategy, total_return, sharpe,
                max_drawdown, win_rate, trades_count.

    Returns:
        Feishu interactive card message dict.
    """
    total_return = report.get("total_return", 0)
    try:
        return_val = float(total_return)
    except (TypeError, ValueError):
        return_val = 0.0
    template = "green" if return_val >= 0 else "red"

    strategy = report.get("strategy", "")
    sharpe = report.get("sharpe", 0)
    max_drawdown = report.get("max_drawdown", 0)
    win_rate = report.get("win_rate", 0)
    trades_count = report.get("trades_count", 0)

    title = f"回测报告 · {strategy}"

    sections = [
        f"**策略**：{strategy}\n**总收益**：{total_return}\n**夏普比率**：{sharpe}",
        f"**最大回撤**：{max_drawdown}\n**胜率**：{win_rate}\n**交易次数**：{trades_count}",
    ]

    return _card(title, template, sections, note="明策 · gugu · 回测报告")


def format_system_error(error: dict[str, Any]) -> dict[str, Any]:
    """Format a system error into a Feishu card.

    Args:
        error: Dict with keys module, message, suggestion.

    Returns:
        Feishu interactive card message dict.
    """
    module = error.get("module", "")
    message = error.get("message", "")
    suggestion = error.get("suggestion", "")

    title = f"系统异常 · {module}"

    sections = [
        f"**模块**：{module}\n**异常**：{message}",
        f"**恢复建议**：{suggestion}",
    ]

    return _card(title, "red", sections, note="明策 · gugu · 异常告警")


def format_holdings_sell_alert(stocks: list[dict]) -> dict[str, Any]:
    """格式化为持仓卖出告警卡片。

    Args:
        stocks: 卖出信号明确的持仓股列表，每项含 name, symbol, price,
                profit_pct, sell_count, buy_count, sell_signals.
    """
    if not stocks:
        return {
            "msg_type": "interactive",
            "card": {"header": {"title": {"tag": "plain_text", "content": "持仓卖出检查"}, "template": "green"},
                     "elements": []},
        }

    lines = ["**以下持仓股出现卖出信号：**", ""]
    for s in stocks:
        name = s.get("name", s.get("symbol", ""))
        symbol = s.get("symbol", "")
        price = s.get("price", 0)
        profit = s.get("profit_pct", 0)
        sell_n = s.get("sell_count", 0)
        buy_n = s.get("buy_count", 0)
        profit_icon = "🟢" if profit >= 0 else "🔴"
        lines.append(
            f"**{name}** ({symbol})\n"
            f"{profit_icon} 现价 {price:.2f} | 盈亏 {profit:+.1f}%\n"
            f"🔴 卖出 {sell_n} vs 🟢 买入 {buy_n}\n"
        )
        for sig in s.get("sell_signals", [])[:3]:
            strategy = sig.get("strategy_name", sig.get("strategy", ""))
            strength = sig.get("signal_strength", sig.get("confidence", 0))
            lines.append(f"  - {strategy} 强度 {strength:.0%}")

    lines.append("")
    lines.append("---\n⚠️ 策略信号提示，不构成投资建议。请结合基本面和仓位管理决策。")

    return _card(
        title="⚠️ 持仓卖出信号告警",
        template="red",
        sections=["\n".join(lines)],
        note="明策 · gugu · 持仓告警",
    )