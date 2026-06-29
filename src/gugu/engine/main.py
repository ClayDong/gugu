"""主引擎：交易系统核心。

整合数据层、策略层、风控、执行、通知、决策层。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pandas as pd

from gugu.analysis.position_controller import PositionController
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
from gugu.analysis.trailing_stop import TrailingStopEngine, TrailingStopState, TrailingStopSignal
from gugu.analysis.danger_signal import DangerSignalDetector
from gugu.config import PROJECT_ROOT, settings
from gugu.config.models import AppConfig
from gugu.data import data_manager
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
from gugu.engine.signal_pipeline import record_signal_history, SignalPipeline
from gugu.engine.signal_router import SignalRouter
from gugu.execution import PaperBroker
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.industry_constraint import IndustryConstraint
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.notifier import FeishuNotifier
from gugu.risk import RiskManager
from gugu.risk.rules import RiskAction
from gugu.selector import StockSelector
from gugu.strategies.registry import get_enabled_strategies
from gugu.utils.calendar import is_trading_day
from gugu.utils.log import get_logger
from gugu.wisdom import WisdomAdvisor

logger = get_logger()


class TradingEngine:
    """交易主引擎。"""

    def __init__(self) -> None:
        self._dm = data_manager()
        # TODO: full migration to AppConfig (currently used as supplement to settings())
        self._app_config: AppConfig | None = None
        try:
            self._app_config = AppConfig.from_settings()
        except Exception:
            pass
        self._strategies = get_enabled_strategies()
        self._router = SignalRouter(self._strategies)
        self._risk = RiskManager()
        self._broker = PaperBroker()
        self._notifier = FeishuNotifier()
        self._last_cycle_date: date | None = None  # 上次循环日期，防止同日多次 reset
        self._wisdom = WisdomAdvisor()
        self._selector = StockSelector(self._dm)
        self._watchlist: list[str] = self._load_watchlist()
        self._running: bool = False  # 运行中标志，防止重入
        # 执行模式缓存（signal_only/paper/live）
        self._exec_mode: str = settings().get("execution", {}).get("mode", "paper")

        # 过滤器
        self._fundamental_filter = FundamentalFilter()
        self._money_flow_filter = MoneyFlowFilter()
        self._industry_constraint = IndustryConstraint()

        # 事件引擎
        self._event_engine = EventEngine()

        # 多周期择时（替换旧的 market_regime）
        self._regime_detector = MultiPeriodRegimeDetector()
        self._position_controller = PositionController()

        # 移动止损引擎 + 危险信号检测器
        self._trailing_stop_engine = TrailingStopEngine()
        self._danger_detector = DangerSignalDetector()

        # 信号过滤流水线
        self._pipeline = SignalPipeline(
            data_manager=self._dm,
            signal_router=self._router,
            wisdom_advisor=self._wisdom,
            regime_detector=self._regime_detector,
            position_controller=self._position_controller,
            fundamental_filter=self._fundamental_filter,
            money_flow_filter=self._money_flow_filter,
            industry_constraint=self._industry_constraint,
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
        if self._app_config is not None:
            return self._app_config.strategy.auto_select
        # TODO: remove settings() fallback after full AppConfig migration
        return bool(settings().get("strategy", {}).get("auto_select", False))

    def _load_watchlist(self) -> list[str]:
        """加载自选股列表（优先从 AppConfig 读取，fallback 到 settings.yaml）。"""
        if self._app_config is not None:
            watchlist = list(self._app_config.watchlist)
            if watchlist:
                return [str(code).strip().zfill(6) for code in watchlist]
        # TODO: remove settings() fallback after full AppConfig migration
        watchlist = settings().get("watchlist", [])
        if not watchlist:
            logger.warning("settings.yaml 中未配置 watchlist，使用空自选股列表")
            return []
        return [str(code).strip().zfill(6) for code in watchlist]

    async def run_daily_cycle(self) -> None:
        """每日交易循环：采集 → 策略 → 风控 → 执行 → 通知。"""
        # 重入保护：避免调度器在前一次循环未完成时再次触发
        if self._running:
            logger.warning("交易循环正在运行中，跳过本次重入调用")
            return

        # 铁律：L2 熔断状态下，即使同日再次调用也必须保持熔断
        if self._risk.is_halted:
            logger.warning("L2 熔断状态激活中，跳过本次交易循环（需人工 reset_halt 后恢复）")
            return

        if not is_trading_day():
            logger.info("非交易日，跳过")
            return

        logger.info("=== 开始每日交易循环 ===")
        self._running = True
        # 确保事件引擎存在（兼容测试中跳过 __init__ 的场景）
        if not hasattr(self, "_event_engine"):
            self._event_engine = EventEngine()
        self._event_engine.put(EVENT_CYCLE_START, {"timestamp": pd.Timestamp.now().isoformat()})

        # 1. T+1 结算 + 日初净值重置（仅新交易日执行，防止重启后绕过 T+1）
        # P0-1/P1-a 修复：settle_t_plus_1 移入新交易日分支，
        # 避免进程重启后把当日新买入的份额误置为可卖（违反 T+1）
        today = date.today()
        if self._last_cycle_date != today:
            self._broker.settle_t_plus_1()
            self._broker.reset_daily_start_value()  # P0-1: 每日重置日初净值
            self._risk.reset()
            self._last_cycle_date = today

        try:
            # 2. 采集行情
            await self._update_prices()

            # 3. 持仓止损检查：先止损再扫描信号（A-05 修复）
            # 止损卖出会改变持仓与资金，必须在信号扫描前执行，
            # 确保后续信号基于最新持仓状态生成
            await self._check_stop_loss()

            # 3.5 止损后立即检查日亏（P1-d 修复）
            # 止损可能造成大亏，若不先检查则后续买单会在已熔断状态下继续执行
            await self._check_daily_loss()
            if self._risk.is_halted:
                logger.warning("止损后触发 L2 熔断，跳过本次信号扫描与下单")
                self._write_heartbeat("halted")
                return

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
                        if s["symbol"] not in self._watchlist and s["symbol"] not in today_extra:
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

            # 6. 检查日亏
            await self._check_daily_loss()

            # 事件推送：交易循环完成
            self._event_engine.put(EVENT_CYCLE_END, {
                "status": "ok",
                "positions_count": len(self._broker.get_portfolio()),
                "signals_count": len(signals),
            })
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
        logger.info("=== 每日交易循环完成 ===")

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
        self._event_engine.put(EVENT_MARKET_REGIME, {
            "regime": regime.get("regime", "unknown"),
            "reason": regime.get("reason", ""),
            "total_limit": budget.total_limit,
            "single_limit": budget.single_limit,
        })

        signals = []
        # 批量拉取实时行情一次，避免 per-symbol 重复请求（P-04 修复）
        try:
            rt_all = await self._dm.fetch_stock_realtime(scan_list) if scan_list else None
        except Exception:
            rt_all = None

        for symbol in scan_list:
            try:
                df = await self._dm.fetch_stock_history(symbol, days=60)
                if df.empty:
                    continue

                meta = await self._dm.fetch_stock_meta(symbol)
                portfolio = self._broker.get_portfolio()
                account = self._broker.get_account()

                # delegating filtering to SignalPipeline
                signal = await self._pipeline.process(
                    symbol=symbol,
                    df=df,
                    meta=meta,
                    budget=budget,
                    rt_all=rt_all,
                    watchlist=self._watchlist,
                    portfolio=portfolio,
                    account=account,
                )
                if signal is None:
                    continue

                # 市场 regime 覆盖（SignalPipeline 无法访问 TradingEngine 的 _regime_detector 返回）
                signal["market_context"]["regime"] = regime["regime"]

                # L3 市场状态仓位修正信息记录
                if signal["direction"] == "buy" and not signal.get("wisdom_filtered"):
                    if regime.get("sell_signal_required"):
                        logger.info(
                            f"{symbol} 市场状态 {regime['regime']} 建议减仓，"
                            f"仓位上限已由 budget 限制为 {signal['suggested_position_ratio']:.2%}"
                        )

                signals.append(signal)
            except Exception as e:
                logger.error(f"扫描 {symbol} 失败: {e}")
        return signals

    async def _process_signal(self, signal: dict[str, Any]) -> None:
        """处理单个信号：signal_only 模式只通知不下单，paper/live 模式走风控+下单+通知。"""
        symbol = signal["symbol"]
        direction = signal["direction"]
        price = signal.get("price", 0)

        # signal_only 模式：只发信号通知，不下单（监控验证用）
        if self._exec_mode == "signal_only":
            logger.info(f"{symbol} {direction} 信号（signal_only 模式，仅通知不下单）")
            # 记录信号历史（不下单，order_result=None）
            try:
                record_signal_history(signal, None, None)
            except Exception as e:
                logger.warning(f"记录信号历史失败: {e}")
            notify_ok = await self._notifier.notify_signal(signal)
            if not notify_ok:
                logger.error(f"{symbol} 信号通知失败，请检查飞书配置")
            return

        # 入场过滤：wisdom 判定低置信度，仅通知不下单
        if signal.get("wisdom_filtered"):
            logger.info(f"{symbol} 信号被 wisdom 入场过滤，仅通知不下单")
            notify_ok = await self._notifier.notify_signal(signal)
            # P1-o 修复：统一通知失败处理，与下单后通知逻辑一致
            if not notify_ok:
                logger.error(
                    f"{symbol} wisdom 过滤信号通知失败，请检查飞书配置与网络"
                )
            return

        # 使用 wisdom 调整后的仓位比例（已在 _scan_signals 中设置）
        suggested_ratio = signal.get("suggested_position_ratio", 0.0)
        if suggested_ratio <= 0:
            max_ratio = settings().get("risk", {}).get("max_position_ratio", 0.30)
            suggested_ratio = max_ratio * 0.8

        account = self._broker.get_account()
        target_value = account.total_value * suggested_ratio
        quantity = int(target_value / price / 100) * 100 if price > 0 else 0

        # P1-i 修复：price 合法性校验前置，避免负数参与计算
        if price <= 0:
            logger.warning(f"{symbol} 信号价格异常 price={price}，跳过下单并发送告警")
            await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"{symbol} 信号价格异常 price={price}，已跳过下单",
                    "suggestion": "检查数据源是否返回异常价格（0/负值）",
                }
            )
            return

        # 最低1手保底：试仓比例过小时（如高价股），确保至少买入100股
        # P1-i 修复：保底前校验 1 手金额不超过 L1 单股上限，避免绕过 wisdom 仓位意图
        max_ratio = settings().get("risk", {}).get("max_position_ratio", 0.30)
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
            await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"{symbol} 计算下单数量为 0（price={price}, ratio={suggested_ratio:.2%}, cash={account.cash:.0f}），已跳过",
                    "suggestion": "检查仓位比例配置或资金是否充足",
                }
            )
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
            await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"{symbol} {direction} 被风控拦截: {risk_result.message}",
                    "suggestion": "检查仓位/日亏/涨跌停",
                }
            )
            return

        # 下单
        result = self._broker.order(symbol, direction, quantity, price)

        # 买入成功后，将止损价与 L3 元数据注入 Position，供后续止损检查使用
        if result.success and direction == "buy":
            pos = self._broker.get_position(symbol)
            if pos is not None:
                stop_loss = signal.get("stop_loss_price")
                if stop_loss and stop_loss > 0:
                    pos.stop_loss_price = float(stop_loss)
                if signal.get("prev_close"):
                    pos.prev_close = float(signal["prev_close"])
                pos.is_st = bool(signal.get("is_st", False))
                pos.is_suspended = bool(signal.get("is_suspended", False))

                # 初始化移动止损状态
                trailing_state = self._trailing_stop_engine.init_stop(
                    entry_price=result.price,
                    initial_stop_pct=None if not stop_loss else None,
                )
                if stop_loss and stop_loss > 0:
                    # 使用 wisdom 设定的止损价作为初始止损
                    trailing_state.initial_stop_price = float(stop_loss)
                    trailing_state.current_stop_price = float(stop_loss)
                pos.trailing_stop = self._trailing_stop_engine.state_to_dict(trailing_state)

                # 注入危险信号信息
                danger_info = signal.get("danger_signals", {})
                if danger_info.get("signals"):
                    pos.danger_signals = danger_info["signals"]

        # 事件推送：信号 + 订单
        self._event_engine.put(EVENT_SIGNAL, {
            "symbol": symbol,
            "direction": direction,
            "price": price,
            "quantity": quantity,
            "wisdom_filtered": signal.get("wisdom_filtered", False),
            "risk_allowed": risk_result.allowed,
        })
        if result.success:
            event_type = EVENT_ORDER_FILLED
        else:
            event_type = EVENT_ORDER_SUBMITTED
        self._event_engine.put(event_type, {
            "symbol": symbol,
            "direction": direction,
            "quantity": result.quantity,
            "price": result.price,
            "commission": result.commission,
            "success": result.success,
        })

        # 通知
        signal["order_result"] = {
            "success": result.success,
            "price": result.price,
            "quantity": result.quantity,
            "commission": result.commission,
            "message": result.message,
        }
        notify_ok = await self._notifier.notify_signal(signal)
        # 通知失败时记录告警，确保用户可通过本地日志感知交易
        if not notify_ok:
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
        account = self._broker.get_account()

        # 先收集需止损的持仓信息，避免遍历中修改（P-10+D-09）
        stop_list: list[tuple[str, float, float, "Position"]] = []
        for symbol, pos in portfolio.items():
            # 移动止损更新：如果有 trailing_stop 状态，先更新
            if pos.trailing_stop:
                try:
                    df = await self._dm.fetch_stock_history(symbol, days=60)
                    if not df.empty:
                        state = TrailingStopEngine.dict_to_state(pos.trailing_stop)
                        danger_signals = pos.danger_signals or []
                        state, signal = self._trailing_stop_engine.update(
                            state, df, danger_signals
                        )
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
                                stop_list.append(
                                    (symbol, pos.current_price, float(pos.available), pos)
                                )
                            continue
                        elif signal in (TrailingStopSignal.WARNING, TrailingStopSignal.ALERT):
                            logger.info(
                                f"移动止损预警: {symbol} 信号={signal.value}, "
                                f"跌破浪谷 {state.valley_break_count} 次"
                            )
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
            self._event_engine.put(EVENT_STOP_LOSS, {
                "symbol": symbol,
                "price": price,
                "quantity": int(available),
                "stop_price": float(pos.stop_loss_price),
            })
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
                    # 清除移动止损状态
                    pos.trailing_stop = None
                    pos.danger_signals = None

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

    async def _check_daily_loss(self) -> None:
        """检查当日亏损（以日初净值为基准）。"""
        account = self._broker.get_account()
        start_value = self._broker.daily_start_value
        if start_value <= 0:
            start_value = settings().get("execution", {}).get("paper", {}).get(
                "initial_capital", 1_000_000
            )
        loss_pct = (start_value - account.total_value) / start_value

        risk_result = self._risk.check_daily_loss(loss_pct)
        if risk_result.action == RiskAction.WARN:
            self._event_engine.put(EVENT_DAILY_LOSS_WARN, {"loss_pct": loss_pct})
            await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"当日亏损 {loss_pct:.2%} 触发预警",
                    "suggestion": "关注持仓，考虑减仓",
                }
            )
        elif risk_result.action == RiskAction.HALT:
            self._event_engine.put(EVENT_DAILY_LOSS_HALT, {"loss_pct": loss_pct})
            await self._notifier.notify_risk_alert(
                {
                    "level": "halt",
                    "message": f"当日亏损 {loss_pct:.2%} 触发熔断，停止交易",
                    "suggestion": "检查持仓，必要时一键平仓",
                }
            )

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
                        stops.append({
                            "symbol": sym,
                            "current_stop": trailing_state.get("current_stop_price", 0),
                            "highest": trailing_state.get("highest_price", 0),
                            "signal": trailing_state.get("last_signal", "hold"),
                        })
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

        # 异步填充持仓股票名称（含 _STOCK_NAMES fallback）
        _local_names = {
            "600519": "贵州茅台", "300750": "宁德时代", "000858": "五粮液",
            "601318": "中国平安", "000333": "美的集团", "300059": "东方财富",
            "600030": "中信证券", "000776": "广发证券", "603259": "药明康德",
            "600600": "青岛啤酒", "002625": "光启技术", "600674": "川投能源",
            "688396": "华润微", "601238": "广汽集团", "600460": "士兰微",
            "000977": "浪潮信息", "002049": "紫光国微", "300033": "同花顺",
            "600026": "中远海能", "600150": "中国船舶", "600489": "中金黄金",
            "600584": "长电科技", "601899": "紫金矿业", "603019": "中科曙光",
            "603799": "华友钴业", "600036": "招商银行", "601398": "工商银行",
            "601939": "建设银行", "000538": "云南白药",
        }
        for sym, info in data["portfolio_summary"].items():
            name = _local_names.get(sym, "")
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

    def _load_today_signals(self) -> list[dict[str, Any]]:
        """从 signals_history.jsonl 读取今日信号。"""
        import json
        from datetime import date as _date

        path = PROJECT_ROOT / "data" / "signals_history.jsonl"
        if not path.exists():
            return []

        today_str = _date.today().isoformat()

        # 本地名称映射（用于 signals 中 name 为空时的 fallback）
        _local_names = {
            "600519": "贵州茅台", "300750": "宁德时代", "000858": "五粮液",
            "601318": "中国平安", "000333": "美的集团", "300059": "东方财富",
            "600030": "中信证券", "000776": "广发证券", "603259": "药明康德",
            "600600": "青岛啤酒", "002625": "光启技术", "600674": "川投能源",
            "688396": "华润微", "601238": "广汽集团", "600460": "士兰微",
            "000977": "浪潮信息", "002049": "紫光国微", "300033": "同花顺",
            "600026": "中远海能", "600150": "中国船舶", "600489": "中金黄金",
            "600584": "长电科技", "601899": "紫金矿业", "603019": "中科曙光",
            "603799": "华友钴业", "600036": "招商银行", "601398": "工商银行",
            "601939": "建设银行", "000538": "云南白药",
        }

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
                        if not raw_name or raw_name == symbol:
                            raw_name = _local_names.get(symbol, "") or symbol
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
        logger.warning("L2 熔断状态已手动解除（clear_halt_only），日初净值保留不变，当日亏损继续累计")

    def _on_risk_alert(self, event: dict[str, Any]) -> None:
        """风险事件处理器。"""
        data = event.get("data", {})
        logger.warning(
            f"风险事件: level={data.get('level')}, "
            f"message={data.get('message', '')}"
        )

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


def run_engine() -> None:
    """运行交易引擎（同步入口）。"""
    engine = TradingEngine()
    asyncio.run(engine.run_daily_cycle())
