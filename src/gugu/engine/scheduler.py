"""APScheduler 调度器：定时执行交易任务。

调度时间表（A 股交易日）：
- 08:00  早间全球简报（宏观六维 + A股指数 + 资金流复盘 + 大师兄解读）
- 09:10  盘前信号汇总（个股层面，先发）
- 09:15  A股盘前宏观简报（含 A 股指数，错开 5 分钟避免资源竞争）
- 09:30  开盘扫描（信号 + 下单 + 飞书通知）
- 10:30  盘中扫描（捕捉盘中信号变化）
- 11:35  午盘信号汇总（个股层面）
- 11:40  午间宏观简报（含 A 股指数，错开 5 分钟）
- 13:05  午盘扫描（下午开盘后信号更新）
- 14:30  尾盘扫描（尾盘信号 + 止损检查）+ 尾盘选股 + 盘后资金流决策报告
- 15:10  收盘信号汇总（个股层面）
- 15:15  收盘宏观全球概览（含 A 股指数 + 资金流日报 + 大师兄解读，错开 5 分钟）
- 15:35  基金监控推送
- 07:00  Parquet 缓存定期清理（删除 >7天 过期文件）
- 每 10 分钟重试失败的通知队列

注：资金流数据已合并到宏观卡片中（08:00/15:15），不再单独推送。
宏观简报与信号汇总错开 5 分钟，避免同时并发竞争资源。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gugu.config import get_config
from gugu.engine.main import TradingEngine
from gugu.screener.terminal_screener import TerminalScreener
from gugu.screener.validator import (
    ScreenerRecord,
    save_screener_records,
    validate_yesterday_screener,
)
from gugu.utils.calendar import is_trading_day
from gugu.utils.log import get_logger

logger = get_logger()

# 盘中扫描时刻（hour, minute）
SCAN_TIMES = [
    (9, 30),  # 开盘
    (10, 30),  # 上午中段
    (13, 5),  # 午盘开盘
    (14, 30),  # 尾盘
]


class TradingScheduler:
    """交易调度器。"""

    def __init__(self) -> None:
        cfg = get_config()
        tz = cfg.scheduler.timezone
        self._scheduler = AsyncIOScheduler(timezone=tz)
        self._engine = TradingEngine()

    def setup(self) -> None:
        """配置定时任务（清除旧任务防重复）。"""
        # 清除所有旧 job，防止多次调用 setup 导致重复注册
        self._scheduler.remove_all_jobs()

        feishu_cfg = get_config().feishu
        report_times = feishu_cfg.daily_report_times

        # 盘中多轮扫描：每个扫描时刻执行一次完整交易循环
        for i, (h, m) in enumerate(SCAN_TIMES):
            self._scheduler.add_job(
                self._safe_run,
                CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
                id=f"scan_{i + 1}",
                name=f"盘中扫描-{h:02d}:{m:02d}",
            )

        # 尾盘选股（14:30 独立任务，与扫描并存）
        self._scheduler.add_job(
            self._safe_screener,
            CronTrigger(hour=14, minute=30, day_of_week="mon-fri"),
            id="screener_1430",
            name="尾盘选股-14:30",
        )

        # 盘后资金流决策报告（14:30，交易日）
        # 采集资金流数据生成决策指导卡片，与尾盘扫描/选股并行（I/O 密集不阻塞 CPU 扫描）
        self._scheduler.add_job(
            self._safe_afternoon_flow,
            CronTrigger(hour=14, minute=30, day_of_week="mon-fri"),
            id="afternoon_flow_1430",
            name="盘后资金流决策报告-14:30",
        )

        # 尾盘选股次日验证（09:35，开盘价已更新）
        self._scheduler.add_job(
            self._safe_validate_screener,
            CronTrigger(hour=9, minute=35, day_of_week="mon-fri"),
            id="screener_validate_0935",
            name="尾盘验证-09:35",
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

        # 小白学堂每日概念推送：20:00（交易日）
        self._scheduler.add_job(
            self._safe_concept_daily,
            CronTrigger(hour=20, minute=0, day_of_week="mon-fri"),
            id="concept_daily_2000",
            name="每日概念-20:00",
        )

        # Parquet 缓存定期清理：07:00（交易日，盘前清理过期缓存文件）
        self._scheduler.add_job(
            self._safe_cleanup_cache,
            CronTrigger(hour=7, minute=0, day_of_week="mon-fri"),
            id="cache_cleanup_0700",
            name="缓存清理-07:00",
        )

        # 资金流数据已合并到宏观卡片中（08:00/15:15），不再单独注册 flow 任务
        # 宏观调度器 register_macro_jobs 已处理 early(08:00) 和 close(15:15) 的资金流合并

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
            screener = TerminalScreener(dm=self._engine._dm)
            results = await screener.scan()
            total = len(results) if results else 0
            # 保存选股结果到 JSONL（供次日验证使用）
            if results:
                today_str = datetime.now().strftime("%Y-%m-%d")
                records = [
                    ScreenerRecord(
                        symbol=r.symbol,
                        name=r.name,
                        close_price=r.price,
                        change_pct=r.change_pct,
                        volume_ratio=r.volume_ratio,
                        screener_date=today_str,
                        passed=r.passed,
                        failed_conditions=r.failed_conditions,
                    )
                    for r in results
                ]
                save_screener_records(records)
            await self._engine._notifier.notify_screener(results, total)
        except Exception as e:
            logger.exception(f"尾盘选股异常: {e}")

    async def _safe_validate_screener(self) -> None:
        """安全执行尾盘选股次日验证（非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            results = await validate_yesterday_screener()
            if results:
                # 统计并记录日志
                win_open = sum(1 for v in results if v.open_premium > 0)
                win_high = sum(1 for v in results if v.intraday_high_premium > 0)
                n = len(results)
                logger.info(
                    f"📊 尾盘验证日报: "
                    f"昨日选股 {n} 只 | "
                    f"高开 {win_open}/{n}({win_open/n*100:.0f}%) | "
                    f"日内冲高 {win_high}/{n}({win_high/n*100:.0f}%) | "
                    f"平均开盘溢价 {sum(v.open_premium for v in results)/n:+.2f}%"
                )
        except Exception as e:
            logger.exception(f"尾盘选股验证异常: {e}")

    async def _safe_afternoon_flow(self) -> None:
        """安全执行盘后资金流决策报告（14:30，非交易日跳过）。

        采集资金流数据，生成决策指导卡片推送飞书。
        与 15:10 收盘日报区别：聚焦"决策指导"而非"数据汇总"。
        """
        if not is_trading_day():
            return
        try:
            from gugu.notifier.flow_report import run_afternoon_report

            result = await run_afternoon_report()
            await self._engine._notifier.notify_flow_report("afternoon", result)
            logger.info("盘后资金流决策报告推送完成（14:30）")
        except Exception as e:
            logger.exception(f"盘后资金流决策报告异常: {e}")

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

    # 资金流推送已合并到宏观卡片中（macro/scheduler.py 的 _push_macro_report）
    # 不再单独推送 flow_morning / flow_close

    async def _safe_concept_daily(self) -> None:
        """安全执行每日概念推送（20:00，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.wisdom.tutor import get_daily_concept

            concept = get_daily_concept()
            if not concept:
                logger.info("每日概念: 无可用概念")
                return

            # 用 KB Agent 检索详细解释
            explanation = ""
            citations: list[dict[str, str]] = []
            try:
                from gugu.wisdom.kb_agent import get_kb_agent

                agent = get_kb_agent()
                answer = await agent.ask(concept.get("kb_query", concept["name"]))
                explanation = answer.answer
                citations = [c.to_dict() if hasattr(c, "to_dict") else c for c in answer.citations]
            except Exception as e:
                logger.debug(f"KB Agent 检索概念解释失败: {e}")

            await self._engine._notifier.notify_concept(
                concept["name"], concept["definition"], explanation, citations
            )
            logger.info(f"每日概念推送完成: {concept['name']}")
        except Exception as e:
            logger.exception(f"每日概念推送异常: {e}")

    async def _safe_cleanup_cache(self) -> None:
        """安全执行 Parquet 缓存清理（非交易日也执行，长假后清理积压）。"""
        try:
            from gugu.data.cache import persistent_cache

            count = persistent_cache().cleanup_stale(max_age_days=7)
            if count > 0:
                logger.info(f"Parquet 缓存清理: 删除 {count} 个过期文件")
        except Exception as e:
            logger.debug(f"缓存清理异常: {e}")

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

        # 独立心跳协程：每 60 秒写入一次，区分"进程活着"和"交易循环运行中"
        async def _heartbeat_loop() -> None:
            """每 60 秒写入心跳，独立于交易循环。"""
            while True:
                try:
                    self._engine._write_heartbeat("alive")
                except Exception:
                    pass
                await asyncio.sleep(60)

        hb_task = asyncio.create_task(_heartbeat_loop())

        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
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
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(scheduler.start())


if __name__ == "__main__":
    _main()
