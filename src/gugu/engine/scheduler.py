"""APScheduler 调度器：定时执行交易任务。

调度时间表（A 股交易日）：
- 08:00  早间全球简报（宏观五维）+ 资金流复盘（昨日T-1）
- 09:10  盘前信号汇总 + A股盘前宏观简报
- 09:30  开盘扫描（信号 + 下单 + 飞书通知）
- 10:30  盘中扫描（捕捉盘中信号变化）
- 11:35  午间宏观简报 + 午盘信号汇总
- 13:05  午盘扫描（下午开盘后信号更新）
- 14:30  尾盘扫描（尾盘信号 + 止损检查）+ 尾盘选股
- 15:10  收盘信号汇总 + 宏观全球概览 + 资金流日报（当日）
- 15:35  基金监控推送
- 每 10 分钟重试失败的通知队列
"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gugu.config import settings
from gugu.engine.main import TradingEngine
from gugu.screener.terminal_screener import TerminalScreener
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
        """配置定时任务（清除旧任务防重复）。"""
        # 清除所有旧 job，防止多次调用 setup 导致重复注册
        self._scheduler.remove_all_jobs()

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

        # 尾盘选股（14:30 独立任务，与扫描并存）
        self._scheduler.add_job(
            self._safe_screener,
            CronTrigger(hour=14, minute=30, day_of_week="mon-fri"),
            id="screener_1430",
            name="尾盘选股-14:30",
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
                name=f"信号汇总-{period}",
            )

        # 通知重试队列：每 10 分钟重试失败的通知
        self._scheduler.add_job(
            self._safe_retry_notify,
            CronTrigger(minute="*/10"),
            id="retry_notify",
            name="通知重试队列",
        )

        # 宏观日报推送：独立于信号报告，覆盖全时段
        from gugu.macro.scheduler import register_macro_jobs
        register_macro_jobs(self._scheduler)

        # 基金监控推送：15:35（交易日）
        self._scheduler.add_job(
            self._safe_fund_monitor,
            CronTrigger(hour=15, minute=35, day_of_week="mon-fri"),
            id="fund_monitor_1535",
            name="基金监控-15:35",
        )

        # 资金流日报
        flow_cfg = settings().get("flow_report", {})
        if flow_cfg.get("enabled", True):
            morning_t = flow_cfg.get("morning_time", "08:00")
            close_t = flow_cfg.get("close_time", "15:10")
            mh, mm = morning_t.split(":")
            ch, cm = close_t.split(":")
            self._scheduler.add_job(
                self._safe_flow_morning,
                CronTrigger(hour=int(mh), minute=int(mm), day_of_week="mon-fri"),
                id="flow_morning",
                name="资金流复盘-盘前",
            )
            self._scheduler.add_job(
                self._safe_flow_close,
                CronTrigger(hour=int(ch), minute=int(cm), day_of_week="mon-fri"),
                id="flow_close",
                name="资金流日报-收盘",
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
        """安全执行信号汇总推送。"""
        if not is_trading_day():
            return
        try:
            await self._engine.send_daily_report(period)
        except Exception as e:
            logger.exception(f"信号汇总推送异常: {e}")

    async def _safe_retry_notify(self) -> None:
        """重试失败的通知队列。"""
        try:
            count = await self._engine._notifier.retry_queued()
            if count > 0:
                logger.info(f"通知重试: {count} 条成功")
        except Exception as e:
            logger.debug(f"通知重试异常: {e}")

    async def _safe_screener(self) -> None:
        """安全执行尾盘选股（非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            screener = TerminalScreener()
            results = await screener.scan()
            total = 5526  # 全市场约数，实际在 scan 中动态获取
            await self._engine._notifier.notify_screener(results, total)
        except Exception as e:
            logger.exception(f"尾盘选股异常: {e}")

    async def _safe_fund_monitor(self) -> None:
        """安全执行基金监控（非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.notifier.fund_monitor import run_all_fund_monitors

            report = await run_all_fund_monitors()
            results = report.get("results", [])
            if not results:
                logger.info("基金监控: 无结果")
                return

            # 每只基金单独推送卡片
            for result in results:
                await self._engine._notifier.notify_fund_monitor(result)

            logger.info(f"基金监控推送完成: {len(results)} 只基金")
        except Exception as e:
            logger.exception(f"基金监控异常: {e}")

    async def _safe_flow_morning(self) -> None:
        """安全执行盘前资金流复盘（08:00，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.notifier.flow_report import run_morning_report

            data = await run_morning_report()
            await self._engine._notifier.notify_flow_report("morning", data)
            logger.info("资金流复盘(盘前)推送完成")
        except Exception as e:
            logger.exception(f"资金流复盘(盘前)异常: {e}")

    async def _safe_flow_close(self) -> None:
        """安全执行收盘资金流日报（15:10，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.notifier.flow_report import run_close_report

            data = await run_close_report()
            await self._engine._notifier.notify_flow_report("close", data)
            logger.info("资金流日报(收盘)推送完成")
        except Exception as e:
            logger.exception(f"资金流日报(收盘)异常: {e}")

    async def start(self) -> None:
        """启动调度器。启动后立即执行一次盘中扫描（如果是交易日）。"""
        self.setup()
        self._scheduler.start()
        logger.info("调度器已启动，按 Ctrl+C 退出")

        # 启动后立即扫描一次（避免重启后等到下一个时间点）
        if is_trading_day():
            logger.info("启动后立即执行一次盘中扫描...")
            try:
                await self._engine.run_daily_cycle()
            except Exception as e:
                logger.exception(f"启动扫描异常: {e}")

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

def _main() -> None:
    """命令行入口：启动交易调度器守护进程。"""
    logging.basicConfig(level=logging.INFO)
    scheduler = TradingScheduler()
    try:
        asyncio.run(scheduler.start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _main()
