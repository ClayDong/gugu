"""主引擎：交易系统核心。

整合数据层、策略层、风控、执行、通知、决策层。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import date
from typing import Any

import pandas as pd

from gugu.analysis.position_controller import PositionController
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.analysis.signal_tracker import SignalTracker
from gugu.analysis.trailing_stop import TrailingStopEngine, TrailingStopSignal
from gugu.analysis.dynamic_take_profit import DynamicTakeProfitEngine
from gugu.config import PROJECT_ROOT, get_config
from gugu.config.models import AppConfig
from gugu.data import data_manager
from gugu.data.stock_names import lookup_stock_name
from gugu.engine.event_engine import (
    EVENT_CYCLE_END,
    EVENT_CYCLE_START,
    EVENT_DAILY_LOSS_HALT,
    EVENT_DAILY_LOSS_WARN,
    EVENT_MARKET_REGIME,
    EVENT_ORDER_FILLED,
    EVENT_ORDER_SUBMITTED,
    EVENT_RISK_ALERT,
    EVENT_SIGNAL,
    EVENT_STOP_LOSS,
    EventEngine,
)
from gugu.engine.market_filter import MarketFilter
from gugu.engine.signal_pipeline import SignalPipeline, record_signal_history
from gugu.engine.signal_router import SignalRouter
from gugu.execution import PaperBroker
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.industry_constraint import IndustryConstraint
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.models import Position  # forward reference for ruff
from gugu.notifier import FeishuNotifier
from gugu.risk import RiskManager
from gugu.risk.rules import RiskAction
from gugu.selector import StockSelector
from gugu.strategies.registry import get_enabled_strategies
from gugu.utils.calendar import is_trading_day, is_trading_time
from gugu.utils.log import get_logger
from gugu.wisdom import WisdomAdvisor

logger = get_logger()


class TradingEngine:
    """交易主引擎。

    依赖注入支持：所有依赖参数可选，不传时使用默认实现（向后兼容）。
    传入参数时覆盖默认实现，便于测试 mock 和切换 broker。
    """

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
        strategies: list | None = None,
        exec_mode: str | None = None,
        watchlist: list[str] | None = None,
    ) -> None:
        # P1 修复：_lock 必须在可能跳过后续初始化的 mock 测试前初始化
        self._lock = asyncio.Lock()
        self._running: bool = False

        # 读取配置
        self._app_config: AppConfig | None = None
        with contextlib.suppress(Exception):
            self._app_config = get_config()

        # ---- 依赖注入：若传入则用传入的，否则创建默认实现 ----
        self._dm = data_mgr if data_mgr is not None else data_manager()
        self._broker = broker if broker is not None else PaperBroker()
        self._notifier = notifier if notifier is not None else FeishuNotifier()
        self._risk = risk if risk is not None else RiskManager()
        self._wisdom = wisdom if wisdom is not None else WisdomAdvisor()
        self._strategies = strategies if strategies is not None else get_enabled_strategies()
        self._router = router if router is not None else SignalRouter(self._strategies)
        self._selector = selector if selector is not None else StockSelector(
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
        # 财叔动态止盈减仓引擎（与移动止损互补：止损保本金，止盈锁利润）
        self._take_profit_engine = (
            take_profit_engine if take_profit_engine is not None else DynamicTakeProfitEngine()
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

        # 信号去重：{symbol_direction_strategy: date} 当日已通知的信号不重复发送
        # 持久化到文件，防止引擎重启后丢失
        self._notified_signals_file = PROJECT_ROOT / "data" / "notified_signals.json"
        self._notified_signals: dict[str, str] = self._load_notified_signals()

        # 自选股列表
        if watchlist is not None:
            self._watchlist = watchlist
        else:
            self._watchlist = self._load_watchlist()

        # 执行模式
        cfg_mode = self._app_config.execution.mode if self._app_config else "paper"
        self._exec_mode = exec_mode if exec_mode is not None else cfg_mode

        # P1 修复：_last_cycle_date 必须在 run_daily_cycle 使用前初始化
        self._last_cycle_date: date | None = None

        # 信号过滤流水线
        self._pipeline = pipeline if pipeline is not None else SignalPipeline(
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

        # 注册事件处理器
        self._event_engine.register(EVENT_RISK_ALERT, self._on_risk_alert)
        self._event_engine.register(EVENT_STOP_LOSS, self._on_stop_loss_triggered)
        self._event_engine.register(EVENT_ORDER_FILLED, self._on_order_filled)

        logger.info(
            f"交易引擎初始化: {len(self._strategies)} 策略, "
            f"自选股 {len(self._watchlist)} 只, 模式={self._exec_mode}"
        )

    @property
    def auto_select_enabled(self) -> bool:
        try:
            return get_config().strategy.auto_select
        except Exception:
            return False

    def _load_watchlist(self) -> list[str]:
        """加载自选股列表（从 AppConfig 强类型配置读取）。"""
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
        # P0 修复：L2 熔断状态下仍需执行止损检查（卖出豁免），仅跳过买入相关流程
        # 防止熔断后无法止损导致亏损从15%扩大到30%+
        if self._risk.is_halted:
            logger.warning("L2 熔断状态激活中，跳过买入流程但仍执行止损检查（需人工 reset_halt 后恢复买入）")
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

        # P0 修复：交易时段校验（原逻辑仅校验交易日，盘前盘后/午休也会"成交"）
        # 止损检查豁免（卖出随时可执行），仅买入流程需在交易时段内
        if not is_trading_time():
            logger.info("非交易时段（盘前/午休/盘后），跳过买入流程，仅执行止损检查")
            async with self._lock:
                if self._running:
                    return
                self._running = True
                # 确保事件引擎存在（兼容测试中跳过 __init__ 的场景）
                if not hasattr(self, "_event_engine"):
                    self._event_engine = EventEngine()
                try:
                    await self._update_prices()
                    await self._check_stop_loss()
                finally:
                    self._running = False
                    self._event_engine.put(EVENT_CYCLE_END, {"off_hours": True})
            return

        # P1 修复：使用 asyncio.Lock 确保整个循环的并发安全
        async with self._lock:
            if self._running:
                logger.warning("交易循环正在运行中，跳过本次重入调用")
                return
            self._running = True
            # 确保事件引擎存在（兼容测试中跳过 __init__ 的场景）
            if not hasattr(self, "_event_engine"):
                self._event_engine = EventEngine()
            self._event_engine.put(EVENT_CYCLE_START, {"timestamp": pd.Timestamp.now().isoformat()})

            # 1. T+1 结算 + 日初净值重置（仅新交易日执行，防止重启后绕过 T+1）
            # P0-1/P1-a 修复：使用 risk.last_reset_date 持久化状态判断新交易日，
            # 若风险管理器不支持（如 mock 测试），回退到 _last_cycle_date 内存判断。
            today = date.today()
            last_date = getattr(self._risk, "last_reset_date", None)
            if last_date is None:
                last_date = self._last_cycle_date.isoformat() if self._last_cycle_date else ""
            if last_date != today.isoformat():
                self._broker.settle_t_plus_1()
                self._broker.reset_daily_start_value()  # P0-1: 每日重置日初净值
                self._risk.reset()
                self._last_cycle_date = today

            try:
                # 2. 采集行情
                await self._update_prices()

                # 2.5 组合回撤检查（T+1 结算后、止损前）
                # 先更新组合峰值，再检查回撤是否触发熔断
                account_snapshot = self._broker.get_account()
                self._risk.update_portfolio_peak(account_snapshot.total_value)
                drawdown_result = self._risk.check_portfolio_drawdown(account_snapshot.total_value)
                if drawdown_result.action == RiskAction.HALT:
                    self._event_engine.put(EVENT_DAILY_LOSS_HALT, {"drawdown": True})
                    logger.warning(f"组合回撤触发熔断: {drawdown_result.message}")
                    await self._notifier.notify_risk_alert({
                        "level": "halt",
                        "message": drawdown_result.message,
                        "suggestion": "组合回撤过大，已自动熔断，需人工 reset_halt 后恢复",
                    })
                    self._write_heartbeat("halted")
                    return
                elif drawdown_result.action == RiskAction.WARN:
                    logger.warning(f"组合回撤预警: {drawdown_result.message}")

                # 3. 持仓止损检查：先止损再扫描信号（A-05 修复）
                # 止损卖出会改变持仓与资金，必须在信号扫描前执行，
                # 确保后续信号基于最新持仓状态生成
                await self._check_stop_loss()

                # 3.5 止损后立即检查日亏 & 组合回撤（P1-d 修复）
                # 止损可能造成大亏，若不先检查则后续买单会在已熔断状态下继续执行
                await self._check_daily_loss()
                if self._risk.is_halted:
                    logger.warning("止损后触发 L2 熔断，跳过本次信号扫描与下单")
                    self._write_heartbeat("halted")
                    return
                # 止损后净值可能进一步下降，需再次检查组合回撤
                account_after_stop = self._broker.get_account()
                self._risk.update_portfolio_peak(account_after_stop.total_value)
                dd_after_stop = self._risk.check_portfolio_drawdown(account_after_stop.total_value)
                if dd_after_stop.action == RiskAction.HALT:
                    self._event_engine.put(EVENT_DAILY_LOSS_HALT, {"drawdown": True})
                    logger.warning(f"止损后组合回撤触发熔断: {dd_after_stop.message}")
                    await self._notifier.notify_risk_alert({
                        "level": "halt",
                        "message": dd_after_stop.message,
                        "suggestion": "组合回撤过大，已自动熔断，需人工 reset_halt 后恢复",
                    })
                    self._write_heartbeat("halted")
                    return
                elif dd_after_stop.action == RiskAction.WARN:
                    logger.warning(f"止损后组合回撤预警: {dd_after_stop.message}")

                # 4. 自动选股 + 自选股扫描
                # 当日自动选股候选：仅作用于本次扫描，不写入实例 watchlist（A-01 修复）
                today_extra: list[str] = []
                if self.auto_select_enabled:
                    # 降级源不支持全市场快照，降级时跳过自动选股
                    if self._dm.is_degraded:
                        logger.info("数据源已降级，跳过自动选股（降级源不支持全市场快照）")
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
                                logger.info(f"自动选股候选（当日）: {s['symbol']}")

                signals = await self._scan_signals(today_extra)

                # 5. 执行信号（风控 + 下单）—— per-signal 异常隔离（P1-n 修复）
                # 单个信号处理失败不影响其他信号执行
                for signal in signals:
                    try:
                        await self._process_signal(signal)
                    except Exception as sig_err:
                        logger.exception(f"处理信号 {signal.get('symbol')} 失败，跳过: {sig_err}")

                # 6. 检查日亏 & 组合回撤
                await self._check_daily_loss()

                # 6.5 信号处理后再次检查组合回撤（账户净值已因订单执行而变化）
                account_after = self._broker.get_account()
                self._risk.update_portfolio_peak(account_after.total_value)
                dd_result = self._risk.check_portfolio_drawdown(account_after.total_value)
                if dd_result.action == RiskAction.HALT:
                    self._event_engine.put(EVENT_DAILY_LOSS_HALT, {"drawdown": True})
                    logger.warning(f"信号处理后组合回撤触发熔断: {dd_result.message}")
                    await self._notifier.notify_risk_alert({
                        "level": "halt",
                        "message": dd_result.message,
                        "suggestion": "组合回撤过大，已自动熔断，需人工 reset_halt 后恢复",
                    })
                elif dd_result.action == RiskAction.WARN:
                    logger.warning(f"信号处理后组合回撤预警: {dd_result.message}")

                # 事件推送：交易循环完成
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
                logger.exception(f"交易循环异常: {e}")
                # 异常路径：持久化 error 心跳 + 主动飞书告警
                self._write_heartbeat("error")
                try:
                    await self._notifier.notify_error(
                        {
                            "module": "engine.run_daily_cycle",
                            "message": str(e)[:500],
                            "suggestion": "检查日志 logs/gugu_*.log，必要时手动 reset_halt 或排查数据源",
                        }
                    )
                except Exception as notify_err:
                    logger.error(f"异常告警通知失败: {notify_err}")
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
            # P1-e 修复：per-row try/except，单行失败不中断其余持仓现价更新
            for _, row in df.iterrows():
                try:
                    symbol = row["symbol"]
                    price = float(row["price"])
                    if price > 0:
                        self._broker.update_price(symbol, price)
                    else:
                        logger.warning(f"现价更新跳过 {symbol}: price={price}")
                except (ValueError, KeyError, TypeError) as row_err:
                    logger.warning(f"解析现价行失败，跳过该行: {row_err}")
        except Exception as e:
            logger.error(f"更新现价失败: {e}")

    async def _scan_signals(self, extra_symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """扫描自选股策略信号，经过滤链后输出。

        过滤链：策略信号 → 基本面过滤 → 资金流过滤 → 行业约束 → 市场状态修正 → wisdom 决策

        Args:
            extra_symbols: 当日自动选股产生的候选（仅本次扫描有效，不污染 watchlist）。
        """
        # 合并 watchlist + 当日候选（去重）
        scan_list = list(self._watchlist)
        for s in extra_symbols or []:
            if s not in scan_list:
                scan_list.append(s)

        # 大市过滤：《炒股的智慧》铁律——大市不满足顺势条件时仅观察不选股
        # 但 watchlist 自选股仍需扫描（用户手动关注的股票不应被大市过滤阻塞）
        market_status = await self._market_filter.check_market()
        if not market_status["bullish"]:
            logger.warning(
                f"大市过滤: {market_status['regime']} — {market_status['reason']}，"
                f"跳过自动选股候选，仅扫描 watchlist（{len(self._watchlist)} 只自选股）"
            )
            # 大市不佳时，仅扫描 watchlist，不扫描 extra_symbols（自动选股候选）
            scan_list = list(self._watchlist)
            extra_symbols = []  # 清空自动选股候选

        # 市场状态判断（每日一次，影响所有信号的仓位修正）
        regime = await self._regime_detector.detect()
        logger.info(f"市场择时: {regime['reason']}")

        # 计算仓位预算
        budget = self._position_controller.calculate(
            regime=regime,
            account=self._broker.get_account(),
            is_halted=self._risk.is_halted,
        )
        logger.info(
            f"仓位预算: 总上限={budget.total_limit:.0%}, "
            f"单股上限={budget.single_limit:.0%}, {budget.reason}"
        )
        self._event_engine.put(
            EVENT_MARKET_REGIME,
            {
                "regime": regime.get("regime", "unknown"),
                "reason": regime.get("reason", ""),
                "total_limit": budget.total_limit,
                "single_limit": budget.single_limit,
            },
        )

        signals = []
        # 批量拉取实时行情一次，避免 per-symbol 重复请求（P-04 修复）
        try:
            rt_all = await self._dm.fetch_stock_realtime(scan_list) if scan_list else None
        except Exception:
            rt_all = None

        # P3 优化：异步并发扫描，限制并发数避免过载
        from gugu.config import get_config as _get_config

        max_concurrency = getattr(_get_config().execution, "scan_concurrency", 10)
        semaphore = asyncio.Semaphore(max_concurrency)

        # P3 优化（2026-07）：批量预取所有股票的历史数据，减少竞争
        # 先批量获取所有待扫描股票的历史数据，再并发处理
        if scan_list and len(scan_list) > 5:
            logger.info(f"批量预取 {len(scan_list)} 只股票历史数据...")
            batch_history = {}
            for sym in scan_list:
                try:
                    df = await self._dm.fetch_stock_history(sym, days=90)
                    if not df.empty:
                        batch_history[sym] = df
                except Exception:
                    continue
            logger.info(f"批量预取完成: {len(batch_history)}/{len(scan_list)}")
        else:
            batch_history = {}

        async def _scan_single(symbol: str) -> dict[str, Any] | None:
            """扫描单只股票（供并发调用）。"""
            async with semaphore:
                try:
                    # 优先使用预取数据
                    df = batch_history.get(symbol)
                    if df is None:
                        df = await self._dm.fetch_stock_history(symbol, days=90)
                    if df is None or df.empty:
                        return None

                    meta = await self._dm.fetch_stock_meta(symbol)
                    portfolio = self._broker.get_portfolio()
                    account = self._broker.get_account()

                    signal = await self._pipeline.process(
                        symbol=symbol,
                        df=df,
                        meta=meta,
                        budget=budget,
                        rt_all=rt_all,
                        watchlist=self._watchlist,
                        portfolio=portfolio,
                        account=account,
                        regime=regime["regime"],
                    )
                    if signal is None:
                        return None

                    # 市场 regime 覆盖
                    signal["market_context"]["regime"] = regime["regime"]
                    # 注入原始 df 数据，供 _process_signal 中的 ATR 自适应移动止损使用
                    signal["_df"] = df

                    # L3 市场状态仓位修正信息记录
                    if (
                        signal["direction"] == "buy"
                        and not signal.get("wisdom_filtered")
                        and regime.get("sell_signal_required")
                    ):
                        logger.info(
                            f"{symbol} 市场状态 {regime['regime']} 建议减仓，"
                            f"仓位上限已由 budget 限制为 {signal['suggested_position_ratio']:.2%}"
                        )

                    return signal
                except Exception as e:
                    logger.error(f"扫描 {symbol} 失败: {e}")
                    return None

        # 并发执行所有扫描任务
        if scan_list:
            tasks = [_scan_single(sym) for sym in scan_list]
            results = await asyncio.gather(*tasks)
            signals = [s for s in results if s is not None]

        return signals

    def _load_notified_signals(self) -> dict[str, str]:
        """从文件加载已通知信号记录（持久化去重）。"""
        try:
            if self._notified_signals_file.exists():
                with open(self._notified_signals_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 清理过期记录（只保留今日的）
                today_str = date.today().isoformat()
                # P0 修复：清理旧格式 key（含 strategy_key 后缀的 3 段下划线格式）
                # 旧格式如 "600519_sell_money_flow_follow" 会导致新代码用
                # "{symbol}_{direction}" 查不到记录，去重失效
                cleaned = {
                    k: v for k, v in data.items()
                    if v == today_str and k.count("_") <= 1
                }
                if len(cleaned) != len(data):
                    logger.info(
                        f"清理旧格式去重 key: {len(data)} → {len(cleaned)} 条"
                    )
                return cleaned
        except Exception as e:
            logger.warning(f"加载已通知信号记录失败: {e}")
        return {}

    def _save_notified_signals(self) -> None:
        """保存已通知信号记录到文件（P0 修复：原子写入）。"""
        try:
            from gugu.utils.atomic import atomic_write_json
            atomic_write_json(
                self._notified_signals_file,
                self._notified_signals,
                ensure_ascii=False,
            )
        except Exception as e:
            logger.warning(f"保存已通知信号记录失败: {e}")

    def _mark_notified(self, signal: dict[str, Any]) -> None:
        """记录信号已通知，防止多轮扫描重复发送。"""
        symbol = signal["symbol"]
        direction = signal["direction"]
        # P0 修复：去重键去掉 strategy_key — 同股同方向当日只推一次
        # 原逻辑含 strategy_key，导致同股同方向但不同策略的信号不被去重
        dedup_key = f"{symbol}_{direction}"
        self._notified_signals[dedup_key] = date.today().isoformat()
        # 持久化到文件
        self._save_notified_signals()

    async def _process_signal(self, signal: dict[str, Any]) -> None:
        """处理单个信号：signal_only 模式只通知不下单，paper/live 模式走风控+下单+通知。"""
        symbol = signal["symbol"]
        direction = signal["direction"]
        price = signal.get("price", 0)

        # 信号去重：同股同方向当日已通知的信号不重复发送（防止多轮扫描重复推送）
        if not hasattr(self, "_notified_signals"):
            self._notified_signals = {}
        # P0 修复：去重键去掉 strategy_key — 同股同方向当日只推一次
        dedup_key = f"{symbol}_{direction}"
        today_str = date.today().isoformat()
        if self._notified_signals.get(dedup_key) == today_str:
            logger.info(
                f"{symbol} {direction} 信号已通知过，跳过重复发送"
            )
            return

        # signal_only 模式：只发信号通知，不下单（监控验证用）
        if self._exec_mode == "signal_only":
            logger.info(f"{symbol} {direction} 信号（signal_only 模式，仅通知不下单）")
            # 前置硬校验：避免脏数据进入通知和历史记录
            try:
                sig_price = float(signal.get("price", 0)) if signal.get("price") is not None else 0.0
            except (TypeError, ValueError):
                sig_price = 0.0
            sig_confidence = signal.get("confidence")
            sig_strategies = signal.get("strategies", [])
            if sig_price <= 1.0:
                logger.error(f"{symbol} signal_only 模式价格异常 price={sig_price}，拒绝通知并跳过历史记录")
                return
            if sig_confidence is None or (isinstance(sig_confidence, (int, float)) and sig_confidence <= 0):
                logger.error(f"{symbol} signal_only 模式置信度异常 confidence={sig_confidence}，拒绝通知并跳过历史记录")
                return
            if not sig_strategies or (isinstance(sig_strategies, list) and len(sig_strategies) == 0):
                logger.error(f"{symbol} signal_only 模式策略为空，拒绝通知并跳过历史记录")
                return

            # 记录信号历史（不下单，order_result=None）
            try:
                record_signal_history(signal, None, None)
            except Exception as e:
                logger.warning(f"记录信号历史失败: {e}")
            self._inject_performance_summary(signal)
            notify_ok = await self._notifier.notify_signal(signal)
            if notify_ok:
                self._mark_notified(signal)
            else:
                logger.error(f"{symbol} 信号通知失败，请检查飞书配置")
            return

        # 入场过滤：wisdom 判定低置信度，仅通知不下单
        if signal.get("wisdom_filtered"):
            logger.info(f"{symbol} 信号被 wisdom 入场过滤，仅通知不下单")
            # P1-1 修复：被 wisdom 过滤的信号也记录历史，确保绩效分析无盲区
            try:
                record_signal_history(signal, None, None)
            except Exception as e:
                logger.warning(f"记录 wisdom 过滤信号历史失败: {e}")
            notify_ok = await self._notifier.notify_signal(signal)
            if notify_ok:
                self._mark_notified(signal)
            # P1-o 修复：统一通知失败处理，与下单后通知逻辑一致
            else:
                logger.error(f"{symbol} wisdom 过滤信号通知失败，请检查飞书配置与网络")
            return

        # 使用 wisdom 调整后的仓位比例（已在 _scan_signals 中设置）
        suggested_ratio = signal.get("suggested_position_ratio", 0.0)
        if suggested_ratio <= 0:
            suggested_ratio = get_config().risk.max_position_ratio * 0.8

        # ATR 波动率调整仓位（风险平价）
        df_for_sizing = signal.get("_df")
        if df_for_sizing is not None and not df_for_sizing.empty and price > 0:
            try:
                from gugu.analysis.technical import atr as calc_atr

                atr_value = float(calc_atr(df_for_sizing, period=14).iloc[-1])
                if atr_value > 0:
                    atr_ratio = atr_value / price
                    # 将仓位与波动率反比调整：ATR/price 越大，仓位越低
                    if atr_ratio > 0.03:
                        suggested_ratio *= 0.7
                    elif atr_ratio < 0.01:
                        suggested_ratio *= 1.3
                    # 安全护栏
                    suggested_ratio = max(0.02, min(0.30, suggested_ratio))
                    signal["atr"] = round(atr_value, 3)
                    signal["atr_ratio"] = round(atr_ratio, 4)
            except Exception:
                pass

        account = self._broker.get_account()
        target_value = account.total_value * suggested_ratio
        quantity = int(target_value / price / 100) * 100 if price > 0 else 0

        # P1-i 修复：price 合法性校验前置，避免负数参与计算
        if price <= 0:
            logger.warning(f"{symbol} 信号价格异常 price={price}，跳过下单并发送告警")
            notify_alert_ok = await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"{symbol} 信号价格异常 price={price}，已跳过下单",
                    "suggestion": "检查数据源是否返回异常价格（0/负值）",
                }
            )
            if not notify_alert_ok:
                logger.error(f"{symbol} 价格异常告警通知失败")
            return

        # 最低1手保底：试仓比例过小时（如高价股），确保至少买入100股
        # P1-i 修复：保底前校验 1 手金额不超过 L1 单股上限，避免绕过 wisdom 仓位意图
        max_ratio = get_config().risk.max_position_ratio
        one_lot_value = price * 100
        if quantity <= 0 and account.cash >= one_lot_value:
            # 仅当 1 手金额不超过总资产的 max_ratio 时才保底
            if one_lot_value / account.total_value <= max_ratio:
                quantity = 100
                logger.info(f"{symbol} 试仓比例过小，使用最低1手保底: 100股")
            else:
                logger.warning(
                    f"{symbol} 1手金额 {one_lot_value:.0f} 超过单股上限 {max_ratio:.0%}，"
                    f"不保底（避免绕过 wisdom 仓位意图）"
                )

        if quantity <= 0:
            logger.warning(f"{symbol} 计算下单数量为 0，跳过并发送告警")
            notify_alert_ok = await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"{symbol} 计算下单数量为 0（price={price}, ratio={suggested_ratio:.2%}, cash={account.cash:.0f}），已跳过",
                    "suggestion": "检查仓位比例配置或资金是否充足",
                }
            )
            if not notify_alert_ok:
                logger.error(f"{symbol} 数量为0告警通知失败")
            return

        # 风控检查（传入 L3 元数据，确保涨跌停/停牌/ST 不被绕过）
        portfolio = self._broker.get_portfolio()
        risk_result = self._risk.check_order(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            price=price,
            portfolio=portfolio,
            cash=account.cash,
            prev_close=signal.get("prev_close"),
            is_st=bool(signal.get("is_st", False)),
            is_suspended=bool(signal.get("is_suspended", False)),
        )

        if not risk_result.allowed:
            logger.warning(f"{symbol} 风控拦截: {risk_result.message}")
            notify_alert_ok = await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"{symbol} {direction} 被风控拦截: {risk_result.message}",
                    "suggestion": "检查仓位/日亏/涨跌停",
                }
            )
            if not notify_alert_ok:
                logger.error(f"{symbol} 风控拦截告警通知失败")
            return

        # 下单
        result = self._broker.order(symbol, direction, quantity, price)

        # 买入成功后，将止损价与 L3 元数据注入 Position，供后续止损检查使用
        if result.success and direction == "buy":
            pos = self._broker.get_position(symbol)
            if pos is not None:
                if signal.get("prev_close"):
                    pos.prev_close = float(signal["prev_close"])
                pos.is_st = bool(signal.get("is_st", False))
                pos.is_suspended = bool(signal.get("is_suspended", False))

                # 初始化移动止损状态（优先 ATR 自适应，wisdom 止损作上限补齐）
                df_for_stop = (
                    signal.pop("_df", None) if isinstance(signal.get("_df"), pd.DataFrame) else None
                )
                trailing_state = self._trailing_stop_engine.init_stop(
                    entry_price=result.price,
                    df=df_for_stop,
                )
                # wisdom 预设的止损价若比 ATR 自适应止损更保守（更高），则优先采纳
                stop_loss = signal.get("stop_loss_price")
                if stop_loss and stop_loss > 0:
                    if stop_loss > trailing_state.current_stop_price:
                        trailing_state.initial_stop_price = float(stop_loss)
                        trailing_state.current_stop_price = float(stop_loss)
                    pos.stop_loss_price = float(stop_loss)
                else:
                    pos.stop_loss_price = trailing_state.initial_stop_price
                pos.trailing_stop = self._trailing_stop_engine.state_to_dict(trailing_state)

                # 注册财叔动态止盈监控（买入时记录入场价）
                self._take_profit_engine.register(symbol, result.price)

                # 注入危险信号信息
                danger_info = signal.get("danger_signals", {})
                if danger_info.get("signals"):
                    pos.danger_signals = danger_info["signals"]

        # 事件推送：信号 + 订单
        self._event_engine.put(
            EVENT_SIGNAL,
            {
                "symbol": symbol,
                "direction": direction,
                "price": price,
                "quantity": quantity,
                "wisdom_filtered": signal.get("wisdom_filtered", False),
                "risk_allowed": risk_result.allowed,
            },
        )
        if result.success:
            event_type = EVENT_ORDER_FILLED
        else:
            event_type = EVENT_ORDER_SUBMITTED
        self._event_engine.put(
            event_type,
            {
                "symbol": symbol,
                "direction": direction,
                "quantity": result.quantity,
                "price": result.price,
                "commission": result.commission,
                "success": result.success,
            },
        )

        signal["order_result"] = {
            "success": result.success,
            "price": result.price,
            "quantity": result.quantity,
            "commission": result.commission,
            "message": result.message,
        }
        self._inject_performance_summary(signal)
        notify_ok = await self._notifier.notify_signal(signal)
        if notify_ok:
            self._mark_notified(signal)
        # 通知失败时记录告警，确保用户可通过本地日志感知交易
        else:
            logger.error(
                f"{symbol} 下单已执行（success={result.success}, qty={result.quantity}@{result.price}）"
                f"但飞书通知失败，请检查飞书配置与网络"
            )

        # BIZ-01 修复：信号与决策持久化到 signals_history.jsonl
        # 便于回溯"某日某股为何被过滤/下单/拦截"
        record_signal_history(signal, risk_result, result)

    async def _check_stop_loss(self) -> None:
        """遍历持仓，若现价触及止损价则执行卖出。

        集成移动止损引擎：
        - 对有 trailing_stop 状态的持仓，先用 TrailingStopEngine 更新止损价
        - 对无 trailing_stop 状态的持仓，使用固定 stop_loss_price
        - 检测危险信号并联动收紧止损

        L3 元数据（prev_close/is_st/is_suspended）从 Position 取出传入风控，
        确保涨跌停时止损卖出也受 L3 规则约束（跌停时不可卖出）。

        遍历安全（P-10+D-09 修复）：先收集需止损的 (symbol, price, available) 列表，
        再逐个执行卖出，避免遍历中修改 portfolio 引用。
        """
        portfolio = self._broker.get_portfolio()

        symbols = list(portfolio.keys())
        if not symbols:
            return

        # P2 优化：批量拉取所有持仓的历史数据，避免 N 次串行 HTTP 请求
        try:
            batch_dfs = await self._dm.fetch_stock_history_batch(symbols, days=90)
        except Exception as e:
            logger.warning(f"批量拉取持仓历史数据失败，回退到逐股获取: {e}")
            batch_dfs = {}

        # 先收集需止损的持仓信息，避免遍历中修改（P-10+D-09）
        stop_list: list[tuple[str, float, float, Position]] = []
        for symbol, pos in portfolio.items():
            df = batch_dfs.get(symbol, pd.DataFrame())
            if df.empty:
                with contextlib.suppress(Exception):
                    df = await self._dm.fetch_stock_history(symbol, days=90)

            # 移动止损更新：如果有 trailing_stop 状态，先更新
            # P0 修复：注入持仓无 trailing_stop 状态时自动补建（避免移动止损完全失效）
            if not pos.trailing_stop and pos.avg_cost > 0 and not df.empty:
                try:
                    state = self._trailing_stop_engine.init_stop(
                        entry_price=pos.avg_cost, df=df
                    )
                    pos.trailing_stop = self._trailing_stop_engine.state_to_dict(state)
                    if state.current_stop_price > 0 and (
                        pos.stop_loss_price is None
                        or pos.stop_loss_price <= 0
                        or pos.stop_loss_price < state.current_stop_price
                    ):
                        pos.stop_loss_price = state.current_stop_price
                    logger.info(
                        f"持仓 {symbol} 补建 trailing_stop 状态："
                        f"入场价={pos.avg_cost:.3f} 止损价={pos.stop_loss_price:.3f}"
                    )
                except Exception as e:
                    logger.warning(f"{symbol} 补建 trailing_stop 失败: {e}")

            if pos.trailing_stop and not df.empty:
                try:
                    state = TrailingStopEngine.dict_to_state(pos.trailing_stop)
                    danger_signals = pos.danger_signals or []
                    state, signal = self._trailing_stop_engine.update(state, df, danger_signals)
                    pos.trailing_stop = self._trailing_stop_engine.state_to_dict(state)
                    # 更新止损价为移动止损价
                    if state.current_stop_price > 0:
                        pos.stop_loss_price = state.current_stop_price

                    # 移动止损信号触发
                    if signal == TrailingStopSignal.EXIT:
                        logger.warning(
                            f"移动止损触发: {symbol} 信号={signal.value}, "
                            f"止损价={state.current_stop_price:.2f}"
                        )
                        if pos.available > 0:
                            stop_list.append((symbol, pos.current_price, float(pos.available), pos))
                        continue
                    elif signal in (TrailingStopSignal.WARNING, TrailingStopSignal.ALERT):
                        logger.info(
                            f"移动止损预警: {symbol} 信号={signal.value}, "
                            f"跌破浪谷 {state.valley_break_count} 次"
                        )
                        continue
                except Exception as e:
                    logger.warning(f"{symbol} 移动止损更新失败: {e}")

            # 固定止损检查
            stop_price = getattr(pos, "stop_loss_price", None)
            if stop_price is None or stop_price <= 0:
                continue
            if pos.current_price <= stop_price and pos.available > 0:
                stop_list.append((symbol, pos.current_price, float(pos.available), pos))

        for symbol, price, available, pos in stop_list:
            logger.warning(
                f"止损触发: {symbol} 现价 {price} <= 止损价 {pos.stop_loss_price}，执行卖出"
            )
            self._event_engine.put(
                EVENT_STOP_LOSS,
                {
                    "symbol": symbol,
                    "price": price,
                    "quantity": int(available),
                    "stop_price": float(pos.stop_loss_price),
                },
            )
            risk_result = self._risk.check_order(
                symbol=symbol,
                direction="sell",
                quantity=int(available),
                price=price,
                portfolio=self._broker.get_portfolio(),  # 重新获取最新 portfolio
                cash=self._broker.get_account().cash,
                prev_close=getattr(pos, "prev_close", None),
                is_st=bool(getattr(pos, "is_st", False)),
                is_suspended=bool(getattr(pos, "is_suspended", False)),
            )
            if risk_result.allowed:
                result = self._broker.order(symbol, "sell", int(available), price)
                if result.success:
                    # P0-1 修复：通过 broker 重新获取 position 引用，
                    # 避免 stop_list 中的 pos 在清仓后变成孤儿对象
                    fresh_pos = self._broker.get_position(symbol)
                    if fresh_pos:
                        fresh_pos.trailing_stop = None
                        fresh_pos.danger_signals = None

                    notify_ok = await self._notifier.notify_risk_alert(
                        {
                            "level": "warn",
                            "message": f"止损卖出: {symbol} {result.quantity}股 @ {result.price}",
                            "suggestion": "止损价由移动止损引擎管理，已自动执行",
                        }
                    )
                    # P-07 修复：止损通知失败时记录 error 日志
                    if not notify_ok:
                        logger.error(
                            f"止损卖出 {symbol} 已执行（{result.quantity}股@{result.price}）"
                            f"但飞书通知失败，请检查飞书配置与网络"
                        )
            else:
                logger.warning(f"止损卖出 {symbol} 被风控拦截: {risk_result.message}")

        # ===== 财叔动态止盈减仓检查 =====
        # 与移动止损互补：止损保本金（下跌时），止盈锁利润（上涨时）
        await self._check_take_profit(portfolio)

    async def _check_take_profit(self, portfolio: dict) -> None:
        """财叔动态止盈减仓检查（四步策略）。

        在 _check_stop_loss 之后执行，遍历持仓检查是否触发止盈减仓：
        - 第1步：+30% 减 1/3
        - 第3步：每 +15% 减 10%
        - 第4步：大跌减半

        触发时执行减仓 + 飞书通知（标注出处和策略逻辑）。
        """
        from datetime import date as _date
        from gugu.data.stock_names import lookup_stock_name

        today_str = _date.today().isoformat()

        # P2-2 优化：缓存 portfolio 和 cash 快照，避免循环内重复查询
        # 止盈减仓是卖出操作，风控检查使用快照即可（与止损一致）
        cached_portfolio = portfolio
        cached_cash = self._broker.get_account().cash

        for symbol, pos in portfolio.items():
            if pos.available <= 0:
                continue

            # 获取止盈状态，未注册的跳过
            tp_state = self._take_profit_engine.get_state(symbol)
            if tp_state is None:
                continue

            prev_close = getattr(pos, "prev_close", 0) or pos.current_price
            tp_signal = self._take_profit_engine.check(
                symbol=symbol,
                current_price=pos.current_price,
                prev_close=prev_close,
                today_str=today_str,
            )

            if tp_signal.action != "reduce":
                continue

            # 计算减仓数量（向下取整，至少 100 股）
            reduce_qty = int(pos.available * tp_signal.reduce_ratio)
            reduce_qty = (reduce_qty // 100) * 100  # A股最小交易单位 100 股
            if reduce_qty < 100:
                logger.info(
                    f"[take_profit] {symbol} 减仓数量不足 100 股"
                    f"（available={pos.available}, ratio={tp_signal.reduce_ratio:.0%}），跳过"
                )
                continue

            logger.warning(
                f"[take_profit] {symbol} 触发第{tp_signal.trigger_step}步止盈减仓: "
                f"{tp_signal.reason}，减仓 {reduce_qty} 股"
            )

            # 风控检查（P2-2: 使用缓存的快照，避免重复查询）
            risk_result = self._risk.check_order(
                symbol=symbol,
                direction="sell",
                quantity=reduce_qty,
                price=pos.current_price,
                portfolio=cached_portfolio,
                cash=cached_cash,
                prev_close=getattr(pos, "prev_close", None),
                is_st=bool(getattr(pos, "is_st", False)),
                is_suspended=bool(getattr(pos, "is_suspended", False)),
            )

            if not risk_result.allowed:
                logger.warning(
                    f"[take_profit] {symbol} 止盈减仓被风控拦截: {risk_result.message}"
                )
                continue

            # 执行减仓
            result = self._broker.order(symbol, "sell", reduce_qty, pos.current_price)
            if not result.success:
                logger.warning(f"[take_profit] {symbol} 止盈减仓下单失败")
                continue

            # 更新止盈状态
            self._take_profit_engine.mark_reduced(
                symbol=symbol,
                executed_qty=result.quantity,
                price=result.price,
                signal=tp_signal,
                today_str=today_str,
            )

            # 飞书通知（标注出处和策略逻辑）
            stock_name = lookup_stock_name(symbol) or ""
            signal_dict = {
                "trigger_step": tp_signal.trigger_step,
                "reason": tp_signal.reason,
                "reduce_ratio": tp_signal.reduce_ratio,
                "current_profit_pct": tp_signal.current_profit_pct,
                "current_price": result.price,
                "entry_price": tp_state.entry_price,
            }
            from dataclasses import asdict as _asdict

            state_dict = _asdict(tp_state) if tp_state else None
            notify_ok = await self._notifier.notify_take_profit_alert(
                symbol, stock_name, signal_dict, state_dict
            )
            if not notify_ok:
                logger.error(
                    f"[take_profit] {symbol} 止盈减仓已执行"
                    f"（{result.quantity}股@{result.price}）"
                    f"但飞书通知失败，请检查飞书配置"
                )

            # 清仓时移除止盈监控
            remaining = pos.available - result.quantity
            if remaining <= 0:
                self._take_profit_engine.unregister(symbol)

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
            notify_alert_ok = await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"当日亏损 {loss_pct:.2%} 触发预警",
                    "suggestion": "关注持仓，考虑减仓",
                }
            )
            if not notify_alert_ok:
                logger.error(f"日亏预警（{loss_pct:.2%}）通知失败")
        elif risk_result.action == RiskAction.HALT:
            self._event_engine.put(EVENT_DAILY_LOSS_HALT, {"loss_pct": loss_pct})
            notify_alert_ok = await self._notifier.notify_risk_alert(
                {
                    "level": "halt",
                    "message": f"当日亏损 {loss_pct:.2%} 触发熔断，停止交易",
                    "suggestion": "检查持仓，必要时一键平仓",
                }
            )
            if not notify_alert_ok:
                logger.error(f"日亏熔断（{loss_pct:.2%}）通知失败")

    async def send_daily_report(self, period: str) -> None:
        """发送信号汇总报告。

        弱化传统日报，聚焦项目核心：信号汇总 + 绩效验证。
        - morning: 盘前推送昨日信号绩效 + 今日关注
        - noon: 午盘推送今日已触发信号汇总
        - close: 收盘推送今日信号汇总 + 持仓状态 + 绩效报告
        """
        account = self._broker.get_account()

        # 读取今日信号（从 signals_history.jsonl）
        today_signals = self._load_today_signals()

        # 收盘日报附带信号绩效报告 + 额外状态
        performance_report = None
        regime_info = None
        risk_info = None
        trailing_stop_info = None
        if period == "close":
            try:
                from gugu.analysis.signal_tracker import SignalTracker

                tracker = SignalTracker()
                performance_report = await tracker.generate_report(days=30)
            except Exception as e:
                logger.warning(f"生成信号绩效报告失败: {e}")

            # 市场状态信息
            try:
                regime = await self._regime_detector.detect()
                regime_info = {
                    "regime": regime.get("regime", "unknown"),
                    "reason": regime.get("reason", ""),
                    "total_limit": regime.get("total_position_limit", 0),
                }
            except Exception as e:
                logger.debug(f"获取市场状态失败: {e}")

            # 风控状态
            try:
                daily_loss_pct = 0.0
                start_value = self._broker.daily_start_value
                if start_value > 0:
                    daily_loss_pct = (start_value - account.total_value) / start_value
                risk_info = {
                    "halted": self._risk.is_halted,
                    "daily_loss_pct": round(daily_loss_pct, 4),
                    "daily_start_value": start_value,
                }
            except Exception as e:
                logger.debug(f"获取风控状态失败: {e}")

            # 移动止损状态
            try:
                portfolio = self._broker.get_portfolio()
                stops = []
                for sym, pos in portfolio.items():
                    trailing_state = getattr(pos, "trailing_stop", None)
                    if trailing_state:
                        stops.append(
                            {
                                "symbol": sym,
                                "current_stop": trailing_state.get("current_stop_price", 0),
                                "highest": trailing_state.get("highest_price", 0),
                                "signal": trailing_state.get("last_signal", "hold"),
                            }
                        )
                trailing_stop_info = stops
            except Exception as e:
                logger.debug(f"获取移动止损状态失败: {e}")

        data = {
            "market_summary": {
                "total_value": account.total_value,
                "cash": account.cash,
                "positions_count": len(account.positions),
            },
            "signals": today_signals,
            "portfolio_summary": {
                sym: {
                    "name": "",
                    "quantity": pos.quantity,
                    "profit": pos.profit,
                    "market_value": pos.market_value,
                }
                for sym, pos in account.positions.items()
            },
            "performance": performance_report,
            "regime": regime_info,
            "risk": risk_info,
            "trailing_stops": trailing_stop_info,
        }

        # 异步填充持仓股票名称（含 fallback）
        for sym, info in data["portfolio_summary"].items():
            name = lookup_stock_name(sym)
            if not name:
                try:
                    meta = await self._dm.fetch_stock_meta(sym)
                    name = meta.get("name", "")
                except Exception:
                    pass
            info["name"] = name or sym

        # 先重试之前失败的通知
        try:
            await self._notifier.retry_queued()
        except Exception as e:
            logger.debug(f"重试队列处理失败: {e}")

        await self._notifier.notify_daily_report(period, data)

        # 收盘后检查持仓股卖出信号（迁移自 MingCe _check_holdings_sell_signals）
        # P1-2 修复：使用 _lock 保护，避免与交易循环并发操作 broker
        if period == "close":
            try:
                async with self._lock:
                    if self._running:
                        logger.warning("交易循环仍在运行，跳过收盘持仓卖出检查")
                        return
                    self._running = True
                    try:
                        portfolio = self._broker.get_portfolio()
                        if portfolio:
                            sell_alerts = await self._check_holdings_sell_signals(portfolio)
                            if sell_alerts:
                                await self._notifier.notify_holdings_sell_alert(sell_alerts)
                    finally:
                        self._running = False
            except Exception as e:
                logger.warning(f"持仓卖出检查失败（不影响日报）: {e}")

    def _load_today_signals(self) -> list[dict[str, Any]]:
        """从 signals_history.jsonl 读取今日信号。"""
        import json
        from datetime import date as _date

        path = PROJECT_ROOT / "data" / "signals_history.jsonl"
        if not path.exists():
            return []

        today_str = _date.today().isoformat()

        seen: dict[str, dict[str, Any]] = {}  # key = symbol_direction
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = rec.get("timestamp", "")
                        if not ts.startswith(today_str):
                            continue
                        symbol = rec.get("symbol", "")
                        direction = rec.get("direction", "")
                        key = f"{symbol}_{direction}"
                        raw_name = rec.get("name", "") or ""
                        # 本地映射表优先（可信源），过滤 API 脏数据与策略名污染
                        local_name = lookup_stock_name(symbol)
                        if local_name:
                            raw_name = local_name
                        elif not raw_name or raw_name == symbol:
                            raw_name = symbol
                        seen[key] = {
                            "symbol": symbol,
                            "name": raw_name,
                            "direction": direction,
                            "price": rec.get("price"),
                            "strategies": rec.get("strategies", []),
                            "order_success": rec.get("order_success"),
                            "wisdom_filtered": rec.get("wisdom_filtered", False),
                        }
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"读取今日信号失败: {e}")

        # 去重后只保留每个 (symbol, direction) 的最后一条记录
        return list(seen.values())

    async def shutdown(self) -> None:
        """关闭引擎资源（HTTP client 等）。"""
        await self._notifier.close()
        self._write_heartbeat("shutdown")

    def reset_halt(self) -> None:
        """手动解除 L2 熔断（需人工确认后执行）。

        铁律：仅清除 _halted 标志，不重置日初净值，当日亏损继续累计。
        避免盘中熔断 reset 后"掩盖"当日已发生的亏损。
        若需跨日重置，应在新交易日由 run_daily_cycle 自动调用 _risk.reset()。
        """
        self._risk.clear_halt_only()
        logger.warning(
            "L2 熔断状态已手动解除（clear_halt_only），日初净值保留不变，当日亏损继续累计"
        )

    def _on_risk_alert(self, event: dict[str, Any]) -> None:
        """风险事件处理器。"""
        data = event.get("data", {})
        logger.warning(f"风险事件: level={data.get('level')}, message={data.get('message', '')}")

    def _on_stop_loss_triggered(self, event: dict[str, Any]) -> None:
        """止损触发事件处理器。"""
        data = event.get("data", {})
        logger.warning(
            f"止损触发事件: symbol={data.get('symbol')}, "
            f"price={data.get('price')}, quantity={data.get('quantity')}"
        )

    def _on_order_filled(self, event: dict[str, Any]) -> None:
        """订单成交事件处理器。"""
        data = event.get("data", {})
        logger.info(
            f"订单成交: {data.get('symbol')} "
            f"{data.get('direction')} {data.get('quantity')}@{data.get('price')}"
        )

    @staticmethod
    def _rotate_jsonl(path: Path, max_lines: int = 5000) -> None:
        """JSONL 文件行数超过 max_lines 时截断，保留最新 max_lines//2 行。"""
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > max_lines:
                    keep = lines[-max_lines // 2 :]
                    with path.open("w", encoding="utf-8") as f:
                        f.writelines(keep)
                    logger.info(f"历史文件轮转: {path.name} {len(lines)}→{len(keep)} 行")
        except Exception as e:
            logger.warning(f"历史文件轮转失败: {e}")

    def _write_heartbeat(self, status: str) -> None:
        """写入心跳文件，便于外部监控。

        OP-05 修复：同时追加到历史文件 heartbeat_history.jsonl，
        每行一个 JSON，便于回溯崩溃时间点与历史状态。
        """
        try:
            hb_dir = PROJECT_ROOT / "data"
            hb_dir.mkdir(exist_ok=True)
            path = hb_dir / "heartbeat.json"
            history_path = hb_dir / "heartbeat_history.jsonl"
            self._rotate_jsonl(history_path)
            account = self._broker.get_account()
            payload = {
                "last_cycle_at": pd.Timestamp.now().isoformat(),
                "status": status,
                "halted": self._risk.is_halted,
                "total_value": account.total_value,
                "cash": account.cash,
                "positions_count": len(account.positions),
            }
            # 最新状态覆盖写入
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            # 历史记录追加写入（每行一个 JSON）
            with history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"写入心跳文件失败: {e}")

    async def _check_holdings_sell_signals(self, portfolio: dict) -> list[dict]:
        """收盘后检查持仓股卖出信号。

        遍历所有持仓股，运行策略信号扫描，若 sell > buy 达到阈值则生成告警。
        迁移自 MingCe _check_holdings_sell_signals。

        Args:
            portfolio: {symbol: Position} 持仓字典。

        Returns:
            卖出信号明确的股票列表，每项含 name, symbol, price, profit_pct, sell_count 等。
        """
        if not portfolio:
            return []

        symbols = list(portfolio.keys())
        logger.info(f"持仓卖出信号检查: {len(symbols)} 只持仓股")

        # 用现有信号管道扫描所有持仓股
        signals = await self._scan_signals(extra_symbols=symbols)
        if not signals:
            return []

        # 获取最新行情用于计算盈亏

        sell_alerts = []
        for sig in signals:
            sym = sig.get("symbol", "")
            if sym not in portfolio:
                continue
            direction = str(sig.get("direction", "")).lower()
            if direction != "sell":
                continue
            pos = portfolio[sym]
            if not isinstance(pos, dict) and not hasattr(pos, "avg_cost"):
                continue
            cost = float(
                pos.get("avg_cost", 0) if isinstance(pos, dict) else getattr(pos, "avg_cost", 0)
            )
            price = sig.get("price", 0) or 0
            profit_pct = ((price - cost) / cost * 100) if cost and price else 0

            sell_alerts.append(
                {
                    "name": sig.get("name", sym),
                    "symbol": sym,
                    "price": price,
                    "profit_pct": round(profit_pct, 2),
                    "sell_count": len(sig.get("sell_signals", sig.get("strategies", []))),
                    "buy_count": 1 if sig.get("buy_signals") else 0,
                    "sell_signals": [
                        {"strategy_name": s, "signal_strength": sig.get("confidence", 0.5)}
                        for s in (
                            sig.get("strategies", [])
                            if isinstance(sig.get("strategies"), list)
                            else [sig.get("strategy", "?")]
                        )
                    ],
                }
            )

        if sell_alerts:
            logger.info(f"持仓卖出信号: {len(sell_alerts)} 只股票需关注")
        return sell_alerts

    @staticmethod
    def _inject_performance_summary(signal: dict[str, Any]) -> None:
        """注入同类信号历史表现摘要到信号 dict 中。

        通过 SignalTracker 查询最近 30 天的同策略买入信号表现，
        将胜率/平均收益等数据写入 signal["performance_summary"]，
        供 formatter 在卡片中展示。
        """
        try:
            tracker = SignalTracker()
            records = tracker.load_history(days=30)
            if len(records) < 3:
                return

            strategies = set()
            sig_strategies = signal.get("strategies", [])
            if isinstance(sig_strategies, str):
                strategies.add(sig_strategies)
            elif isinstance(sig_strategies, list):
                strategies.update(sig_strategies)
            if not strategies:
                return

            buy_records = [r for r in records if r.get("direction") == "buy"]
            relevant = [
                r
                for r in buy_records
                if set(
                    r.get("strategies", [])
                    if isinstance(r.get("strategies"), list)
                    else [r.get("strategy", "")]
                )
                & strategies
            ]
            if len(relevant) < 3:
                return

            valid = [r for r in relevant if r.get("return_5d") is not None]
            if len(valid) < 3:
                return
            wins = [r for r in valid if r["return_5d"] > 0]
            returns = [r["return_5d"] for r in valid]
            signal["performance_summary"] = {
                "sample_count": len(valid),
                "win_rate": round(len(wins) / len(valid), 3),
                "avg_return_5d": round(sum(returns) / len(valid), 4),
                "best_return": round(max(returns), 4),
                "worst_return": round(min(returns), 4),
                "strategies": list(strategies),
            }
        except Exception as e:
            logger.debug(f"注入信号绩效摘要失败: {e}")


def run_engine() -> None:
    """运行交易引擎（同步入口）。"""
    engine = TradingEngine()
    asyncio.run(engine.run_daily_cycle())
