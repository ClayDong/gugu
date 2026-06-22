"""FastAPI 应用：gugu 交易系统 Web 仪表盘。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

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
                "profit_pct": round(pos.profit_pct, 4),
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

    return app
