"""FastAPI 应用：gugu 交易系统 Web 仪表盘。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from gugu.config import settings
from gugu.data import data_manager
from gugu.data.stock_names import lookup_stock_name
from gugu.execution import PaperBroker
from gugu.execution.base import Direction
from gugu.risk import RiskManager


class OrderRequest(BaseModel):
    """下单请求体校验模型。"""

    symbol: str
    direction: str = "buy"
    quantity: int
    price: float | None = None


def _execution_mode() -> str:
    """读取当前执行模式。Web 下单仅允许 paper 模拟盘。"""
    return str(settings().get("execution", {}).get("mode", "signal_only")).lower().strip()


# P1 修复：全局单例 Broker/RiskManager，避免每次 HTTP 请求从磁盘重载状态
_web_broker: Any = None
_web_risk: Any = None


def _get_web_broker() -> PaperBroker:
    global _web_broker
    if _web_broker is None:
        _web_broker = PaperBroker()
    return cast(PaperBroker, _web_broker)


def _get_web_risk() -> RiskManager:
    global _web_risk
    if _web_risk is None:
        _web_risk = RiskManager()
    return cast(RiskManager, _web_risk)


from gugu.utils.log import get_logger

logger = get_logger()

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。"""
    app = FastAPI(title="gugu 交易仪表盘", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        """仪表盘主页。"""
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return "<h1>gugu 仪表盘</h1><p>静态文件未找到</p>"

    @app.get("/api/account")
    async def get_account() -> dict[str, Any]:
        """获取账户信息。"""
        broker = _get_web_broker()
        account = broker.get_account()
        return {
            "cash": account.cash,
            "total_value": account.total_value,
            "positions_count": len(account.positions),
            "market_value": account.total_value - account.cash,
        }

    @app.get("/api/portfolio")
    async def get_portfolio() -> dict[str, Any]:
        """获取持仓详情。"""
        broker = _get_web_broker()
        portfolio = broker.get_portfolio()
        positions = []
        for sym, pos in portfolio.items():
            positions.append(
                {
                    "symbol": sym,
                    "name": lookup_stock_name(sym),
                    "quantity": pos.quantity,
                    "available": pos.available,
                    "avg_cost": round(pos.avg_cost, 2),
                    "current_price": round(pos.current_price, 2),
                    "market_value": round(pos.market_value, 2),
                    "profit": round(pos.profit, 2),
                    "profit_ratio": round(pos.profit_ratio, 4),
                }
            )
        return {"positions": positions}

    @app.get("/api/trades")
    async def get_trades() -> dict[str, Any]:
        """获取交易记录。"""
        broker = _get_web_broker()
        trades = broker.trades
        for t in trades:
            if "name" not in t:
                t["name"] = lookup_stock_name(t.get("symbol", ""))
        return {"trades": trades, "count": len(trades)}

    @app.get("/api/risk")
    async def get_risk_status() -> dict[str, Any]:
        """获取风控状态。"""
        risk = _get_web_risk()
        broker = _get_web_broker()
        account = broker.get_account()
        start_value = broker.daily_start_value
        loss_pct = 0.0
        if start_value > 0:
            loss_pct = (start_value - account.total_value) / start_value
        return {
            "halted": risk.is_halted,
            "daily_loss_pct": round(loss_pct, 4),
            "daily_start_value": round(start_value, 2),
        }

    @app.get("/api/heartbeat")
    async def get_heartbeat() -> dict[str, Any]:
        """获取系统心跳。"""
        hb_path = Path("data/heartbeat.json")
        if hb_path.exists():
            import json

            try:
                return cast(dict[str, Any], json.loads(hb_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return {"status": "unknown", "timestamp": None}

    @app.get("/api/equity/history")
    async def get_equity_history(days: int = 30) -> dict[str, Any]:
        """获取净值历史曲线数据。

        从 heartbeat_history.jsonl 读取，每行一个时间点的总资产。
        """
        import json
        from datetime import datetime, timedelta

        hb_dir = Path("data")
        path = hb_dir / "heartbeat_history.jsonl"
        if not path.exists():
            return {"points": [], "count": 0}

        cutoff = datetime.now() - timedelta(days=days)
        points: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            for line in lines:
                try:
                    rec = json.loads(line)
                    ts = datetime.fromisoformat(rec.get("last_cycle_at", ""))
                    if ts < cutoff:
                        continue
                    points.append(
                        {
                            "timestamp": rec["last_cycle_at"],
                            "total_value": rec.get("total_value", 0),
                            "cash": rec.get("cash", 0),
                            "status": rec.get("status", ""),
                            "halted": rec.get("halted", False),
                        }
                    )
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue
        except Exception as e:
            logger.warning(f"读取净值历史失败: {e}")

        return {"points": points, "count": len(points)}

    @app.get("/api/signals")
    async def get_signals(limit: int = 50) -> dict[str, Any]:
        """获取信号历史记录。"""
        import json
        from datetime import date as _date

        path = Path("data/signals_history.jsonl")
        if not path.exists():
            return {"signals": [], "count": 0}

        today_str = _date.today().isoformat()
        signals: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            for line in reversed(lines):  # 最新的在前
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    signals.append(
                        {
                            "timestamp": rec.get("timestamp", ""),
                            "symbol": rec.get("symbol", ""),
                            "name": rec.get("name", ""),
                            "direction": rec.get("direction", ""),
                            "price": rec.get("price", 0),
                            "confidence": rec.get("confidence", 0),
                            "strategies": rec.get("strategies", []),
                            "wisdom_filtered": rec.get("wisdom_filtered", False),
                            "filter_reason": rec.get("filter_reason", ""),
                            "wisdom_decision": rec.get("wisdom_decision", {}),
                            "suggested_position_ratio": rec.get("suggested_position_ratio", 0),
                            "stop_loss_price": rec.get("stop_loss_price"),
                            "stage": rec.get("stage", {}),
                            "danger_signals": rec.get("danger_signals", {}),
                            "decision_chain": rec.get("decision_chain", []),
                            "order_success": rec.get("order_success"),
                            "order_quantity": rec.get("order_quantity", 0),
                            "order_price": rec.get("order_price", 0),
                            "is_today": rec.get("timestamp", "").startswith(today_str),
                        }
                    )
                    if len(signals) >= limit:
                        break
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.warning(f"读取信号历史失败: {e}")

        return {"signals": signals, "count": len(signals)}

    @app.get("/api/books")
    async def get_books() -> dict[str, Any]:
        """获取仓颉蒸馏的书籍视角列表。"""
        try:
            from gugu.wisdom.book_router import BookPerspectiveRouter

            router = BookPerspectiveRouter()
            books = []
            for name in router.available_perspectives:
                summary = router.get_perspective_summary(name)
                books.append({"name": name, "summary": summary})
            return {"books": books, "count": len(books)}
        except Exception as e:
            logger.warning(f"获取书籍视角失败: {e}")
            return {"books": [], "count": 0, "error": str(e)}

    @app.get("/api/health")
    async def get_health() -> dict[str, Any]:
        """系统健康检查端点（A-08 修复）。

        返回系统运行状态：心跳、数据源、风控、资源使用。
        用于外部监控（supervisor/健康检查）或仪表盘状态显示。

        P2 增强：添加数据源健康监控详情（各通道状态、延迟、错误率）。
        """
        import json
        import os

        # 心跳状态
        hb_path = Path("data/heartbeat.json")
        hb_status = "unknown"
        hb_time = None
        if hb_path.exists():
            try:
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
                hb_status = hb.get("status", "unknown")
                hb_time = hb.get("last_cycle_at")
            except Exception:
                pass

        # 熔断状态
        try:
            risk = _get_web_risk()
            is_halted = risk.is_halted
        except Exception:
            is_halted = False

        # 数据源状态
        is_degraded = False
        data_sources = {
            "primary": {"name": "sina", "status": "unknown", "error_count": 0},
            "fallback": {"name": "eastmoney", "status": "unknown", "error_count": 0},
            "secondary": {"name": "akshare", "status": "unknown", "error_count": 0},
        }
        try:
            dm = data_manager()
            is_degraded = dm.is_degraded
            # 尝试获取数据源统计信息
            if hasattr(dm, "get_source_stats"):
                stats = dm.get_source_stats()
                for src, info in stats.items():
                    if src in data_sources:
                        data_sources[src].update(info)
            # 根据降级状态推断
            if is_degraded:
                data_sources["primary"]["status"] = "degraded"
                data_sources["fallback"]["status"] = "active"
            else:
                data_sources["primary"]["status"] = "active"
        except Exception:
            pass

        # 资金流数据源状态
        flow_source_status = "unknown"
        try:
            from gugu.notifier.flow_report import _FLOW_CACHE_FILE

            if _FLOW_CACHE_FILE.exists():
                import json as _json

                cached = _json.loads(_FLOW_CACHE_FILE.read_text(encoding="utf-8"))
                if cached.get("sources_unavailable"):
                    flow_source_status = "unavailable"
                elif cached.get("market", {}).get("main_net_inflow") is not None:
                    flow_source_status = "active"
                else:
                    flow_source_status = "partial"
        except Exception:
            pass

        return {
            "status": "ok",
            "heartbeat": hb_status,
            "last_cycle": hb_time,
            "halted": is_halted,
            "data_source": "degraded" if is_degraded else "primary",
            "data_sources": data_sources,
            "flow_source_status": flow_source_status,
            "mode": settings().get("execution", {}).get("mode", "unknown"),
            "pid": os.getpid(),
        }

    @app.get("/api/signals/performance")
    async def signal_performance(days: int = 30) -> dict[str, Any]:
        """信号绩效统计（Web 看板展示用）。"""
        from gugu.analysis.signal_tracker import SignalTracker

        try:
            tracker = SignalTracker()
            report = await tracker.generate_report(days=days)
            return {"success": True, "data": report}
        except Exception as e:
            logger.exception(f"获取信号绩效失败: {e}")
            return {"success": False, "message": str(e)[:200]}

    @app.get("/api/portfolio/correlation")
    async def portfolio_correlation() -> dict[str, Any]:
        """持仓相关性检查（高相关对告警）。"""
        import json as _json

        from gugu.data import data_manager
        from gugu.execution.paper import STATE_FILE

        try:
            if not STATE_FILE.exists():
                return {"success": True, "data": [], "message": "无持仓状态文件"}
            state = _json.loads(STATE_FILE.read_text(encoding="utf-8"))
            positions = state.get("positions", {})
            if not positions:
                return {"success": True, "data": [], "message": "当前无持仓"}

            from gugu.models import Position as PosModel

            dm = data_manager()
            portfolio = {k: PosModel(**v) for k, v in positions.items()}
            alerts = await RiskManager.check_correlation(portfolio, dm)
            return {"success": True, "data": alerts}
        except Exception as e:
            logger.exception(f"相关性检查失败: {e}")
            return {"success": False, "message": str(e)[:200]}

    @app.post("/api/halt/reset")
    async def reset_halt() -> dict[str, Any]:
        """手动解除 L2 熔断（使用 Web 单例 RiskManager）。"""
        risk = _get_web_risk()
        risk.reset()
        return {"success": True, "message": "L2 熔断已手动解除"}

    @app.post("/api/order")
    async def place_order(req: OrderRequest) -> dict[str, Any]:
        """通用模拟盘下单接口（买入/卖出）。

        仅在 paper 模式可用；真实 live/QMT 不通过 Web 下单。
        下单前统一经过 RiskManager.check_order()。
        """
        mode = _execution_mode()
        if mode != "paper":
            return {
                "status": "error",
                "message": f"当前模式为 {mode}，Web 下单仅允许 paper 模拟盘模式",
            }

        symbol = req.symbol.strip().zfill(6)
        raw_direction = req.direction.lower().strip()
        if raw_direction not in ("buy", "sell"):
            return {"status": "error", "message": f"不支持的方向: {req.direction}"}
        direction: Direction = cast(Direction, raw_direction)
        if req.quantity <= 0:
            return {"status": "error", "message": f"下单数量必须为正数: {req.quantity}"}
        if req.price is not None and req.price <= 0:
            return {"status": "error", "message": f"下单价格必须为正数: {req.price}"}

        broker = _get_web_broker()
        risk = _get_web_risk()
        account = broker.get_account()
        portfolio = broker.get_portfolio()
        position = portfolio.get(symbol)
        risk_price = req.price or (position.current_price if position else 0.0)
        if risk_price <= 0:
            return {"status": "error", "symbol": symbol, "message": "缺少有效价格，无法进行风控检查"}

        risk_result = risk.check_order(
            symbol,
            direction,
            req.quantity,
            risk_price,
            portfolio,
            cash=account.cash,
            prev_close=position.prev_close if position else None,
        )
        if not risk_result.allowed:
            return {
                "status": "error",
                "symbol": symbol,
                "message": f"风控拒绝: {risk_result.message}",
            }

        try:
            result = broker.order(symbol, direction, req.quantity, price=req.price)
            if result and result.success:
                return {
                    "status": "ok",
                    "symbol": symbol,
                    "direction": direction,
                    "quantity": result.quantity,
                    "price": round(result.price, 2) if result.price else None,
                    "message": f"模拟盘{'买入' if direction == 'buy' else '卖出'}成功",
                }
            reason = result.message if result else "下单返回空结果"
            return {"status": "error", "symbol": symbol, "message": reason}
        except Exception as e:
            logger.exception(f"模拟盘下单失败 {symbol}: {e}")
            return {"status": "error", "message": str(e)[:200]}

    @app.post("/api/stop_loss/{symbol}")
    async def stop_loss(symbol: str) -> dict[str, Any]:
        """手动触发指定持仓的止损卖出。"""
        broker = _get_web_broker()
        try:
            position = broker.get_position(symbol)
            if position is None or position.quantity <= 0:
                return {
                    "status": "error",
                    "message": f"未找到 {symbol} 的持仓",
                }
            quantity = position.quantity
            result = broker.order(symbol, "sell", quantity, price=None)
            if result.success:
                return {
                    "status": "ok",
                    "symbol": symbol,
                    "sold": result.quantity,
                    "price": round(result.price, 2),
                }
            return {
                "status": "error",
                "symbol": symbol,
                "message": result.message or "卖出下单失败",
            }
        except Exception as e:
            logger.error(f"止损卖出 {symbol} 失败: {e}")
            return {"status": "error", "message": str(e)}

    @app.post("/api/engine/run")
    async def trigger_engine_run() -> dict[str, Any]:
        """触发一次交易循环。"""
        from gugu.engine.main import TradingEngine

        engine = TradingEngine()
        try:
            await engine.run_daily_cycle()
            return {"success": True, "message": "交易循环执行完成"}
        except Exception as e:
            logger.exception(f"触发交易循环失败: {e}")
            return {"success": False, "message": str(e)[:200]}

    @app.get("/api/portfolio/detail")
    async def get_portfolio_detail() -> dict[str, Any]:
        """获取持仓详情（含移动止损状态和危险信号）。"""
        broker = _get_web_broker()
        portfolio = broker.get_portfolio()
        positions = []
        for sym, pos in portfolio.items():
            pos_data = {
                "symbol": sym,
                "name": lookup_stock_name(sym),
                "quantity": pos.quantity,
                "available": pos.available,
                "avg_cost": round(pos.avg_cost, 2),
                "current_price": round(pos.current_price, 2),
                "market_value": round(pos.market_value, 2),
                "profit": round(pos.profit, 2),
                "profit_ratio": round(pos.profit_ratio, 4),
                "stop_loss_price": round(pos.stop_loss_price, 2) if pos.stop_loss_price else 0,
                "trailing_stop": pos.trailing_stop,
                "danger_signals": pos.danger_signals,
            }
            positions.append(pos_data)
        return {"positions": positions, "count": len(positions)}

    # 注册知识库问答路由
    from gugu.web.qa import router as qa_router

    app.include_router(qa_router)

    return app


# 模块级 app 实例，供 uvicorn 直接导入
app = create_app()
