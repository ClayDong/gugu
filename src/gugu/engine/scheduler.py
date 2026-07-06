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
- 20:00  舆情日报（个股新闻情感分析 + 操作建议）
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
from gugu.utils.calendar import is_trading_day, is_trading_time
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

        # P1-1: 震荡市盘中观察列表推送（10:30 / 13:05）
        # 在震荡市下，盘中扫描不推荐买入，但推送接近 MA20 支撑的股票列表
        # 让用户有心理准备，14:30 推送正式选股结果
        for h, m in [(10, 30), (13, 5)]:
            self._scheduler.add_job(
                self._safe_range_watchlist,
                CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
                id=f"range_watchlist_{h:02d}{m:02d}",
                name=f"震荡市观察列表-{h:02d}:{m:02d}",
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

        register_macro_jobs(self._scheduler, broker=self._engine._broker)

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

        # 舆情日报推送：20:30（交易日，收盘后分析新闻情感）
        # 通过 SentimentSystem 采集东方财富新闻，金融情感词典分析，
        # 生成舆情报告推送到飞书（含操作建议）
        self._scheduler.add_job(
            self._safe_sentiment_daily,
            CronTrigger(hour=20, minute=30, day_of_week="mon-fri"),
            id="sentiment_daily_2030",
            name="舆情日报-20:30",
        )

        # Parquet 缓存定期清理：07:00（交易日，盘前清理过期缓存文件）
        self._scheduler.add_job(
            self._safe_cleanup_cache,
            CronTrigger(hour=7, minute=0, day_of_week="mon-fri"),
            id="cache_cleanup_0700",
            name="缓存清理-07:00",
        )

        # P1-7: 周参数优化（周日 03:00，避免交易日 + 凌晨低谷）
        # 用多股聚合 Walk-Forward 寻找跨股鲁棒参数，写回需通过 4 项解锁条件
        # 选 6 只行业代表股：白酒/养殖/电池/CXO/安防/金融
        self._scheduler.add_job(
            self._safe_optimize_params,
            CronTrigger(hour=3, minute=0, day_of_week="sun"),
            id="weekly_optimize_0300",
            name="周参数优化-周日03:00",
        )

        # P2-8: 周日复盘推送（20:00，避免交易日 + 周末有充足时间复盘）
        # 汇总本周交易信号、实际成交、盈亏归因，推送飞书复盘卡片
        # 陈江挺："炒股靠的是经验，经验来自复盘"
        self._scheduler.add_job(
            self._safe_weekly_review,
            CronTrigger(hour=20, minute=0, day_of_week="sun"),
            id="weekly_review_2000",
            name="周复盘推送-周日20:00",
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
        """安全执行尾盘选股（非交易日跳过）。

        P0 修复：异常时也发送错误通知（避免静默失败，用户不知情）。
        """
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
            # P0 修复：异常时也通知用户（避免静默失败）
            try:
                await self._engine._notifier.notify_screener([], 0)
            except Exception as notify_err:
                logger.error(f"尾盘选股异常通知发送失败: {notify_err}")

    async def _safe_validate_screener(self) -> None:
        """安全执行尾盘选股次日验证（非交易日跳过）。

        P0 修复：无结果时也通知用户"昨日无选股记录"（避免静默）。
        """
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
                # 推送飞书验证结果卡片
                await self._engine._notifier.notify_screener_validation(results)
            else:
                logger.info("尾盘验证: 无结果（昨日无选股记录或行情获取失败）")
                # P0 修复：无结果也通知，告知用户系统正常运行但昨日无选股
                await self._engine._notifier.notify_screener_validation([])
        except Exception as e:
            logger.exception(f"尾盘选股验证异常: {e}")

    async def _safe_range_watchlist(self) -> None:
        """P1-1: 震荡市盘中观察列表推送。

        在震荡市（range）下，盘中扫描不推荐买入（box_breakout 实证亏钱），
        但推送接近 MA20 支撑的股票列表，让用户有心理准备，
        14:30 推送正式选股结果（含入场/止损/目标位）。

        牛市下不推送（避免干扰，牛市依赖 box_breakout 盘中扫描推荐）。
        """
        if not is_trading_day():
            return
        try:
            market_status = await self._engine._market_filter.check_market()
            regime = market_status.get("regime", "unknown")

            # 仅在震荡市和熊市推送观察列表（牛市依赖盘中扫描推荐）
            if regime == "bull":
                logger.debug(f"盘中观察: 牛市，跳过观察列表推送（依赖盘中扫描推荐）")
                return

            # 扫描 watchlist + 自动选股候选中接近 MA20 的股票
            candidates = await self._scan_range_candidates()
            if candidates:
                logger.info(f"盘中观察: 震荡市推送 {len(candidates)} 只接近 MA20 的候选")
                await self._engine._notifier.notify_range_watchlist(candidates, regime)
            else:
                # 无候选也推送（避免静默）
                await self._engine._notifier.notify_range_watchlist([], regime)
        except Exception as e:
            logger.exception(f"震荡市观察列表异常: {e}")

    async def _scan_range_candidates(self) -> list[dict]:
        """扫描接近 MA20 支撑位的候选股（用于震荡市观察列表）。"""
        import asyncio

        import pandas as pd

        from gugu.utils.calendar import last_trading_day

        candidates: list[dict] = []
        watchlist = self._engine._watchlist[:20]  # 最多扫描 20 只，避免耗时过长

        async def check_one(symbol: str) -> dict | None:
            try:
                df = await self._engine._dm.fetch_stock_kline(
                    symbol, end_date=last_trading_day().isoformat(), limit=60
                )
                if df is None or len(df) < 30:
                    return None
                price = float(df["close"].iloc[-1])
                ma20 = float(df["close"].rolling(20).mean().iloc[-1])
                if ma20 <= 0:
                    return None
                distance_pct = (price - ma20) / ma20 * 100
                # 距离 MA20 ±5% 以内的股票才推送
                if abs(distance_pct) > 5.0:
                    return None
                return {
                    "symbol": symbol,
                    "name": self._engine._dm.get_stock_name(symbol) or symbol,
                    "price": price,
                    "ma20": round(ma20, 2),
                    "distance_pct": round(distance_pct, 2),
                }
            except Exception:
                return None

        results = await asyncio.gather(*[check_one(s) for s in watchlist], return_exceptions=False)
        for r in results:
            if r is not None:
                candidates.append(r)
        return candidates

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
            logger.exception(f"概念推送异常: {e}")

    async def _safe_sentiment_daily(self) -> None:
        """安全执行舆情日报推送（20:30 交易日）。"""
        if not __import__("gugu.utils.calendar", fromlist=["is_trading_day"]).is_trading_day():
            return
        try:
            from gugu.analysis.sentiment import SentimentSystem
            from gugu.notifier import feishu_notifier

            notifier = feishu_notifier()
            system = SentimentSystem(stock_name="自选股池")
            await system.run_and_notify(notifier)
        except Exception as e:
            logger.exception(f"舆情日报异常: {e}")

    async def _safe_cleanup_cache(self) -> None:
        """安全执行 Parquet 缓存清理（非交易日也执行，长假后清理积压）。"""
        try:
            from gugu.data.cache import persistent_cache

            count = persistent_cache().cleanup_stale(max_age_days=7)
            if count > 0:
                logger.info(f"Parquet 缓存清理: 删除 {count} 个过期文件")
        except Exception as e:
            logger.debug(f"缓存清理异常: {e}")

    async def _safe_optimize_params(self) -> None:
        """P1-7: 周参数优化（周日 03:00）。

        用多股聚合 Walk-Forward 寻找跨股鲁棒参数。
        写回需通过 4 项解锁条件（样本外 Sharpe>0、参数稳定性、回撤<25%、收益非负）。
        失败不会污染生产参数（strategy_defaults.yaml 不变）。

        设计原则：
        - 周日 03:00 执行避免影响交易日
        - 6 只行业代表股（白酒/养殖/电池/CXO/安防/金融）
        - --write 模式：通过解锁条件才写回
        - 失败仅记录日志，不影响下周一交易
        """
        try:
            import subprocess
            from pathlib import Path

            from gugu.config import PROJECT_ROOT

            script = PROJECT_ROOT / "scripts" / "optimize_params.py"
            if not script.exists():
                logger.warning(f"优化脚本不存在: {script}")
                return

            # 6 只行业代表股 + --write 模式（自动门控）
            cmd = [
                "uv", "run", "python", str(script),
                "--multi-stock", "600519,002714,300750,603259,002415,601318",
                "--days", "500",
                "--splits", "3",
                "--write",
            ]
            logger.info(f"启动周参数优化: {' '.join(cmd)}")

            # 同步执行（ sunday 03:00 无其他任务竞争）
            result = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=3600,  # 1 小时超时
            )

            if result.returncode == 0:
                logger.info(
                    f"周参数优化完成: stdout={result.stdout[-500:] if result.stdout else ''}"
                )
            else:
                logger.error(
                    f"周参数优化失败 rc={result.returncode}: "
                    f"stderr={result.stderr[-500:] if result.stderr else ''}"
                )
        except subprocess.TimeoutExpired:
            logger.error("周参数优化超时（>1h），已终止")
        except Exception as e:
            logger.exception(f"周参数优化异常: {e}")

    async def _safe_weekly_review(self) -> None:
        """P2-8: 周日复盘推送（周日 20:00）。

        汇总本周交易信号、实际成交、盈亏归因，推送飞书复盘卡片。
        陈江挺："炒股靠的是经验，经验来自复盘"。

        复盘内容：
        - 本周信号数 / 实际成交 / 盈亏归因
        - 按策略分组的胜率统计
        - 飞书卡片标注「学到的教训」
        """
        try:
            from datetime import timedelta

            from gugu.config import PROJECT_ROOT

            history_file = PROJECT_ROOT / "data" / "signals_history.jsonl"
            if not history_file.exists():
                logger.info("周复盘: 无信号历史文件")
                return

            # 读取近 7 天的信号记录
            today = datetime.now().date()
            week_ago = today - timedelta(days=7)
            signals: list[dict] = []
            try:
                with history_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            import json as _json
                            record = _json.loads(line)
                            ts_str = record.get("timestamp", record.get("date", ""))
                            if ts_str:
                                record_date = datetime.fromisoformat(
                                    ts_str.replace("Z", "")
                                ).date()
                                if record_date >= week_ago:
                                    signals.append(record)
                        except (ValueError, KeyError):
                            continue
            except Exception as e:
                logger.warning(f"周复盘: 读取信号历史失败: {e}")
                return

            if not signals:
                logger.info("周复盘: 本周无信号记录")
                return

            # 统计信号
            total_signals = len(signals)
            buy_signals = sum(1 for s in signals if s.get("signal") == 1 or s.get("action") == "buy")
            sell_signals = sum(1 for s in signals if s.get("signal") == -1 or s.get("action") == "sell")

            # 按策略分组统计
            strategy_stats: dict[str, int] = {}
            for s in signals:
                strategy = s.get("strategy", "unknown")
                strategy_stats[strategy] = strategy_stats.get(strategy, 0) + 1

            # 推送飞书复盘卡片
            try:
                await self._engine._notifier.notify_weekly_review({
                    "period": f"{week_ago.isoformat()} ~ {today.isoformat()}",
                    "total_signals": total_signals,
                    "buy_signals": buy_signals,
                    "sell_signals": sell_signals,
                    "strategy_stats": strategy_stats,
                    "signals": signals[:20],  # 仅展示前 20 条
                })
                logger.info(
                    f"周复盘推送完成: 信号 {total_signals} 条 "
                    f"(买 {buy_signals} / 卖 {sell_signals})"
                )
            except AttributeError:
                logger.debug("周复盘: notifier 未实现 notify_weekly_review 方法")
            except Exception as e:
                logger.warning(f"周复盘推送失败: {e}")

        except Exception as e:
            logger.exception(f"周复盘异常: {e}")

    async def start(self) -> None:
        """启动调度器。启动后立即执行一次盘中扫描（如果是交易日）。"""
        self.setup()
        self._scheduler.start()
        logger.info("调度器已启动，按 Ctrl+C 退出")

        # 启动后立即扫描一次（避免重启后等到下一个时间点）
        # P0 修复：原仅 is_trading_day() 检查，导致非交易时段启动也会立即执行
        # 必须同时满足 is_trading_time() 才执行，否则会产生非交易时段垃圾信号
        if is_trading_day() and is_trading_time():
            logger.info("启动后立即执行一次盘中扫描（交易时段内）...")
            try:
                await self._engine.run_daily_cycle()
            except Exception as e:
                logger.exception(f"启动扫描异常: {e}")
        elif is_trading_day():
            logger.info("启动时为交易日但非交易时段，跳过立即扫描（等待下次定时触发）")

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
