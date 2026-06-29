"""Web API 单元测试（使用 FastAPI TestClient）。"""
from __future__ import annotations

import json
from unittest import mock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from gugu.web.app import create_app


class TestHealthEndpoint:
    """健康检查端点测试。"""

    def test_health_returns_ok(self):
        """GET /api/health 返回 200 含 status=ok。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.data.manager.data_manager") as mock_dm_fn:
            mock_dm = mock.MagicMock()
            mock_dm.is_degraded = False
            mock_dm_fn.return_value = mock_dm
            resp = client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"

    def test_health_includes_pid(self):
        """健康检查返回 pid 字段。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.data.manager.data_manager") as mock_dm_fn:
            mock_dm = mock.MagicMock()
            mock_dm.is_degraded = False
            mock_dm_fn.return_value = mock_dm
            resp = client.get("/api/health")
            data = resp.json()
            assert "pid" in data

    def test_health_includes_mode(self):
        """健康检查返回运行模式。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.data.manager.data_manager") as mock_dm_fn:
            mock_dm = mock.MagicMock()
            mock_dm.is_degraded = False
            mock_dm_fn.return_value = mock_dm
            resp = client.get("/api/health")
            data = resp.json()
            assert "mode" in data


class TestAccountEndpoint:
    """账户信息端点测试。"""

    def test_account_returns_data(self):
        """GET /api/account 返回账户信息。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.web.app._get_web_broker") as mock_broker_fn:
            mock_broker = mock.MagicMock()
            mock_account = mock.MagicMock()
            mock_account.cash = 700000.0
            mock_account.total_value = 1000000.0
            mock_account.positions = {"600519": mock.MagicMock()}
            mock_broker.get_account.return_value = mock_account
            mock_broker_fn.return_value = mock_broker
            resp = client.get("/api/account")
            assert resp.status_code == 200
            data = resp.json()
            assert data["cash"] == 700000.0
            assert data["total_value"] == 1000000.0
            assert data["positions_count"] == 1


class TestPortfolioEndpoint:
    """持仓信息端点测试。"""

    def test_portfolio_returns_positions(self):
        """GET /api/portfolio 返回持仓列表。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.web.app._get_web_broker") as mock_broker_fn:
            mock_broker = mock.MagicMock()
            mock_pos = mock.MagicMock()
            mock_pos.quantity = 100
            mock_pos.available = 100
            mock_pos.avg_cost = 2000.0
            mock_pos.current_price = 2100.0
            mock_pos.market_value = 210000.0
            mock_pos.profit = 10000.0
            mock_pos.profit_ratio = 0.05
            mock_broker.get_portfolio.return_value = {"600519": mock_pos}
            mock_broker_fn.return_value = mock_broker
            resp = client.get("/api/portfolio")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["positions"]) == 1
            assert data["positions"][0]["symbol"] == "600519"


class TestRiskEndpoint:
    """风控状态端点测试。"""

    def test_risk_returns_state(self):
        """GET /api/risk 返回风控状态。"""
        app = create_app()
        client = TestClient(app)
        with (
            mock.patch("gugu.web.app._get_web_broker") as mock_broker_fn,
            mock.patch("gugu.web.app._get_web_risk") as mock_risk_fn,
        ):
            mock_broker = mock.MagicMock()
            mock_account = mock.MagicMock()
            mock_account.total_value = 980000.0
            mock_broker.get_account.return_value = mock_account
            mock_broker.daily_start_value = 1000000.0
            mock_broker_fn.return_value = mock_broker

            mock_risk = mock.MagicMock()
            mock_risk.is_halted = False
            mock_risk_fn.return_value = mock_risk

            resp = client.get("/api/risk")
            assert resp.status_code == 200
            data = resp.json()
            assert data["halted"] is False
            assert data["daily_loss_pct"] == 0.02  # (1M - 980K) / 1M


class TestHeartbeatEndpoint:
    """心跳端点测试。"""

    def test_heartbeat_file_not_found(self):
        """文件不存在时返回 unknown。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.web.app.Path.exists", return_value=False):
            resp = client.get("/api/heartbeat")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "unknown"

    def test_heartbeat_returns_data(self, tmp_path):
        """正常心跳返回解析数据。"""
        hb_data = {
            "last_cycle_at": "2026-06-30T09:30:00",
            "status": "ok",
            "halted": False,
            "total_value": 1000000,
            "cash": 700000,
            "positions_count": 2,
        }
        app = create_app()
        client = TestClient(app)
        with (
            mock.patch("gugu.web.app.Path.exists", return_value=True),
            mock.patch("gugu.web.app.Path.read_text", return_value=json.dumps(hb_data)),
        ):
            resp = client.get("/api/heartbeat")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["total_value"] == 1000000


