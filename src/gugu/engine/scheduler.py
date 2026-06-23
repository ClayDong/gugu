"""APScheduler 调度器：定时执行交易任务。

调度时间表（A 股交易日）：
- 09:10  盘前日报（昨日持仓、今日关注）
- 09:30  开盘扫描（信号 + 下单 + 飞书通知）
- 10:30  盘中扫描（捕捉盘中信号变化）
- 13:05  午盘扫描（下午开盘后信号更新）
- 14:30  尾盘扫描（尾盘信号 + 止损检查）
- 15:10  收盘日报（全天交易汇总）
"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gugu.config import settings
from gugu.engine.main import TradingEngine
from gugu.utils.calendar import is_trading_day
from gugu.utils.log import get_logger

logger = get_logger()

# 盘中扫描时刻（hour, minute）
SCAN_TIMES = [
    (9, 30),   # 开盘
    (10, 30),  # 上午中段
    (13, 5),   # 午盘开盘
    (14, 30),  # 尾盘
]


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
        report_times = feishu_cfg.get("daily_report_times", ["09:10", "15:10"])

        # 盘中多轮扫描：每个扫描时刻执行一次完整交易循环
        for i, (h, m) in enumerate(SCAN_TIMES):
            self._scheduler.add_job(
                self._safe_run,
                CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
                id=f"scan_{i+1}",
                name=f"盘中扫描-{h:02d}:{m:02d}",
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
        for job in self._scheduler.get_jobs():
            logger.info(f"  {job.id}: {job.name} -> {job.trigger}")

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
