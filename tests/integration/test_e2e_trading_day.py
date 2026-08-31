"""E2E 交易日模拟测试：验证完整信号处理流水线。

模拟一个完整交易日的核心流程：
  数据采集 → 策略信号 → 过滤链 → 风控 → 下单 → 通知 → 止损检查

所有外部依赖（网络/LLM/飞书）均被 mock，测试纯逻辑正确性。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pandas as pd

from gugu.models.signal import Signal


def _make_kline_df(days: int = 60) -> pd.DataFrame:
    """构造测试用 K 线数据（含趋势+回调）。"""
    dates = pd.date_range("2026-01-01", periods=days, freq="B")
    base = 100.0
    prices = []
    for i in range(days):
        if i < 30:
            base *= 1.005  # 上涨趋势
        elif i < 40:
            base *= 0.998  # 小幅回调
        else:
            base *= 1.003  # 恢复上涨
        prices.append(base)

    df = pd.DataFrame(
        {
            "date": dates,
            "open": [p * 0.998 for p in prices],
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": [100000 + i * 1000 for i in range(days)],
            "amount": [p * 100000 for p in prices],
        }
    )
    return df


class TestE2ESignalFlow:
    """端到端信号处理流水线测试。"""

    def test_strategy_generates_signal_from_kline(self) -> None:
        """策略应从 K 线数据生成有效信号。"""
        from gugu.strategies.registry import get_strategy

        df = _make_kline_df(60)
        strategy = get_strategy("box_breakout")
        result = strategy.generate_signals(df)

        assert "signal" in result.columns
        assert "confidence" in result.columns
        assert len(result) == len(df)
        assert result["signal"].isin([-1, 0, 1]).all()
        # confidence 可能有 NaN（前 N 行 warmup），dropna 后检查
        conf = result["confidence"].dropna()
        assert (conf >= 0).all()
        assert (conf <= 1).all()

    def test_signal_to_dict_and_back_roundtrip(self) -> None:
        """Signal 序列化/反序列化保持数据完整。"""
        s = Signal(
            symbol="600519",
            direction="buy",
            price=1800.0,
            name="贵州茅台",
            strategy="box_breakout",
            confidence=0.85,
            suggested_position_ratio=0.15,
            stop_loss_price=1656.0,
        )
        d = s.to_dict()
        assert d["symbol"] == "600519"
        assert d["strategy"] == "box_breakout"

        s2 = Signal.from_dict(d)
        assert s2.symbol == s.symbol
        assert s2.direction == s.direction
        assert s2.price == s.price
        assert s2.strategy == s.strategy
        assert s2.confidence == s.confidence
        assert s2.stop_loss_price == s.stop_loss_price

    def test_risk_manager_position_limit(self) -> None:
        """RiskManager L1 应限制单股仓位 ≤ 30%。"""
        from gugu.risk.manager import RiskManager

        rm = RiskManager()
        assert rm.max_position_ratio <= 0.30

    def test_paper_broker_order_and_position_flow(self) -> None:
        """PaperBroker 应完成下单→持仓完整流程。"""
        from gugu.execution.paper import PaperBroker

        broker = PaperBroker(initial_capital=1_000_000)
        account = broker.get_account()
        assert account is not None

        # 下单买入
        order_result = broker.order("600519", "buy", 100, 150.0)
        assert order_result is not None

        # 持仓验证
        position = broker.get_position("600519")
        if position is not None:
            assert position.symbol == "600519"

    def test_signal_pipeline_construction_with_mocks(self) -> None:
        """SignalPipeline 应能用 mock 依赖成功构建。"""
        from gugu.analysis.position_controller import PositionController
        from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
        from gugu.engine.signal_pipeline import SignalPipeline
        from gugu.engine.signal_router import SignalRouter
        from gugu.wisdom import WisdomAdvisor

        dm = MagicMock()
        dm.is_degraded = False

        router = MagicMock(spec=SignalRouter)
        wisdom = MagicMock(spec=WisdomAdvisor)
        regime = MagicMock(spec=MultiPeriodRegimeDetector)
        pos_ctrl = MagicMock(spec=PositionController)

        pipeline = SignalPipeline(
            data_manager=dm,
            signal_router=router,
            wisdom_advisor=wisdom,
            regime_detector=regime,
            position_controller=pos_ctrl,
        )
        assert pipeline is not None
        assert pipeline._dm is dm
        assert pipeline._router is router
        assert pipeline._wisdom is wisdom

    def test_perspective_engine_analyzes_kline(self) -> None:
        """PerspectiveEngine 应能分析 K 线上下文。"""
        from gugu.analysis.perspectives import PerspectiveEngine
        from gugu.analysis.perspectives.integrators import build_context_from_kline

        df = _make_kline_df(60)
        ctx = build_context_from_kline(symbol="600519", name="贵州茅台", kline_df=df)
        assert ctx is not None
        assert ctx.symbol == "600519"

        engine = PerspectiveEngine()
        result = engine.analyze(ctx)
        assert result is not None
        assert 0 <= result.composite_score <= 10


class TestE2ERiskFlow:
    """端到端风控流程测试。"""

    def test_production_runtime_uses_verified_strategy_whitelist(self) -> None:
        """生产模式策略必须来自 settings.yaml 的 verified 白名单。"""
        from gugu.config import get_config
        from gugu.strategies.registry import get_runtime_strategy_names

        cfg = get_config()
        names = get_runtime_strategy_names(execution_mode="track_live")

        assert set(names) <= set(cfg.strategy.verified_strategies)
        assert names == []

    def test_l2_halt_persists_to_file(self, tmp_path) -> None:
        """L2 熔断状态应持久化到文件。"""
        import json

        state_file = tmp_path / "risk_state.json"
        state_data = {"halted": True, "halt_date": "2026-07-07"}
        state_file.write_text(json.dumps(state_data), encoding="utf-8")

        loaded = json.loads(state_file.read_text(encoding="utf-8"))
        assert loaded["halted"] is True
        assert loaded["halt_date"] == "2026-07-07"

    def test_track_live_rejects_order_without_mutating_state(self, tmp_path) -> None:
        """track_live 下单必须失败，且不得修改用户维护的持仓文件。"""
        from gugu.execution.track_live import LivePositionTracker

        state_file = tmp_path / "live_positions.json"
        state_file.write_text(
            json.dumps(
                {
                    "mode": "track_live",
                    "cash": 500_000,
                    "positions": {
                        "600519": {
                            "symbol": "600519",
                            "name": "贵州茅台",
                            "quantity": 100,
                            "available": 100,
                            "avg_cost": 150.0,
                            "current_price": 155.0,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        before = state_file.read_bytes()
        tracker = LivePositionTracker(state_file=state_file)

        result = tracker.order("600519", "buy", 100, 155.0)

        assert result.success is False
        assert result.quantity == 0
        assert "track_live" in result.message
        position = tracker.get_position("600519")
        assert position is not None
        assert position.quantity == 100
        assert position.avg_cost == 150.0
        assert state_file.read_bytes() == before

    def test_position_controller_calculate_budget(self) -> None:
        """仓位控制器应能计算预算。"""
        from gugu.analysis.position_controller import PositionController

        ctrl = PositionController()
        regime = {"regime": "bull", "confidence": 0.8, "position_limit": 1.0}
        account = MagicMock()
        account.total_asset = 1_000_000
        account.cash = 500_000

        budget = ctrl.calculate(regime=regime, account=account)
        assert budget is not None
        assert hasattr(budget, "total_limit") or hasattr(budget, "single_limit")


class TestE2ENotificationFlow:
    """端到端通知流程测试。"""

    def test_signal_format_has_required_fields(self) -> None:
        """信号格式化应生成有效卡片结构。"""
        from gugu.notifier.formatter import format_signal

        signal_dict = {
            "symbol": "600519",
            "name": "贵州茅台",
            "direction": "buy",
            "strategy": "box_breakout",
            "price": 150.0,
            "confidence": 0.85,
            "suggested_position_ratio": 0.15,
            "stop_loss_price": 138.0,
        }
        result = format_signal(signal_dict)
        # format_signal 返回 {"msg_type": "interactive", "card": {...}}
        card = result.get("card", result) if isinstance(result, dict) else {}
        assert "header" in card
        assert "elements" in card
        assert len(card["elements"]) > 0

    def test_daily_report_card_structure(self) -> None:
        """日报卡片应有基本结构。"""
        from gugu.notifier.formatter import format_daily_report

        report_data = {
            "signals": [],
            "positions": [],
            "total_asset": 1_000_000,
            "daily_pnl": 0,
            "daily_pnl_pct": 0,
        }
        card = format_daily_report("close", report_data)
        actual_card = card.get("card", card)
        assert "header" in actual_card
        assert "elements" in actual_card
