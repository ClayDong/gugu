"""日报管理器：从 TradingEngine 提取的信号汇总报告生成器。

负责收集市场状态、风控状态、持仓信息、信号历史，
并委托 FeishuNotifier 发送日报卡片。
"""

from __future__ import annotations

from typing import Any

from gugu.config import PROJECT_ROOT
from gugu.data.stock_names import lookup_stock_name
from gugu.notifier.feishu import FeishuNotifier
from gugu.utils.log import get_logger

logger = get_logger()


class DailyReportManager:
    """日报管理器：收集报告数据并推送飞书卡片。

    TradingEngine 创建此管理器并传入 broker/risk/dm/notifier/regime_detector 等依赖。
    """

    def __init__(
        self,
        broker: Any,
        risk: Any,
        data_manager: Any,
        notifier: FeishuNotifier,
        regime_detector: Any,
    ) -> None:
        self._broker = broker
        self._risk = risk
        self._dm = data_manager
        self._notifier = notifier
        self._regime_detector = regime_detector

    async def send_report(
        self,
        period: str,
        check_holdings_sell: Any = None,
        engine_lock: Any = None,
        engine_running: Any = None,
        filtered_signals: list[dict[str, Any]] | None = None,
    ) -> None:
        """发送信号汇总报告。

        Args:
            period: 'morning', 'noon', 'close'
            check_holdings_sell: 收盘后检查持仓卖出信号的回调
            engine_lock: TradingEngine 的 asyncio.Lock
            engine_running: TradingEngine 的 _running 属性引用
            filtered_signals: 当日被过滤的信号列表（解决沉默拒绝问题）
        """
        account = self._broker.get_account()
        today_signals = self._load_today_signals()

        performance_report = None
        regime_info = None
        risk_info = None
        trailing_stop_info = None

        if period == "close":
            performance_report = await self._get_performance_report()
            regime_info = await self._get_regime_info()
            risk_info = self._get_risk_info(account)
            trailing_stop_info = self._get_trailing_stop_info()

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
            "filtered_signals": filtered_signals or [],
        }

        await self._fill_portfolio_names(data)
        await self._notifier.retry_queued()
        await self._notifier.notify_daily_report(period, data)

        if period == "close" and check_holdings_sell is not None:
            try:
                if engine_lock is not None:
                    async with engine_lock:
                        if engine_running and engine_running():
                            logger.warning("交易循环仍在运行，跳过收盘持仓卖出检查")
                            return
                        self._set_running(engine_running, True)
                        try:
                            portfolio = self._broker.get_portfolio()
                            if portfolio:
                                sell_alerts = await check_holdings_sell(portfolio)
                                if sell_alerts:
                                    await self._notifier.notify_holdings_sell_alert(sell_alerts)
                        finally:
                            self._set_running(engine_running, False)
            except Exception as e:
                logger.warning(f"持仓卖出检查失败（不影响日报）: {e}")

    async def _get_performance_report(self) -> dict[str, Any] | None:
        try:
            from gugu.analysis.signal_tracker import SignalTracker

            tracker = SignalTracker()
            return await tracker.generate_report(days=30)
        except Exception as e:
            logger.warning(f"生成信号绩效报告失败: {e}")
            return None

    async def _get_regime_info(self) -> dict[str, Any] | None:
        try:
            regime = await self._regime_detector.detect()
            return {
                "regime": regime.get("regime", "unknown"),
                "reason": regime.get("reason", ""),
                "total_limit": regime.get("total_position_limit", 0),
            }
        except Exception as e:
            logger.debug(f"获取市场状态失败: {e}")
            return None

    def _get_risk_info(self, account: Any) -> dict[str, Any] | None:
        try:
            daily_loss_pct = 0.0
            start_value = getattr(self._broker, "daily_start_value", 0)
            if start_value and start_value > 0:
                daily_loss_pct = (start_value - account.total_value) / start_value
            return {
                "halted": self._risk.is_halted,
                "daily_loss_pct": round(daily_loss_pct, 4),
                "daily_start_value": start_value,
            }
        except Exception as e:
            logger.debug(f"获取风控状态失败: {e}")
            return None

    def _get_trailing_stop_info(self) -> list[dict[str, Any]] | None:
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
            return stops
        except Exception as e:
            logger.debug(f"获取移动止损状态失败: {e}")
            return None

    async def _fill_portfolio_names(self, data: dict[str, Any]) -> None:
        for sym, info in data["portfolio_summary"].items():
            name = lookup_stock_name(sym)
            if not name:
                try:
                    meta = await self._dm.fetch_stock_meta(sym)
                    name = meta.get("name", "")
                except Exception:
                    pass
            info["name"] = name or sym

    @staticmethod
    def _load_today_signals() -> list[dict[str, Any]]:
        """从 signals_history.jsonl 读取今日信号。"""
        import json
        from datetime import date as _date

        path = PROJECT_ROOT / "data" / "signals_history.jsonl"
        if not path.exists():
            return []

        today_str = _date.today().isoformat()
        seen: dict[str, dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = record.get("timestamp", "")
                    if not ts.startswith(today_str):
                        continue
                    key = f"{record.get('symbol', '')}_{record.get('direction', '')}"
                    if key not in seen:
                        seen[key] = record
            return list(seen.values())
        except Exception as e:
            logger.warning(f"读取今日信号失败: {e}")
            return []

    @staticmethod
    def _set_running(attr_getter: Any, value: bool) -> None:
        """设置 engine 的 _running 标志（通过 setattr hack）。"""
        try:
            if hasattr(attr_getter, "__self__"):
                attr_getter.__self__._running = value
        except Exception:
            pass
