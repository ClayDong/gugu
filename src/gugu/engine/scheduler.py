"""APScheduler 调度器：定时执行交易任务。

调度时间表（A 股交易日）：
- 08:00  早间全球简报（宏观六维 + A 股指数 + 资金流复盘 + 大师兄解读）
- 09:10  盘前信号汇总（股层面，先发）
- 09:15  A股盘前宏观简报（含 A 股指数，错开 5 分钟避免资源竞争）
- 09:30  开盘扫描（信号 + 下单 + 飞书通知）
- 10:30  盘中扫描（捕捉中信号变化）
- 11:35  午盘信号汇总（股层面）
- 11:40  午间宏观简报（含 A 股指数，错开 5 分钟）
- 13:05  午盘扫描（下午开盘后信号更新）
- 14:30  尾盘扫描（尾盘信号 + 止损检查）+ 尾盘选股 + 盘后资金流决策报告
- 15:10  收盘信号汇总（股层面）
- 15:15  收盘宏观全球概览（含 A 股指数 + 资金流日报 + 大师兄解读，错开 5 分钟）
- 15:20  信号绩效验证（追踪信号实际收益，推送命中率/策略对比报告）
- 15:25  信号次日验证（T+1 收盘收益/盘中回撤/止损命中率）
- 15:35  基金监控推送
- 20:00  情感日报（个股新闻情感分析 + 操作建议）
- 周日 20:30 信号绩效周报（近7日胜率/策略贡献/分日密度）
- 07:00  Parquet 缓存定期清理（删除 >7天 过期文件）
- 每 10 分钟重试失败的通知队列

子模块：
- scheduler_jobs: 各定时任务的安全执行方法（_split from scheduler.py for maintainability）

解耦说明（v0.9.33+）：
TradingScheduler 接受 engine 注入（EngineProtocol），不再硬编码依赖 TradingEngine 类。
引擎实例由启动入口（main/scheduler_runner）构造并传入，scheduler 只负责定时调度，
不负责引擎的构造和生命周期管理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from datetime import datetime
from typing import Any, Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from gugu.config import get_config
from gugu.engine.scheduler_jobs import SchedulerJobsMixin
from gugu.utils.atomic import atomic_write_json
from gugu.utils.calendar import is_trading_day, is_trading_time
from gugu.utils.log import get_logger

logger = get_logger()


class _ConfiguredAsyncIOScheduler(AsyncIOScheduler):
    def add_job(self, func, trigger=None, *args, **kwargs):
        kwargs.update(max_instances=1, misfire_grace_time=120, coalesce=True)
        return super().add_job(func, trigger, *args, **kwargs)


# 盘中扫描时刻（hour, minute）
SCAN_TIMES = [
    (9, 30),  # 开盘
    (10, 30),  # 上午中段
    (13, 5),  # 午盘开盘
    (14, 30),  # 尾盘
]


class EngineProtocol(Protocol):
    """调度器对交易引擎的最小依赖协议。

    定义 TradingScheduler 需要的引擎接口，不绑定具体实现类。
    使得 scheduler 可被独立测试，无需实例化 TradingEngine。
    """

    async def run_daily_cycle(self) -> None: ...

    async def send_daily_report(self, period: str) -> None: ...

    async def shutdown(self) -> None: ...

    @property
    def notifier(self) -> Any: ...

    @property
    def data_manager(self) -> Any: ...

    @property
    def broker(self) -> Any: ...

    @property
    def router(self) -> Any: ...

    @property
    def market_filter(self) -> Any: ...

    @property
    def watchlist(self) -> list[Any]: ...

    def write_heartbeat(self, status: str = "alive") -> None: ...

    async def scan_signals(self, watchlist: list[Any] | None = None) -> list[Any]: ...

    async def process_signal(self, signal: Any) -> Any: ...


class TradingScheduler(SchedulerJobsMixin):
    """交易调度器。

    Args:
        engine: 交易引擎实例。None 时通过延迟导入构造默认 TradingEngine
            （保持向后兼容，推荐生产入口显式传入）。
    """

    def __init__(self, engine: EngineProtocol | None = None) -> None:
        cfg = get_config()
        tz = cfg.scheduler.timezone
        self._scheduler = _ConfiguredAsyncIOScheduler(timezone=tz)
        self._shutdown_requested = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._main_task: asyncio.Task[None] | None = None
        self._install_signal_handlers()
        self._engine = engine if engine is not None else self._default_engine()
        self._intraday_monitor = self._build_intraday_monitor()
        self._realtime_scanner = self._build_realtime_scanner()

    @staticmethod
    def _default_engine() -> Any:
        """延迟导入构造默认 TradingEngine，避免模块级耦合。"""
        from gugu.engine.main import TradingEngine

        return TradingEngine()

    def _build_realtime_scanner(self):
        """构建 RealtimeSignalScanner 单例（v0.9.0 实时推荐）。

        依赖盘中盯盘开关：intraday_monitor 关闭时实时推荐也关闭。
        """
        try:
            from gugu.engine.realtime_scanner import RealtimeSignalScanner

            cfg = get_config()
            im_cfg = getattr(cfg, "intraday_monitor", None)
            enabled = getattr(im_cfg, "enabled", True) if im_cfg else True
            recommend = getattr(im_cfg, "realtime_recommend", True) if im_cfg else True
            cooldown = getattr(im_cfg, "signal_cooldown_sec", 1800) if im_cfg else 1800
            scan_interval = getattr(im_cfg, "signal_scan_interval_sec", 300) if im_cfg else 300
            if not enabled or not recommend or not self._task_enabled("intraday_monitor"):
                logger.info(
                    "实时推荐: 未启用（enabled={} recommend={}），跳过构建", enabled, recommend
                )
                return None
            return RealtimeSignalScanner(
                engine=self._engine, cooldown_sec=cooldown, scan_interval_sec=scan_interval
            )
        except Exception as e:
            logger.warning("RealtimeSignalScanner 构建失败，实时推荐禁用: {}", e)
            return None

    def _build_intraday_monitor(self):
        """构建 IntradayMonitor 单例（启动时构建一次，后续复用）。"""
        try:
            from gugu.engine.intraday_monitor import IntradayMonitor

            cfg = get_config()
            im_cfg = getattr(cfg, "intraday_monitor", None)
            mode = getattr(im_cfg, "performance_mode", "auto") if im_cfg else "auto"
            thresholds = getattr(im_cfg, "alert_thresholds", None) if im_cfg else None

            return IntradayMonitor(
                data_manager=self._engine.data_manager,
                watchlist=self._engine.watchlist,
                portfolio_symbols=[],
                performance_mode=mode,
                alert_thresholds=thresholds,
            )
        except Exception as e:
            logger.warning("IntradayMonitor 构建失败，盘中监控禁用: {}", e)
            return None

    def _task_enabled(self, task_name: str) -> bool:
        """检查调度任务是否启用（从 settings.yaml scheduler.tasks 读取）。"""
        try:
            cfg = get_config()
            tasks = getattr(cfg.scheduler, "tasks", None)
            if tasks is None:
                return True
            return getattr(tasks, task_name, True)
        except Exception:
            return True

    def setup(self) -> None:
        """配置定时任务（清除旧任务防重复）。"""
        self._scheduler.remove_all_jobs()

        feishu_cfg = get_config().feishu
        report_times = feishu_cfg.daily_report_times

        # 盘中多轮扫描：每轮独立受配置开关控制。
        scan_flags = ("scan_open", "scan_mid_morning", "scan_afternoon", "scan_close")
        for i, (h, m) in enumerate(SCAN_TIMES):
            if self._task_enabled(scan_flags[i]):
                self._scheduler.add_job(
                    self._safe_run,
                    CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
                    id=f"scan_{i + 1}",
                    name=f"盘中扫描-{h:02d}:{m:02d}",
                )

        # RSI 专项扫描（13:30，仅扫描 rsi_reversal 策略）
        if self._task_enabled("rsi_scan"):
            self._scheduler.add_job(
                self._safe_rsi_scan,
                CronTrigger(hour=13, minute=30, day_of_week="mon-fri"),
                id="rsi_scan_1330",
                name="RSI专项扫描-13:30",
            )

        # 尾盘选股（14:31）
        if self._task_enabled("screener"):
            self._scheduler.add_job(
                self._safe_screener,
                CronTrigger(hour=14, minute=31, day_of_week="mon-fri"),
                id="screener_1430",
                name="尾盘选股-14:30",
            )

        # 盘后资金流决策报告（14:32）
        if self._task_enabled("flow_afternoon"):
            self._scheduler.add_job(
                self._safe_afternoon_flow,
                CronTrigger(hour=14, minute=32, day_of_week="mon-fri"),
                id="afternoon_flow_1430",
                name="盘后资金流决策报告-14:30",
            )

        # 尾盘选股次日验证（09:35）
        if self._task_enabled("screener"):
            self._scheduler.add_job(
                self._safe_validate_screener,
                CronTrigger(hour=9, minute=35, day_of_week="mon-fri"),
                id="screener_validate_0935",
                name="尾盘验证-09:35",
            )

        # 尾盘选股收盘验证（15:05）
        if self._task_enabled("screener"):
            self._scheduler.add_job(
                self._safe_validate_screener_eod,
                CronTrigger(hour=15, minute=5, day_of_week="mon-fri"),
                id="screener_validate_1505",
                name="尾盘收盘验证-15:05",
            )

        # 尾盘选股参数自动调优（15:10）
        if self._task_enabled("screener_auto_tune"):
            self._scheduler.add_job(
                self._safe_auto_tune_screener,
                CronTrigger(hour=15, minute=10, day_of_week="mon-fri"),
                id="screener_auto_tune_1510",
                name="尾盘选股自动调优-15:10",
            )

        # P1-1: 震荡市盘中观察列表推送（跟随对应扫描开关）
        for h, m, task_name in (
            (10, 30, "scan_mid_morning"),
            (13, 5, "scan_afternoon"),
        ):
            if self._task_enabled(task_name):
                self._scheduler.add_job(
                    self._safe_range_watchlist,
                    CronTrigger(hour=h, minute=m, day_of_week="mon-fri"),
                    id=f"range_watchlist_{h:02d}{m:02d}",
                    name=f"震荡市观察列表-{h:02d}:{m:02d}",
                )

        # 日报推送
        period_map = {"09:10": "morning", "11:35": "noon", "15:10": "close"}
        task_map = {"morning": "signal_morning", "noon": "signal_noon", "close": "signal_close"}
        for t in report_times:
            h, m = t.split(":")
            period = period_map.get(t, "morning")
            if self._task_enabled(task_map.get(period, "signal_morning")):
                self._scheduler.add_job(
                    self._safe_report,
                    CronTrigger(hour=int(h), minute=int(m), day_of_week="mon-fri"),
                    args=[period],
                    id=f"report_{period}",
                    name=f"信号汇总-{period}",
                )

        # 通知重试队列：每 2 分钟
        self._scheduler.add_job(
            self._safe_retry_notify,
            IntervalTrigger(minutes=2),
            id="retry_notify",
            name="通知重试队列",
        )

        # Digest 汇总刷新：盘前一次、收盘全部业务报告完成后一次；Action 告警不延迟。
        if self._task_enabled("digest_flush"):
            for hour, minute, label in ((9, 12, "盘前"), (15, 40, "收盘")):
                self._scheduler.add_job(
                    self._safe_flush_digest,
                    CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri"),
                    id=f"digest_flush_{hour:02d}{minute:02d}",
                    name=f"{label}Digest汇总刷新-{hour:02d}:{minute:02d}",
                )

        # 宏观日报推送
        from gugu.macro.scheduler import register_macro_jobs

        register_macro_jobs(
            self._scheduler,
            broker=self._engine.broker,
            task_enabled=self._task_enabled,
        )

        # 另类因子数据盘后批量采集：18:00
        if self._task_enabled("altalpha_fetch"):
            self._scheduler.add_job(
                self._safe_altalpha_fetch,
                CronTrigger(hour=18, minute=0, day_of_week="mon-fri"),
                id="altalpha_fetch_1800",
                name="另类因子采集-18:00",
            )

        # Alpha因子综合评分报告：18:05
        if self._task_enabled("alpha_summary"):
            self._scheduler.add_job(
                self._safe_alpha_summary,
                CronTrigger(hour=18, minute=5, day_of_week="mon-fri"),
                id="alpha_summary_1805",
                name="因子综合评分报告-18:05",
            )

        # 持仓认知分析报告：15:35
        if self._task_enabled("portfolio_report"):
            self._scheduler.add_job(
                self._safe_portfolio_report,
                CronTrigger(hour=15, minute=35, day_of_week="mon-fri"),
                id="portfolio_report_1535",
                name="持仓认知分析-15:35",
            )

        # 舆情日报推送：20:30
        if self._task_enabled("sentiment_daily"):
            self._scheduler.add_job(
                self._safe_sentiment_daily,
                CronTrigger(hour=20, minute=30, day_of_week="mon-fri"),
                id="sentiment_daily_2030",
                name="情感日报-20:30",
            )

        # Parquet 缓存定期清理：07:00
        self._scheduler.add_job(
            self._safe_cleanup_cache,
            CronTrigger(hour=7, minute=0, day_of_week="mon-fri"),
            id="cache_cleanup_0700",
            name="缓存清理-07:00",
        )

        # P1-7: 周参数优化（周日 03:00）
        if self._task_enabled("weekly_optimize"):
            self._scheduler.add_job(
                self._safe_optimize_params,
                CronTrigger(hour=3, minute=0, day_of_week="sun"),
                id="weekly_optimize_0300",
                name="周参数优化-周日03:00",
            )

        # P1-7: 启发式参数优化（每半月 04:00，GA/PSO 交替）
        if self._task_enabled("heuristic_optimize"):
            self._scheduler.add_job(
                self._safe_heuristic_optimize,
                CronTrigger(hour=4, minute=0, day="1,15"),
                id="heuristic_optimize_0400",
                name="启发式优化-每月1/15日04:00",
            )

        # P2-8: 周日复盘推送（20:00）
        if self._task_enabled("weekly_review"):
            self._scheduler.add_job(
                self._safe_weekly_review,
                CronTrigger(hour=20, minute=0, day_of_week="sun"),
                id="weekly_review_2000",
                name="周复盘推送-周日20:00",
            )

        # 十大视角分析日报：08:30
        if self._task_enabled("perspective_daily"):
            self._scheduler.add_job(
                self._safe_perspective_report,
                CronTrigger(hour=8, minute=30, day_of_week="mon-fri"),
                id="perspective_report_0830",
                name="十大视角日报-08:30",
            )

        # 收盘后十大视角二次分析：15:30
        if self._task_enabled("perspective_afternoon"):
            self._scheduler.add_job(
                self._safe_perspective_report,
                CronTrigger(hour=15, minute=30, day_of_week="mon-fri"),
                id="perspective_report_1530",
                name="十大视角收盘分析-15:30",
            )

        # 市场温度计：08:25
        if self._task_enabled("market_temp"):
            self._scheduler.add_job(
                self._safe_market_temp,
                CronTrigger(hour=8, minute=25, day_of_week="mon-fri"),
                id="market_temp_0825",
                name="市场温度计-08:25",
            )

        # 信号绩效验证：15:20
        if self._task_enabled("signal_performance"):
            self._scheduler.add_job(
                self._safe_signal_performance,
                CronTrigger(hour=15, minute=20, day_of_week="mon-fri"),
                id="signal_performance_1520",
                name="信号绩效验证-15:20",
            )

        # 信号 T+1 次日验证：15:25
        if self._task_enabled("signal_daily_validation"):
            self._scheduler.add_job(
                self._safe_signal_daily_validation,
                CronTrigger(hour=15, minute=25, day_of_week="mon-fri"),
                id="signal_daily_validation_1525",
                name="信号次日验证-15:25",
            )

        # 信号绩效周报：周日 20:30
        if self._task_enabled("signal_weekly"):
            self._scheduler.add_job(
                self._safe_signal_weekly,
                CronTrigger(hour=20, minute=30, day_of_week="sun"),
                id="signal_weekly_2030",
                name="信号绩效周报-周日20:30",
            )

        # v0.5.0 盘中实时盯盘
        try:
            _cfg = get_config()
            _im_cfg = getattr(_cfg, "intraday_monitor", None)
            _im_enabled = getattr(_im_cfg, "enabled", True) if _im_cfg else True
        except Exception:
            _im_enabled = True

        if _im_enabled and self._task_enabled("intraday_monitor"):
            self._scheduler.add_job(
                self._safe_intraday_monitor,
                CronTrigger(minute="*"),
                id="intraday_monitor",
                name="盘中实时盯盘",
            )

        # P2: 配置热加载检测（每 5 分钟）
        self._scheduler.add_job(
            self._safe_check_config,
            CronTrigger(minute="*/5"),
            id="config_hot_reload",
            name="配置热加载检测-每5分钟",
        )

        logger.info("调度器配置完成: {} 个任务", len(self._scheduler.get_jobs()))
        for job in self._scheduler.get_jobs():
            logger.info("  {}: {} -> {}", job.id, job.name, job.trigger)

    async def _safe_run(self) -> None:
        """安全执行交易循环（非交易日跳过，熊市/震荡市降低扫描频率）。"""
        if not is_trading_day():
            logger.info("非交易日，跳过交易循环")
            return

        try:
            market_status = await self._engine.market_filter.check_market()
            regime = market_status.get("regime", "unknown")
            now = datetime.now()
            current_hour_min = (now.hour, now.minute)

            if regime in ("bear", "crash") and current_hour_min in [(10, 30), (13, 5)]:
                logger.info("市场状态={}，跳过盘中扫描（仅保留开盘/尾盘扫描）", regime)
                return
        except Exception:
            pass

        try:
            await self._engine.run_daily_cycle()
        except Exception as e:
            logger.exception("交易循环异常: {}", e)
            try:
                await self._engine.notifier.notify_error(
                    {
                        "message": f"交易循环异常: {e}",
                        "level": "error",
                        "source": "scheduler._safe_run",
                    }
                )
            except Exception as ne:
                logger.error("交易循环异常通知发送失败: {}", ne)

    async def _safe_intraday_monitor(self) -> None:
        """盘中实时盯盘（资金流异动 + 策略信号实时推荐，仅交易日盘中时段运行）。"""
        if self._intraday_monitor is None:
            return

        if not is_trading_day() or not is_trading_time():
            return

        try:
            portfolio_symbols = (
                list(self._engine.broker.positions.keys())
                if hasattr(self._engine.broker, "positions")
                else []
            )
            self._intraday_monitor._portfolio_symbols = portfolio_symbols

            alerts = await self._intraday_monitor.run_cycle()
            if alerts:
                await self._engine.notifier.notify_intraday_alert(alerts)

            # v0.9.0 实时推荐：盘中策略信号持续扫描（分钟级）
            if self._realtime_scanner is not None:
                try:
                    pushed = await self._realtime_scanner.run_cycle()
                    if pushed:
                        logger.info("实时推荐: 推送 {} 条新信号", len(pushed))
                except Exception as e:
                    logger.warning("实时推荐扫描异常: {}", e)
        except Exception as e:
            logger.exception("盘中盯盘异常: {}", e)

    async def _safe_report(self, period: str) -> None:
        """安全执行信号汇总推送。"""
        if not is_trading_day():
            return
        try:
            await self._engine.send_daily_report(period)
        except Exception as e:
            logger.exception("信号汇总推送异常: {}", e)
            try:
                await self._engine.notifier.notify_error(
                    {
                        "message": f"信号汇总推送异常: {e}",
                        "level": "error",
                        "source": "scheduler._safe_report",
                    }
                )
            except Exception as notify_err:
                logger.error("信号汇总异常通知发送失败: {}", notify_err)

    async def _safe_flush_digest(self) -> None:
        """刷新延迟 Digest；发送失败时保留缓冲等待下一次刷新。"""
        try:
            count = await self._engine.notifier.flush_all_digests()
            if count:
                logger.info("Digest 汇总通知刷新: {} 张", count)
        except Exception as exc:
            logger.warning("Digest 汇总刷新失败: {}", exc)

    async def _safe_retry_notify(self) -> None:
        """重试失败的通知队列。"""
        try:
            count = await self._engine.notifier.retry_queued()
            if count > 0:
                logger.info("通知重试: {} 条成功", count)
        except Exception as e:
            logger.debug("通知重试异常: {}", e)
        try:
            await self._engine.notifier.check_queue_health()
        except Exception as e:
            logger.debug("队列健康检查异常: {}", e)

    async def _safe_check_config(self) -> None:
        """P2: 配置热加载检测——检查 settings.yaml 是否被修改。"""
        try:
            from gugu.config import config_has_changed, refresh_config

            if config_has_changed():
                logger.info("检测到 settings.yaml 已修改，执行配置热加载...")
                new_cfg = refresh_config()
                logger.info(
                    f"配置热加载完成: 策略={new_cfg.strategy.enabled}, "
                    f"模式={new_cfg.execution.mode}"
                )
        except Exception as e:
            logger.debug("配置热加载检测异常: {}", e)

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, lambda: asyncio.create_task(self.shutdown()))
            except (NotImplementedError, RuntimeError, ValueError):
                logger.warning("无法注册信号处理器: {}", signum)

    async def start(self) -> None:
        """启动调度器。"""
        # v0.9.11 防线2：策略注册表 vs 配置一致性自检（幽灵策略 fail-fast）
        # fatal（配置启用但未注册）→ 拒绝启动；warning（死代码残留）→ 记日志
        try:
            from gugu.strategies.registry import list_strategies, verify_registry_consistency

            fatal, warning = verify_registry_consistency()
            if fatal:
                for issue in fatal:
                    logger.error("策略注册表一致性: {}", issue)
                raise RuntimeError(
                    f"策略注册表一致性检查失败（{len(fatal)} 项致命），拒绝启动。"
                    "请修正 settings.yaml 与 registry.py 后重启。"
                )
            for w in warning:
                logger.warning("策略注册表一致性: {}", w)
            logger.info(
                "策略注册表一致性检查通过（{} 策略，{} 项死代码残留待清理）",
                len(list_strategies()),
                len(warning),
            )
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning("策略注册表一致性检查跳过（非致命）: {}", e)

        self._install_signal_handlers()
        self.setup()
        self._scheduler.start()
        self._record_boot()
        logger.info("调度器已启动，按 Ctrl+C 退出")

        async def _heartbeat_loop() -> None:
            """每 60 秒写入心跳。"""
            while True:
                with contextlib.suppress(Exception):
                    self._engine.write_heartbeat("alive")
                await asyncio.sleep(60)

        self._shutdown_event = asyncio.Event()
        self._heartbeat_task = asyncio.create_task(_heartbeat_loop())

        # 心跳循环必须先于启动扫描创建（2026-08-10 修复实证）：数据源降级时
        # run_daily_cycle 可能耗时 5-10 分钟（当日两次重启均 ~6 分钟无心跳），
        # 若心跳循环在其后创建，watchdog 心跳阈值(15min)可能在慢扫描下误判挂死
        # 而触发 auto-recover 重启 → 重启风暴风险。先启动心跳，扫描期间心跳照常新鲜。
        if is_trading_day() and is_trading_time():
            logger.info("启动后立即执行一次盘中扫描（交易时段内）...")
            try:
                await self._engine.run_daily_cycle()
            except Exception as e:
                logger.exception("启动扫描异常: {}", e)
        elif is_trading_day():
            logger.info("启动时为交易日但非交易时段，跳过立即扫描")

        self._main_task = asyncio.current_task()
        try:
            while not self._shutdown_requested:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            await self.shutdown()
        except asyncio.CancelledError:
            if not self._shutdown_requested:
                await self.shutdown()
            return
        except BaseException:
            await self.shutdown()
            raise

    def _record_boot(self) -> None:
        """记录调度器启动（P0 watchdog 用）。

        写入 data/scheduler_boot.json：
        - boot_time: 本次启动时间
        - pid: 本次进程 PID
        - history: 最近 20 次启动历史（watchdog 用它检测重启风暴）
        """
        try:
            from gugu.config import PROJECT_ROOT

            boot_path = PROJECT_ROOT / "data" / "scheduler_boot.json"
            boot_path.parent.mkdir(exist_ok=True)
            now_iso = datetime.now().isoformat()
            pid = os.getpid()

            history: list[dict] = []
            try:
                if boot_path.exists():
                    import json as _json

                    data = _json.loads(boot_path.read_text(encoding="utf-8"))
                    history = data.get("history", [])
            except (OSError, ValueError):
                history = []

            history.append({"boot_time": now_iso, "pid": pid})
            history = history[-20:]  # 只保留最近 20 次

            import json as _json

            atomic_write_json(
                boot_path,
                {"boot_time": now_iso, "pid": pid, "history": history},
                ensure_ascii=False,
                indent=2,
            )
            logger.info("调度器启动记录已写入: {}", boot_path)
        except Exception as e:
            logger.warning("调度器启动记录写入失败: {}", e)

    async def shutdown(self) -> None:
        """停止调度器并释放资源。"""
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
        current_task = asyncio.current_task()
        if (
            self._main_task is not None
            and self._main_task is not current_task
            and not self._main_task.done()
        ):
            self._main_task.cancel()
        self._scheduler.shutdown(wait=False)
        await self._engine.shutdown()
        logger.info("调度器已停止")


def _main() -> None:
    """命令行入口：启动交易调度器守护进程。"""
    logging.basicConfig(level=logging.INFO)
    # P0 防护：单实例锁，防止与 launchd/手动启动的实例并行运行
    from gugu.utils.single_instance import acquire_single_instance_lock

    if not acquire_single_instance_lock():
        return
    scheduler = TradingScheduler()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(scheduler.start())


if __name__ == "__main__":
    _main()
