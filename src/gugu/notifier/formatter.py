"""Feishu interactive card formatters for gugu notifications.

Color semantics (A 股惯例：红涨绿跌):
- red: buy / profit / up / positive
- green: sell / loss / down / negative
- yellow: warning
- grey: info
"""

from __future__ import annotations

import re
from typing import Any

from gugu.data.stock_names import lookup_stock_name

# 优先级：signal["name"]（有效时）> stock_names 模块映射 > signal["symbol"]
# _STOCK_NAMES 保留为兼容别名，实际查询通过 lookup_stock_name() 统一管理
_STOCK_NAMES: dict[str, str] = {}


def _resolve_name(signal: dict[str, Any]) -> str:
    """从信号字典解析股票中文名称，支持多层 fallback。

    优先级：signal["name"]（有效时）> _STOCK_NAMES 映射 > signal["symbol"]
    同时校验 name 不能等于代码本身（防止 API 返回脏数据）。
    """
    symbol = signal.get("symbol", "")
    name = signal.get("name", "") or ""
    # 如果 name 为空或等于代码（API 脏数据），用内置映射
    if not name or name == symbol:
        name = lookup_stock_name(symbol)
    # 仍然为空，回退为代码
    return name or symbol


def _card(
    title: str,
    template: str,
    sections: list[str],
    note: str = "",
    subtitle: str = "",
) -> dict[str, Any]:
    """Build a Feishu interactive card message.

    Args:
        title: Card header title.
        template: Header color template (green/red/yellow/grey).
        sections: List of markdown content blocks, separated by hr.
        note: Optional footer note text.
        subtitle: Optional subtitle — rendered as the first element (Feishu header has no subtitle field).

    Returns:
        Feishu interactive card message dict.
    """
    elements: list[dict[str, Any]] = []
    # P0 修复：Feishu card header 无 subtitle 字段，将摘要作为第一个元素渲染
    if subtitle:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": subtitle}})
        elements.append({"tag": "hr"})
    for _i, section in enumerate(sections):
        if elements:
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

    header_payload: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": title},
        "template": template,
    }

    return {
        "msg_type": "interactive",
        "card": {"header": header_payload, "elements": elements},
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
    # LLM 决策后台进行中
    if decision.get("source") == "pending_llm":
        lines.append("- ⏳ **LLM 决策分析中**，稍后更新")
        return "\n".join(lines)
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
    # A 股惯例：红涨绿跌 — 买入(看涨)用红色，卖出(看跌)用绿色
    template = "red" if is_buy else "green"
    action = "买入" if is_buy else "卖出"

    symbol = signal.get("symbol", "")
    name = _resolve_name(signal)
    strategies = signal.get("strategies", [])
    strategy = signal.get("strategy", "") or (
        ",".join(str(s) for s in strategies) if strategies else "未提供"
    )
    reason = signal.get("reason", "") or ""
    raw_confidence = signal.get("confidence")
    confidence = raw_confidence if isinstance(raw_confidence, (int, float)) and raw_confidence > 0 else None
    position = signal.get("suggested_position", "")
    position_ratio = float(signal.get("suggested_position_ratio", 0))
    if not position and position_ratio > 0:
        position = f"{position_ratio:.0%}"
    price_raw = signal.get("price")
    price = float(price_raw) if price_raw is not None else 0.0
    wisdom = signal.get("wisdom", {})
    wisdom_decision = signal.get("wisdom_decision", {})
    stop_loss = signal.get("stop_loss_price", 0)
    take_profit = signal.get("take_profit", {})

    # 检测信号完整性：核心字段缺失时标记为"数据异常"而非静默使用默认值
    # 注意：此处检查原始信号 dict 中 key 是否存在，而非 fallback 后的值
    missing_fields = []
    if not symbol:
        missing_fields.append("股票代码")
    if "strategy" not in signal and not signal.get("strategies"):
        missing_fields.append("策略")
    if price <= 0:
        missing_fields.append("价格")
    # 硬校验：A 股正常股价不会 <= 1.0，置信度必须 > 0
    if price <= 1.0:
        missing_fields.append("价格异常(<=1.0)")
    if confidence is None:
        missing_fields.append("置信度缺失")
    data_incomplete = len(missing_fields) > 0

    # 信号强度标签
    strength = ""
    if data_incomplete:
        strength = "⚠️ 数据异常"
    elif signal.get("wisdom_filtered"):
        strength = "⚠️ 危险信号"
    elif wisdom_decision.get("entry_filtered"):
        strength = "⚠️ 已过滤"
    elif signal.get("sector_check", {}).get("is_hot") and signal.get("multi_period", {}).get(
        "weekly_aligned"
    ):
        # A 股惯例：红涨绿跌 — 强买入信号用红色
        strength = "🔴 强信号"
    elif signal.get("weekly_misaligned") or signal.get("sector_check", {}).get("is_cold"):
        strength = "🟡 弱信号"
    else:
        strength = "🔵 中信号" if isinstance(confidence, (int, float)) and confidence >= 0.6 else "🟡 弱信号"

    # entry-filtered signals use yellow card
    if signal.get("wisdom_filtered"):
        template = "yellow"
        action = f"{action}（危险信号）"
    if wisdom_decision.get("entry_filtered"):
        template = "yellow"
        action = f"{action}（已过滤）"

    # ===== 30秒决策摘要行 =====
    if data_incomplete:
        summary_line = f"⚠️ 数据异常: {', '.join(missing_fields)}"
    else:
        summary_parts = [f"{action}信号"]
        conf_text = f"置信度 {confidence:.0%}" if isinstance(confidence, (int, float)) else "置信度 --"
        summary_parts.append(conf_text)
        if position_ratio > 0:
            summary_parts.append(f"建议仓位 {position_ratio:.0%}")
        if stop_loss and stop_loss > 0:
            sl_pct = (price - stop_loss) / price * 100 if price > 0 else 0
            summary_parts.append(f"止损 -{sl_pct:.1f}%")
        if take_profit:
            tp_pct = take_profit.get("target_1_pct", 0)
            if tp_pct:
                summary_parts.append(f"止盈目标 +{tp_pct:.0f}%")
        summary_line = " | ".join(summary_parts)

    title = f"{action}信号 · {name}({symbol})"
    show_price = isinstance(price, (int, float)) and price > 1.0
    if data_incomplete:
        title = f"{strength} {title}"
    elif strength and not wisdom_decision.get("entry_filtered"):
        title = f"{strength} {title}"
    subtitle = f"💰 ￥{price:.2f} | {summary_line}" if show_price else summary_line

    # 数据异常时 section 中显示具体缺失字段
    if data_incomplete:
        sections = [
            f"**股票**：{name}({symbol})\n**方向**：{action}\n**当前价**：{price}\n**信号强度**：{strength}",
            f"**⚠️ 数据异常**：以下字段缺失 — {', '.join(missing_fields)}\n"
            f"**触发策略**：{strategy}\n**建议仓位**：{position or '未提供'}\n**触发理由**：{reason}",
        ]
    else:
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

    # 信号绩效摘要（历史胜率/平均收益，帮助 30 秒决策）
    perf = signal.get("performance_summary")
    if perf:
        perf_lines = [
            f"📈 **同类信号历史表现（{perf.get('sample_count', 0)} 条）**",
            f"胜率：{perf.get('win_rate', 0):.0%}",
            f"5日平均收益：{perf.get('avg_return_5d', 0):.1%}",
        ]
        if perf.get("best_return") is not None:
            perf_lines.append(f"最佳：{perf['best_return']:.1%}")
        if perf.get("worst_return") is not None:
            perf_lines.append(f"最差：{perf['worst_return']:.1%}")
        sections.append("\n".join(perf_lines))

    # 止盈方案
    if take_profit:
        tp_lines = ["🎯 **止盈方案**"]
        if take_profit.get("target_1_pct"):
            tp_lines.append(
                f"第一目标：+{take_profit['target_1_pct']:.0%}（￥{take_profit.get('target_1', 0):.2f}）"
                f" | 建议减仓 {take_profit.get('reduce_at_target1', '50%')}"
            )
        if take_profit.get("target_2_pct"):
            tp_lines.append(
                f"第二目标：+{take_profit['target_2_pct']:.0%}（￥{take_profit.get('target_2', 0):.2f}）"
                f" | 建议清仓"
            )
        if take_profit.get("note"):
            tp_lines.append(f"💡 {take_profit['note']}")
        sections.append("\n".join(tp_lines))

    # 导师视角栏（来自冲突裁决器的多视角建议）
    mentor_text = _format_mentor_perspectives(signal.get("mentor_perspectives", []))
    if mentor_text:
        sections.append(mentor_text)

    return _card(title, template, sections, note="明策 · gugu · 信号通知", subtitle=subtitle)


def _format_mentor_perspectives(perspectives: list[dict[str, Any]]) -> str:
    """格式化导师视角栏（最多2个视角，避免信息过载）。

    Args:
        perspectives: [{perspective, action, reason, weight}, ...]

    Returns:
        格式化的 markdown 文本，或空字符串
    """
    if not perspectives:
        return ""

    # 视角名映射（中文化）
    name_map = {
        "jesse-livermore-perspective": "利弗莫尔",
        "chen-jiang-ting-perspective": "陈江挺",
        "benjamin-graham-perspective": "格雷厄姆",
        "warren-buffett-perspective": "巴菲特",
        "charles-munger-perspective": "芒格",
        "peter-lynch-perspective": "林奇",
        "william-oneil-perspective": "欧奈尔",
        "turtle-trader-perspective": "海龟",
        "van-tharp-perspective": "范·撒普",
        "mark-douglas-perspective": "马克道格拉斯",
        "george-soros-perspective": "索罗斯",
        "nassim-taleb-perspective": "塔勒布",
        "pat-dorsey-perspective": "多尔西",
        "qiu-guo-lu-perspective": "邱国鹭",
        "richard-schabacker-perspective": "沙巴克",
        "steve-nison-perspective": "尼森",
        "ghost-trader-perspective": "幽灵",
        "bernard-baruch-perspective": "巴鲁克",
        "burton-malkiel-perspective": "马尔基尔",
        "zeng-shi-qiang-perspective": "曾仕强",
        "investing-habits-perspective": "投资习惯",
        "k-line-analysis-perspective": "K线分析",
        "technical-analysis-perspective": "技术分析",
        "adam-smith-perspective": "亚当斯密",
        "macroeconomics-perspective": "宏观经济学",
        "microeconomics-perspective": "微观经济学",
        "dashi-xiong-perspective": "大师兄",
        "common-prosperity-perspective": "共同富裕",
    }

    action_map = {"buy": "买入", "sell": "卖出", "hold": "持有", "filter": "过滤"}

    lines = ["📚 **导师视角**"]
    for p in perspectives[:2]:  # 最多2个
        name = name_map.get(p.get("perspective", ""), p.get("perspective", ""))
        action = action_map.get(p.get("action", ""), p.get("action", ""))
        weight = p.get("weight", 0)
        reason = p.get("reason", "")[:60]  # 限60字
        lines.append(f"• **{name}**（权重{weight:.0%}）：{action} — {reason}")

    return "\n".join(lines)


def format_conflict_card(
    symbol: str, name: str, resolution: dict[str, Any]
) -> dict[str, Any]:
    """格式化多视角冲突研讨卡片。

    当冲突裁决器检测到分歧（is_conflict=True）时发送。

    Args:
        symbol: 股票代码
        name: 股票名称
        resolution: Resolution.to_dict() 输出

    Returns:
        Feishu 交互卡片
    """
    name_map = {
        "jesse-livermore-perspective": "利弗莫尔",
        "chen-jiang-ting-perspective": "陈江挺",
        "benjamin-graham-perspective": "格雷厄姆",
        "warren-buffett-perspective": "巴菲特",
        "charles-munger-perspective": "芒格",
        "peter-lynch-perspective": "林奇",
        "turtle-trader-perspective": "海龟",
        "van-tharp-perspective": "范·撒普",
        "george-soros-perspective": "索罗斯",
        "nassim-taleb-perspective": "塔勒布",
    }
    action_map = {"buy": "买入", "sell": "卖出", "hold": "持有", "filter": "过滤"}

    title = f"⚠️ 多视角研讨 · {name}({symbol})"

    # 裁决结果摘要
    final_action = action_map.get(resolution.get("final_action", ""), "未知")
    dispersion = resolution.get("dispersion", 0)
    summary = f"🎯 **加权裁决**：{final_action} | 分散度 {dispersion:.0%} | {'⚠️ 存在分歧' if resolution.get('is_conflict') else '✅ 共识'}"

    # 各视角建议（最多4个，避免过载）
    opinion_lines = ["📋 **各视角建议**"]
    for op in resolution.get("opinions", [])[:4]:
        p_name = name_map.get(op.get("perspective", ""), op.get("perspective", ""))
        op_action = action_map.get(op.get("action", ""), op.get("action", ""))
        weight = resolution.get("weights", {}).get(op.get("perspective", ""), 0)
        reason = op.get("reason", "")[:50]
        opinion_lines.append(f"• **{p_name}**（权重{weight:.0%}）：{op_action} — {reason}")

    # 分歧视角
    dissenting = resolution.get("dissenting_views", [])
    dissent_text = ""
    if dissenting:
        dissent_lines = ["⚖️ **分歧观点**"]
        for d in dissenting[:3]:
            dissent_lines.append(f"• {d}")
        dissent_text = "\n".join(dissent_lines)

    sections = [summary, "\n".join(opinion_lines)]
    if dissent_text:
        sections.append(dissent_text)

    return _card(
        title,
        "yellow",  # 黄色：警告分歧
        sections,
        note="明策 · gugu · 冲突研讨",
        subtitle=summary,
    )


def format_concept_card(
    concept_name: str, definition: str, explanation: str, citations: list[dict[str, str]]
) -> dict[str, Any]:
    """格式化每日概念卡（小白学堂）。

    Args:
        concept_name: 概念名
        definition: 一句话定义
        explanation: 详细解释
        citations: [{book, snippet}, ...]

    Returns:
        Feishu 交互卡片
    """
    title = f"📖 每日概念 · {concept_name}"

    sections = [f"**{concept_name}**\n\n{definition}"]

    if explanation:
        sections.append(f"**详细解释**\n\n{explanation[:500]}")

    if citations:
        cite_lines = ["📚 **出处**"]
        for c in citations[:3]:
            cite_lines.append(f"• 《{c.get('book', '')}》：{c.get('snippet', '')[:60]}")
        sections.append("\n".join(cite_lines))

    return _card(
        title,
        "green",  # 绿色：教育信息
        sections,
        note="明策 · gugu · 小白学堂 | 回复'深入了解'查看更多",
        subtitle=f"💡 今日概念：{concept_name}",
    )


def _format_market_summary(summary: dict[str, Any]) -> str:
    """Format market summary dict into readable markdown text."""
    if not summary:
        return ""
    total_value = summary.get("total_value", 0)
    cash = summary.get("cash", 0)
    positions_count = summary.get("positions_count", 0)
    return f"总资产: ￥{total_value:,.0f} | 现金: ￥{cash:,.0f} | 持仓: {positions_count} 只"


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
        lines.append(f"- {sector}: 主力净流入 ￥{net_val / 1e8:,.2f}亿 ({pct_val:+.2%})")
    return "\n".join(lines) if lines else ""


def _format_portfolio_summary(portfolio: dict[str, Any]) -> str:
    """Format portfolio dict into readable markdown lines."""
    if not portfolio:
        return ""
    lines: list[str] = []
    for sym, info in portfolio.items():
        raw = info.get("name", "") or ""
        name = raw if (raw and raw != sym) else (lookup_stock_name(sym) or raw)
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
        lines.append(f"- {stock_label}: {quantity}股 | 市值 ￥{mv_val:,.0f} | 盈亏 {profit_str}")
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
                stop_lines.append(f"- {sym}: 止损 {stop:.2f} / 最高 {highest:.2f} ({sig})")
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
        f"近 {period_days} 天: {total} 信号, {buy_count} 买, {executed} 已执行, {tracked} 可追踪",
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
    # A 股惯例：红涨绿跌 — 盈利用红色，亏损用绿色
    template = "red" if return_val >= 0 else "green"

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

    # P1 修复：对 error message 做二次脱敏（防御纵深）
    # 主脱敏在 feishu.py notify_error 中，此处作为最后防线
    message = _sanitize_message(message)

    title = f"系统异常 · {module}"

    sections = [
        f"**模块**：{module}\n**异常**：{message}",
        f"**恢复建议**：{suggestion}",
    ]

    return _card(title, "red", sections, note="明策 · gugu · 异常告警")


def _sanitize_message(msg: str) -> str:
    """对错误消息中的敏感凭证做正则脱敏（防御纵深，主脱敏在 feishu.py）。"""
    for pattern in (
        r"(app_secret[\"\'\s]*[:=]\s*[\"\']?)[^\"\'\s,}]+",
        r"(api_key[\"\'\s]*[:=]\s*[\"\']?)[^\"\'\s,}]+",
        r"(api-key[\"\'\s]*[:=]\s*[\"\']?)[^\"\'\s,}]+",
        r"(Bearer\s+)[A-Za-z0-9._\-]{16,}",
        r"(Authorization:\s*Bearer\s+)[^\s]+",
    ):
        msg = re.sub(pattern, r"\1***REDACTED***", msg, flags=re.IGNORECASE)
    return msg


def format_holdings_sell_alert(stocks: list[dict]) -> dict[str, Any]:
    """格式化为持仓卖出告警卡片。

    Args:
        stocks: 卖出信号明确的持仓股列表，每项含 name, symbol, price,
                profit_pct, sell_count, buy_count, sell_signals.
    """
    if not stocks:
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "持仓卖出检查"},
                    "template": "green",
                },
                "elements": [],
            },
        }

    lines = ["**以下持仓股出现卖出信号：**", ""]
    for s in stocks:
        name = s.get("name", s.get("symbol", ""))
        symbol = s.get("symbol", "")
        price = s.get("price", 0)
        profit = s.get("profit_pct", 0)
        sell_n = s.get("sell_count", 0)
        buy_n = s.get("buy_count", 0)
        profit_icon = "🔴" if profit >= 0 else "🟢"
        lines.append(
            f"**{name}** ({symbol})\n"
            f"{profit_icon} 现价 {price:.2f} | 盈亏 {profit:+.1f}%\n"
            f"🟢 卖出 {sell_n} vs 🔴 买入 {buy_n}\n"
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


def format_take_profit_alert(
    symbol: str,
    name: str,
    signal: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Format a take-profit reduce alert into a Feishu card.

    财叔动态止盈减仓策略通知卡片，标注出处和策略逻辑。

    Args:
        symbol: 股票代码。
        name: 股票名称。
        signal: TakeProfitSignal 的 dict 形式，含 action/reason/reduce_ratio/
            trigger_step/current_profit_pct。
        state: 可选的 TakeProfitState dict，用于展示减仓历史。

    Returns:
        Feishu interactive card message dict.
    """
    trigger_step = signal.get("trigger_step", 0)
    profit_pct = signal.get("current_profit_pct", 0)
    reduce_ratio = signal.get("reduce_ratio", 0)
    reason = signal.get("reason", "")
    current_price = signal.get("current_price", 0)
    entry_price = signal.get("entry_price", 0)

    # 步骤映射
    step_names = {
        1: "第1步 · 首次止盈（+30% 减 1/3）",
        3: "第3步 · 阶梯递减（+15% 减 10%）",
        4: "第4步 · 大跌减半保命",
    }
    step_name = step_names.get(trigger_step, f"第{trigger_step}步")
    profit_icon = "🔴" if profit_pct >= 0 else "🟢"

    # 主信息段
    main_lines = [
        f"**{name}** ({symbol})" if name else f"**{symbol}**",
        "",
        f"📈 入场价：￥{entry_price:.2f}",
        f"💰 当前价：￥{current_price:.2f}",
        f"{profit_icon} 累计盈亏：{profit_pct:+.1%}",
        f"📉 建议减仓：{reduce_ratio:.0%}",
        "",
        f"**触发步骤**：{step_name}",
        f"**触发原因**：{reason}",
    ]

    # 策略逻辑段（标注出处）
    strategy_lines = [
        "**📋 财叔动态止盈减仓策略**",
        "",
        "出处：财叔实战经验——「立于不败之地，再让利润奔跑」",
        "",
        "**四步逻辑**：",
        "1️⃣ **+30% 首减 1/3**：锁定利润，立于不败之地",
        "   — 平庸之辈要调整了，减是对的；妖股减了心态更稳拿更久，减也是对的",
        "2️⃣ **判断去留**：无论平庸还是妖股，减仓总是对的",
        "3️⃣ **每 +15% 减 10%**：阶梯递减，持续锁利直到趋势结束",
        "4️⃣ **大跌减半**：单日大跌直接减半，风控保命",
        "",
        "与移动止损互补：止损保本金，止盈锁利润",
    ]

    # 减仓历史段（若有）
    history_lines: list[str] = []
    if state and state.get("history"):
        history_lines.append("**减仓历史**：")
        for h in state["history"][-5:]:  # 最近5次
            step_h = h.get("step", 0)
            date_h = h.get("date", "")
            price_h = h.get("price", 0)
            qty_h = h.get("qty", 0)
            pct_h = h.get("profit_pct", 0)
            history_lines.append(
                f"  {date_h} 第{step_h}步：{qty_h}股@￥{price_h:.2f}（盈亏 {pct_h:+.1%}）"
            )
        total_reduced = state.get("total_reduced", 0)
        history_lines.append(f"  **累计减仓**：{total_reduced} 股")

    sections = ["\n".join(main_lines), "\n".join(strategy_lines)]
    if history_lines:
        sections.append("\n".join(history_lines))

    # 大跌用红色，止盈用黄色
    template = "red" if trigger_step == 4 else "yellow"
    title_prefix = "大跌减半" if trigger_step == 4 else "止盈减仓"

    return _card(
        title=f"💰 {title_prefix} · {name or symbol}",
        template=template,
        sections=sections,
        note="出处：财叔实战经验 · 策略逻辑：动态止盈减仓 · gugu · 止盈告警",
        subtitle=f"当前价 ￥{current_price:.2f} | 盈亏 {profit_pct:+.1%} | 减仓 {reduce_ratio:.0%}",
    )

