"""Strategy-specific formatters (cloud trend, hot money) and special cards."""

from __future__ import annotations

import re
from typing import Any

from gugu.notifier.formatter import (
    _card,
    _resolve_name,
    _validation_status_label,
    estimate_max_loss,
)


def format_cloud_trend_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """格式化云策略专属信号卡片。"""
    direction = str(signal.get("direction", "buy")).lower()
    is_buy = direction == "buy"
    template = "red" if is_buy else "green"
    action = "买入" if is_buy else "卖出"

    symbol = signal.get("symbol", "")
    name = _resolve_name(signal)
    price = float(signal.get("price", 0))
    confidence = signal.get("confidence", 0)
    position_ratio = float(signal.get("suggested_position_ratio", 0))
    reason = signal.get("reason", "")
    stop_loss = signal.get("stop_loss_price", 0)

    market_context = signal.get("market_context", {})
    dim1 = market_context.get("dim1_ma_bull", False)
    dim2 = market_context.get("dim2_hl_rising", False)
    dim3 = market_context.get("dim3_vol_healthy", False)
    dims_count = market_context.get("trend_dims_count", 0)
    trend_days = market_context.get("trend_all_3_days", 0)
    entry_type = market_context.get("entry_type", "")
    is_double_yin_reversal = market_context.get("double_yin_reversal", False)

    if trend_days <= 0:
        stage = "无趋势"
        stage_emoji = "⚪"
    elif trend_days <= 5:
        stage = "趋势初期"
        stage_emoji = "🟡"
    elif trend_days <= 20:
        stage = "趋势确认"
        stage_emoji = "🟢"
    else:
        stage = "趋势成熟"
        stage_emoji = "🔵"

    entry_label = {"pullback": "回踩低吸", "breakout": "放量突破"}.get(
        entry_type, entry_type or "—"
    )

    dim1_status = "✅" if dim1 else "❌"
    dim2_status = "✅" if dim2 else "❌"
    dim3_status = "✅" if dim3 else "❌"

    title = f"☁️ 云策略 · {action}信号 · {name}({symbol})"
    price_display = f"￥{price:.2f}" if price > 1.0 else f"￥{price:.4f}"

    summary_parts = [f"{action}信号"]
    if isinstance(confidence, (int, float)) and confidence > 0:
        summary_parts.append(f"置信度 {confidence:.0%}")
    if position_ratio > 0:
        summary_parts.append(f"仓位 {position_ratio:.0%}")
    if stop_loss and stop_loss > 0 and price > 0:
        sl_pct = (price - stop_loss) / price * 100
        summary_parts.append(f"止损 -{sl_pct:.1f}%")
    summary_line = " | ".join(summary_parts)

    subtitle = f"💰 {price_display} | {summary_line}"

    sections = [
        f"**验证等级**：{_validation_status_label(signal)}\n"
        f"**股票**：{name}({symbol})\n"
        f"**方向**：{action}\n"
        f"**当前价**：{price_display}\n"
        f"**入场类型**：{entry_label}",
        f"**三维趋势判定**（{dims_count}/3 满足）\n"
        f"{dim1_status} 均线多头：MA20斜率向上\n"
        f"{dim2_status} 高低点抬升：HH/LL 逐步抬高\n"
        f"{dim3_status} 量价配合：阳量 > 阴量",
        f"**趋势阶段**：{stage_emoji} {stage}（连续 {trend_days} 日）\n"
        f"**建议仓位**：{position_ratio:.0%}\n"
        f"**触发理由**：{reason}",
    ]

    if is_double_yin_reversal:
        sections.insert(2, "🔥 **双阴反转加强**：前两日连续阴线 + 今日企稳收阳")

    if is_buy:
        sections.append(estimate_max_loss(signal))

    if stop_loss and stop_loss > 0 and price > 0:
        sl_pct = (price - stop_loss) / price * 100
        sections.append(
            f"🛑 **止损规则**\n"
            f"• 止损价：￥{stop_loss:.2f}（-{sl_pct:.1f}%）\n"
            f"• 单笔亏损上限：总资金 2%\n"
            f"• 跌破 MA20 清仓 | 绝不扛单"
        )

    sections.append(
        "📊 **分层仓位逻辑**\n"
        "• 趋势初期（≤5日）：40%\n"
        "• 趋势确认（>5日）：60-70%\n"
        "• 趋势末期（维度弱化）：20%\n"
        "• 禁止梭哈逆势补仓"
    )

    note = "☁️ 云策略 · 三维趋势共振 · 独立推送"

    return _card(title=title, template=template, sections=sections, note=note, subtitle=subtitle)


