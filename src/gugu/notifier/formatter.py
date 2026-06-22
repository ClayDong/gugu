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


def _format_signals(signals: Any) -> str:
    """Format signals list (strings or dicts) into markdown lines."""
    if not signals:
        return "无"
    if isinstance(signals, str):
        return signals
    if isinstance(signals, list):
        lines: list[str] = []
        for s in signals:
            if isinstance(s, dict):
                direction = str(s.get("direction", "")).lower()
                symbol = s.get("symbol", "")
                name = s.get("name", "")
                action = {"buy": "买入", "sell": "卖出"}.get(direction, direction)
                lines.append(f"- {action} {name}({symbol})")
            else:
                lines.append(f"- {s}")
        return "\n".join(lines) if lines else "无"
    return str(signals)


def format_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Format a trade signal into a Feishu card.

    Args:
        signal: Dict with keys symbol, name, direction("buy"/"sell"),
                strategy, reason, suggested_position, price.

    Returns:
        Feishu interactive card message dict.
    """
    direction = str(signal.get("direction", "buy")).lower()
    is_buy = direction == "buy"
    template = "green" if is_buy else "red"
    action = "买入" if is_buy else "卖出"

    symbol = signal.get("symbol", "")
    name = signal.get("name", "")
    strategy = signal.get("strategy", "")
    reason = signal.get("reason", "")
    position = signal.get("suggested_position", "")
    price = signal.get("price", "")

    title = f"{action}信号 · {name}({symbol})"

    sections = [
        f"**股票**：{name}({symbol})\n**方向**：{action}\n**当前价**：{price}",
        f"**触发策略**：{strategy}\n**建议仓位**：{position}\n**触发理由**：{reason}",
    ]

    return _card(title, template, sections, note="gugu 交易系统 · 信号通知")


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
        sections.append(f"**市场概览**\n{market_summary}")
    if sector_top:
        sections.append(f"**热门板块**\n{sector_top}")
    if signals:
        sections.append(f"**今日信号**\n{_format_signals(signals)}")
    if portfolio_summary:
        sections.append(f"**持仓概况**\n{portfolio_summary}")

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
