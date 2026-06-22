"""APScheduler 调度器：定时执行交易任务。"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gugu.config import settings
from gugu.engine.main import TradingEngine
from gugu.utils.calendar import is_trading_day
from gugu.utils.log import get_logger

logger = get_logger()


class TradingScheduler:
    """交易调度器。"""

    def __init__(self) -> None:
        cfg = settings().get("scheduler", {})
        tz = cfg.get("timezone", "Asia/Shanghai")
        self._scheduler = AsyncIOScheduler(timezone=tz)
        self._engine = TradingEngine()

    def setup(self) -> None:
        """配置定时任务。"""
        feishu_cfg = settings().get("feishu", {})
        report_times = feishu_cfg.get("daily_report_times", ["09:10", "11:35", "15:10"])

        # 每日交易循环：9:25 集合竞价后开始
        self._scheduler.add_job(
            self._safe_run,
            CronTrigger(hour=9, minute=25, day_of_week="mon-fri"),
            id="daily_cycle",
            name="每日交易循环",
        )

        # 日报推送
        period_map = {"09:10": "morning", "11:35": "noon", "15:10": "close"}
        for t in report_times:
            h, m = t.split(":")
            period = period_map.get(t, "morning")
            self._scheduler.add_job(
                self._safe_report,
                CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri"),
                args=[period],
                id=f"report_{period}",
                name=f"日报-{period}",
            )

        logger.info(f"调度器配置完成: {len(self._scheduler.get_jobs())} 个任务")

    async def _safe_run(self) -> None:
        """安全执行交易循环（非交易日跳过）。"""
        if not is_trading_day():
            logger.info("非交易日，跳过交易循环")
            return
        try:
            await self._engine.run_daily_cycle()
        except Exception as e:
            logger.exception(f"交易循环异常: {e}")

    async def _safe_report(self, period: str) -> None:
        """安全执行日报推送。"""
        if not is_trading_day():
            return
        try:
            await self._engine.send_daily_report(period)
        except Exception as e:
            logger.exception(f"日报推送异常: {e}")

    async def start(self) -> None:
        """启动调度器。"""
        self.setup()
        self._scheduler.start()
        logger.info("调度器已启动，按 Ctrl+C 退出")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            await self.shutdown()

    async def shutdown(self) -> None:
        """停止调度器并释放资源。"""
        self._scheduler.shutdown()
        await self._engine.shutdown()
        logger.info("调度器已停止")
