"""SignalPipeline 信号过滤流水线独立测试。

测试过滤链各阶段的行为：基本面过滤、资金流过滤、行业约束、Wisdom 决策。
全部 mock 外部依赖，只测试 SignalPipeline 的编排逻辑。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from gugu.analysis.position_controller import PositionBudget
from gugu.engine.signal_pipeline import SignalPipeline, record_signal_history


@pytest.fixture
def sample_df() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=10),
        "open": [100.0] * 10,
        "high": [101.0] * 10,
        "low": [99.0] * 10,
        "close": [100.0] * 10,
        "volume": [1000000] * 10,
        "signal": [1] + [0] * 9,
        "confidence": [0.8] + [0.0] * 9,
    })


@pytest.fixture
def budget() -> PositionBudget:
    return PositionBudget(
        total_limit=0.8,
        single_limit=0.3,
        available_budget=1_000_000,
        max_positions=5,
        reason="test budget",
    )


@pytest.fixture
def mock_portfolio() -> dict:
    return {}


@pytest.fixture
def mock_account() -> MagicMock:
    acct = MagicMock()
    acct.total_value = 1_000_000
    acct.cash = 700_000
    return acct


@pytest.fixture
def pipeline() -> SignalPipeline:
    """创建一个完全 mock 的 SignalPipeline。"""
    dm = MagicMock()
    dm.is_degraded = False
    router = MagicMock()
    wisdom = MagicMock()
    regime = MagicMock()
    pos_ctrl = MagicMock()

    pl = SignalPipeline(
        data_manager=dm,
        signal_router=router,
        wisdom_advisor=wisdom,
        regime_detector=regime,
        position_controller=pos_ctrl,
    )
    # Mock all filters
    pl._fundamental_filter = MagicMock()
    pl._money_flow_filter = MagicMock()
    pl._industry_constraint = MagicMock()
    pl._industry_constraint.get_industry.return_value = "白酒"
    pl._wisdom = MagicMock()
    pl._router = MagicMock()
    pl._dm = MagicMock()
    pl._dm.is_degraded = False
    # Mock sector rotation: detect is async
    pl._sector_rotation = MagicMock()
    pl._sector_rotation.SW_INDUSTRY_MAP = {"白酒": "消费", "半导体": "科技", "银行": "金融", "房地产": "周期"}
    pl._hot_sectors_cache = None
    return pl


class TestSignalPipeline:
    """SignalPipeline 过滤链行为测试。"""

    @pytest.mark.asyncio
    async def test_basic_signal_passes_through(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_portfolio: dict, mock_account: MagicMock,
    ) -> None:
        """正常买入信号应通过所有过滤层。"""
        pipeline._router.route = MagicMock(return_value={
            "symbol": "600519",
            "direction": "buy",
            "strategy": "turtle",
            "strategies": ["turtle"],
            "reason": "突破",
            "confidence": 0.8,
        })
        pipeline._fundamental_filter.check = MagicMock(return_value={
            "pass": True, "reasons": [], "industry": "白酒",
        })
        pipeline._money_flow_filter.check = AsyncMock(return_value={
            "pass": True, "reasons": [], "score": 0.5,
        })
        pipeline._industry_constraint.check_buy = MagicMock(return_value={
            "allowed": True, "reason": "",
        })
        pipeline._wisdom.advise = MagicMock(side_effect=lambda s: dict(s, **{
            "wisdom": {}, "wisdom_decision": {"source": "mock"},
        }))

        meta = {"name": "贵州茅台", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=["600519"],
            portfolio=mock_portfolio, account=mock_account,
        )

        assert signal is not None
        assert signal["symbol"] == "600519"
        assert signal["direction"] == "buy"

    @pytest.mark.asyncio
    async def test_fundamental_filter_blocks_buy(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_portfolio: dict, mock_account: MagicMock,
    ) -> None:
        """基本面过滤失败应阻止买入信号。"""
        pipeline._router.route = MagicMock(return_value={
            "symbol": "600519", "direction": "buy",
            "strategy": "turtle", "strategies": ["turtle"],
            "reason": "突破", "confidence": 0.8,
        })
        pipeline._fundamental_filter.check = MagicMock(return_value={
            "pass": False, "reasons": ["PE 过高"], "industry": "",
        })
        pipeline._wisdom.advise = MagicMock(side_effect=lambda s: dict(s))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=[],
            portfolio=mock_portfolio, account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "PE" in signal.get("filter_reason", "")

    @pytest.mark.asyncio
    async def test_money_flow_blocks_after_fundamental_pass(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_portfolio: dict, mock_account: MagicMock,
    ) -> None:
        """资金流过滤失败应阻止买入信号。"""
        pipeline._router.route = MagicMock(return_value={
            "symbol": "600519", "direction": "buy",
            "strategy": "turtle", "strategies": ["turtle"],
            "reason": "突破", "confidence": 0.8,
        })
        pipeline._fundamental_filter.check = MagicMock(return_value={
            "pass": True, "reasons": [], "industry": "白酒",
        })
        pipeline._money_flow_filter.check = AsyncMock(return_value={
            "pass": False, "reasons": ["主力净流出"], "score": 0.0,
        })
        pipeline._wisdom.advise = MagicMock(side_effect=lambda s: dict(s))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=[],
            portfolio=mock_portfolio, account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "资金流" in signal.get("filter_reason", "")

    @pytest.mark.asyncio
    async def test_industry_constraint_blocks_buy(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_account: MagicMock,
    ) -> None:
        """行业约束应在新建仓位时阻止。"""
        pipeline._router.route = MagicMock(return_value={
            "symbol": "600519", "direction": "buy",
            "strategy": "turtle", "strategies": ["turtle"],
            "reason": "突破", "confidence": 0.8,
        })
        pipeline._fundamental_filter.check = MagicMock(return_value={
            "pass": True, "reasons": [], "industry": "白酒",
        })
        pipeline._money_flow_filter.check = AsyncMock(return_value={
            "pass": True, "reasons": [], "score": 0.5,
        })
        pipeline._industry_constraint.check_buy = MagicMock(return_value={
            "allowed": False, "reason": "同行业持仓已达上限",
        })
        pipeline._wisdom.advise = MagicMock(side_effect=lambda s: dict(s))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=[],
            portfolio={},  # 空持仓
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True

    @pytest.mark.asyncio
    async def test_sell_signals_skip_filters(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_portfolio: dict, mock_account: MagicMock,
    ) -> None:
        """卖出信号应跳过基本面/资金流/行业过滤。"""
        pipeline._router.route = MagicMock(return_value={
            "symbol": "600519", "direction": "sell",
            "strategy": "turtle", "strategies": ["turtle"],
            "reason": "止损", "confidence": 0.9,
        })
        pipeline._wisdom.advise = MagicMock(side_effect=lambda s: dict(s, **{
            "wisdom": {}, "wisdom_decision": {"source": "mock"},
        }))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=[],
            portfolio=mock_portfolio, account=mock_account,
        )

        assert signal is not None
        # 基本面过滤不应被调用
        pipeline._fundamental_filter.check.assert_not_called()
        # wisdom 应被调用
        pipeline._wisdom.advise.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_if_already_filtered(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_portfolio: dict, mock_account: MagicMock,
    ) -> None:
        """已过滤的信号应跳过后续过滤层。"""
        pipeline._router.route = MagicMock(return_value={
            "symbol": "600519", "direction": "buy",
            "strategy": "turtle", "strategies": ["turtle"],
            "reason": "突破", "confidence": 0.8,
        })
        pipeline._fundamental_filter.check = MagicMock(return_value={
            "pass": False, "reasons": ["PE 过高"], "industry": "",
        })
        pipeline._wisdom.advise = MagicMock(side_effect=lambda s: dict(s, wisdom_filtered=True))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=[],
            portfolio=mock_portfolio, account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        # 资金流过滤不应再被调用（已过滤）
        pipeline._money_flow_filter.check.assert_not_called()

    @pytest.mark.asyncio
    async def test_router_returns_none(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_portfolio: dict, mock_account: MagicMock,
    ) -> None:
        """路由无信号返回 None。"""
        pipeline._router.route = MagicMock(return_value=None)

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=[],
            portfolio=mock_portfolio, account=mock_account,
        )

        assert signal is None

    @pytest.mark.asyncio
    async def test_exception_isolation(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_portfolio: dict, mock_account: MagicMock,
    ) -> None:
        """单个股票异常不影响整个流水线。"""
        pipeline._router.route = MagicMock(side_effect=Exception("路由异常"))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=[],
            portfolio=mock_portfolio, account=mock_account,
        )

        assert signal is None  # 异常被捕获，返回 None

    @pytest.mark.asyncio
    async def test_realtime_price_override(
        self, pipeline: SignalPipeline, sample_df: pd.DataFrame,
        budget: PositionBudget, mock_portfolio: dict, mock_account: MagicMock,
    ) -> None:
        """实时行情应覆盖历史收盘价。"""
        pipeline._router.route = MagicMock(return_value={
            "symbol": "600519", "direction": "buy",
            "strategy": "turtle", "strategies": ["turtle"],
            "reason": "突破", "confidence": 0.8,
        })
        pipeline._fundamental_filter.check = MagicMock(return_value={
            "pass": True, "reasons": [], "industry": "",
        })
        pipeline._money_flow_filter.check = AsyncMock(return_value={
            "pass": True, "reasons": [], "score": 0.5,
        })
        pipeline._industry_constraint.check_buy = MagicMock(return_value={
            "allowed": True, "reason": "",
        })
        pipeline._wisdom.advise = MagicMock(side_effect=lambda s: dict(s))

        rt_all = pd.DataFrame({
            "symbol": ["600519"],
            "price": [1500.0],
        })
        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519", df=sample_df, meta=meta, budget=budget,
            rt_all=rt_all, watchlist=[],
            portfolio=mock_portfolio, account=mock_account,
        )

        assert signal is not None
        assert signal["price"] == 1500.0  # 被实时价覆盖


class TestMultiPeriodTrend:
    """多周期趋势判断测试。"""

    def test_weekly_trend_up(self):
        """上升趋势应返回 aligned=True。"""
        # 构造 60 天稳定上升行情
        dates = pd.bdate_range("2024-01-01", periods=60)
        close = [100.0 + i * 0.5 for i in range(60)]
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [1_000_000] * 60,
        })
        result = SignalPipeline._check_weekly_trend(df)
        assert result["weekly_trend"] == "up"
        assert result["weekly_aligned"] is True

    def test_weekly_trend_down(self):
        """下降趋势应返回 aligned=False。"""
        dates = pd.bdate_range("2024-01-01", periods=60)
        close = [100.0 - i * 0.5 for i in range(60)]
        df = pd.DataFrame({
            "date": dates,
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [1_000_000] * 60,
        })
        result = SignalPipeline._check_weekly_trend(df)
        assert result["weekly_trend"] == "down"
        assert result["weekly_aligned"] is False

    def test_weekly_trend_short_df(self):
        """数据不足时返回 unknown 且 aligned=True。"""
        df = pd.DataFrame({
            "date": pd.bdate_range("2024-01-01", periods=5),
            "close": [100.0] * 5,
        })
        result = SignalPipeline._check_weekly_trend(df)
        assert result["weekly_trend"] == "unknown"
        assert result["weekly_aligned"] is True

    def test_weekly_trend_empty(self):
        """空 DataFrame 返回 unknown。"""
        result = SignalPipeline._check_weekly_trend(pd.DataFrame())
        assert result["weekly_trend"] == "unknown"


class TestSectorRotationCheck:
    """板块轮动感知测试。"""

    async def _run_with_sector(self, pipeline, sample_df, budget, mock_account,
                                symbol, industry, hot_sectors, categories):
        """辅助方法：运行完整的 signal process 测试。"""
        from unittest.mock import AsyncMock, MagicMock

        pipeline._router.route = MagicMock(return_value={
            "symbol": symbol, "direction": "buy",
            "strategy": "turtle", "strategies": ["turtle"],
            "reason": "突破", "confidence": 0.8,
        })
        pipeline._fundamental_filter.check = MagicMock(return_value={
            "pass": True, "reasons": [], "industry": industry,
        })
        pipeline._money_flow_filter.check = AsyncMock(return_value={
            "pass": True, "reasons": [], "score": 0.5,
        })
        pipeline._industry_constraint.check_buy = MagicMock(return_value={
            "allowed": True, "reason": "",
        })
        pipeline._wisdom.advise = MagicMock(side_effect=lambda s: dict(s))

        # Mock sector rotation detect as async
        pipeline._sector_rotation.detect = AsyncMock(return_value={
            "hot_sectors": hot_sectors,
            "categories": categories,
            "reason": f"热点: {', '.join(hot_sectors[:3])}",
        })
        pipeline._industry_constraint.get_industry.return_value = industry
        pipeline._hot_sectors_cache = None

        meta = {"name": "Test", "is_st": False, "is_suspended": False}
        return await pipeline.process(
            symbol=symbol, df=sample_df, meta=meta, budget=budget,
            rt_all=None, watchlist=[],
            portfolio={}, account=mock_account,
        )

    @pytest.mark.asyncio
    async def test_sector_hot(self, pipeline, sample_df, budget, mock_account):
        """热点板块的股票 should be marked is_hot=True。"""
        signal = await self._run_with_sector(
            pipeline, sample_df, budget, mock_account,
            symbol="600519", industry="白酒",
            hot_sectors=["白酒", "半导体"], categories=["消费", "科技"],
        )
        assert signal is not None
        sector = signal.get("sector_check", {})
        assert sector.get("is_hot") is True
        assert sector.get("is_cold") is False

    @pytest.mark.asyncio
    async def test_sector_cold(self, pipeline, sample_df, budget, mock_account):
        """冷门板块的股票 should be marked is_cold=True。"""
        signal = await self._run_with_sector(
            pipeline, sample_df, budget, mock_account,
            symbol="601398", industry="银行",
            hot_sectors=["白酒", "半导体"], categories=["消费", "科技"],
        )
        assert signal is not None
        sector = signal.get("sector_check", {})
        assert sector.get("is_hot") is False
        assert sector.get("is_cold") is True


class TestRecordSignalHistory:
    """信号历史记录测试。"""

    def test_record_signal_history(self, tmp_path) -> None:
        """记录信号历史到 JSONL 文件。"""
        import json
        from pathlib import Path
        from gugu.config import PROJECT_ROOT
        import gugu.engine.signal_pipeline as sp

        # 临时替换 PROJECT_ROOT
        original_root = sp.gugu_config.PROJECT_ROOT
        sp.gugu_config.PROJECT_ROOT = tmp_path

        try:
            signal = {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.8,
                "strategies": ["turtle"],
                "wisdom_filtered": False,
                "wisdom_decision": {"source": "fallback"},
                "suggested_position_ratio": 0.15,
            }
            risk_result = MagicMock()
            risk_result.allowed = True
            risk_result.message = "approved"

            order_result = MagicMock()
            order_result.success = True
            order_result.quantity = 100
            order_result.price = 1500.0
            order_result.commission = 3.75

            record_signal_history(signal, risk_result, order_result)

            history_file = tmp_path / "data" / "signals_history.jsonl"
            assert history_file.exists()
            lines = history_file.read_text().strip().split("\n")
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["symbol"] == "600519"
            assert record["direction"] == "buy"
            assert record["order_success"] is True
        finally:
            sp.gugu_config.PROJECT_ROOT = original_root

    def test_record_signal_history_error_handling(self, tmp_path) -> None:
        """记录失败不抛异常。"""
        from pathlib import Path
        import gugu.engine.signal_pipeline as sp

        original_root = sp.gugu_config.PROJECT_ROOT
        sp.gugu_config.PROJECT_ROOT = Path("/nonexistent/path")

        try:
            # 即使路径不存在也不应抛异常
            record_signal_history({}, None, None)
            assert True
        finally:
            sp.gugu_config.PROJECT_ROOT = original_root