"""宏观日报调度任务：在 APScheduler 中注册定时宏观推送。

优化（v0.4.0）：将资金流数据合并到宏观卡片中，消除重复发送。
- 08:00 early: 宏观 + 资金流复盘 → 一张卡
- 15:15 close: 宏观 + 资金流日报 → 一张卡
- 09:15 morning / 11:40 noon: 仅宏观（含 A 股指数，与信号报告错峰 5 分钟）
"""

from __future__ import annotations

import asyncio
from typing import Any

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


def _split_interpretation_to_elements(interpretation: str) -> list[dict]:
    """将大师兄解读长文本按 ### 标题拆分为多个 div 元素。

    v0.5.2 修复：
    原代码将全部内容放在单个 div 的 lark_md 中，
    飞书 lark_md 单 div 内容过长可能导致渲染截断。
    按 ### 标题拆分为多个 div，用 hr 分隔，确保完整渲染。

    Args:
        interpretation: 大师兄解读文本（含 ### 标题分节）

    Returns:
        飞书卡片 elements 列表（多个 div + hr 分隔）
    """
    if not interpretation:
        return [{"tag": "div", "text": {"tag": "lark_md", "content": "无解读内容"}}]

    # 按 ### 标题拆分
    sections: list[str] = []
    current_section: list[str] = []

    for line in interpretation.split("\n"):
        if line.startswith("### ") and current_section:
            # 新节开始，保存上一节
            sections.append("\n".join(current_section))
            current_section = []
        current_section.append(line)

    # 最后一节
    if current_section:
        sections.append("\n".join(current_section))

    # 如果没有 ### 标题，整段作为一个 div
    if len(sections) <= 1:
        return [
            {"tag": "div", "text": {"tag": "lark_md", "content": interpretation}},
        ]

    # 构建多个 div + hr 分隔
    elements: list[dict] = []
    for i, section in enumerate(sections):
        section_text = section.strip()
        if not section_text:
            continue
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": section_text},
        })
        # 节之间用 hr 分隔（最后一节不加 hr）
        if i < len(sections) - 1:
            elements.append({"tag": "hr"})

    return elements if elements else [
        {"tag": "div", "text": {"tag": "lark_md", "content": interpretation}},
    ]