def format_hot_money_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """格式化游资策略（三数交易法）专属信号卡片。"""
    direction = str(signal.get("direction", "buy")).lower()
    is_buy = direction == "buy"
    template = "red" if is_buy else "green"
    action = "买入" if is_buy else "卖出"

    symbol = signal.get("symbol", "")
    name = _resolve_name(signal)
    price = float(signal.get("price", 0))
    confidence = signal.get("confidence", 0)
    position_ratio = float(signal.get("suggested_position_ratio", 0))
    reason = signal.get("reason", "")

    market_context = signal.get("market_context", {})
    is_shrink_entry = market_context.get("hm_is_shrink_entry", False)
    is_expand_exit = market_context.get("hm_is_expand_exit", False)
    is_rolling = market_context.get("hm_is_rolling", False)
    days_since_lu = market_context.get("hm_days_since_limit_up", 0)
    ref_volume = market_context.get("hm_ref_volume", 0)
    current_volume = market_context.get("hm_current_volume", 0)

    if is_rolling:
        signal_type = "🔄 半仓滚动"
        signal_desc = "阴线买入（滚动加仓）" if is_buy else "阳线卖出（滚动减仓）"
    elif is_shrink_entry:
        signal_type = "📉 缩量入场"
        signal_desc = "量能缩至涨停日一半以下，筹码锁定"
    elif is_expand_exit:
        signal_type = "📈 放量离场"
        signal_desc = "量能放大至涨停日两倍以上，主力出货"
    else:
        signal_type = "📊 三数信号"
        signal_desc = reason

    vol_ratio_str = "—"
    if ref_volume > 0 and current_volume > 0:
        ratio = current_volume / ref_volume
        vol_ratio_str = (
            f"{ratio:.1%}（涨停日 {ref_volume / 1e8:.2f}亿 → 今日 {current_volume / 1e8:.2f}亿）"
        )

    title = f"🔥 游资策略 · {action}信号 · {name}({symbol})"
    price_display = f"￥{price:.2f}" if price > 1.0 else f"￥{price:.4f}"

    summary_parts = [f"{action}信号"]
    if isinstance(confidence, (int, float)) and confidence > 0:
        summary_parts.append(f"置信度 {confidence:.0%}")
    if position_ratio > 0:
        summary_parts.append(f"仓位 {position_ratio:.0%}")
    summary_line = " | ".join(summary_parts)

    subtitle = f"💰 {price_display} | {summary_line}"

    sections = [
        f"**验证等级**：{_validation_status_label(signal)}\n"
        f"**股票**：{name}({symbol})\n"
        f"**方向**：{action}\n"
        f"**当前价**：{price_display}\n"
        f"**信号类型**：{signal_type}",
        f"**{signal_type}**\n"
        f"{signal_desc}\n"
        f"**量能对比**：{vol_ratio_str}\n"
        f"**涨停距今**：{days_since_lu} 天",
        f"**建议仓位**：{position_ratio:.0%}\n**触发理由**：{reason}",
    ]

    if is_buy:
        sections.append(estimate_max_loss(signal))

    sections.append(
        "📐 **三数交易法**\n"
        "① 找7天内涨停 → 主力进场信号\n"
        "② 进场找缩倍量 → 筹码锁定，洗盘结束\n"
        "③ 离场找放倍量 → 主力出货，风险增加"
    )

    if is_rolling:
        sections.append(
            "🔄 **半仓滚动策略**\n"
            "• 底仓不动，吃趋势主升浪\n"
            "• 滚动仓做T，阴买阳卖增厚收益\n"
            "• 鱼和熊掌兼得"
        )

    note = "🔥 游资策略 · 三数交易法 · 独立推送"

    return _card(title=title, template=template, sections=sections, note=note, subtitle=subtitle)


