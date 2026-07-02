"""宏观日报调度任务：在 APScheduler 中注册定时宏观推送。

优化（v0.4.0）：将资金流数据合并到宏观卡片中，消除重复发送。
- 08:00 early: 宏观 + 资金流复盘 → 一张卡
- 15:10 close: 宏观 + 资金流日报 → 一张卡
- 09:10 morning / 11:35 noon: 仅宏观（含 A 股指数）
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gugu.macro.collectors import MacroCollector
from gugu.macro.report import MacroReportBuilder
from gugu.notifier.feishu import FeishuNotifier
from gugu.utils.calendar import is_trading_day
from gugu.utils.log import get_logger

logger = get_logger()

# 需要合并资金流数据的版本（08:00 盘前 + 15:10 收盘）
_FLOW_MERGE_VERSIONS = {"early", "close"}


async def _collect_flow_sections(version: str) -> list[str]:
    """采集资金流数据并返回卡片段落（仅 early/close 版本调用）。"""
    if version not in _FLOW_MERGE_VERSIONS:
        return []
    try:
        from gugu.notifier.flow_report import (
            collect_all,
            _format_key_industries_section,
            _fmt_flow,
            _fmt,
            _rotation_label,
        )

        data = await collect_all()
        sections: list[str] = []

        # ── 行业资金流 TOP ──
        sectors = data.get("sectors", {})
        inflow = sectors.get("inflow_top", [])
        if inflow:
            lines = ["**行业流入 TOP**" if version == "close" else "**昨日行业流入 TOP**"]
            for i, s in enumerate(inflow[:5], 1):
                name = s.get("name", "")
                f_main = _fmt_flow(s.get("main_inflow"))
                pct = _fmt(s.get("price_change"), "%")
                lines.append(f"{i}. {name} {f_main} ({pct})")
            sections.append("\n".join(lines))

        outflow = sectors.get("outflow_top", [])
        if outflow:
            lines = ["**行业流出 TOP**" if version == "close" else "**昨日行业流出 TOP**"]
            for i, s in enumerate(outflow[:5], 1):
                name = s.get("name", "")
                f_main = _fmt_flow(s.get("main_inflow"))
                pct = _fmt(s.get("price_change"), "%")
                lines.append(f"{i}. {name} {f_main} ({pct})")
            sections.append("\n".join(lines))

        # ── 重点行业资金流 ──
        key_section = _format_key_industries_section(data.get("key_industries", []))
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
            lines = ["⚠️ **背离预警**"]
            for d in divergences:
                icon = "🔻" if "底" in d.get("type", "") else "🔺"
                lines.append(f"{icon} {d['name']}: {d['detail']}")
            sections.append("\n".join(lines))

        # ── 强流入个股 ──
        strong_stocks = data.get("strong_stocks", [])
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

        logger.info(f"[macro] {version} 资金流段落合并: {len(sections)} 段")
        return sections
    except Exception as e:
        logger.warning(f"[macro] {version} 资金流数据合并失败: {e}")
        return []


async def _push_macro_report(
    version: str,
    notifier: FeishuNotifier | None = None,
) -> None:
    """采集宏观数据并推送飞书卡片（含 A 股大盘 + 大师兄解读 + 资金流）。"""
    from gugu.wisdom.advisor import WisdomAdvisor

    collector = MacroCollector()
    snapshot = await collector.collect()
    builder = MacroReportBuilder()
    if notifier is None:
        notifier = FeishuNotifier()

    card_builders = {
        "early": builder.build_early_card,
        "morning": builder.build_morning_card,
        "noon": builder.build_close_card,  # 午间用收盘模板（数据相同）
        "close": builder.build_close_card,
    }
    build_fn = card_builders.get(version, builder.build_early_card)
    card = build_fn(snapshot)

    elements = card.get("elements", [])

    # ── 合并资金流数据段落（仅 early/close 版本）──
    flow_sections = await _collect_flow_sections(version)
    if flow_sections:
        for section in flow_sections:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": section}})

    # ── 添加大师兄解读（使用 WisdomAdvisor 的宏观视角）──
    try:
        wisdom = WisdomAdvisor()
        # 构建宏观数据摘要作为 LLM 上下文（含 A 股大盘 + 资金流）
        summary = builder.build_text_summary(snapshot)
        interpretation = await wisdom.advise_macro_async(summary)
        if interpretation:
            interpretation_md = f"\n\n**📖 大师兄解读**\n{interpretation}"
            if elements:
                last_element = elements[-1]
                if "content" in last_element:
                    last_element["content"] += interpretation_md
                else:
                    elements.append({"tag": "hr"})
                    elements.append({"tag": "markdown", "content": interpretation_md})
            else:
                elements.append({"tag": "markdown", "content": interpretation_md})
            logger.info(f"[macro] {version} 添加大师兄解读成功")
        else:
            logger.warning(f"[macro] {version} 大师兄解读为空，跳过")
    except Exception as e:
        logger.warning(f"[macro] {version} 添加大师兄解读失败: {e}")

    card["elements"] = elements

    ok = await notifier.send_card(card)
    if ok:
        logger.info(f"[macro] {version} 日报推送成功（含资金流+大师兄解读）")
    else:
        logger.warning(f"[macro] {version} 日报推送失败（已入重试队列）")


async def scheduled_macro_report(version: str) -> None:
    """调度入口：非交易日跳过。"""
    if not is_trading_day():
        logger.info(f"[macro] 非交易日，跳过 {version} 日报")
        return
    try:
        await _push_macro_report(version)
    except Exception as e:
        logger.exception(f"[macro] {version} 日报异常: {e}")


def register_macro_jobs(scheduler: AsyncIOScheduler) -> None:
    """在 APScheduler 中注册宏观日报定时任务。"""
    jobs = [
        ("macro_early", "早间全球简报", 8, 0),
        ("macro_morning", "A股盘前简报", 9, 10),
        ("macro_noon", "午间盘面简报", 11, 35),
        ("macro_close", "收盘全球概览", 15, 10),
    ]
    for job_id, name, hour, minute in jobs:
        scheduler.add_job(
            scheduled_macro_report,
            CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri"),
            args=[job_id.split("_")[1]],
            id=job_id,
            name=name,
            misfire_grace_time=600,
            replace_existing=True,
        )
    logger.info(f"[macro] 注册 {len(jobs)} 个日报定时任务")
