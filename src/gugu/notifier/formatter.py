"""Feishu interactive card formatters for gugu notifications.

Color semantics (A 股惯例：红涨绿跌):
- red: buy / profit / up / positive
- green: sell / loss / down / negative
- yellow: warning
- grey: info

Submodules:
- formatter_reports: Daily reports, performance, risk alerts, backtest, validation
- formatter_special: Strategy-specific (cloud, hot money), conflict, concept, holdings, take profit
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from gugu.data.stock_names import lookup_stock_name

# 优先级：signal["name"]（有效时）> stock_names 模块映射 > signal["symbol"]
# _STOCK_NAMES 保留为兼容别名，实际查询通过 lookup_stock_name() 统一管理
_STOCK_NAMES: dict[str, str] = {}


@lru_cache(maxsize=1)
def _strategy_names() -> frozenset[str]:
    """获取已注册的策略名集合（用于识别脏数据：name='money_flow_follow' 等）。"""
    try:
        from gugu.strategies.registry import list_strategies

        return frozenset(list_strategies())
    except Exception:
        return frozenset()


def _resolve_name(signal: dict[str, Any]) -> str:
    """从信号字典解析股票中文名称，支持多层 fallback。"""
    symbol = signal.get("symbol", "")
    name = signal.get("name", "") or ""
    local_name = lookup_stock_name(symbol)
    if local_name:
        return local_name
    if name and name != symbol and name not in _strategy_names():
        return name
    return symbol


def _card(
    title: str,
    template: str,
    sections: list[str],
    note: str = "",
    subtitle: str = "",
) -> dict[str, Any]:
    """Build a Feishu interactive card message."""
    elements: list[dict[str, Any]] = []
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
        if len(text) > 400:
            text = text[:400] + "..."
        lines.append(f"- **{label}**：{text}")
    return "\n".join(lines) if lines else ""


def _format_wisdom_decision(decision: Any) -> str:
    """Format wisdom decision dict into readable markdown lines."""
    if not decision or not isinstance(decision, dict):
        return ""
    lines: list[str] = []
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
    """Format decision_chain into compact markdown lines."""
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


def _format_capability_line(signal: dict) -> str:
    """格式化能力圈标注（内部包装，无标注返回空串）。"""
    try:
        from gugu.strategies.capability import format_capability_line as _fmt

        return _fmt(signal)
    except Exception:
        return ""


def format_cxbr_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """格式化 CXBR 研究/模拟盘事件；observe 永远显示为观察而非买入。"""
    action = str(signal.get("action", "none"))
    stage = str(signal.get("stage", ""))
    labels = {
        "observe": "次日关注 / 观察",
        "buy": "买入条件成立（模拟盘）",
        "sell": "卖出条件成立（模拟盘）",
        "none": "CXBR 状态更新",
    }
    templates = {"buy": "red", "sell": "green", "observe": "yellow", "none": "grey"}
    code = str(signal.get("code", signal.get("symbol", "")))
    name = _resolve_name({"symbol": code, "name": signal.get("name", "")})
    price = signal.get("price")
    price_text = f"￥{float(price):.2f}" if price is not None else "未提供"
    sections = [
        f"**股票**：{name}({code})\n**状态**：{labels.get(action, action)}\n**阶段**：{stage}",
        f"**数据截止**：{signal.get('asof') or '未提供'}\n"
        f"**策略**：{signal.get('strategy_name', 'cxbr_v1')}\n"
        f"**规则版本**：{signal.get('rule_version', 'cxbr-v1')}\n"
        f"**入场假设**：{signal.get('entry_assumption', 'next_session_open')}\n"
        f"**数据质量**：{signal.get('data_quality', 'unknown')}\n"
        f"**参考价格**：{price_text}",
        f"**触发理由**：{signal.get('reason', '')}\n"
        f"**买点**：{signal.get('buy_point') or '—'}\n"
        f"**严格止损**：{_price_text(signal.get('strict_stop_price'))}\n"
        f"**条件单提醒**：{_price_text(signal.get('conditional_stop_price'))}\n"
        f"**建议仓位**：{_ratio_text(signal.get('suggested_position'))}\n"
        f"**失效条件**：{signal.get('invalidation', '数据失效、形态破坏或超过护盘窗口')}",
    ]
    if signal.get("execution"):
        execution = signal["execution"]
        sections.append(
            f"**模拟结果**：{'成交' if execution.get('success') else '未成交'}\n"
            f"**成交价/数量**：￥{execution.get('fill_price', 0):.2f} / {execution.get('quantity', 0)}股\n"
            f"**计划数量/部分成交**：{execution.get('requested_quantity', 0)} / "
            f"{'是' if execution.get('partial_fill') else '否'}\n"
            f"**滑点**：{float(execution.get('slippage', 0)):.3%}\n"
            f"**备注**：{execution.get('message', '')}"
        )
    return _card(
        f"{labels.get(action, action)} · {name}({code})",
        templates.get(action, "grey"),
        sections,
        note="CXBR V1 研究/模拟盘通知；不代表实盘成交，不触发自动下单",
    )


def _price_text(value: Any) -> str:
    return f"￥{float(value):.2f}" if value is not None else "—"


def _ratio_text(value: Any) -> str:
    return f"{float(value):.0%}" if value is not None else "—"


def estimate_max_loss(signal: dict[str, Any]) -> str:
    """Estimate planned stop-loss loss conservatively for a long buy signal."""
    if str(signal.get("direction", "buy")).lower() != "buy":
        return ""
    try:
        order_result = signal.get("order_result") or {}
        fill_price = float(order_result.get("price", 0) or 0)
        entry = (
            fill_price
            if order_result.get("success") and fill_price > 0
            else float(signal.get("price"))
        )
        stop = float(signal.get("stop_loss_price"))
        if (
            not math.isfinite(entry)
            or not math.isfinite(stop)
            or entry <= 0
            or stop <= 0
            or stop >= entry
        ):
            return "⚠️ 暂无法估算最大亏损：缺少有效入场价或止损价"

        quantity = order_result.get("quantity") if order_result.get("success") else None
        if quantity is None:
            quantity = signal.get("quantity")
        if quantity is None:
            capital = float(signal.get("total_value"))
            ratio = float(signal.get("suggested_position_ratio"))
            if capital <= 0 or ratio <= 0 or ratio > 1:
                raise ValueError
            quantity = int(capital * ratio / entry / 100) * 100
        quantity_value = float(quantity)
        if (
            not math.isfinite(quantity_value)
            or quantity_value <= 0
            or not quantity_value.is_integer()
        ):
            raise ValueError
        quantity = int(quantity_value)
        if quantity % 100 != 0:
            return "⚠️ 暂无法估算最大亏损：计划数量不是 A 股有效整手（100 股）"

        loss = (entry - stop) * quantity
        if not math.isfinite(loss) or loss <= 0:
            raise ValueError
        account_ratio = None
        total_value = signal.get("total_value")
        if total_value is not None and math.isfinite(float(total_value)) and float(total_value) > 0:
            account_ratio = loss / float(total_value)
        account_text = f"（约占账户 {account_ratio:.2%}）" if account_ratio is not None else ""
        return (
            f"⚠️ **按计划 {quantity} 股、入场 ￥{entry:.2f}、止损 ￥{stop:.2f}："
            f"触发止损约亏 ￥{loss:,.2f}{account_text}**\n"
            "不含跳空、跌停无法成交、滑点和手续费，实际损失可能更高。"
        )
    except (TypeError, ValueError, OverflowError):
        return "⚠️ 暂无法估算最大亏损：缺少有效止损、数量或账户规模"


def _validation_status_label(signal: dict[str, Any]) -> str:
    """Return the most conservative validation label available on a signal."""
    capability = signal.get("capability") or {}
    status = str(capability.get("validation_status") or signal.get("validation_status") or "")
    labels = {
        "verified": "✅ 已验证",
        "conditional": "🟡 条件通过",
        "experimental": "🧪 未验证·仅供观察",
        "in_review": "🔍 审查中·仅供观察",
    }
    return labels.get(status, "🔍 验证状态未知·仅供观察")


def format_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Format a trade signal into a Feishu card."""
    direction = str(signal.get("direction", "buy")).lower()
    decision = signal.get("decision", {}) or {}
    decision_blocked = isinstance(decision, dict) and decision.get("status") == "blocked"
    is_buy = direction == "buy"
    template = "red" if is_buy else "green"
    action = "买入" if is_buy else "卖出"
    if decision_blocked:
        template = "yellow"
        action = "观察（不可交易）"

    symbol = signal.get("symbol", "")
    name = _resolve_name(signal)
    strategies = signal.get("strategies", [])
    strategy = signal.get("strategy", "") or (
        ",".join(str(s) for s in strategies) if strategies else "未提供"
    )
    reason = signal.get("reason", "") or ""
    raw_confidence = signal.get("confidence")
    confidence = (
        raw_confidence if isinstance(raw_confidence, (int, float)) and raw_confidence > 0 else None
    )
    position = signal.get("suggested_position", "")
    position_ratio = float(signal.get("suggested_position_ratio", 0))
    if decision_blocked:
        position = "不可交易"
        position_ratio = 0.0
    if not position and position_ratio > 0:
        position = f"{position_ratio:.0%}"
    price_raw = signal.get("price")
    price = float(price_raw) if price_raw is not None else 0.0
    wisdom = signal.get("wisdom", {})
    wisdom_decision = signal.get("wisdom_decision", {})
    stop_loss = signal.get("stop_loss_price", 0)
    take_profit = signal.get("take_profit", {})

    missing_fields = []
    if not symbol:
        missing_fields.append("股票代码")
    if "strategy" not in signal and not signal.get("strategies"):
        missing_fields.append("策略")
    if price <= 0:
        missing_fields.append("价格")
    if price <= 1.0:
        missing_fields.append("价格异常(<=1.0)")
    if confidence is None:
        missing_fields.append("置信度缺失")
    data_incomplete = len(missing_fields) > 0

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
        strength = "🔴 强信号"
    elif signal.get("weekly_misaligned") or signal.get("sector_check", {}).get("is_cold"):
        strength = "🟡 弱信号"
    else:
        strength = (
            "🔵 中信号"
            if isinstance(confidence, (int, float)) and confidence >= 0.6
            else "🟡 弱信号"
        )

    if signal.get("wisdom_filtered") and not decision_blocked:
        template = "yellow"
        action = f"{action}（危险信号）"
    if wisdom_decision.get("entry_filtered") and not decision_blocked:
        template = "yellow"
        action = f"{action}（已过滤）"

    if data_incomplete:
        summary_line = f"⚠️ 数据异常: {', '.join(missing_fields)}"
    else:
        summary_parts = [f"{action}信号"]
        conf_text = (
            f"置信度 {confidence:.0%}" if isinstance(confidence, (int, float)) else "置信度 --"
        )
        summary_parts.append(conf_text)
        if position_ratio > 0:
            summary_parts.append(f"建议仓位 {position_ratio:.0%}")
        if stop_loss and stop_loss > 0:
            sl_pct = (price - stop_loss) / price * 100 if price > 0 else 0
            summary_parts.append(f"止损 -{sl_pct:.1f}%")
        if take_profit:
            tp_pct = take_profit.get("target_1_pct", 0)
            if tp_pct:
                summary_parts.append(f"止盈目标 +{tp_pct:.0%}")
        summary_line = " | ".join(summary_parts)

    title = f"{action}信号 · {name}({symbol})"
    show_price = isinstance(price, (int, float)) and price > 1.0
    price_note = signal.get("price_note", "")
    price_display = f"￥{price:.2f}" + (" ⚠️非实时" if price_note else "")
    if data_incomplete or (strength and not wisdom_decision.get("entry_filtered")):
        title = f"{strength} {title}"
    subtitle = f"💰 {price_display} | {summary_line}" if show_price else summary_line

    price_note_line = f"\n**⚠️ 价格提示**：{price_note}" if price_note else ""
    if data_incomplete:
        sections = [
            f"**股票**：{name}({symbol})\n**方向**：{action}\n**当前价**：{price_display}\n**信号强度**：{strength}{price_note_line}",
            f"**⚠️ 数据异常**：以下字段缺失 — {', '.join(missing_fields)}\n"
            f"**触发策略**：{strategy}\n**建议仓位**：{position or '未提供'}\n**触发理由**：{reason}",
        ]
    else:
        sections = [
            f"**股票**：{name}({symbol})\n**方向**：{action}\n**当前价**：{price_display}\n**信号强度**：{strength}{price_note_line}",
            f"**触发策略**：{strategy}\n**建议仓位**：{position or '未提供'}\n**触发理由**：{reason}",
        ]

    # P1/P2：数据质量戳 + 共振等级 + 能力圈标注（三大师共识：可靠性和自我认知）
    quality_badges = []
    dq = signal.get("data_quality")
    if dq in {"fresh", "fresh_valid"}:
        quality_badges.append("🟢 实时数据")
    elif dq in {"degraded", "stale_degraded"}:
        quality_badges.append("🟡 数据降级")
    elif dq == "stale":
        quality_badges.append("🔴 非实时价（数据不可用或非实时）")
    elif dq == "unavailable":
        quality_badges.append("🔴 数据不可用")
    res_lvl = signal.get("resonance_level")
    if res_lvl == "strong":
        quality_badges.append("🔵 多策略共振")
    elif res_lvl == "single":
        quality_badges.append("⚪ 单策略触发")
    if signal.get("signal_conflict"):
        quality_badges.append("🟠 买卖矛盾")
    if signal.get("observation_only"):
        quality_badges.append("👀 仅供观察")
    if quality_badges:
        sections.append("**信号质量**：" + " | ".join(quality_badges))

    dq_note = signal.get("data_quality_note")
    if dq_note:
        sections.append(f"⚠️ {dq_note}")

    cap_line = _format_capability_line(signal)
    if cap_line:
        sections.append(f"**能力圈**：{cap_line}")
    if is_buy:
        sections.append(f"**验证等级**：{_validation_status_label(signal)}")
        sections.append(estimate_max_loss(signal))

    conflict = signal.get("conflict_detail")
    if conflict:
        sections.append(
            "**⚠️ 矛盾信号**\n"
            f"买入方：{', '.join(conflict.get('buy', [])) or '无'}\n"
            f"卖出方：{', '.join(conflict.get('sell', [])) or '无'}\n"
            "建议观望，等待方向明确"
        )

    sl_tp_lines = []
    if stop_loss and stop_loss > 0:
        sl_pct = (price - stop_loss) / price * 100 if price > 0 else 0
        sl_tp_lines.append(f"🛑 止损 ￥{stop_loss:.2f}（-{sl_pct:.1f}%）")
    if take_profit:
        tp_pct = take_profit.get("target_1_pct", 0)
        if tp_pct:
            sl_tp_lines.append(f"🎯 止盈 +{tp_pct:.0%}（￥{take_profit.get('target_1', 0):.2f}）")
    if sl_tp_lines:
        sections.append("**止损止盈**\n" + "\n".join(sl_tp_lines))

    perf = signal.get("performance_summary")
    if perf:
        sample_count = perf.get("sample_count", perf.get("total_signals", 0))
        perf_lines = [
            f"📈 **同类信号历史表现（n={sample_count}）**",
            f"胜率：{perf.get('win_rate', 0):.0%}",
            f"5日平均收益：{perf.get('avg_return_5d', 0):.1%}",
        ]
        if perf.get("best_return") is not None:
            perf_lines.append(f"最佳：{perf['best_return']:.1%}")
        if perf.get("worst_return") is not None:
            perf_lines.append(f"最差：{perf['worst_return']:.1%}")
        sections.append("\n".join(perf_lines))

    decision_chain_text = _format_decision_chain(signal.get("decision_chain", []))
    if decision_chain_text:
        sections.append(f"**决策链路**\n{decision_chain_text}")

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

    hc = signal.get("holder_concentration")
    if hc:
        hc_label = hc.get("label", "")
        hc_icon = "🟢" if hc_label == "潜伏者" else "🔴" if hc_label == "避雷针" else "⚪"
        hc_holder_num_pct = hc.get("holder_num_pct", 0) or 0
        hc_avg_hold_change = hc.get("avg_hold_change", 0) or 0
        hc_end_date = hc.get("end_date", "") or "--"
        hc_reasons = hc.get("reasons", []) or []
        hc_reasons_text = "；".join(hc_reasons) if hc_reasons else "无"
        sections.append(
            f"**筹码结构**\n"
            f"{hc_icon} 筹码结构：{hc_label}\n"
            f"• 户数变化率：{hc_holder_num_pct:+.2f}%\n"
            f"• 户均持股变化：{hc_avg_hold_change:+.2f}%\n"
            f"• 报告期：{hc_end_date}\n"
            f"• 说明：{hc_reasons_text}"
        )

    decision_text = _format_wisdom_decision(wisdom_decision)
    if decision_blocked:
        reasons = decision.get("block_reasons", [])
        reason_text = "；".join(str(reason) for reason in reasons) or signal.get(
            "filter_reason", "门禁未通过"
        )
        sections.append(f"**决策门禁**\n- ⛔ **观察，不可交易**：{reason_text}")
    elif decision_text:
        sections.append(f"**智慧决策**\n{decision_text}")

    wisdom_text = _format_wisdom(wisdom)
    if wisdom_text:
        sections.append(f"**交易智慧参考**\n{wisdom_text}")

    if take_profit and not sl_tp_lines:
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

    mentor_text = _format_mentor_perspectives(signal.get("mentor_perspectives", []))
    if mentor_text:
        sections.append(mentor_text)

    # v0.7.8 数据时效性：在卡片底部标注数据来源和复权方式
    data_source = signal.get("data_source", "")
    adjust_type = signal.get("adjust_type", "")

    if data_source or adjust_type:
        data_labels = []
        if data_source:
            source_map = {"sina": "新浪", "akshare": "akshare", "eastmoney": "东方财富"}
            data_labels.append(f"数据源: {source_map.get(data_source, data_source)}")
        if adjust_type:
            adjust_map = {"none": "不复权", "qfq": "前复权", "hfq": "后复权"}
            data_labels.append(f"复权: {adjust_map.get(adjust_type, adjust_type)}")
        if data_labels:
            sections.append("---\n📊 **数据说明**: " + " | ".join(data_labels))

    if (
        signal.get("llm_pending") is False
        and signal.get("wisdom_decision", {}).get("source") != "llm"
    ):
        sections.append(
            "---\n⚠️ **仅通知模式**：此信号仅供参考，不构成交易建议。"
            "系统不会自动执行任何交易操作，请自行判断决策。"
        )

    return _card(title, template, sections, note="明策 · gugu · 信号通知", subtitle=subtitle)


