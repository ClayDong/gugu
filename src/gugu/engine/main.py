"""主引擎：交易系统核心。

整合数据层、策略层、风控、执行、通知、决策层。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pandas as pd

from gugu.config import PROJECT_ROOT, settings
from gugu.data import data_manager
from gugu.engine.signal_router import SignalRouter
from gugu.execution import PaperBroker
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
        self._strategies = get_enabled_strategies()
        self._router = SignalRouter(self._strategies)
        self._risk = RiskManager()
        self._broker = PaperBroker()
        self._notifier = FeishuNotifier()
        self._last_cycle_date: date | None = None  # 上次循环日期，防止同日多次 reset
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
        """加载自选股列表（从 settings.yaml 的 watchlist 配置读取）。"""
        watchlist = settings().get("watchlist", [])
        if not watchlist:
            logger.warning("settings.yaml 中未配置 watchlist，使用空自选股列表")
            return []
        # 规范化：去除空白、补零
        return [str(code).strip().zfill(6) for code in watchlist]

    async def run_daily_cycle(self) -> None:
        """每日交易循环：采集 → 策略 → 风控 → 执行 → 通知。"""
        # 铁律：L2 熔断状态下，即使同日再次调用也必须保持熔断
        if self._risk.is_halted:
            logger.warning("L2 熔断状态激活中，跳过本次交易循环（需人工 reset_halt 后恢复）")
            return

        if not is_trading_day():
            logger.info("非交易日，跳过")
            return

        logger.info("=== 开始每日交易循环 ===")

        # 1. T+1 结算
        self._broker.settle_t_plus_1()
        # 仅在新交易日开始时 reset 风控（防止同日多次调用绕过 L2 熔断）
        today = date.today()
        if self._last_cycle_date != today:
            self._risk.reset()
            self._last_cycle_date = today

        # 2. 采集行情
        await self._update_prices()

        # 3. 自动选股 + 自选股扫描
        if self.auto_select_enabled:
            # 降级源不支持全市场快照，降级时跳过自动选股
            if self._dm.is_degraded:
                logger.info("数据源已降级，跳过自动选股（降级源不支持全市场快照）")
            else:
                selected = self._selector.select()
                if not selected:
                    logger.info("自动选股未产生候选")
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

        self._write_heartbeat("ok")
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

                meta = self._dm.fetch_stock_meta(symbol)
                signal = self._router.route(df, symbol, name=meta.get("name", ""))
                if signal:
                    # L3 元数据
                    signal["prev_close"] = (
                        float(df.iloc[-2]["close"]) if len(df) >= 2 else float(df.iloc[-1]["close"])
                    )
                    signal["is_st"] = bool(meta.get("is_st", False))
                    signal["is_suspended"] = bool(meta.get("is_suspended", False))
                    signal["name"] = signal.get("name") or meta.get("name", "")
                    signal["price"] = float(df.iloc[-1]["close"])
                    # 先设置基础仓位比例，再交给 wisdom 调整
                    max_ratio = settings().get("risk", {}).get("max_position_ratio", 0.30)
                    signal["suggested_position_ratio"] = max_ratio * 0.8
                    # 标记是否已有持仓（wisdom 据此决定试仓/加码）
                    portfolio = self._broker.get_portfolio()
                    signal["has_position"] = symbol in portfolio
                    # 决策层增强（可能调整仓位比例、预设止损、过滤入场）
                    signal = self._wisdom.advise(signal)
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

        # 入场过滤：wisdom 判定低置信度，仅通知不下单
        if signal.get("wisdom_filtered"):
            logger.info(f"{symbol} 信号被 wisdom 入场过滤，仅通知不下单")
            await self._notifier.notify_signal(signal)
            return

        # 使用 wisdom 调整后的仓位比例（已在 _scan_signals 中设置）
        suggested_ratio = signal.get("suggested_position_ratio", 0.0)
        if suggested_ratio <= 0:
            max_ratio = settings().get("risk", {}).get("max_position_ratio", 0.30)
            suggested_ratio = max_ratio * 0.8

        account = self._broker.get_account()
        target_value = account.total_value * suggested_ratio
        quantity = int(target_value / price / 100) * 100 if price > 0 else 0

        # 最低1手保底：试仓比例过小时（如高价股），确保至少买入100股
        if quantity <= 0 and price > 0 and account.cash >= price * 100:
            quantity = 100
            logger.info(f"{symbol} 试仓比例过小，使用最低1手保底: 100股")

        if quantity <= 0:
            logger.warning(f"{symbol} 计算下单数量为 0，跳过")
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
            await self._notifier.notify_risk_alert(
                {
                    "level": "warn",
                    "message": f"当日亏损 {loss_pct:.2%} 触发预警",
                    "suggestion": "关注持仓，考虑减仓",
                }
            )
        elif risk_result.action == RiskAction.HALT:
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

    async def shutdown(self) -> None:
        """关闭引擎资源（HTTP client 等）。"""
        await self._notifier.close()
        self._write_heartbeat("shutdown")

    def reset_halt(self) -> None:
        """手动解除 L2 熔断（需人工确认后执行）。"""
        self._risk.reset()
        self._broker.reset_daily_start_value()
        logger.warning("L2 熔断状态已手动重置")

    def _write_heartbeat(self, status: str) -> None:
        """写入心跳文件，便于外部监控。"""
        try:
            hb_dir = PROJECT_ROOT / "data"
            hb_dir.mkdir(exist_ok=True)
            path = hb_dir / "heartbeat.json"
            account = self._broker.get_account()
            payload = {
                "last_cycle_at": pd.Timestamp.now().isoformat(),
                "status": status,
                "halted": self._risk.is_halted,
                "total_value": account.total_value,
                "cash": account.cash,
                "positions_count": len(account.positions),
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"写入心跳文件失败: {e}")


def run_engine() -> None:
    """运行交易引擎（同步入口）。"""
    engine = TradingEngine()
    asyncio.run(engine.run_daily_cycle())