class TestSignalsEndpoint:
    """信号历史端点测试。"""

    def test_signals_file_not_found(self):
        """文件不存在时返回空列表。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.web.app.Path.exists", return_value=False):
            resp = client.get("/api/signals")
            assert resp.status_code == 200
            data = resp.json()
            assert data["signals"] == []
            assert data["count"] == 0

    def test_signals_returns_data(self):
        """正常信号历史返回解析数据。"""
        test_records = [
            {
                "timestamp": "2026-06-30T09:30:00",
                "symbol": "600519",
                "name": "贵州茅台",
                "direction": "buy",
                "price": 2100.0,
                "confidence": 0.8,
                "strategies": ["turtle"],
                "wisdom_filtered": False,
                "order_success": True,
            },
            {
                "timestamp": "2026-06-29T14:00:00",
                "symbol": "000858",
                "name": "五粮液",
                "direction": "sell",
                "price": 150.0,
                "confidence": 0.6,
                "strategies": ["bollinger"],
                "wisdom_filtered": True,
                "filter_reason": "危险信号",
                "order_success": None,
            },
        ]
        jsonl_data = "\n".join(json.dumps(r) for r in test_records)

        app = create_app()
        client = TestClient(app)
        with (
            mock.patch("gugu.web.app.Path.exists", return_value=True),
            mock.patch("gugu.web.app.Path.read_text", return_value=jsonl_data),
        ):
            resp = client.get("/api/signals", params={"limit": 10})
            assert resp.status_code == 200
            data = resp.json()
            assert data["count"] >= 2
            # 最新信号在前（reversed）
            assert data["signals"][0]["symbol"] == "000858"  # last line = newest after reversed


class TestEquityEndpoint:
    """净值历史端点测试。"""

    def test_equity_no_data(self):
        """无数据时返回空列表。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.web.app.Path.exists") as mock_exists:
            mock_exists.return_value = False
            resp = client.get("/api/equity/history")
            assert resp.status_code == 200
            data = resp.json()
            assert data["points"] == []

    def test_equity_with_data(self, tmp_path):
        """有数据时返回解析后的点。"""
        from pathlib import Path

        records = [
            {"last_cycle_at": "2026-06-25T09:30:00", "total_value": 1000000, "cash": 700000, "status": "ok", "halted": False},
            {"last_cycle_at": "2026-06-25T10:30:00", "total_value": 1005000, "cash": 650000, "status": "ok", "halted": False},
        ]

        app = create_app()
        client = TestClient(app)
        with (
            mock.patch("gugu.web.app.Path.exists", return_value=True),
            mock.patch(
                "gugu.web.app.Path.read_text",
                return_value="\n".join(json.dumps(r) for r in records),
            ),
            mock.patch("gugu.web.app.Path") as MockPath,
        ):
            MockPath.return_value = Path(tmp_path)
            resp = client.get("/api/equity/history")
            assert resp.status_code == 200


class TestHaltEndpoint:
    """熔断重置端点测试。"""

    def test_halt_reset_returns_200(self):
        """POST /api/halt/reset 返回 200。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.engine.main.TradingEngine") as MockEngine:
            MockEngine.return_value.reset_halt.return_value = None
            resp = client.post("/api/halt/reset")
            assert resp.status_code == 200


class TestSignalPerformanceEndpoint:
    """信号绩效端点测试。"""

    def test_signal_performance_returns_data(self):
        """GET /api/signals/performance 返回绩效数据。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.analysis.signal_tracker.SignalTracker") as MockTracker:
            mock_tracker = mock.MagicMock()
            mock_report = {
                "period_days": 30,
                "total_signals": 10,
                "win_rate": 0.6,
                "avg_return_5d": 0.02,
                "by_strategy": [],
                "recent_signals": [],
            }
            mock_tracker.generate_report = mock.AsyncMock(return_value=mock_report)
            MockTracker.return_value = mock_tracker

            resp = client.get("/api/signals/performance")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True


class TestPortfolioDetailEndpoint:
    """持仓详情端点测试。"""

    def test_portfolio_detail_with_trailing_stop(self):
        """GET /api/portfolio/detail 返回含移动止损的持仓详情。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.web.app._get_web_broker") as mock_broker_fn:
            mock_broker = mock.MagicMock()
            mock_pos = mock.MagicMock()
            mock_pos.quantity = 100
            mock_pos.available = 50  # T+1
            mock_pos.avg_cost = 2000.0
            mock_pos.current_price = 2100.0
            mock_pos.market_value = 210000.0
            mock_pos.profit = 10000.0
            mock_pos.profit_ratio = 0.05
            mock_pos.stop_loss_price = 1900.0
            mock_pos.trailing_stop = {"current_stop_price": 1950, "highest_price": 2150}
            mock_pos.danger_signals = ["volume_price_stall"]
            mock_broker.get_portfolio.return_value = {"600519": mock_pos}
            mock_broker_fn.return_value = mock_broker

            resp = client.get("/api/portfolio/detail")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["positions"]) == 1
            pos = data["positions"][0]
            assert pos["stop_loss_price"] == 1900.0
            assert pos["trailing_stop"]["current_stop_price"] == 1950
            assert "volume_price_stall" in pos["danger_signals"]
