"""资金流日报 — 格式化/卡片构建函数。

从 flow_report.py 提取的所有格式化与卡片构建逻辑，
集中到本模块以便复用和维护。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# ── 工具函数 ──────────────────────────────────────────────────


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── 格式化函数 ──────────────────────────────────────────────────


def _fmt(val: Any, suffix: str = "") -> str:
    """格式化数值，None 显示 '--'。"""
    if val is None:
        return "--"
    try:
        f = float(val)
        return f"{f:+.2f}{suffix}" if f != 0 else f"0.00{suffix}"
    except (ValueError, TypeError):
        return str(val)


def _fmt_flow(val: Any) -> str:
    """格式化资金流金额（亿），带颜色（A 股惯例：流入红，流出绿）。"""
    if val is None:
        return "获取不到"
    try:
        f = float(val)
        if f > 0:
            return f"🔴 +{f:.1f}亿"
        elif f < 0:
            return f"🟢 {f:.1f}亿"
        return "⚪ 0.0亿"
    except (ValueError, TypeError):
        return str(val)


def _rotation_label(score: float) -> str:
    """轮动强度文字标签。"""
    if score >= 5:
        return "🟠 轮动极强"
    elif score >= 3:
        return "🟡 轮动较强"
    elif score >= 1.5:
        return "🔵 轮动正常"
    elif score > 0:
        return "🟢 轮动偏弱"
    return "⚪ 市场普涨"


def _format_key_industries_section(key_industries: list[dict[str, Any]]) -> str | None:
    """格式化重点行业资金流段落。"""
    if not key_industries:
        return None
    lines = ["**🏥 重点行业资金流**"]
    for ind in key_industries:
        category = ind.get("category", "")
        name = ind.get("name", "")
        inflow = ind.get("main_inflow")
        pct = ind.get("price_change")
        if name == "暂无数据" or inflow is None:
            lines.append(f"• {category}: 暂无数据")
        else:
            f_main = _fmt_flow(inflow)
            pct_str = _fmt(pct, "%") if pct is not None else "--"
            lines.append(f"• {category}({name}) {f_main} ({pct_str})")
    return "\n".join(lines)


# ── 卡片构建函数 ──────────────────────────────────────────────────


def build_morning_card(data: dict[str, Any]) -> dict[str, Any]:
    """构建 08:00 盘前 — 昨日(T-1)资金流复盘卡片。"""
    from gugu.notifier.formatter import _card

    # 数据源全链路不可用 → 发红色告警卡片（非灰色，确保用户注意）
    if data.get("sources_unavailable"):
        return _card(
            "⚠️ 资金流复盘（数据源不可用）",
            "red",
            [
                "**🚨 资金流数据源全链路不可用**",
                "",
                "东方财富资金流 API 当前不可用，HTTP 降级通道也无法获取数据。",
                "今日所有资金流相关推送（行业TOP/背离预警/强流入个股）将为空。",
                "",
                "**⚙ 建议排查**：",
                "1. 检查服务器网络环境是否变更",
                "2. 确认 akshare/东方财富服务状态",
                "3. 数据源恢复后系统会自动重试",
                "",
                "---",
                "💡 此告警每日仅首次触发时推送，后续推送将静默跳过。",
            ],
            note="明策 · gugu · 资金流数据源告警 | 需人工排查",
        )

    market = data.get("market", {})
    north = data.get("north_bound", {})
    overview = data.get("market_overview", {})
    sectors = data.get("sectors", {})
    key_industries = data.get("key_industries", [])
    concepts = data.get("concepts", [])
    strong_stocks = data.get("strong_stocks", [])

    sections: list[str] = []

    # ── 大盘概览 ──
    if market:
        m_date = market.get("date", "")
        north_only = market.get("north_only", False)
        lines = ["**大盘资金流**"]
        if north_only:
            nb = north or {}
            lines.append(f"📅 {m_date}")
            lines.append("📊 **北向资金代理**（东方财富 API 暂不可用）")
            n_total = _fmt_flow(nb.get("total"))
            n_sh = _fmt_flow(nb.get("sh_connect"))
            n_sz = _fmt_flow(nb.get("sz_connect"))
            lines.append(f"  北向总额: {n_total}")
            lines.append(f"  沪股通: {n_sh}")
            lines.append(f"  深股通: {n_sz}")
            if overview.get("main_vs_retail"):
                lines.append(f"  博弈: {overview['main_vs_retail']}")
        else:
            sh_pct = _fmt(market.get("sh_change"), "%")
            sz_pct = _fmt(market.get("sz_change"), "%")
            main_flow = _fmt_flow(market.get("main_net_inflow"))
            lines.append(f"📅 {m_date}")
            lines.append(f"上证 {sh_pct} | 深证 {sz_pct}")
            lines.append(f"主力净流入: {main_flow}")
            if north:
                lines.append(f"北向资金: {_fmt_flow(north.get('total'))}")
            if overview.get("main_vs_retail"):
                lines.append(f"博弈: {overview['main_vs_retail']}")
        sections.append("\n".join(lines))
    else:
        sections.append("**大盘资金流**\n数据暂不可用（非交易时段）")

    # ── 行业流入TOP5 ──
    inflow = sectors.get("inflow_top", [])
    if inflow:
        lines = ["**行业流入 TOP**"]
        for i, s in enumerate(inflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 行业流出TOP5 ──
    outflow = sectors.get("outflow_top", [])
    if outflow:
        lines = ["**行业流出 TOP**"]
        for i, s in enumerate(outflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 重点行业资金流 ──
    key_section = _format_key_industries_section(key_industries)
    if key_section:
        sections.append(key_section)

    # ── 轮动强度 ──
    rot_score = sectors.get("rotation_score", 0)
    rot_note = sectors.get("rotation_note", "")
    rot_label = _rotation_label(rot_score)
    rot_text = f"{rot_label}（{rot_score}）"
    if rot_note:
        rot_text += f" — {rot_note}"
    sections.append(f"🔄 **轮动强度**: {rot_text}")

    # ── 概念热点 ──
    if concepts:
        lines = ["**概念热点 TOP**"]
        for i, c in enumerate(concepts[:3], 1):
            name = c.get("name", "")
            f_main = _fmt_flow(c.get("main_inflow"))
            lines.append(f"{i}. {name} {f_main}")
        sections.append("\n".join(lines))

    # ── 背离预警 ──
    divergences = sectors.get("divergence", [])
    if divergences:
        lines = ["⚠️ **背离预警**"]
        for d in divergences:
            icon = "🔻" if "底" in d.get("type", "") else "🔺"
            lines.append(f"{icon} {d['name']}: {d['detail']}")
        sections.append("\n".join(lines))

    # ── 强流入个股 ──
    if strong_stocks:
        lines = ["**强流入个股 TOP**"]
        for i, s in enumerate(strong_stocks[:5], 1):
            name = s.get("name", "")
            code = s.get("code", "")
            ratio = s.get("main_ratio", 0)
            inflow_val = s.get("main_inflow", 0)
            pct = s.get("price_change", 0)
            lines.append(
                f"{i}. {name}({code}) 主力+{ratio:.1f}% {_fmt_flow(inflow_val)} ({_fmt(pct, '%')})"
            )
        sections.append("\n".join(lines))

    note = f"明策 · gugu · 资金流复盘 | 📈 数据截至 {data.get('timestamp', _now_str())}"

    title = f"📊 昨日资金流复盘（{market.get('date', _date_str()) if market else _date_str()}）"
    template = "blue"

    return _card(title, template, sections, note=note)


def build_close_card(data: dict[str, Any]) -> dict[str, Any]:
    """构建 15:10 收盘 — 今日资金流日报卡片。"""
    from gugu.notifier.formatter import _card

    # 数据源全链路不可用 → 发红色告警卡片
    if data.get("sources_unavailable"):
        return _card(
            "⚠️ 今日资金流日报（数据源不可用）",
            "red",
            [
                "**🚨 资金流数据源全链路不可用**",
                "",
                "东方财富资金流 API 当前不可用，HTTP 降级通道也无法获取数据。",
                "今日所有资金流相关推送将为空。",
                "",
                "**⚙ 建议排查**：",
                "1. 检查服务器网络环境是否变更",
                "2. 确认 akshare/东方财富服务状态",
                "3. 数据源恢复后系统会自动重试",
            ],
            note="明策 · gugu · 资金流数据源告警 | 需人工排查",
        )

    market = data.get("market", {})
    north = data.get("north_bound", {})
    overview = data.get("market_overview", {})
    sectors = data.get("sectors", {})
    key_industries = data.get("key_industries", [])
    concepts = data.get("concepts", [])
    strong_stocks = data.get("strong_stocks", [])

    sections: list[str] = []

    # ── 大盘概览 ──
    if market:
        m_date = market.get("date", "")
        north_only = market.get("north_only", False)
        lines = ["**今日大盘**"]
        if north_only:
            nb = north or {}
            lines.append(f"📅 {m_date}")
            lines.append("📊 **北向资金代理**（东方财富 API 暂不可用）")
            n_total = _fmt_flow(nb.get("total"))
            n_sh = _fmt_flow(nb.get("sh_connect"))
            n_sz = _fmt_flow(nb.get("sz_connect"))
            lines.append(f"  北向总额: {n_total}")
            lines.append(f"  沪股通: {n_sh}")
            lines.append(f"  深股通: {n_sz}")
            if overview.get("main_vs_retail"):
                lines.append(f"  博弈: {overview['main_vs_retail']}")
        else:
            sh_pct = _fmt(market.get("sh_change"), "%")
            sz_pct = _fmt(market.get("sz_change"), "%")
            main_flow = _fmt_flow(market.get("main_net_inflow"))
            lines.append(f"📅 {m_date}")
            lines.append(f"上证 {sh_pct} | 深证 {sz_pct}")
            lines.append(f"主力净流入: {main_flow}")
            if north:
                lines.append(f"北向资金: {_fmt_flow(north.get('total'))}")
            if overview.get("main_vs_retail"):
                lines.append(f"博弈: {overview['main_vs_retail']}")
        sections.append("\n".join(lines))
    else:
        sections.append("**今日大盘**\n数据暂不可用（非交易时段）")

    # ── 行业流入TOP5 ──
    inflow = sectors.get("inflow_top", [])
    if inflow:
        lines = ["**行业流入 TOP**"]
        for i, s in enumerate(inflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 行业流出TOP5 ──
    outflow = sectors.get("outflow_top", [])
    if outflow:
        lines = ["**行业流出 TOP**"]
        for i, s in enumerate(outflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 重点行业资金流 ──
    key_section = _format_key_industries_section(key_industries)
    if key_section:
        sections.append(key_section)

    # ── 轮动强度 ──
    rot_score = sectors.get("rotation_score", 0)
    rot_note = sectors.get("rotation_note", "")
    rot_label = _rotation_label(rot_score)
    rot_text = f"{rot_label}（{rot_score}）"
    if rot_note:
        rot_text += f" — {rot_note}"
    sections.append(f"🔄 **轮动强度**: {rot_text}")

    # ── 背离预警 ──
    divergences = sectors.get("divergence", [])
    if divergences:
        lines = ["⚠️ **今日背离预警**"]
        for d in divergences:
            icon = "🔻" if "底" in d.get("type", "") else "🔺"
            lines.append(f"{icon} {d['name']}: {d['detail']} → {d.get('action', '观察')}")
        sections.append("\n".join(lines))

    # ── 强流入个股 ──
    if strong_stocks:
        lines = ["**今日强流入个股**"]
        for i, s in enumerate(strong_stocks[:5], 1):
            name = s.get("name", "")
            code = s.get("code", "")
            ratio = s.get("main_ratio", 0)
            inflow_val = s.get("main_inflow", 0)
            pct = s.get("price_change", 0)
            lines.append(
                f"{i}. {name}({code}) 主力+{ratio:.1f}% {_fmt_flow(inflow_val)} ({_fmt(pct, '%')})"
            )
        sections.append("\n".join(lines))

    # ── 概念热点 ──
    if concepts:
        lines = ["**热门概念**"]
        for i, c in enumerate(concepts[:3], 1):
            name = c.get("name", "")
            f_main = _fmt_flow(c.get("main_inflow"))
            lines.append(f"{i}. {name} {f_main}")
        sections.append("\n".join(lines))

    note = f"明策 · gugu · 资金流日报 | 📈 {data.get('timestamp', _now_str())}"

    title = f"📊 今日资金流日报（{_date_str()}）"
    template = "blue"

    return _card(title, template, sections, note=note)


def build_unavailable_card(report_name: str) -> dict[str, Any]:
    """当所有数据源不可用时发送状态卡片。"""
    from gugu.notifier.formatter import _card

    content = (
        "当前资金流数据源（东方财富）暂时不可用，已跳过本次推送。\n\n"
        "可能原因：\n"
        "- 东方财富 API 网络不通\n"
        "- 盘中数据尚未到位\n\n"
        "系统会在下一执行周期自动重试。"
    )
    return _card(
        title=f"⚠️ {report_name}（数据暂不可用）",
        template="yellow",
        sections=[content],
        note=_now_str(),
    )


def _format_decision_guidance(data: dict[str, Any]) -> str:
    """根据资金流数据生成决策指导建议（基于跟随资金强度原则）。"""
    sectors = data.get("sectors", {})
    strong_stocks = data.get("strong_stocks", [])
    market = data.get("market", {})
    divergences = sectors.get("divergence", [])

    lines: list[str] = []

    # 大盘资金方向判断
    main_inflow = market.get("main_net_inflow") if market else None
    if main_inflow is not None:
        if main_inflow > 30:
            lines.append("📈 **大盘资金面偏强**：主力大幅流入，可适度参与强势板块")
        elif main_inflow > 0:
            lines.append("📊 **大盘资金中性偏多**：主力小幅流入，跟随强势个股")
        elif main_inflow > -30:
            lines.append("⚠️ **大盘资金中性偏空**：主力小幅流出，谨慎操作")
        else:
            lines.append("🟢 **大盘资金面偏弱**：主力大幅流出，建议观望为主")

    # 背离预警提示
    top_div = [d for d in divergences if "顶背离" in d.get("type", "")]
    bottom_div = [d for d in divergences if "底背离" in d.get("type", "")]
    if top_div:
        names = "、".join(d["name"] for d in top_div[:3])
        lines.append(f"🔺 **顶背离预警**：{names} 资金流出但上涨，警惕主力出货")
    if bottom_div:
        names = "、".join(d["name"] for d in bottom_div[:3])
        lines.append(f"🔻 **底背离关注**：{names} 资金流入但下跌，可能吸筹中")

    # 强势个股提示
    if strong_stocks:
        top_stock = strong_stocks[0]
        lines.append(
            f"🔥 **资金最强个股**：{top_stock.get('name', '')}({top_stock.get('code', '')}) "
            f"主力净占比 +{top_stock.get('main_ratio', 0):.1f}%"
        )

    # 策略原则提醒
    lines.append("---")
    lines.append("**📋 操作原则**：")
    lines.append("• 跟随资金强度，拒绝滞涨股补涨幻想")
    lines.append("• 优先创历史新高个股（主力消化套牢盘）")
    lines.append("• 等待缩量回踩后放量确认入场，不追涨")
    lines.append("• 无论确定性多强，必须设止损")

    return "\n".join(lines) if lines else ""


def build_afternoon_card(data: dict[str, Any]) -> dict[str, Any]:
    """构建 14:30 盘后资金流决策报告卡片。

    全量版：大盘资金面、行业流入流出TOP、重点行业资金流、概念热点、
    轮动强度、背离预警、强流入个股、决策指导。
    """
    from gugu.notifier.formatter import _card

    market = data.get("market", {})
    north = data.get("north_bound", {})
    overview = data.get("market_overview", {})
    sectors = data.get("sectors", {})
    key_industries = data.get("key_industries", [])
    concepts = data.get("concepts", [])
    strong_stocks = data.get("strong_stocks", [])

    sections: list[str] = []

    # ── 大盘资金面速览 ──
    if market:
        m_date = market.get("date", "")
        north_only = market.get("north_only", False)
        is_fallback = market.get("data_source", "").startswith("量价推算")
        lines = ["**📊 大盘资金面**"]
        if is_fallback:
            sh_pct = _fmt(market.get("sh_change"), "%")
            sz_pct = _fmt(market.get("sz_change"), "%")
            vol_ratio = market.get("vol_ratio", 1.0)
            flow_direction = market.get("flow_direction", "")
            lines.append(f"📅 {m_date}")
            lines.append(f"上证 {sh_pct} | 深证 {sz_pct} | 量比 {vol_ratio}")
            lines.append(f"⚠️ 资金流API不可用，量价推算: {flow_direction}")
            lines.append(f"估算主力净流入: {_fmt_flow(market.get('main_net_inflow'))}")
            if north:
                n_total = _fmt_flow(north.get("total"))
                lines.append(f"北向资金: {n_total}")
        elif north_only:
            nb = north or {}
            lines.append(f"📅 {m_date}")
            lines.append("📊 **北向资金代理**（东方财富 API 暂不可用）")
            n_total = _fmt_flow(nb.get("total"))
            lines.append(f"  北向总额: {n_total}")
            if overview.get("main_vs_retail"):
                lines.append(f"  博弈: {overview['main_vs_retail']}")
        else:
            sh_pct = _fmt(market.get("sh_change"), "%")
            sz_pct = _fmt(market.get("sz_change"), "%")
            main_flow = _fmt_flow(market.get("main_net_inflow"))
            lines.append(f"📅 {m_date}")
            lines.append(f"上证 {sh_pct} | 深证 {sz_pct}")
            lines.append(f"主力净流入: {main_flow}")
            if north:
                lines.append(f"北向资金: {_fmt_flow(north.get('total'))}")
            if overview.get("main_vs_retail"):
                lines.append(f"  博弈: {overview['main_vs_retail']}")
        sections.append("\n".join(lines))
    else:
        sections.append("**📊 大盘资金面**\n数据暂不可用（非交易时段）")

    # ── 热点行业 TOP5（流入）──
    inflow = sectors.get("inflow_top", [])
    if inflow:
        lines = ["**🔥 行业流入 TOP5**"]
        for i, s in enumerate(inflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 行业流出 TOP5（恢复：之前版本被裁剪）──
    outflow = sectors.get("outflow_top", [])
    if outflow:
        lines = ["**🔄 行业流出 TOP5**"]
        for i, s in enumerate(outflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 重点行业资金流 ──
    key_section = _format_key_industries_section(key_industries)
    if key_section:
        sections.append(key_section)

    # ── 轮动强度（恢复）──
    rot_score = sectors.get("rotation_score", 0)
    rot_note = sectors.get("rotation_note", "")
    if rot_score:
        rot_label = _rotation_label(rot_score)
        rot_text = f"{rot_label}（{rot_score}）"
        if rot_note:
            rot_text += f" — {rot_note}"
        sections.append(f"🔄 **轮动强度**: {rot_text}")

    # ── 概念热点（恢复）──
    if concepts:
        lines = ["**🔥 热门概念**"]
        for i, c in enumerate(concepts[:3], 1):
            name = c.get("name", "")
            f_main = _fmt_flow(c.get("main_inflow"))
            lines.append(f"{i}. {name} {f_main}")
        sections.append("\n".join(lines))

    # ── 背离预警（恢复为独立板块）──
    divergences = sectors.get("divergence", [])
    if divergences:
        lines = ["⚠️ **背离预警**"]
        for d in divergences:
            icon = "🔻" if "底" in d.get("type", "") else "🔺"
            lines.append(f"{icon} {d['name']}: {d['detail']} → {d.get('action', '观察')}")
        sections.append("\n".join(lines))

    # ── 强流入个股 TOP5 ──
    if strong_stocks:
        lines = ["**💰 强流入个股 TOP**"]
        for i, s in enumerate(strong_stocks[:5], 1):
            name = s.get("name", "")
            code = s.get("code", "")
            ratio = s.get("main_ratio", 0)
            inflow_val = s.get("main_inflow", 0)
            pct = s.get("price_change", 0)
            lines.append(
                f"{i}. {name}({code}) 主力+{ratio:.1f}% {_fmt_flow(inflow_val)} ({_fmt(pct, '%')})"
            )
        sections.append("\n".join(lines))

    # ── 决策指导 ──
    guidance = _format_decision_guidance(data)
    if guidance:
        sections.append(guidance)

    note = f"明策 · gugu · 盘后资金流决策报告 | 📈 {data.get('timestamp', _now_str())}"

    title = f"📊 盘后资金流决策报告（{_date_str()} 14:30）"
    template = "blue"

    return _card(title, template, sections, note=note)
