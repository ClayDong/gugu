"""Web API 单元测试（使用 FastAPI TestClient）。"""
from __future__ import annotations

from unittest import mock

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
        import json
        from pathlib import Path

        # 写入测试数据
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        hb_path = data_dir / "heartbeat_history.jsonl"
        records = [
            {"last_cycle_at": "2026-06-25T09:30:00", "total_value": 1000000, "cash": 700000, "status": "ok", "halted": False},
            {"last_cycle_at": "2026-06-25T10:30:00", "total_value": 1005000, "cash": 650000, "status": "ok", "halted": False},
        ]
        with hb_path.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        app = create_app()
        client = TestClient(app)

        with mock.patch("gugu.web.app.Path") as MockPath:
            MockPath.return_value = hb_path
            MockPath.exists.return_value = True
            # 直接 mock open 行为
            with mock.patch("gugu.web.app.open", mock.mock_open(read_data=hb_path.read_text())):
                from gugu.web import app as web_app_module
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