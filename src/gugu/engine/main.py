"""主引擎：交易系统核心。

整合数据层、策略层、风控、执行、通知、决策层。
"""
from __future__ import annotations

import asyncio
from typing import Any

from gugu.config import settings
from gugu.data import data_manager
from gugu.engine.signal_router import SignalRouter
from gugu.execution import PaperBroker
from gugu.notifier import FeishuNotifier
from gugu.risk import RiskManager
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
        self._strategies = get_enabled_strategies()
        self._router = SignalRouter(self._strategies)
        self._risk = RiskManager()
        self._broker = PaperBroker()
        self._notifier = FeishuNotifier()
        self._wisdom = WisdomAdvisor()
        self._selector = StockSelector(self._dm)
        self._watchlist: list[str] = self._load_watchlist()

        logger.info(
            f"交易引擎初始化: {len(self._strategies)} 策略, "
            f"自选股 {len(self._watchlist)} 只, 模式=paper"
        )

    @property
    def auto_select_enabled(self) -> bool:
        return bool(settings().get("strategy", {}).get("auto_select", False))

    def _load_watchlist(self) -> list[str]:
        """加载自选股列表。"""
        # 从配置加载，后续可扩展为数据库
        return ["600519", "300750", "000858", "601318", "000333"]

    async def run_daily_cycle(self) -> None:
        """每日交易循环：采集 → 策略 → 风控 → 执行 → 通知。"""
        if not is_trading_day():
            logger.info("非交易日，跳过")
            return

        logger.info("=== 开始每日交易循环 ===")

        # 1. T+1 结算
        self._broker.settle_t_plus_1()
        self._risk.reset()

        # 2. 采集行情
        await self._update_prices()

        # 3. 自动选股 + 自选股扫描
        if self.auto_select_enabled:
            selected = self._selector.select()
            for s in selected:
                if s["symbol"] not in self._watchlist:
                    self._watchlist.append(s["symbol"])
                    logger.info(f"自动选股加入 watchlist: {s['symbol']}")

        signals = await self._scan_signals()

        # 4. 执行信号（风控 + 下单）
        for signal in signals:
            await self._process_signal(signal)

        # 5. 检查日亏
        await self._check_daily_loss()

        logger.info("=== 每日交易循环完成 ===")

    async def _update_prices(self) -> None:
        """更新持仓现价。"""
        portfolio = self._broker.get_portfolio()
        if not portfolio:
            return
        symbols = list(portfolio.keys())
        try:
            df = self._dm.fetch_stock_realtime(symbols)
            for _, row in df.iterrows():
                self._broker.update_price(row["symbol"], float(row["price"]))
        except Exception as e:
            logger.error(f"更新现价失败: {e}")

    async def _scan_signals(self) -> list[dict[str, Any]]:
        """扫描自选股策略信号。"""
        signals = []
        for symbol in self._watchlist:
            try:
                df = self._dm.fetch_stock_history(symbol, days=60)
                if df.empty:
                    continue
                signal = self._router.route(df, symbol)
                if signal:
                    # 决策层增强
                    signal = self._wisdom.advise(signal)
                    signal["price"] = float(df.iloc[-1]["close"])
                    signals.append(signal)
                    logger.info(
                        f"信号: {signal['symbol']} {signal['direction']} "
                        f"置信度 {signal['confidence']} 策略 {signal['strategies']}"
                    )
            except Exception as e:
                logger.error(f"扫描 {symbol} 失败: {e}")
        return signals

    async def _process_signal(self, signal: dict[str, Any]) -> None:
        """处理单个信号：风控检查 → 下单 → 通知。"""
        symbol = signal["symbol"]
        direction = signal["direction"]
        price = signal.get("price", 0)

        # 计算下单数量：按风控 max_position_ratio 的 80% 作为目标仓位
        account = self._broker.get_account()
        max_ratio = settings().get("risk", {}).get("max_position_ratio", 0.30)
        target_value = account.total_value * max_ratio * 0.8
        quantity = int(target_value / price / 100) * 100 if price > 0 else 0

        if quantity <= 0:
            logger.warning(f"{symbol} 计算下单数量为 0，跳过")
            return

        # 风控检查
        portfolio = self._broker.get_portfolio()
        risk_result = self._risk.check_order(
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            price=price,
            portfolio=portfolio,
            cash=account.cash,
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

        # 通知
        signal["order_result"] = {
            "success": result.success,
            "price": result.price,
            "quantity": result.quantity,
            "commission": result.commission,
            "message": result.message,
        }
        await self._notifier.notify_signal(signal)

    async def _check_daily_loss(self) -> None:
        """检查当日亏损。"""
        account = self._broker.get_account()
        initial = settings().get("execution", {}).get("paper", {}).get(
            "initial_capital", 1_000_000
        )
        loss_pct = (initial - account.total_value) / initial

        risk_result = self._risk.check_daily_loss(loss_pct)
        if risk_result.action.value == "warn":
            await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"当日亏损 {loss_pct:.2%} 触发预警",
                    "suggestion": "关注持仓，考虑减仓",
                }
            )
        elif risk_result.action.value == "halt":
            await self._notifier.notify_risk_alert(
                {
                    "level": "halt",
                    "message": f"当日亏损 {loss_pct:.2%} 触发熔断，停止交易",
                    "suggestion": "检查持仓，必要时一键平仓",
                }
            )

    async def send_daily_report(self, period: str) -> None:
        """发送每日日报。"""
        account = self._broker.get_account()
        sector_df = self._dm.fetch_sector_flow()

        data = {
            "market_summary": {
                "total_value": account.total_value,
                "cash": account.cash,
                "positions_count": len(account.positions),
            },
            "sector_top": sector_df.head(5).to_dict("records") if not sector_df.empty else [],
            "signals": [],
            "portfolio_summary": {
                sym: {
                    "quantity": pos.quantity,
                    "profit": pos.profit,
                    "market_value": pos.market_value,
                }
                for sym, pos in account.positions.items()
            },
        }
        await self._notifier.notify_daily_report(period, data)


def run_engine() -> None:
    """运行交易引擎（同步入口）。"""
    engine = TradingEngine()
    asyncio.run(engine.run_daily_cycle())
