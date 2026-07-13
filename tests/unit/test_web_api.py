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
            {
                "last_cycle_at": "2026-06-25T09:30:00",
                "total_value": 1000000,
                "cash": 700000,
                "status": "ok",
                "halted": False,
            },
            {
                "last_cycle_at": "2026-06-25T10:30:00",
                "total_value": 1005000,
                "cash": 650000,
                "status": "ok",
                "halted": False,
            },
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


class TestOrderEndpoint:
    """模拟盘下单端点测试。"""

    def test_order_rejects_non_paper_mode(self):
        """非 paper 模式拒绝 Web 下单。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.web.app._execution_mode", return_value="signal_only"):
            resp = client.post(
                "/api/order",
                json={"symbol": "600276", "direction": "buy", "quantity": 100, "price": 50.0},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "paper" in data["message"]

    def test_order_rejects_invalid_quantity(self):
        """数量必须为正数。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.web.app._execution_mode", return_value="paper"):
            resp = client.post(
                "/api/order",
                json={"symbol": "600276", "direction": "buy", "quantity": 0, "price": 50.0},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"
        assert "数量" in resp.json()["message"]

    def test_order_rejects_when_risk_halts(self):
        """风控拒绝时不调用 broker.order。"""
        from gugu.risk.rules import RiskAction, RiskCheckResult, RiskLevel

        app = create_app()
        client = TestClient(app)
        with (
            mock.patch("gugu.web.app._execution_mode", return_value="paper"),
            mock.patch("gugu.web.app._get_web_broker") as mock_broker_fn,
            mock.patch("gugu.web.app._get_web_risk") as mock_risk_fn,
        ):
            mock_broker = mock.MagicMock()
            mock_account = mock.MagicMock(cash=100000.0)
            mock_broker.get_account.return_value = mock_account
            mock_broker.get_portfolio.return_value = {}
            mock_broker_fn.return_value = mock_broker

            mock_risk = mock.MagicMock()
            mock_risk.check_order.return_value = RiskCheckResult(
                RiskLevel.L1_POSITION, RiskAction.HALT, "position too large"
            )
            mock_risk_fn.return_value = mock_risk

            resp = client.post(
                "/api/order",
                json={"symbol": "600276", "direction": "buy", "quantity": 100, "price": 50.0},
            )

        data = resp.json()
        assert data["status"] == "error"
        assert "风控拒绝" in data["message"]
        mock_broker.order.assert_not_called()

    def test_order_places_paper_buy_after_risk_allows(self):
        """风控通过后执行模拟盘买入。"""
        from gugu.execution.base import OrderResult
        from gugu.risk.rules import RiskAction, RiskCheckResult, RiskLevel

        app = create_app()
        client = TestClient(app)
        with (
            mock.patch("gugu.web.app._execution_mode", return_value="paper"),
            mock.patch("gugu.web.app._get_web_broker") as mock_broker_fn,
            mock.patch("gugu.web.app._get_web_risk") as mock_risk_fn,
        ):
            mock_broker = mock.MagicMock()
            mock_broker.get_account.return_value = mock.MagicMock(cash=100000.0)
            mock_broker.get_portfolio.return_value = {}
            mock_broker.order.return_value = OrderResult(
                True, "600276", "buy", 50.0, 100, 5.0, message="ok"
            )
            mock_broker_fn.return_value = mock_broker

            mock_risk = mock.MagicMock()
            mock_risk.check_order.return_value = RiskCheckResult(
                RiskLevel.L1_POSITION, RiskAction.ALLOW, "approved"
            )
            mock_risk_fn.return_value = mock_risk

            resp = client.post(
                "/api/order",
                json={"symbol": "600276", "direction": "buy", "quantity": 100, "price": 50.0},
            )

        data = resp.json()
        assert data["status"] == "ok"
        assert data["symbol"] == "600276"
        assert data["direction"] == "buy"
        mock_risk.check_order.assert_called_once()
        mock_broker.order.assert_called_once_with("600276", "buy", 100, price=50.0)


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


# ===== 知识库 QA 端点测试 =====


def _make_kb_answer(question: str = "测试问题") -> object:
    """构造一个 KBAnswer 对象用于 mock 返回。"""
    from gugu.wisdom.kb_agent import Citation, KBAnswer

    return KBAnswer(
        question=question,
        answer="测试答案：安全边际是格雷厄姆提出的核心概念。",
        citations=[Citation(book="聪明的投资者", snippet="安全边际是投资的核心原则")],
        related_concepts=["安全边际"],
        source="llm",
    )


@pytest.fixture
def clean_qa_history():
    """每个 QA 测试前后清理模块级 _qa_history。"""
    from gugu.web import qa

    qa._qa_history.clear()
    yield
    qa._qa_history.clear()


class TestQAPage:
    """/api/qa 页面测试。"""

    def test_qa_page_returns_html(self):
        """GET /api/qa 返回 200 且含 HTML 内容。"""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/qa")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


class TestQAAsk:
    """/api/qa/ask 问答测试。"""

    def test_ask_returns_answer(self, clean_qa_history):
        """POST /api/qa/ask mock KB Agent 后返回 answer + citations。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.wisdom.kb_agent.get_kb_agent") as mock_get:
            mock_agent = mock.MagicMock()
            mock_agent.ask = mock.AsyncMock(return_value=_make_kb_answer())
            mock_get.return_value = mock_agent

            resp = client.post("/api/qa/ask", json={"question": "什么是安全边际"})
            assert resp.status_code == 200
            data = resp.json()
            assert "answer" in data
            assert "citations" in data
            assert len(data["citations"]) >= 1
            assert data["source"] == "llm"

    def test_ask_empty_question_returns_400(self, clean_qa_history):
        """空 question 返回 400。"""
        app = create_app()
        client = TestClient(app)
        resp = client.post("/api/qa/ask", json={"question": ""})
        assert resp.status_code == 400

    def test_ask_kb_failure_returns_500(self, clean_qa_history):
        """KB Agent 抛异常时返回 500。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.wisdom.kb_agent.get_kb_agent") as mock_get:
            mock_agent = mock.MagicMock()
            mock_agent.ask = mock.AsyncMock(side_effect=RuntimeError("KB 故障"))
            mock_get.return_value = mock_agent

            resp = client.post("/api/qa/ask", json={"question": "测试"})
            assert resp.status_code == 500


class TestQAHistory:
    """/api/qa/history 历史测试。"""

    def test_history_returns_list(self, clean_qa_history):
        """GET /api/qa/history 返回 history 列表。"""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/qa/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data
        assert isinstance(data["history"], list)

    def test_history_grows_after_ask(self, clean_qa_history):
        """ask 后 history 数量增加。"""
        app = create_app()
        client = TestClient(app)
        # 初始历史为空
        resp1 = client.get("/api/qa/history")
        assert len(resp1.json()["history"]) == 0

        with mock.patch("gugu.wisdom.kb_agent.get_kb_agent") as mock_get:
            mock_agent = mock.MagicMock()
            mock_agent.ask = mock.AsyncMock(return_value=_make_kb_answer())
            mock_get.return_value = mock_agent
            client.post("/api/qa/ask", json={"question": "测试"})

        resp2 = client.get("/api/qa/history")
        assert len(resp2.json()["history"]) == 1


class TestQAStats:
    """/api/qa/stats 统计测试。"""

    def test_stats_returns_dict(self, clean_qa_history):
        """GET /api/qa/stats mock KB Agent 后返回统计。"""
        app = create_app()
        client = TestClient(app)
        with mock.patch("gugu.wisdom.kb_agent.get_kb_agent") as mock_get:
            mock_agent = mock.MagicMock()
            mock_agent.get_stats.return_value = {
                "loaded": True,
                "chunk_count": 22353,
                "book_count": 33,
                "books": ["聪明的投资者", "股票大作手回忆录"],
                "built_at": "2026-07-01",
            }
            mock_get.return_value = mock_agent

            resp = client.get("/api/qa/stats")
            assert resp.status_code == 200
            data = resp.json()
            assert data["loaded"] is True
            assert data["chunk_count"] == 22353
            assert data["book_count"] == 33


class TestConcepts:
    """/api/concepts 概念库测试。"""

    def test_list_concepts_returns_all(self):
        """GET /api/concepts 返回概念列表 + stats。"""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/concepts")
        assert resp.status_code == 200
        data = resp.json()
        assert "concepts" in data
        assert "stats" in data
        assert len(data["concepts"]) > 0
        assert data["stats"]["total_concepts"] > 0

    def test_get_concept_by_id(self):
        """GET /api/concepts/safety_margin 返回概念详情。"""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/concepts/safety_margin")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "safety_margin"
        assert "安全边际" in data["name"]

    def test_get_concept_unknown_returns_404(self):
        """未知 concept_id 返回 404。"""
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/concepts/nonexistent_12345")
        assert resp.status_code == 404


class TestProfile:
    """/api/profile 用户画像测试。"""

    def test_get_profile_returns_dict(self, tmp_path, monkeypatch):
        """GET /api/profile 返回画像 4 字段。"""
        from gugu.models import user_profile

        profile_file = tmp_path / "user_profile.json"
        monkeypatch.setattr(user_profile, "_PROFILE_FILE", profile_file)

        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/profile")
        assert resp.status_code == 200
        data = resp.json()
        assert "risk_tolerance" in data
        assert "investment_style" in data
        assert "experience" in data
        assert "horizon" in data
        assert "style_weights" in data

    def test_post_profile_updates(self, tmp_path, monkeypatch):
        """POST /api/profile 写入并返回更新后画像。"""
        from gugu.models import user_profile

        profile_file = tmp_path / "user_profile.json"
        monkeypatch.setattr(user_profile, "_PROFILE_FILE", profile_file)

        app = create_app()
        client = TestClient(app)
        resp = client.post(
            "/api/profile",
            json={
                "risk_tolerance": "aggressive",
                "investment_style": "trend",
                "experience": "intermediate",
                "horizon": "short",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["profile"]["risk_tolerance"] == "aggressive"
        assert data["profile"]["investment_style"] == "trend"
        # 验证文件已写入
        assert profile_file.exists()
        saved = json.loads(profile_file.read_text(encoding="utf-8"))
        assert saved["risk_tolerance"] == "aggressive"
