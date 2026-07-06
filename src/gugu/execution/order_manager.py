"""订单执行管理器：从信号到下单的全流程。

提取自 TradingEngine._process_signal() 的 paper/live 模式下单逻辑，
形成独立可测试的 OrderManager 类。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from gugu.analysis.trailing_stop import TrailingStopEngine
from gugu.analysis.dynamic_take_profit import DynamicTakeProfitEngine
from gugu.analysis.signal_tracker import SignalTracker
from gugu.config import get_config
from gugu.engine.event_engine import (
    EVENT_ORDER_FILLED,
    EVENT_ORDER_SUBMITTED,
    EVENT_SIGNAL,
    EventEngine,
)
from gugu.engine.signal_pipeline import record_signal_history
from gugu.utils.log import get_logger

logger = get_logger()


def inject_performance_summary(signal: dict[str, Any]) -> None:
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
                else [r.get("strategies", "")]
            ).intersection(strategies)
        ]
        if len(relevant) < 3:
            return

        wins = [r for r in relevant if r.get("return_10d", 0) > 0]
        win_rate = len(wins) / len(relevant)
        avg_return = sum(r.get("return_10d", 0) for r in relevant) / len(relevant)

        signal["performance_summary"] = {
            "win_rate": round(win_rate, 2),
            "avg_return_10d": round(avg_return, 2),
            "total_signals": len(relevant),
            "total_wins": len(wins),
        }
    except Exception:
        pass


class OrderManager:
    """订单执行管理器。

    负责将策略信号转化为实际下单，包含：
    - ATR 波动率仓位调整
    - 最低 1 手保底
    - 风控检查
    - 下单执行
    - 移动止损初始化
    - 事件推送
    - 信号历史记录
    """

    def __init__(
        self,
        *,
        trailing_stop_engine: Any = None,
        take_profit_engine: Any = None,
    ) -> None:
        self._trailing_stop_engine = (
            trailing_stop_engine if trailing_stop_engine is not None else TrailingStopEngine()
        )
        self._take_profit_engine = (
            take_profit_engine if take_profit_engine is not None else DynamicTakeProfitEngine()
        )

    async def execute(
        self,
        signal: dict[str, Any],
        broker: Any,
        risk: Any,
        notifier: Any,
        event_engine: EventEngine,
        data_mgr: Any = None,
    ) -> dict[str, Any] | None:
        """执行信号并返回下单结果 dict。

        Args:
            signal: 策略信号 dict。
            broker: 交易执行器（PaperBroker / QmtBroker）。
            risk: 风控管理器（RiskManager）。
            notifier: 飞书通知器（FeishuNotifier）。
            event_engine: 事件引擎（EventEngine）。
            data_mgr: 数据管理器（可选，当前未使用，保留兼容性）。

        Returns:
            下单结果 dict（含 risk_allowed / risk_message / order_result / notify_ok），
            若跳过下单则返回 None。
        """
        symbol = signal["symbol"]
        direction = signal["direction"]
        price = signal.get("price", 0)

        # 使用 wisdom 调整后的仓位比例
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

        account = broker.get_account()
        target_value = account.total_value * suggested_ratio
        quantity = int(target_value / price / 100) * 100 if price > 0 else 0

        # 价格合法性校验前置
        if price <= 0:
            logger.warning(f"{symbol} 信号价格异常 price={price}，跳过下单并发送告警")
            notify_alert_ok = await notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"{symbol} 信号价格异常 price={price}，已跳过下单",
                    "suggestion": "检查数据源是否返回异常价格（0/负值）",
                }
            )
            if not notify_alert_ok:
                logger.error(f"{symbol} 价格异常告警通知失败")
            return None

        # 最低1手保底
        max_ratio = get_config().risk.max_position_ratio
        one_lot_value = price * 100
        if quantity <= 0 and account.cash >= one_lot_value:
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
            notify_alert_ok = await notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": (
                        f"{symbol} 计算下单数量为 0"
                        f"（price={price}, ratio={suggested_ratio:.2%}, cash={account.cash:.0f}），已跳过"
                    ),
                    "suggestion": "检查仓位比例配置或资金是否充足",
                }
            )
            if not notify_alert_ok:
                logger.error(f"{symbol} 数量为0告警通知失败")
            return None

        # 风控检查
        portfolio = broker.get_portfolio()
        risk_result = risk.check_order(
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
            notify_alert_ok = await notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"{symbol} {direction} 被风控拦截: {risk_result.message}",
                    "suggestion": "检查仓位/日亏/涨跌停",
                }
            )
            if not notify_alert_ok:
                logger.error(f"{symbol} 风控拦截告警通知失败")
            # 风控拦截也记录历史
            record_signal_history(signal, risk_result, None)
            return {
                "risk_allowed": False,
                "risk_message": risk_result.message,
                "order_result": None,
                "notify_ok": False,
            }

        # 下单
        result = broker.order(symbol, direction, quantity, price)

        # 买入成功后，将止损价与 L3 元数据注入 Position
        if result.success and direction == "buy":
            pos = broker.get_position(symbol)
            if pos is not None:
                if signal.get("prev_close"):
                    pos.prev_close = float(signal["prev_close"])
                pos.is_st = bool(signal.get("is_st", False))
                pos.is_suspended = bool(signal.get("is_suspended", False))

                # 初始化移动止损状态
                df_for_stop = (
                    signal.pop("_df", None)
                    if isinstance(signal.get("_df"), pd.DataFrame)
                    else None
                )
                trailing_state = self._trailing_stop_engine.init_stop(
                    entry_price=result.price,
                    df=df_for_stop,
                )
                # wisdom 预设的止损价若比 ATR 自适应止损更保守，则优先采纳
                stop_loss = signal.get("stop_loss_price")
                if stop_loss and stop_loss > 0:
                    if stop_loss > trailing_state.current_stop_price:
                        trailing_state.initial_stop_price = float(stop_loss)
                        trailing_state.current_stop_price = float(stop_loss)
                    pos.stop_loss_price = float(stop_loss)
                else:
                    pos.stop_loss_price = trailing_state.initial_stop_price
                pos.trailing_stop = self._trailing_stop_engine.state_to_dict(trailing_state)

                # 注册财叔动态止盈监控
                self._take_profit_engine.register(symbol, result.price)

                # 注入危险信号信息
                danger_info = signal.get("danger_signals", {})
                if danger_info.get("signals"):
                    pos.danger_signals = danger_info["signals"]

        # 事件推送：信号 + 订单
        event_engine.put(
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
        event_engine.put(
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

        # 构造返回结果
        order_result = {
            "success": result.success,
            "price": result.price,
            "quantity": result.quantity,
            "commission": result.commission,
            "message": result.message,
        }
        signal["order_result"] = order_result

        # 通知
        inject_performance_summary(signal)
        notify_ok = await notifier.notify_signal(signal)
        if not notify_ok:
            logger.error(
                f"{symbol} 下单已执行（success={result.success}, qty={result.quantity}@{result.price}）"
                f"但飞书通知失败，请检查飞书配置与网络"
            )

        # 记录信号历史
        record_signal_history(signal, risk_result, result)

        return {
            "risk_allowed": True,
            "risk_message": risk_result.message,
            "order_result": order_result,
            "notify_ok": notify_ok,
        }
