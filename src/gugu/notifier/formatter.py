"""Feishu interactive card formatters for gugu notifications.

Color semantics:
- green: buy / profit
- red: sell / loss
- yellow: warning
- grey: info
"""
from __future__ import annotations

from typing import Any


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
        name = s.get("name", "")
        action = {"buy": "买入", "sell": "卖出"}.get(direction, direction)
        lines.append(f"- {action} {name}({symbol})")
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
    name = signal.get("name", "") or symbol
    strategies = signal.get("strategies", [])
    strategy = signal.get("strategy", "") or (
        ",".join(str(s) for s in strategies) if strategies else "未提供"
    )
    reason = signal.get("reason", "")
    position = signal.get("suggested_position", "")
    if not position and "suggested_position_ratio" in signal:
        position = f"{signal['suggested_position_ratio']:.0%}"
    price = signal.get("price", "")
    wisdom = signal.get("wisdom", {})
    wisdom_decision = signal.get("wisdom_decision", {})

    # entry-filtered signals use yellow card
    if wisdom_decision.get("entry_filtered"):
        template = "yellow"
        action = f"{action}（已过滤）"

    title = f"{action}信号 · {name}({symbol})"

    sections = [
        f"**股票**：{name}({symbol})\n**方向**：{action}\n**当前价**：{price}",
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

    decision_text = _format_wisdom_decision(wisdom_decision)
    if decision_text:
        sections.append(f"**智慧决策**\n{decision_text}")

    wisdom_text = _format_wisdom(wisdom)
    if wisdom_text:
        sections.append(f"**交易智慧参考**\n{wisdom_text}")

    return _card(title, template, sections, note="gugu 交易系统 · 信号通知")


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
            f"- {sym}: {quantity}股 | 市值 ￥{mv_val:,.0f} | 盈亏 {profit_str}"
        )
    return "\n".join(lines) if lines else ""


def format_daily_report(period: str, data: dict[str, Any]) -> dict[str, Any]:
    """Format a daily report into a Feishu card.

    Args:
        period: "morning" / "noon" / "close".
        data: Dict with keys market_summary, sector_top, signals, portfolio_summary.

    Returns:
        Feishu interactive card message dict.
    """
    period_map = {
        "morning": ("盘前日报", "grey"),
        "noon": ("午盘日报", "yellow"),
        "close": ("收盘日报", "grey"),
    }
    title_text, template = period_map.get(period, ("每日日报", "grey"))

    market_summary = data.get("market_summary", "")
    sector_top = data.get("sector_top", "")
    signals = data.get("signals", [])
    portfolio_summary = data.get("portfolio_summary", "")

    sections: list[str] = []
    if market_summary:
        sections.append(f"**市场概览**\n{_format_market_summary(market_summary)}")
    if sector_top:
        sections.append(f"**热门板块**\n{_format_sector_top(sector_top)}")
    if signals:
        sections.append(f"**今日信号**\n{_format_signals(signals)}")
    if portfolio_summary:
        sections.append(f"**持仓概况**\n{_format_portfolio_summary(portfolio_summary)}")

    if not sections:
        sections.append("暂无数据")

    return _card(title_text, template, sections, note="gugu 交易系统 · 每日日报")


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

    return _card(title, template, sections, note="gugu 交易系统 · 风控告警")


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

    return _card(title, template, sections, note="gugu 交易系统 · 回测报告")


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

    return _card(title, "red", sections, note="gugu 交易系统 · 异常告警")