async def _collect_flow_sections(version: str) -> list[str]:
    """采集资金流数据并返回卡片段落（仅 early/close 版本调用）。"""
    if version not in _FLOW_MERGE_VERSIONS:
        return []
    try:
        from gugu.notifier.flow_report import (
            _fmt,
            _fmt_flow,
            _format_key_industries_section,
            _rotation_label,
            collect_all,
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


def _build_portfolio_summary(broker: Any) -> str:
    """从 broker 构建用户持仓摘要文本（供大师兄解读个性化使用）。"""
    if broker is None:
        return ""
    try:
        account = broker.get_account()
        total_value = float(account.total_value)
        portfolio = broker.get_portfolio()
        if not portfolio:
            return ""
        lines = [f"总资产 ¥{total_value:,.2f}，当前持仓 {len(portfolio)} 只："]
        for sym, pos in portfolio.items():
            name = getattr(pos, "name", "") or sym
            qty = int(getattr(pos, "quantity", 0))
            avg = float(getattr(pos, "avg_cost", 0))
            price = float(getattr(pos, "current_price", 0))
            market_value = qty * price
            ratio = market_value / total_value if total_value > 0 else 0
            profit_pct = (price - avg) / avg if avg > 0 else 0
            lines.append(
                f"- {name}({sym}) {qty}股 | 成本 ¥{avg:.3f} | "
                f"现价 ¥{price:.3f} | 盈亏 {profit_pct:+.2%} | 仓位 {ratio:.1%}"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"[macro] 构建持仓摘要失败: {e}")
        return ""


async def _push_macro_report(
    version: str,
    notifier: FeishuNotifier | None = None,
    broker: Any = None,
) -> None:
    """采集宏观数据并推送飞书卡片（含 A 股大盘 + 资金流）。

    大师兄解读采用异步补发策略：先推送宏观+资金流卡片（保证数据及时性），
    后台异步生成大师兄解读，完成后单独补发，避免 LLM 超时阻塞主流程。
    """
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

    card["elements"] = elements

    # ── 先推送宏观+资金流卡片（数据优先，不等待 LLM）──
    ok = await notifier.send_card(card)
    if ok:
        logger.info(f"[macro] {version} 日报推送成功（含资金流）")
    else:
        logger.warning(f"[macro] {version} 日报推送失败（已入重试队列）")

    # ── 后台异步补发大师兄解读（不阻塞主流程）──
    # LLM 超时或失败不影响宏观数据推送，解读为锦上添花
    portfolio_summary = _build_portfolio_summary(broker)
    _wisdom_task = asyncio.create_task(
        _push_wisdom_interpretation(version, snapshot, builder, notifier, portfolio_summary)
    )
    _wisdom_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


async def _push_wisdom_interpretation(
    version: str,
    snapshot: Any,
    builder: MacroReportBuilder,
    notifier: FeishuNotifier,
    portfolio_summary: str = "",
) -> None:
    """后台异步生成并补发大师兄解读卡片。

    与主流程解耦：LLM 超时或失败不影响宏观日报已推送的数据。

    v0.5.2 修复：
    - timeout 从 30s 提高到 60s（max_tokens 提高到 2500 后生成时间变长）
    - 将长文本按 ### 标题拆分为多个 div 元素，
      避免飞书 lark_md 单 div 内容过长导致渲染截断
    """
    try:
        from gugu.wisdom.advisor import WisdomAdvisor

        wisdom = WisdomAdvisor()
        summary = builder.build_text_summary(snapshot)
        interpretation = await asyncio.wait_for(
            wisdom.advise_macro_async(summary, portfolio_summary=portfolio_summary),
            timeout=60.0,  # v0.5.2: 30s → 60s，配合 max_tokens 2500
        )
        if not interpretation:
            logger.warning(f"[macro] {version} 大师兄解读为空，跳过补发")
            return

        # v0.5.2 修复：按 ### 标题拆分为多个 div 元素
        # 原代码将全部内容放在单个 div 的 lark_md 中，
        # 飞书 lark_md 单 div 内容过长可能导致渲染截断
        elements = _split_interpretation_to_elements(interpretation)

        card = {
            "header": {
                "title": {"tag": "plain_text", "content": f"📖 大师兄解读 · {version}"},
                "template": "blue",
            },
            "elements": elements,
        }
        ok = await notifier.send_card(card)
        if ok:
            logger.info(f"[macro] {version} 大师兄解读补发成功")
        else:
            logger.warning(f"[macro] {version} 大师兄解读补发失败（已入重试队列）")
    except TimeoutError:
        logger.warning(f"[macro] {version} 大师兄解读 LLM 超时(60s)，跳过补发")
    except Exception as e:
        logger.warning(f"[macro] {version} 大师兄解读补发失败: {e}")


async def scheduled_macro_report(version: str, broker: Any = None) -> None:
    """调度入口：非交易日跳过。"""
    if not is_trading_day():
        logger.info(f"[macro] 非交易日，跳过 {version} 日报")
        return
    try:
        await _push_macro_report(version, broker=broker)
    except Exception as e:
        logger.exception(f"[macro] {version} 日报异常: {e}")


def register_macro_jobs(scheduler: AsyncIOScheduler, broker: Any = None) -> None:
    """在 APScheduler 中注册宏观日报定时任务。

    时间安排与信号汇总（engine/scheduler.py 的 report_* 任务）错开 5 分钟，
    避免同一时刻两个重型异步任务并发竞争资源（数据拉取 + LLM 调用）。
    信号汇总先发（个股层面更紧急），宏观简报随后提供大市背景。
    """
    jobs = [
        ("macro_early", "早间全球简报", 8, 0),
        ("macro_morning", "A股盘前简报", 9, 15),  # signal report 09:10 → macro 09:15
        ("macro_noon", "午间盘面简报", 11, 40),    # signal report 11:35 → macro 11:40
        ("macro_close", "收盘全球概览", 15, 15),   # signal report 15:10 → macro 15:15
    ]
    for job_id, name, hour, minute in jobs:
        scheduler.add_job(
            scheduled_macro_report,
            CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri"),
            args=[job_id.split("_")[1], broker],
            id=job_id,
            name=name,
            misfire_grace_time=600,
            replace_existing=True,
        )
    logger.info(f"[macro] 注册 {len(jobs)} 个日报定时任务")