def format_conflict_card(symbol: str, name: str, resolution: dict[str, Any]) -> dict[str, Any]:
    """格式化多视角冲突研讨卡片。"""
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

    final_action = action_map.get(resolution.get("final_action", ""), "未知")
    dispersion = resolution.get("dispersion", 0)
    summary = f"🎯 **加权裁决**：{final_action} | 分散度 {dispersion:.0%} | {'⚠️ 存在分歧' if resolution.get('is_conflict') else '✅ 共识'}"

    opinion_lines = ["📋 **各视角建议**"]
    for op in resolution.get("opinions", [])[:4]:
        p_name = name_map.get(op.get("perspective", ""), op.get("perspective", ""))
        op_action = action_map.get(op.get("action", ""), op.get("action", ""))
        weight = resolution.get("weights", {}).get(op.get("perspective", ""), 0)
        reason = op.get("reason", "")[:50]
        opinion_lines.append(f"• **{p_name}**（权重{weight:.0%}）：{op_action} — {reason}")

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
        "yellow",
        sections,
        note="明策 · gugu · 冲突研讨",
        subtitle=summary,
    )


def format_concept_card(
    concept_name: str, definition: str, explanation: str, citations: list[dict[str, str]]
) -> dict[str, Any]:
    """格式化每日概念卡（小白学堂）。"""
    title = f"📖 每日概念 · {concept_name}"

    sections = [f"**{concept_name}**\n\n{definition}"]

    if explanation:
        sections.append(f"**详细解释**\n\n{explanation[:500]}")

    if citations:
        cite_lines = ["📚 **出处**"]
        for c in citations[:3]:
            if isinstance(c, dict):
                book = c.get("book", "")
                snippet = c.get("snippet", "")
            else:
                book = getattr(c, "book", "")
                snippet = getattr(c, "snippet", "")
            cite_lines.append(f"• 《{book}》：{snippet[:60]}")
        sections.append("\n".join(cite_lines))

    return _card(
        title,
        "green",
        sections,
        note="明策 · gugu · 小白学堂 | 回复'深入了解'查看更多",
        subtitle=f"💡 今日概念：{concept_name}",
    )


def format_holdings_sell_alert(stocks: list[dict]) -> dict[str, Any]:
    """格式化为持仓卖出告警卡片。"""
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
        template="green",
        sections=["\n".join(lines)],
        note="明策 · gugu · 持仓告警",
    )


def format_take_profit_alert(
    symbol: str,
    name: str,
    signal: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Format a take-profit reduce alert into a Feishu card."""
    trigger_step = signal.get("trigger_step", 0)
    profit_pct = signal.get("current_profit_pct", 0)
    reduce_ratio = signal.get("reduce_ratio", 0)
    reason = signal.get("reason", "")
    current_price = signal.get("current_price", 0)
    entry_price = signal.get("entry_price", 0)

    step_names = {
        1: "第1步 · 首次止盈（+30% 减 1/3）",
        3: "第3步 · 阶梯递减（+15% 减 10%）",
        4: "第4步 · 大跌减半保命",
    }
    step_name = step_names.get(trigger_step, f"第{trigger_step}步")
    profit_icon = "🔴" if profit_pct >= 0 else "🟢"

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

    history_lines: list[str] = []
    if state and state.get("history"):
        history_lines.append("**减仓历史**：")
        for h in state["history"][-5:]:
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

    template = "green" if trigger_step == 4 else "red"
    title_prefix = "大跌减半" if trigger_step == 4 else "止盈减仓"

    return _card(
        title=f"💰 {title_prefix} · {name or symbol}",
        template=template,
        sections=sections,
        note="出处：财叔实战经验 · 策略逻辑：动态止盈减仓 · gugu · 止盈告警",
        subtitle=f"当前价 ￥{current_price:.2f} | 盈亏 {profit_pct:+.1%} | 减仓 {reduce_ratio:.0%}",
    )


def _sanitize_message(msg: str) -> str:
    """对错误消息中的敏感凭证做正则脱敏（防御纵深，主脱敏在 feishu.py）。"""
    for pattern in (
        r"(app_secret[\"\'\s]*[:=]\s*[\"\']?)[^\"'\s,}]+",
        r"(api_key[\"\'\s]*[:=]\s*[\"\']?)[^\"'\s,}]+",
        r"(api-key[\"\'\s]*[:=]\s*[\"\']?)[^\"'\s,}]+",
        r"(Bearer\s+)[A-Za-z0-9._\-]{16,}",
        r"(Authorization:\s*Bearer\s+)[^\s]+",
    ):
        msg = re.sub(pattern, r"\1***REDACTED***", msg, flags=re.IGNORECASE)
    return msg
