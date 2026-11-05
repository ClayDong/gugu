"""止损执行管理器：遍历持仓并执行止损卖出。

提取自 TradingEngine._check_stop_loss() 的止损逻辑，
形成独立可测试的 StopLossManager 类。

集成移动止损引擎 + 固定止损检查 + 财叔动态止盈减仓。
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gugu.engine.event_engine import EventEngine

import pandas as pd

from gugu.analysis.dynamic_take_profit import DynamicTakeProfitEngine
from gugu.analysis.trailing_stop import TrailingStopEngine, TrailingStopSignal
from gugu.data.stock_names import lookup_stock_name
from gugu.execution.paper_ledger import ExecutionContext
from gugu.models import Position
from gugu.risk.rules import RiskAction
from gugu.utils.log import get_logger

logger = get_logger()


class StopLossManager:
    """止损执行管理器。

    负责遍历持仓：
    - 补建缺失的 trailing_stop 状态
    - 更新移动止损价
    - 检查固定止损触发
    - 执行止损卖出
    - 财叔动态止盈减仓
    """

    def __init__(
        self,
        *,
        trailing_stop_engine: Any = None,
        take_profit_engine: Any = None,
        should_alert: Callable[[str], bool] | None = None,
    ) -> None:
        self._trailing_stop_engine = (
            trailing_stop_engine if trailing_stop_engine is not None else TrailingStopEngine()
        )
        self._take_profit_engine = (
            take_profit_engine if take_profit_engine is not None else DynamicTakeProfitEngine()
        )
        self._should_alert = should_alert

    @staticmethod
    def _record_exit_history(
        symbol: str,
        pos: Position,
        result: Any | None,
        risk_result: Any,
        reason: str,
        exit_signal_id: str | None = None,
    ) -> None:
        """将止损/止盈退出写入信号审计，保持 signal 生命周期可追踪。"""
        try:
            from gugu.engine.signal_pipeline import record_signal_history

            entry_signal_id = getattr(pos, "entry_signal_id", "")
            signal = {
                # 退出事件使用独立 ID，同时保留 parent_signal_id 关联入场。
                "signal_id": exit_signal_id or uuid.uuid4().hex,
                "parent_signal_id": entry_signal_id,
                "symbol": symbol,
                "direction": "sell",
                "price": result.price,
                "confidence": 1.0,
                "strategy": getattr(pos, "entry_strategy", ""),
                "strategies": getattr(pos, "entry_strategies", None) or [],
                "reason": reason,
                "exit_reason": reason,
                "data_quality": getattr(pos, "entry_data_quality", "unknown"),
                "attribution_method": getattr(pos, "entry_attribution_method", "primary"),
            }
            record_signal_history(signal, risk_result, result)
        except Exception as exc:
            logger.warning("退出信号历史写入失败 {}：{}", symbol, exc)

    async def execute(
        self,
        broker: Any,
        risk: Any,
        notifier: Any,
        event_engine: EventEngine,
        data_mgr: Any,
    ) -> int:
        """执行止损检查并返回卖出的股票数。

        包括移动止损更新、固定止损检查、执行卖出、风控检查、通知、事件推送。

        Args:
            broker: 交易执行器（PaperBroker / QmtBroker）。
            risk: 风控管理器（RiskManager）。
            notifier: 飞书通知器（FeishuNotifier）。
            event_engine: 事件引擎（EventEngine）。
            data_mgr: 数据管理器（DataManager）。

        Returns:
            卖出的股票数（清仓的持仓数量，非股数）。
        """
        # 局部导入避免循环依赖
        from gugu.engine.event_engine import EVENT_STOP_LOSS

        portfolio = broker.get_portfolio()
        symbols = list(portfolio.keys())
        if not symbols:
            return 0
        stopped_symbols: set[str] = set()

        # 批量拉取所有持仓的历史数据
        try:
            batch_dfs = await data_mgr.fetch_stock_history_batch(symbols, days=120)
        except Exception as e:
            logger.warning("批量拉取持仓历史数据失败，回退到逐股获取: {}", e)
            batch_dfs = {}

        # 先收集需止损的持仓信息，避免遍历中修改（P-10+D-09）
        stop_list: list[tuple[str, float, float, Position]] = []
        for symbol, pos in portfolio.items():
            df = batch_dfs.get(symbol, pd.DataFrame())
            if df.empty:
                with contextlib.suppress(Exception):
                    df = await data_mgr.fetch_stock_history(symbol, days=120)

            # 移动止损更新：如果有 trailing_stop 状态，先更新
            # 自动补建缺失的 trailing_stop 状态
            if not pos.trailing_stop and pos.avg_cost > 0 and not df.empty:
                try:
                    state = self._trailing_stop_engine.init_stop(entry_price=pos.avg_cost, df=df)
                    pos.trailing_stop = self._trailing_stop_engine.state_to_dict(state)
                    if state.current_stop_price > 0 and (
                        pos.stop_loss_price is None
                        or pos.stop_loss_price <= 0
                        or pos.stop_loss_price < state.current_stop_price
                    ):
                        pos.stop_loss_price = state.current_stop_price
                    logger.info(
                        "持仓 {} 补建 trailing_stop 状态：入场价={:.3f} 止损价={:.3f}",
                        symbol,
                        pos.avg_cost,
                        pos.stop_loss_price,
                    )
                except Exception as e:
                    logger.warning("{} 补建 trailing_stop 失败: {}", symbol, e)

            # 移动止损更新
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
                            "移动止损触发: {} 信号={}, 止损价={:.2f}",
                            symbol,
                            signal.value,
                            state.current_stop_price,
                        )
                        if pos.available > 0:
                            stop_list.append((symbol, pos.current_price, float(pos.available), pos))
                        continue
                    elif signal in (TrailingStopSignal.WARNING, TrailingStopSignal.ALERT):
                        logger.info(
                            "移动止损预警: {} 信号={}, 跌破浪谷 {} 次",
                            symbol,
                            signal.value,
                            state.valley_break_count,
                        )
                        continue
                except Exception as e:
                    logger.warning("{} 移动止损更新失败: {}", symbol, e)

            # P0 修复（v0.8.2）：持仓时间检查 — 防止阴跌套牢
            # 放在固定止损前，确保不会因 stop_loss_price 未设置而跳过
            entry_date = getattr(pos, "entry_date", None) or ""
            if entry_date and pos.avg_cost > 0 and pos.current_price > 0:
                try:
                    current_pnl = (pos.current_price - pos.avg_cost) / pos.avg_cost
                    hd_result = risk.check_holding_duration(symbol, str(entry_date), current_pnl)
                    if hd_result.action == RiskAction.HALT:
                        stop_list.append((symbol, pos.current_price, float(pos.available), pos))
                        logger.warning(
                            "持仓时间超限（HALT）: {} entry={} pnl={:.2%} — {}",
                            symbol,
                            entry_date,
                            current_pnl,
                            hd_result.message,
                        )
                    elif hd_result.action == RiskAction.WARN:
                        logger.warning(
                            "持仓时间超限（WARN）: {} entry={} pnl={:.2%} — {}",
                            symbol,
                            entry_date,
                            current_pnl,
                            hd_result.message,
                        )
                except Exception as e:
                    logger.debug("持仓时间检查失败 {}: {}", symbol, e)

            # 固定止损检查
            stop_price = getattr(pos, "stop_loss_price", None)
            if stop_price is None or stop_price <= 0:
                continue
            if pos.current_price <= stop_price and pos.available > 0:
                stop_list.append((symbol, pos.current_price, float(pos.available), pos))

        # 执行止损卖出
        sold_count = 0
        for symbol, price, available, pos in stop_list:
            stop_price = float(pos.stop_loss_price or 0.0)
            # P1 风控增强：跳空缺口追踪
            # 如果现价显著低于止损价（跳空低开），记录缺口幅度并告警，
            # 防止"止损价=成交价"的认知偏差导致实际亏损超预期。
            gap_pct = 0.0
            if stop_price > 0 and price < stop_price:
                gap_pct = (stop_price - price) / stop_price
                if gap_pct >= 0.03:  # 缺口 >= 3% 视为显著跳空
                    logger.warning(
                        "⚠️ 止损跳空风险: {} 止损价={:.2f} 现价={:.2f} 缺口={:.2%} "
                        "（实际亏损将大于止损预期）",
                        symbol,
                        stop_price,
                        price,
                        gap_pct,
                    )
            logger.info(
                "止损触发: {} 现价 {} <= 止损价 {}，执行卖出"
                + (f" [跳空缺口 {gap_pct:.2%}]" if gap_pct >= 0.01 else ""),
                symbol,
                price,
                stop_price,
            )
            event_engine.put(
                EVENT_STOP_LOSS,
                {
                    "symbol": symbol,
                    "price": price,
                    "quantity": int(available),
                    "stop_price": float(pos.stop_loss_price),
                },
            )
            risk_result = risk.check_order(
                symbol=symbol,
                direction="sell",
                quantity=int(available),
                price=price,
                portfolio=broker.get_portfolio(),
                cash=broker.get_account().cash,
                prev_close=getattr(pos, "prev_close", None),
                is_st=bool(getattr(pos, "is_st", False)),
                is_suspended=bool(getattr(pos, "is_suspended", False)),
            )
            if risk_result.allowed:
                context = ExecutionContext(
                    signal_id=uuid.uuid4().hex,
                    strategy=getattr(pos, "entry_strategy", ""),
                    strategies=tuple(getattr(pos, "entry_strategies", None) or []),
                    attribution_method=getattr(pos, "entry_attribution_method", "primary"),
                    regime=getattr(pos, "entry_regime", ""),
                    data_quality=getattr(pos, "entry_data_quality", "unknown"),
                    market_env_snapshot=getattr(pos, "entry_market_env", None),
                    exit_reason="stop_loss",
                    parent_signal_id=getattr(pos, "entry_signal_id", ""),
                    # P1 风控增强：注入止损价和跳空缺口，便于成交账本追踪
                    stop_price=stop_price,
                    gap_from_stop_pct=gap_pct,
                )
                if getattr(broker, "supports_execution_context", False):
                    result = broker.order(
                        symbol, "sell", int(available), price, execution_context=context
                    )
                else:
                    result = broker.order(symbol, "sell", int(available), price)
                if result.success:
                    stopped_symbols.add(symbol)
                    self._record_exit_history(
                        symbol,
                        pos,
                        result,
                        risk_result,
                        reason="stop_loss",
                        exit_signal_id=context.signal_id,
                    )
                    # 只有完整清仓才移除退出监控；部分成交必须保留残仓状态。
                    fresh_pos = broker.get_position(symbol)
                    if fresh_pos is None:
                        self._take_profit_engine.unregister(symbol)
                    else:
                        persist_state = getattr(broker, "persist_state", None)
                        if callable(persist_state):
                            persist_state()

                    # 清仓后清理残余的止损/危险信号状态
                    if fresh_pos is not None and result.position_closed:
                        fresh_pos.trailing_stop = None
                        fresh_pos.danger_signals = None

                    alert_key = f"stop_loss:{symbol}"
                    if self._should_alert is None or self._should_alert(alert_key):
                        # P1 风控增强：跳空缺口显著时升级告警级别
                        gap_info = ""
                        alert_level = "warn"
                        if gap_pct >= 0.03:
                            gap_info = f"\n⚠️ 跳空缺口: {gap_pct:.2%}（实际亏损大于止损预期）"
                            alert_level = "halt"
                        elif gap_pct >= 0.01:
                            gap_info = f"\n跳空缺口: {gap_pct:.2%}"
                        notify_ok = await notifier.notify_risk_alert(
                            {
                                "level": alert_level,
                                "message": (
                                    f"止损卖出: {symbol} {result.quantity}股 @ {result.price}"
                                    f"{gap_info}"
                                ),
                                "suggestion": "止损价由移动止损引擎管理，已自动执行",
                            },
                            dedup_key=f"stop_loss_sold_{symbol}_{date.today().isoformat()}",
                        )
                        if not notify_ok:
                            logger.error(
                                "止损卖出 {} 已执行（{}股@{}）但飞书通知失败，请检查飞书配置与网络",
                                symbol,
                                result.quantity,
                                result.price,
                            )
                    else:
                        logger.info("止损卖出 {} 告警已去重，跳过通知", symbol)
                    sold_count += 1
                else:
                    # v0.6.1: track_live 模式拒绝下单时，推送止损提醒卡片（用户手动执行）
                    # LivePositionTracker.order() 返回 message 含 "track_live"
                    if "track_live" in (result.message or "").lower():
                        alert_key = f"stop_loss_manual:{symbol}"
                        if self._should_alert is None or self._should_alert(alert_key):
                            logger.warning(
                                "[track_live] 止损提醒: {} 现价 {} ≤ 止损价 {}，请手动在券商APP卖出 {}股",
                                symbol,
                                price,
                                pos.stop_loss_price,
                                int(available),
                            )
                            name = lookup_stock_name(symbol) or symbol
                            pnl = (price - pos.avg_cost) * int(available)
                            # P1 风控增强：跳空缺口提示
                            gap_warn = ""
                            if gap_pct >= 0.03:
                                gap_warn = (
                                    f"\n⚠️ 跳空缺口 {gap_pct:.2%}："
                                    f"实际成交价可能低于止损价 {stop_price:.2f}，亏损将大于预期"
                                )
                            notify_ok = await notifier.notify_risk_alert(
                                {
                                    "level": "halt",
                                    "message": (
                                        f"止损提醒（需手动执行）: {name}({symbol}) 现价 ￥{price:.2f} "
                                        f"≤ 止损价 ￥{stop_price:.2f}，"
                                        f"持仓盈亏约 ￥{pnl:+,.2f}"
                                        f"{gap_warn}"
                                    ),
                                    "suggestion": (
                                        f"track_live 模式不自动下单，请手动在券商APP以 ￥{price:.2f} 附近挂单，"
                                        f"卖出 {int(available)}股"
                                    ),
                                },
                                dedup_key=f"stop_loss_manual_{symbol}_{date.today().isoformat()}",
                            )
                            if not notify_ok:
                                logger.error(
                                    "[track_live] 止损提醒 {} 通知未发送（可能是飞书去重/配置/网络问题，首条通知已发送则为正常去重）",
                                    symbol,
                                )
                        else:
                            logger.info("[track_live] 止损提醒 {} 已去重，跳过通知", symbol)
                    else:
                        logger.warning("止损卖出 {} 下单失败: {}", symbol, result.message)
            else:
                logger.warning("止损卖出 {} 被风控拦截: {}", symbol, risk_result.message)

        # 财叔动态止盈减仓检查（与移动止损互补）。使用止损后的最新持仓，
        # 并跳过本轮已处理的标的，避免同一轮同时止损又止盈。
        await self._check_take_profit(
            broker,
            risk,
            notifier,
            broker.get_portfolio(),
            self._should_alert,
            skip_symbols=stopped_symbols,
        )

        return sold_count

    async def _check_take_profit(
        self,
        broker: Any,
        risk: Any,
        notifier: Any,
        portfolio: dict[str, Position],
        should_alert: Callable[[str], bool] | None = None,
        skip_symbols: set[str] | None = None,
    ) -> None:
        """财叔动态止盈减仓检查（四步策略）。

        在止损检查之后执行，遍历持仓检查是否触发止盈减仓。
        """
        from datetime import date as _date

        today_str = _date.today().isoformat()

        # 缓存 portfolio 和 cash 快照，避免循环内重复查询
        cached_portfolio = portfolio
        cached_cash = broker.get_account().cash

        for symbol, pos in portfolio.items():
            if skip_symbols and symbol in skip_symbols:
                continue
            if pos.available <= 0:
                continue

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

            # 计算减仓数量
            reduce_qty = int(pos.available * tp_signal.reduce_ratio)
            reduce_qty = (reduce_qty // 100) * 100
            if reduce_qty < 100:
                logger.info(
                    "[take_profit] {} 减仓数量不足 100 股（available={}, ratio={:.0%}），跳过",
                    symbol,
                    pos.available,
                    tp_signal.reduce_ratio,
                )
                continue

            logger.warning(
                "[take_profit] {} 触发第{}步止盈减仓: {}，减仓 {} 股",
                symbol,
                tp_signal.trigger_step,
                tp_signal.reason,
                reduce_qty,
            )

            risk_result = risk.check_order(
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
                    "[take_profit] {} 止盈减仓被风控拦截: {}", symbol, risk_result.message
                )
                continue

            context = ExecutionContext(
                signal_id=uuid.uuid4().hex,
                strategy=getattr(pos, "entry_strategy", ""),
                strategies=tuple(getattr(pos, "entry_strategies", None) or []),
                attribution_method=getattr(pos, "entry_attribution_method", "primary"),
                regime=getattr(pos, "entry_regime", ""),
                data_quality=getattr(pos, "entry_data_quality", "unknown"),
                market_env_snapshot=getattr(pos, "entry_market_env", None),
                exit_reason=f"take_profit_step_{tp_signal.trigger_step}",
                parent_signal_id=getattr(pos, "entry_signal_id", ""),
            )

            if getattr(broker, "supports_execution_context", False):
                result = broker.order(
                    symbol, "sell", reduce_qty, pos.current_price, execution_context=context
                )
            else:
                result = broker.order(symbol, "sell", reduce_qty, pos.current_price)
            if not result.success:
                # v0.6.1: track_live 模式拒绝下单时，推送止盈减仓提醒卡片
                if "track_live" in (result.message or "").lower():
                    logger.warning(
                        "[track_live] 止盈减仓提醒: {} 触发 {}，请手动在券商APP卖出 {}股",
                        symbol,
                        tp_signal.trigger_step,
                        reduce_qty,
                    )
                    alert_key = f"take_profit_manual:{symbol}:{tp_signal.trigger_step}"
                    should_alert_fn = should_alert or self._should_alert
                    if should_alert_fn is None or should_alert_fn(alert_key):
                        name = lookup_stock_name(symbol) or symbol
                        pnl = (pos.current_price - pos.avg_cost) * reduce_qty
                        notify_ok = await notifier.notify_risk_alert(
                            {
                                "level": "warn",
                                "message": (
                                    f"止盈减仓提醒（需手动执行）: {name}({symbol}) 当前价 ￥{pos.current_price:.2f}，"
                                    f"触发第{tp_signal.trigger_step}步 - {tp_signal.reason}，"
                                    f"本次盈亏约 ￥{pnl:+,.2f}"
                                ),
                                "suggestion": (
                                    f"track_live 模式不自动下单，请手动在券商APP以 ￥{pos.current_price:.2f} 附近挂单，"
                                    f"卖出 {reduce_qty}股（减仓比例 {tp_signal.reduce_ratio:.0%}）"
                                ),
                            },
                            dedup_key=(
                                f"take_profit_manual_{symbol}_{tp_signal.trigger_step}_"
                                f"{date.today().isoformat()}"
                            ),
                        )
                        if not notify_ok:
                            logger.error(
                                "[track_live] 止盈减仓提醒 {} 通知失败，请检查飞书配置", symbol
                            )
                    else:
                        logger.info(
                            "[track_live] 止盈减仓提醒 {} step={} 已去重，跳过通知",
                            symbol,
                            tp_signal.trigger_step,
                        )
                else:
                    logger.warning("[take_profit] {} 止盈减仓下单失败", symbol)
                continue

            exit_signal_id = context.signal_id
            self._record_exit_history(
                symbol,
                pos,
                result,
                risk_result,
                reason=f"take_profit_step_{tp_signal.trigger_step}",
                exit_signal_id=exit_signal_id,
            )
            self._take_profit_engine.mark_reduced(
                symbol=symbol,
                executed_qty=result.quantity,
                price=result.price,
                signal=tp_signal,
                today_str=today_str,
                requested_qty=reduce_qty,
            )
            if result.position_closed:
                self._take_profit_engine.unregister(symbol)
            persist_state = getattr(broker, "persist_state", None)
            if callable(persist_state) and not result.position_closed:
                persist_state()

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
            alert_key = f"take_profit:{symbol}:{tp_signal.trigger_step}"
            should_alert_fn = should_alert or self._should_alert
            if should_alert_fn is None or should_alert_fn(alert_key):
                notify_ok = await notifier.notify_take_profit_alert(
                    symbol, stock_name, signal_dict, state_dict
                )
                if not notify_ok:
                    logger.error(
                        "[take_profit] {} 止盈减仓已执行（{}股@{}）但飞书通知失败，请检查飞书配置",
                        symbol,
                        result.quantity,
                        result.price,
                    )
            else:
                logger.info(
                    "[take_profit] {} step={} 止盈减仓通知已去重，跳过通知",
                    symbol,
                    tp_signal.trigger_step,
                )
