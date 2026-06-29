"""宏观日报调度任务：在 APScheduler 中注册定时宏观推送。"""

from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gugu.macro.collectors import MacroCollector
from gugu.macro.report import MacroReportBuilder
from gugu.notifier.feishu import FeishuNotifier
from gugu.utils.calendar import is_trading_day
from gugu.utils.log import get_logger

logger = get_logger()


async def _push_macro_report(version: str) -> None:
    """采集宏观数据并推送飞书卡片。"""
    collector = MacroCollector()
    snapshot = await collector.collect()
    builder = MacroReportBuilder()
    notifier = FeishuNotifier()

    card_builders = {
        "early": builder.build_early_card,
        "morning": builder.build_morning_card,
        "noon": builder.build_close_card,  # 午间用收盘模板（数据相同）
        "close": builder.build_close_card,
    }
    build_fn = card_builders.get(version, builder.build_early_card)
    card = build_fn(snapshot)
    ok = await notifier.send_card(card)
    if ok:
        logger.info(f"[macro] {version} 日报推送成功")
    else:
        logger.warning(f"[macro] {version} 日报推送失败")


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
        ("macro_early",   "早间全球简报",   8,  0),
        ("macro_morning", "A股盘前简报",    9,  10),
        ("macro_noon",    "午间盘面简报",   11, 35),
        ("macro_close",   "收盘全球概览",   15, 10),
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