def _format_mentor_perspectives(perspectives: list[dict[str, Any]]) -> str:
    """格式化导师视角栏（最多2个视角，避免信息过载）。"""
    if not perspectives:
        return ""

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
        "qiu-guo-l-perspective": "邱国鹭",
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
    for p in perspectives[:2]:
        name = name_map.get(p.get("perspective", ""), p.get("perspective", ""))
        action = action_map.get(p.get("action", ""), p.get("action", ""))
        weight = p.get("weight", 0)
        reason = p.get("reason", "")[:60]
        lines.append(f"• **{name}**（权重{weight:.0%}）：{action} — {reason}")

    return "\n".join(lines)


# Re-export from submodules for backward compatibility
from gugu.notifier.formatter_reports import (  # noqa: E402
    build_signal_daily_validation_card,
    build_signal_performance_card,
    build_signal_weekly_card,
    format_backtest_report,
    format_daily_report,
    format_performance,
    format_risk_alert,
    format_system_error,
    format_validation_report,
)
from gugu.notifier.formatter_special import (  # noqa: E402
    _sanitize_message,
    format_cloud_trend_signal,
    format_concept_card,
    format_conflict_card,
    format_holdings_sell_alert,
    format_hot_money_signal,
    format_take_profit_alert,
)

__all__ = [
    "_card",
    "_format_decision_chain",
    "_format_mentor_perspectives",
    "_format_signals",
    "_format_wisdom",
    "_format_wisdom_decision",
    "_resolve_name",
    "_sanitize_message",
    "_strategy_names",
    "build_signal_daily_validation_card",
    "build_signal_performance_card",
    "build_signal_weekly_card",
    "format_backtest_report",
    "format_cloud_trend_signal",
    "format_concept_card",
    "format_conflict_card",
    "format_daily_report",
    "format_holdings_sell_alert",
    "format_hot_money_signal",
    "format_performance",
    "format_risk_alert",
    "format_signal",
    "format_system_error",
    "format_take_profit_alert",
    "format_validation_report",
]
