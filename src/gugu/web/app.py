"""FastAPI 应用：gugu 交易系统 Web 仪表盘。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from gugu.config import settings
from gugu.data import data_manager
from gugu.execution import PaperBroker
from gugu.risk import RiskManager
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
        broker = PaperBroker()
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
        broker = PaperBroker()
        portfolio = broker.get_portfolio()
        positions = []
        for sym, pos in portfolio.items():
            positions.append({
                "symbol": sym,
                "quantity": pos.quantity,
                "available": pos.available,
                "avg_cost": round(pos.avg_cost, 2),
                "current_price": round(pos.current_price, 2),
                "market_value": round(pos.market_value, 2),
                "profit": round(pos.profit, 2),
                "profit_ratio": round(pos.profit_ratio, 4),
            })
        return {"positions": positions}

    @app.get("/api/trades")
    async def get_trades() -> dict[str, Any]:
        """获取交易记录。"""
        broker = PaperBroker()
        trades = broker.trades
        return {"trades": trades, "count": len(trades)}

    @app.get("/api/risk")
    async def get_risk_status() -> dict[str, Any]:
        """获取风控状态。"""
        risk = RiskManager()
        broker = PaperBroker()
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
                return json.loads(hb_path.read_text(encoding="utf-8"))
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
                    points.append({
                        "timestamp": rec["last_cycle_at"],
                        "total_value": rec.get("total_value", 0),
                        "cash": rec.get("cash", 0),
                        "status": rec.get("status", ""),
                        "halted": rec.get("halted", False),
                    })
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
                    signals.append({
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
                    })
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
            risk = RiskManager()
            is_halted = risk.is_halted
        except Exception:
            is_halted = False

        # 数据源状态
        try:
            dm = data_manager()
            is_degraded = dm.is_degraded
        except Exception:
            is_degraded = False

        return {
            "status": "ok",
            "heartbeat": hb_status,
            "last_cycle": hb_time,
            "halted": is_halted,
            "data_source": "degraded" if is_degraded else "primary",
            "mode": settings().get("execution", {}).get("mode", "unknown"),
            "pid": os.getpid(),
        }

    @app.post("/api/halt/reset")
    async def reset_halt() -> dict[str, Any]:
        """手动解除 L2 熔断。"""
        from gugu.engine.main import TradingEngine
        engine = TradingEngine()
        engine.reset_halt()
        return {"success": True, "message": "L2 熔断已手动解除"}

    @app.post("/api/engine/run")
    async def trigger_engine_run() -> dict[str, Any]:
        """触发一次交易循环。"""
        import asyncio
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
        broker = PaperBroker()
        portfolio = broker.get_portfolio()
        positions = []
        for sym, pos in portfolio.items():
            pos_data = {
                "symbol": sym,
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

    return app


# 模块级 app 实例，供 uvicorn 直接导入
app = create_app()
