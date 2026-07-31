"""主引擎：交易系统核心。

整合数据层、策略层、风控、执行、通知、决策层。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from gugu.analysis.dynamic_take_profit import DynamicTakeProfitEngine
from gugu.analysis.position_controller import PositionController
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.analysis.trailing_stop import TrailingStopEngine
from gugu.config import PROJECT_ROOT, get_config
from gugu.config.models import AppConfig
from gugu.data import data_manager
from gugu.engine.engine_mixin import EngineOpsMixin
from gugu.engine.event_engine import (
    EVENT_CYCLE_END,
    EVENT_CYCLE_START,
    EVENT_DAILY_LOSS_HALT,
    EVENT_DAILY_LOSS_WARN,
    EVENT_ORDER_FILLED,
    EVENT_RISK_ALERT,
    EVENT_STOP_LOSS,
    EventEngine,
)
from gugu.engine.market_filter import MarketFilter
from gugu.engine.signal_pipeline import SignalPipeline
from gugu.engine.signal_router import SignalRouter
from gugu.execution import OrderManager, PaperBroker, StopLossManager
from gugu.execution.track_live import LivePositionTracker
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.industry_constraint import IndustryConstraint
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.notifier import FeishuNotifier
from gugu.notifier.daily_report_manager import DailyReportManager
from gugu.risk import RiskManager
from gugu.risk.rules import RiskAction
from gugu.strategies.registry import get_enabled_strategies
from gugu.utils.atomic import atomic_write_json
from gugu.utils.calendar import is_trading_day, is_trading_time
from gugu.utils.log import get_logger
from gugu.wisdom import WisdomAdvisor

logger = get_logger()


class TradingEngine(EngineOpsMixin):
    """交易主引擎。

    依赖注入支持：所有依赖参数可选，不传时使用默认实现（向后兼容）。
    传入参数时覆盖默认实现，便于测试 mock 和切换 broker。
    """

    # 告警抑制：类级别共享，同一股票同一原因的告警在冷却期内只发一次
    _alert_history: dict[str, float] = {}  # key: symbol_reason, value: timestamp
    _ALERT_COOLDOWN = 600  # 10分钟冷却

    def _should_alert(self, key: str) -> bool:
        """检查是否应该发送告警。

        两层去重：
        1. 每日去重（持久化到文件）：每天每个 key 只发一次，防止引擎重启后重复发送。
        2. 冷却期去重（内存）：10 分钟内同 key 不重复，防止单次运行期内刷屏。
        """
        now = time.time()
        today_str = date.today().isoformat()
        daily_key = f"{key}_{today_str}"

        # 定期清理过期条目，防止无限增长
        self._cleanup_alert_history()

        with self._alert_dedup_lock:
            # 每日持久化去重：今日已发送过则跳过
            if self._daily_alerts.get(daily_key):
                return False

            # 内存冷却去重
            last = self._alert_history.get(key, 0)
            if now - last < self._ALERT_COOLDOWN:
                return False

            self._alert_history[key] = now
            self._daily_alerts[daily_key] = "1"
            self._save_daily_alerts()
            return True

    def _cleanup_alert_history(self) -> None:
        """清理超过冷却期 2 倍的过期告警条目，防止内存无限增长。"""
        now = time.time()
        expiry = now - self._ALERT_COOLDOWN * 2
        stale_keys = [k for k, v in self._alert_history.items() if v < expiry]
        for k in stale_keys:
            del self._alert_history[k]
        if stale_keys:
            logger.debug("清理过期告警条目: {} 条", len(stale_keys))

    def _load_daily_alerts(self) -> dict[str, str]:
        """从文件加载今日已发送的告警 key（跨重启去重）。"""
        try:
            if self._alert_history_file.exists():
                data = json.loads(self._alert_history_file.read_text(encoding="utf-8"))
                alerts = data.get("daily_alerts", {})
                today_str = date.today().isoformat()
                return {k: v for k, v in alerts.items() if k.endswith(f"_{today_str}")}
        except Exception as e:
            logger.warning("加载告警去重记录失败: {}", e)
        return {}

    def _save_daily_alerts(self) -> None:
        """持久化今日已发送的告警 key（原子写入）。"""
        try:
            today_str = date.today().isoformat()
            daily_alerts = {
                k: v for k, v in self._daily_alerts.items() if k.endswith(f"_{today_str}")
            }
            atomic_write_json(
                self._alert_history_file,
                {"daily_alerts": daily_alerts},
                ensure_ascii=False,
                indent=2,
            )
        except Exception as e:
            logger.warning("保存告警去重记录失败: {}", e)

    def __init__(
        self,
        *,
        data_mgr: Any = None,
        broker: Any = None,
        notifier: Any = None,
        risk: Any = None,
        wisdom: Any = None,
        router: Any = None,
        selector: Any = None,
        regime_detector: Any = None,
        position_controller: Any = None,
        trailing_stop_engine: Any = None,
        take_profit_engine: Any = None,
        fundamental_filter: Any = None,
        money_flow_filter: Any = None,
        industry_constraint: Any = None,
        event_engine: Any = None,
        pipeline: Any = None,
        order_manager: Any = None,
        stop_loss_manager: Any = None,
        strategies: list | None = None,
        exec_mode: str | None = None,
        watchlist: list[str] | None = None,
        alert_history_file: Path | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._dedup_lock = asyncio.Lock()
        self._alert_dedup_lock = threading.Lock()
        self._running: bool = False

        self._app_config: AppConfig | None = None
        with contextlib.suppress(Exception):
            self._app_config = get_config()

        self._notifier = notifier if notifier is not None else FeishuNotifier()
        self._dm = data_mgr if data_mgr is not None else data_manager()

        cfg_mode = self._app_config.execution.mode if self._app_config else "paper"
        self._exec_mode = exec_mode if exec_mode is not None else cfg_mode

        if broker is not None:
            self._broker = broker
        elif self._exec_mode == "track_live":
            self._broker = LivePositionTracker()
            logger.info(
                "[track_live] 使用实盘持仓跟踪器，用户真实持仓从 data/live_positions.json 加载，"
                "系统不自动交易，仅推送止损/止盈提醒"
            )
        else:
            self._broker = PaperBroker()

        self._risk = risk if risk is not None else RiskManager()
        self._wisdom = wisdom if wisdom is not None else WisdomAdvisor()
        self._strategies = strategies if strategies is not None else get_enabled_strategies()
        self._router = router if router is not None else SignalRouter(self._strategies)
        if selector is not None:
            self._selector = selector
        else:
            from gugu.selector import StockSelector

            self._selector = StockSelector(
                self._dm,
                top_n=get_config().strategy.select_top_n,
                max_candidates=get_config().strategy.select_max_candidates,
            )
        self._regime_detector = (
            regime_detector if regime_detector is not None else MultiPeriodRegimeDetector()
        )
        self._position_controller = (
            position_controller if position_controller is not None else PositionController()
        )
        self._trailing_stop_engine = (
            trailing_stop_engine if trailing_stop_engine is not None else TrailingStopEngine()
        )
        self._take_profit_engine = (
            take_profit_engine if take_profit_engine is not None else DynamicTakeProfitEngine()
        )
        self._order_manager = (
            order_manager
            if order_manager is not None
            else OrderManager(
                trailing_stop_engine=self._trailing_stop_engine,
                take_profit_engine=self._take_profit_engine,
                should_alert=self._should_alert,
            )
        )
        self._stop_loss_manager = (
            stop_loss_manager
            if stop_loss_manager is not None
            else StopLossManager(
                trailing_stop_engine=self._trailing_stop_engine,
                take_profit_engine=self._take_profit_engine,
                should_alert=self._should_alert,
            )
        )

        self._stop_loss_escalator = None
        if self._exec_mode == "track_live":
            try:
                from gugu.analysis.stop_loss_escalator import StopLossAlertEscalator

                self._stop_loss_escalator = StopLossAlertEscalator()
                logger.info("[track_live] 止损告警升级器已启用")
            except Exception as e:
                logger.debug("止损告警升级器初始化跳过: {}", e)
        self._report_manager = DailyReportManager(
            broker=self._broker,
            risk=self._risk,
            data_manager=self._dm,
            notifier=self._notifier,
            regime_detector=self._regime_detector,
        )
        self._fundamental_filter = (
            fundamental_filter if fundamental_filter is not None else FundamentalFilter()
        )
        self._money_flow_filter = (
            money_flow_filter if money_flow_filter is not None else MoneyFlowFilter()
        )
        self._industry_constraint = (
            industry_constraint if industry_constraint is not None else IndustryConstraint()
        )
        self._event_engine = event_engine if event_engine is not None else EventEngine()
        self._market_filter = MarketFilter()

        self._notified_signals_file = PROJECT_ROOT / "data" / "notified_signals.json"
        self._notified_signals: dict[str, str] = self._load_notified_signals()

        self._alert_history_file = alert_history_file or PROJECT_ROOT / "data" / "alert_history.json"
        self._daily_alerts: dict[str, str] = self._load_daily_alerts()

        if watchlist is not None:
            self._watchlist = watchlist
        else:
            self._watchlist = self._load_watchlist()

        self._last_cycle_date: date | None = None
        self._filtered_signals_today: list[dict[str, Any]] = []

        self._pipeline = (
            pipeline
            if pipeline is not None
            else SignalPipeline(
                data_manager=self._dm,
                signal_router=self._router,
                wisdom_advisor=self._wisdom,
                regime_detector=self._regime_detector,
                position_controller=self._position_controller,
                fundamental_filter=self._fundamental_filter,
                money_flow_filter=self._money_flow_filter,
                industry_constraint=self._industry_constraint,
                exec_mode=self._exec_mode,
            )
        )

        self._event_engine.register(EVENT_RISK_ALERT, self._on_risk_alert)
        self._event_engine.register(EVENT_STOP_LOSS, self._on_stop_loss_triggered)
        self._event_engine.register(EVENT_ORDER_FILLED, self._on_order_filled)

        try:
            from gugu.strategy_consistency import validate_strategy_consistency

            consistency_issues = validate_strategy_consistency()
            if consistency_issues:
                logger.warning(
                    "策略一致性校验发现 {} 个问题，详见上方日志", len(consistency_issues)
                )
        except Exception as e:
            logger.debug("策略一致性校验跳过: {}", e)

        logger.info(
            "交易引擎初始化: {} 策略, 自选股 {} 只, 模式={}",
            len(self._strategies),
            len(self._watchlist),
            self._exec_mode,
        )

    @property
    def auto_select_enabled(self) -> bool:
        try:
            return get_config().strategy.auto_select
        except Exception:
            return False

    def _load_watchlist(self) -> list[str]:
        """加载自选股列表。"""
        try:
            watchlist = list(get_config().watchlist)
        except Exception:
            watchlist = []
        if not watchlist:
            logger.warning("未配置 watchlist，使用空自选股列表")
            return []
        return [str(code).strip().zfill(6) for code in watchlist]

    async def run_daily_cycle(self) -> None:
        """每日交易循环：采集 → 策略 → 风控 → 执行 → 通知。"""
        if self._risk.is_halted:
            logger.warning("L2 熔断状态激活中，跳过买入流程但仍执行止损检查")
            if not is_trading_day():
                logger.info("非交易日，跳过")
                return
            async with self._lock:
                if self._running:
                    logger.warning("交易循环正在运行中，跳过本次重入调用")
                    return
                self._running = True
                try:
                    await self._update_prices()
                    await self._check_stop_loss()
                    logger.info("熔断状态下止损检查完成，跳过信号扫描与买入")
                finally:
                    self._running = False
                    self._event_engine.put(EVENT_CYCLE_END, {"halted": True})
            return

        if not is_trading_day():
            logger.info("非交易日，跳过")
            return

        if not is_trading_time():
            logger.info("非交易时段，跳过买入流程，仅执行止损检查")
            async with self._lock:
                if self._running:
                    return
                self._running = True
                if not hasattr(self, "_event_engine"):
                    self._event_engine = EventEngine()
                try:
                    await self._update_prices()
                    await self._check_stop_loss()
                finally:
                    self._running = False
                    self._event_engine.put(EVENT_CYCLE_END, {"off_hours": True})
            return

        async with self._lock:
            if self._running:
                logger.warning("交易循环正在运行中，跳过本次重入调用")
                return
            self._running = True
            if not hasattr(self, "_event_engine"):
                self._event_engine = EventEngine()
            self._event_engine.put(EVENT_CYCLE_START, {"timestamp": pd.Timestamp.now().isoformat()})

            today = date.today()
            last_date = getattr(self._risk, "last_reset_date", None)
            if last_date is None:
                last_date = self._last_cycle_date.isoformat() if self._last_cycle_date else ""
            if last_date != today.isoformat():
                self._broker.settle_t_plus_1()
                self._broker.reset_daily_start_value()
                self._risk.reset_halt()
                self._last_cycle_date = today

            try:
                await self._update_prices()
                await self._check_stop_loss()

                if self._stop_loss_escalator:
                    await self._check_stop_loss_escalation()

                await self._check_daily_loss()
                if self._risk.is_halted:
                    logger.warning("止损后触发 L2 熔断，跳过本次信号扫描与下单")
                    self._write_heartbeat("halted")
                    return

                account_after_stop = self._broker.get_account()
                self._risk.update_portfolio_peak(account_after_stop.total_value)
                dd_after_stop = self._risk.check_portfolio_drawdown(account_after_stop.total_value)
                if dd_after_stop.action == RiskAction.HALT:
                    self._event_engine.put(EVENT_DAILY_LOSS_HALT, {"drawdown": True})
                    logger.warning("止损后组合回撤触发熔断: {}", dd_after_stop.message)
                    if self._should_alert("portfolio_drawdown_halt"):
                        await self._notifier.notify_risk_alert(
                            {
                                "level": "halt",
                                "message": dd_after_stop.message,
                                "suggestion": "组合回撤过大，已自动熔断",
                            }
                        )
                    self._write_heartbeat("halted")
                    return
                elif dd_after_stop.action == RiskAction.WARN:
                    logger.warning("止损后组合回撤预警: {}", dd_after_stop.message)

                today_extra: list[str] = []
                if self.auto_select_enabled:
                    if self._dm.is_degraded:
                        logger.info("数据源已降级，跳过自动选股")
                    else:
                        selected = await self._selector.select()
                        if not selected:
                            logger.info("自动选股未产生候选")
                        for s in selected:
                            if (
                                s["symbol"] not in self._watchlist
                                and s["symbol"] not in today_extra
                            ):
                                today_extra.append(s["symbol"])
                                logger.info("自动选股候选（当日）: {}", s["symbol"])

                signals = await self._scan_signals(today_extra)

                from gugu.analysis.signal_priority import sort_and_limit_signals

                account = self._broker.get_account()
                total_value = account.total_value if account else 1_000_000
                positions_value = account.positions_value if account else 0
                available_ratio = max(0, (total_value - positions_value) / total_value)
                current_regime = getattr(self, "_current_regime", "unknown")
                signals = sort_and_limit_signals(
                    signals,
                    regime=current_regime,
                    available_position_ratio=available_ratio,
                    strategy_pool=getattr(self._router, "_pool", None),
                )

                for signal in signals:
                    try:
                        await self._process_signal(signal)
                    except Exception as sig_err:
                        logger.exception(
                            "处理信号 {} 失败，跳过: {}", signal.get("symbol"), sig_err
                        )

                await self._check_daily_loss()
                if self._risk.is_halted:
                    logger.warning("信号处理后触发 L2 熔断，跳过本轮后续检查")
                    self._write_heartbeat("halted")
                    return

                account_after = self._broker.get_account()
                self._risk.update_portfolio_peak(account_after.total_value)
                dd_result = self._risk.check_portfolio_drawdown(account_after.total_value)
                if dd_result.action == RiskAction.HALT:
                    self._event_engine.put(EVENT_DAILY_LOSS_HALT, {"drawdown": True})
                    logger.warning("信号处理后组合回撤触发熔断: {}", dd_result.message)
                    if self._should_alert("portfolio_drawdown_halt"):
                        await self._notifier.notify_risk_alert(
                            {
                                "level": "halt",
                                "message": dd_result.message,
                                "suggestion": "组合回撤过大，已自动熔断",
                            }
                        )
                elif dd_result.action == RiskAction.WARN:
                    logger.warning("信号处理后组合回撤预警: {}", dd_result.message)

                self._event_engine.put(
                    EVENT_CYCLE_END,
                    {
                        "status": "ok",
                        "positions_count": len(self._broker.get_portfolio()),
                        "signals_count": len(signals),
                    },
                )
                self._write_heartbeat("ok")
            except Exception as e:
                logger.exception("交易循环异常: {}", e)
                self._write_heartbeat("error")
                try:
                    await self._notifier.notify_error(
                        {
                            "module": "engine.run_daily_cycle",
                            "message": str(e)[:500],
                            "suggestion": "检查日志 logs/gugu_*.log",
                        }
                    )
                except Exception as notify_err:
                    logger.error("异常告警通知失败: {}", notify_err)
            finally:
                self._running = False

    async def _update_prices(self) -> None:
        """更新持仓现价。"""
        portfolio = self._broker.get_portfolio()
        if not portfolio:
            return
        symbols = list(portfolio.keys())
        try:
            df = await self._dm.fetch_stock_realtime(symbols)
            for _, row in df.iterrows():
                try:
                    symbol = row["symbol"]
                    price = float(row["price"])
                    if price > 0:
                        self._broker.update_price(symbol, price)
                    else:
                        logger.warning("现价更新跳过 {}: price={}", symbol, price)
                except (ValueError, KeyError, TypeError) as row_err:
                    logger.warning("解析现价行失败，跳过该行: {}", row_err)
        except Exception as e:
            logger.error("更新现价失败: {}", e)

    async def _check_stop_loss(self) -> None:
        """遍历持仓，若现价触及止损价则执行卖出。"""
        await self._stop_loss_manager.execute(
            broker=self._broker,
            risk=self._risk,
            notifier=self._notifier,
            event_engine=self._event_engine,
            data_mgr=self._dm,
        )

    async def _check_daily_loss(self) -> None:
        """检查当日亏损（以日初净值为基准）。"""
        account = self._broker.get_account()
        start_value = self._broker.daily_start_value
        if start_value <= 0:
            start_value = get_config().execution.paper.initial_capital
        loss_pct = (start_value - account.total_value) / start_value

        risk_result = self._risk.check_daily_loss(loss_pct)
        if risk_result.action == RiskAction.WARN:
            self._event_engine.put(EVENT_DAILY_LOSS_WARN, {"loss_pct": loss_pct})
            if self._should_alert("daily_loss_warn"):
                notify_alert_ok = await self._notifier.notify_risk_alert(
                    {
                        "level": "warn",
                        "message": f"当日亏损 {loss_pct:.2%} 触发预警",
                        "suggestion": "关注持仓，考虑减仓",
                    }
                )
                if not notify_alert_ok:
                    logger.error("日亏预警（{:.2%}）通知失败", loss_pct)
        elif risk_result.action == RiskAction.HALT:
            self._event_engine.put(EVENT_DAILY_LOSS_HALT, {"loss_pct": loss_pct})
            if self._should_alert("daily_loss_halt"):
                notify_alert_ok = await self._notifier.notify_risk_alert(
                    {
                        "level": "halt",
                        "message": f"当日亏损 {loss_pct:.2%} 触发熔断，停止交易",
                        "suggestion": "检查持仓，必要时一键平仓",
                    }
                )
                if not notify_alert_ok:
                    logger.error("日亏熔断（{:.2%}）通知失败", loss_pct)

    async def send_daily_report(self, period: str) -> None:
        """发送信号汇总报告。委托给 DailyReportManager。"""
        filtered = self._filtered_signals_today if period == "close" else []
        await self._report_manager.send_report(
            period=period,
            check_holdings_sell=self._check_holdings_sell_signals,
            engine_lock=self._lock,
            engine_running=self._running,
            filtered_signals=filtered,
        )

    def _load_today_signals(self) -> list[dict[str, Any]]:
        """从 signals_history.jsonl 读取今日信号。委托给 DailyReportManager。"""
        return DailyReportManager._load_today_signals()

    async def shutdown(self) -> None:
        """关闭引擎资源。"""
        await self._notifier.close()
        self._write_heartbeat("shutdown")

    def reset_halt(self) -> None:
        """手动解除 L2 熔断。"""
        self._risk.clear_halt_only()
        self._clear_today_notified_signals()
        logger.warning(
            "L2 熔断状态已手动解除（clear_halt_only），已清除当日去重记录，"
            "下一轮扫描将补发熔断期间错过的信号"
        )


def run_engine() -> None:
    """运行交易引擎（同步入口）。"""
    engine = TradingEngine()
    asyncio.run(engine.run_daily_cycle())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_engine()


# Backward compatibility re-export for conftest.py monkeypatch
from gugu.engine.signal_pipeline import record_signal_history  # noqa: E402,F401
