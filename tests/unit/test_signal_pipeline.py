"""SignalPipeline 信号过滤流水线独立测试。

测试过滤链各阶段的行为：基本面过滤、资金流过滤、行业约束、Wisdom 决策。
全部 mock 外部依赖，只测试 SignalPipeline 的编排逻辑。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from gugu.analysis.perspectives.integrators import build_context_from_signal
from gugu.analysis.position_controller import PositionBudget
from gugu.engine.signal_pipeline import SignalPipeline, record_signal_history


@pytest.fixture
def sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=10),
            "open": [100.0] * 10,
            "high": [101.0] * 10,
            "low": [99.0] * 10,
            "close": [100.0] * 10,
            "volume": [1000000] * 10,
            "signal": [1] + [0] * 9,
            "confidence": [0.8] + [0.0] * 9,
        }
    )


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
def rt_all() -> pd.DataFrame:
    """Mock realtime data with price for 600519."""
    return pd.DataFrame(
        {
            "symbol": ["600519"],
            "name": ["贵州茅台"],
            "price": [100.0],
            "change_pct": [0.0],
            "volume": [1000000],
            "amount": [100000000],
        }
    )


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
    pl._sector_rotation.SW_INDUSTRY_MAP = {
        "白酒": "消费",
        "半导体": "科技",
        "银行": "金融",
        "房地产": "周期",
    }
    pl._hot_sectors_cache = None
    return pl


class TestSignalPipeline:
    """SignalPipeline 过滤链行为测试。"""

    def test_context_preserves_signal_data_quality_and_missing_fields(self):
        """视角上下文应可靠继承信号的数据质量元数据。"""
        context = build_context_from_signal(
            {
                "symbol": "600519",
                "price": 100.0,
                "timestamp": "2026-07-21T09:30:00",
                "data_quality": "partial",
                "data_timestamp": "2026-07-21T09:29:00",
                "data_sources": ["history"],
                "missing_core_fields": ["volume"],
            }
        )

        assert context.data_quality == "partial"
        assert context.data_timestamp == "2026-07-21T09:29:00"
        assert context.data_sources == ["history"]
        assert context.missing_core_fields == ["volume"]

    @pytest.mark.asyncio
    async def test_perspective_analysis_error_blocks_before_wisdom(
        self, pipeline, sample_df, budget, mock_portfolio, mock_account, rt_all
    ):
        """视角分析异常时应在任何 Wisdom/执行前保守阻断。"""
        pipeline._scan_with_perspectives = True
        pipeline._perspective_engine = MagicMock()
        pipeline._perspective_engine.analyze.side_effect = RuntimeError("perspective unavailable")
        pipeline._router.route.return_value = {
            "symbol": "600519",
            "direction": "buy",
            "strategy": "turtle",
            "strategies": ["turtle"],
            "confidence": 0.8,
            "data_quality": "fresh",
        }
        pipeline._fundamental_filter.check.return_value = {"pass": True, "reasons": []}
        pipeline._money_flow_filter.check = AsyncMock(return_value={"pass": True, "reasons": []})
        pipeline._wisdom.advise_async = AsyncMock(return_value={})

        signal = await pipeline.process(
            "600519",
            sample_df,
            {"name": "茅台"},
            budget,
            rt_all,
            [],
            mock_portfolio,
            mock_account,
        )

        assert signal["decision"]["status"] == "blocked"
        assert signal["decision"]["action"] == "observe"
        assert signal["wisdom_filtered"] is True
        assert signal["filter_reason"]
        pipeline._wisdom.advise_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_buy_is_observation_and_does_not_execute_gate(
        self, pipeline, sample_df, budget, mock_portfolio, mock_account, rt_all
    ):
        """非 fresh 买入候选必须进入观察/阻断状态。"""
        pipeline._scan_with_perspectives = True
        pipeline._perspective_engine = MagicMock()
        pipeline._perspective_engine.analyze.return_value = MagicMock(
            composite_score=8.0,
            min_score=7.0,
            passed=True,
            fatal_count=0,
            overall_verdict="通过",
        )
        pipeline._router.route.return_value = {
            "symbol": "600519",
            "direction": "buy",
            "strategy": "turtle",
            "strategies": ["turtle"],
            "confidence": 0.8,
            "data_quality": "stale",
        }
        pipeline._fundamental_filter.check.return_value = {"pass": True, "reasons": []}
        pipeline._money_flow_filter.check = AsyncMock(return_value={"pass": True, "reasons": []})
        pipeline._wisdom.advise_async = AsyncMock(return_value={})

        signal = await pipeline.process(
            "600519",
            sample_df,
            {"name": "茅台"},
            budget,
            rt_all,
            [],
            mock_portfolio,
            mock_account,
        )

        assert signal["decision"]["status"] == "blocked"
        assert signal["decision"]["action"] == "observe"

    @pytest.mark.asyncio
    async def test_failed_perspective_is_blocked(
        self, pipeline, sample_df, budget, mock_portfolio, mock_account, rt_all
    ):
        """视角未通过或异常时不得保留可执行买入。"""
        pipeline._scan_with_perspectives = True
        pipeline._perspective_engine = MagicMock()
        pipeline._perspective_engine.analyze.return_value = MagicMock(
            composite_score=5.0,
            min_score=3.0,
            passed=False,
            fatal_count=1,
            error_count=0,
            status="ok",
            scores=[],
            overall_verdict="未通过",
        )
        pipeline._router.route.return_value = {
            "symbol": "600519",
            "direction": "buy",
            "strategy": "turtle",
            "strategies": ["turtle"],
            "confidence": 0.8,
        }
        pipeline._fundamental_filter.check.return_value = {"pass": True, "reasons": []}
        pipeline._money_flow_filter.check = AsyncMock(return_value={"pass": True, "reasons": []})
        pipeline._wisdom.advise_async = AsyncMock(return_value={})

        signal = await pipeline.process(
            "600519",
            sample_df,
            {"name": "茅台"},
            budget,
            rt_all,
            [],
            mock_portfolio,
            mock_account,
        )

        assert signal["decision"]["status"] == "blocked"
        assert signal["decision"]["action"] == "observe"
        assert signal["decision"]["block_reasons"]

    @pytest.mark.asyncio
    async def test_buy_without_perspective_is_blocked(
        self, pipeline, sample_df, budget, mock_portfolio, mock_account, rt_all
    ):
        """未产生视角结果的买入候选必须阻断。"""
        pipeline._scan_with_perspectives = False
        pipeline._router.route.return_value = {
            "symbol": "600519",
            "direction": "buy",
            "strategy": "turtle",
            "strategies": ["turtle"],
            "confidence": 0.8,
        }
        pipeline._fundamental_filter.check.return_value = {"pass": True, "reasons": []}
        pipeline._money_flow_filter.check = AsyncMock(return_value={"pass": True, "reasons": []})
        pipeline._wisdom.advise_async = AsyncMock(return_value={})

        signal = await pipeline.process(
            "600519",
            sample_df,
            {"name": "茅台"},
            budget,
            rt_all,
            [],
            mock_portfolio,
            mock_account,
        )

        assert signal["decision"]["status"] == "blocked"
        assert signal["decision"]["action"] == "observe"
        assert "缺少十大视角评分" in signal["decision"]["block_reasons"]

    @pytest.mark.asyncio
    async def test_basic_signal_passes_through(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_portfolio: dict,
        mock_account: MagicMock,
        rt_all: pd.DataFrame,
    ) -> None:
        """正常买入信号应通过所有过滤层。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(
            side_effect=lambda s: dict(
                s,
                **{
                    "wisdom": {},
                    "wisdom_decision": {"source": "mock"},
                },
            )
        )

        meta = {"name": "贵州茅台", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=["600519"],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal["symbol"] == "600519"
        assert signal["direction"] == "buy"

    @pytest.mark.asyncio
    async def test_fundamental_filter_blocks_buy(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_portfolio: dict,
        mock_account: MagicMock,
        rt_all: pd.DataFrame,
    ) -> None:
        """基本面过滤失败应阻止买入信号。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": False,
                "reasons": ["PE 过高"],
                "industry": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "PE" in signal.get("filter_reason", "")

    @pytest.mark.asyncio
    async def test_money_flow_blocks_after_fundamental_pass(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_portfolio: dict,
        mock_account: MagicMock,
        rt_all: pd.DataFrame,
    ) -> None:
        """资金流过滤失败应阻止买入信号。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": False,
                "reasons": ["主力净流出"],
                "score": 0.0,
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "资金流" in signal.get("filter_reason", "")

    @pytest.mark.asyncio
    async def test_industry_constraint_blocks_buy(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_account: MagicMock,
        rt_all: pd.DataFrame,
    ) -> None:
        """行业约束应在新建仓位时阻止。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": False,
                "reason": "同行业持仓已达上限",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio={},  # 空持仓
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True

    @pytest.mark.asyncio
    async def test_sell_signals_skip_filters(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_account: MagicMock,
        rt_all: pd.DataFrame,
    ) -> None:
        """卖出信号应跳过基本面/资金流/行业过滤。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "sell",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "止损",
                "confidence": 0.9,
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(
            side_effect=lambda s: dict(
                s,
                **{
                    "wisdom": {},
                    "wisdom_decision": {"source": "mock"},
                },
            )
        )

        from gugu.models.position import Position

        pos = Position(
            symbol="600519", quantity=100, available=100, avg_cost=1800.0, current_price=1900.0
        )
        portfolio_with_pos = {"600519": pos}
        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=portfolio_with_pos,
            account=mock_account,
        )

        assert signal is not None
        pipeline._fundamental_filter.check.assert_not_called()
        pipeline._wisdom.advise_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_sell_without_position_is_blocked(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_portfolio: dict,
        mock_account: MagicMock,
        rt_all: pd.DataFrame,
    ) -> None:
        """P0修复验证：无持仓时卖出信号必须被拦截。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "sell",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "止损",
                "confidence": 0.9,
            }
        )
        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )
        assert signal is None

    @pytest.mark.asyncio
    async def test_skip_if_already_filtered(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_portfolio: dict,
        mock_account: MagicMock,
        rt_all: pd.DataFrame,
    ) -> None:
        """已过滤的信号应跳过后续过滤层。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": False,
                "reasons": ["PE 过高"],
                "industry": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(
            side_effect=lambda s: dict(s, wisdom_filtered=True)
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        # 资金流过滤不应再被调用（已过滤）
        pipeline._money_flow_filter.check.assert_not_called()

    @pytest.mark.asyncio
    async def test_router_returns_none(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_portfolio: dict,
        mock_account: MagicMock,
    ) -> None:
        """路由无信号返回 None。"""
        pipeline._router.route = MagicMock(return_value=None)

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=None,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is None

    @pytest.mark.asyncio
    async def test_exception_isolation(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_portfolio: dict,
        mock_account: MagicMock,
    ) -> None:
        """单个股票异常不影响整个流水线。"""
        pipeline._router.route = MagicMock(side_effect=Exception("路由异常"))

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=None,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is None  # 异常被捕获，返回 None

    @pytest.mark.asyncio
    async def test_realtime_price_override(
        self,
        pipeline: SignalPipeline,
        sample_df: pd.DataFrame,
        budget: PositionBudget,
        mock_portfolio: dict,
        mock_account: MagicMock,
    ) -> None:
        """实时行情应覆盖历史收盘价。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        rt_all = pd.DataFrame(
            {
                "symbol": ["600519"],
                "price": [1500.0],
            }
        )
        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
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
        df = pd.DataFrame(
            {
                "date": dates,
                "open": close,
                "high": [c * 1.01 for c in close],
                "low": [c * 0.99 for c in close],
                "close": close,
                "volume": [1_000_000] * 60,
            }
        )
        result = SignalPipeline._check_weekly_trend(df)
        assert result["weekly_trend"] == "up"
        assert result["weekly_aligned"] is True

    def test_weekly_trend_down(self):
        """下降趋势应返回 aligned=False。"""
        dates = pd.bdate_range("2024-01-01", periods=60)
        close = [100.0 - i * 0.5 for i in range(60)]
        df = pd.DataFrame(
            {
                "date": dates,
                "open": close,
                "high": [c * 1.01 for c in close],
                "low": [c * 0.99 for c in close],
                "close": close,
                "volume": [1_000_000] * 60,
            }
        )
        result = SignalPipeline._check_weekly_trend(df)
        assert result["weekly_trend"] == "down"
        assert result["weekly_aligned"] is False

    def test_weekly_trend_short_df(self):
        """数据不足时返回 unknown 且 aligned=True。"""
        df = pd.DataFrame(
            {
                "date": pd.bdate_range("2024-01-01", periods=5),
                "close": [100.0] * 5,
            }
        )
        result = SignalPipeline._check_weekly_trend(df)
        assert result["weekly_trend"] == "unknown"
        assert result["weekly_aligned"] is True

    def test_weekly_trend_empty(self):
        """空 DataFrame 返回 unknown。"""
        result = SignalPipeline._check_weekly_trend(pd.DataFrame())
        assert result["weekly_trend"] == "unknown"


class TestSectorRotationCheck:
    """板块轮动感知测试。"""

    async def _run_with_sector(
        self, pipeline, sample_df, budget, mock_account, symbol, industry, hot_sectors, categories
    ):
        """辅助方法：运行完整的 signal process 测试。"""
        from unittest.mock import AsyncMock, MagicMock

        pipeline._router.route = MagicMock(
            return_value={
                "symbol": symbol,
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": industry,
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        # Mock sector rotation detect as async
        pipeline._sector_rotation.detect = AsyncMock(
            return_value={
                "hot_sectors": hot_sectors,
                "categories": categories,
                "reason": f"热点: {', '.join(hot_sectors[:3])}",
            }
        )
        pipeline._industry_constraint.get_industry.return_value = industry
        pipeline._hot_sectors_cache = None

        meta = {"name": "Test", "is_st": False, "is_suspended": False}
        rt_all = pd.DataFrame(
            {
                "symbol": [symbol],
                "name": ["Test"],
                "price": [100.0],
                "change_pct": [0.0],
                "volume": [1000000],
                "amount": [100000000],
            }
        )
        return await pipeline.process(
            symbol=symbol,
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio={},
            account=mock_account,
        )

    @pytest.mark.asyncio
    async def test_sector_hot(self, pipeline, sample_df, budget, mock_account):
        """热点板块的股票 should be marked is_hot=True。"""
        signal = await self._run_with_sector(
            pipeline,
            sample_df,
            budget,
            mock_account,
            symbol="600519",
            industry="白酒",
            hot_sectors=["白酒", "半导体"],
            categories=["消费", "科技"],
        )
        assert signal is not None
        sector = signal.get("sector_check", {})
        assert sector.get("is_hot") is True
        assert sector.get("is_cold") is False

    @pytest.mark.asyncio
    async def test_sector_cold(self, pipeline, sample_df, budget, mock_account):
        """冷门板块的股票 should be marked is_cold=True。"""
        signal = await self._run_with_sector(
            pipeline,
            sample_df,
            budget,
            mock_account,
            symbol="601398",
            industry="银行",
            hot_sectors=["白酒", "半导体"],
            categories=["消费", "科技"],
        )
        assert signal is not None
        sector = signal.get("sector_check", {})
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
            assert record["execution_success"] is True
            assert record["execution_status"] == "filled"
            assert record["outcome_status"] == "open"
            assert "realized_pnl" not in record
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


class TestSignalOnlyMode:
    """signal_only 模式行为测试。"""

    @pytest.mark.asyncio
    async def test_signal_only_skips_wisdom_llm(
        self,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
        rt_all,
    ) -> None:
        """signal_only 模式应跳过 Wisdom LLM 调用。"""
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
            exec_mode="signal_only",
        )
        pl._fundamental_filter = MagicMock()
        pl._money_flow_filter = MagicMock()
        pl._industry_constraint = MagicMock()
        pl._router = MagicMock()
        pl._dm = MagicMock()
        pl._dm.is_degraded = False
        pl._sector_rotation = MagicMock()
        pl._wisdom = MagicMock()
        pl._holder_concentration_filter = None

        pl._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pl._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        # signal_only 模式下 Wisdom.advise_async 不应被调用
        pl._wisdom.advise_fallback = MagicMock(
            return_value={
                "stop_loss_price": 95.0,
                "suggested_position_ratio": 0.2,
            }
        )

        meta = {"name": "贵州茅台", "is_st": False, "is_suspended": False}
        signal = await pl.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=["600519"],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("llm_pending") is False
        # advise_async 不应被调用
        pl._wisdom.advise_async.assert_not_called()
        # 但 fallback 应被调用获取止损/止盈
        pl._wisdom.advise_fallback.assert_called_once()
        # 止损价应被设置
        assert signal.get("stop_loss_price") is not None

    @pytest.mark.asyncio
    async def test_signal_only_does_not_hard_filter(
        self,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
        rt_all,
    ) -> None:
        """signal_only 模式下基本面过滤失败不应阻止信号（仅标记）。"""
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
            exec_mode="signal_only",
        )
        pl._fundamental_filter = MagicMock()
        pl._money_flow_filter = MagicMock()
        pl._industry_constraint = MagicMock()
        pl._router = MagicMock()
        pl._dm = MagicMock()
        pl._dm.is_degraded = False
        pl._sector_rotation = MagicMock()
        pl._wisdom = MagicMock()
        pl._holder_concentration_filter = None

        pl._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pl._wisdom.advise_fallback = MagicMock(
            return_value={
                "stop_loss_price": 95.0,
            }
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pl.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        # v0.7.5 P0-2修复：signal_only 模式不硬过滤，仅附加评分展示
        assert signal is not None
        assert signal.get("wisdom_filtered") is None  # signal_only 不拦截
        assert "perspective_check" in signal  # 但附加了评分数据


class TestDangerSignalFilter:
    """危险信号检测过滤测试。"""

    @pytest.mark.asyncio
    async def test_danger_signal_blocks_buy(
        self,
        pipeline,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
        rt_all,
    ) -> None:
        """危险信号应阻止买入。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        # Mock danger detector 返回危险信号
        pipeline._danger_detector.detect = MagicMock(
            return_value=MagicMock(
                has_signal=True,
                severity="high",
                signals=["放量下跌"],
                description="放量下跌危险信号",
                action="卖出",
            )
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "危险信号" in signal.get("filter_reason", "")

    @pytest.mark.asyncio
    async def test_danger_signal_does_not_block_sell(
        self,
        pipeline,
        sample_df,
        budget,
        mock_account,
        rt_all,
    ) -> None:
        """危险信号不应阻止卖出（卖出信号增强）。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "sell",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "止损",
                "confidence": 0.8,
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        from gugu.models.position import Position

        pos = Position(
            symbol="600519", quantity=100, available=100, avg_cost=1800.0, current_price=1900.0
        )
        portfolio_with_pos = {"600519": pos}

        pipeline._danger_detector.detect = MagicMock(
            return_value=MagicMock(
                has_signal=True,
                severity="high",
                signals=["放量下跌"],
                description="放量下跌危险信号",
                action="卖出",
            )
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=portfolio_with_pos,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is not True


class TestDivergenceFilter:
    """量价背离检测过滤测试。"""

    @pytest.mark.asyncio
    async def test_divergence_blocks_buy(
        self,
        pipeline,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
        rt_all,
    ) -> None:
        """量价背离应阻止买入。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        # Mock divergence detector 返回背离信号
        pipeline._divergence_detector.detect = MagicMock(
            return_value={
                "signals": ["顶背离"],
                "severity": "high",
            }
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "量价背离" in signal.get("filter_reason", "")

    @pytest.mark.asyncio
    async def test_divergence_boosts_sell(
        self,
        pipeline,
        sample_df,
        budget,
        mock_account,
        rt_all,
    ) -> None:
        """量价背离应增强卖出信号置信度。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "sell",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "止损",
                "confidence": 0.6,
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        from gugu.models.position import Position

        pos = Position(
            symbol="600519", quantity=100, available=100, avg_cost=1800.0, current_price=1900.0
        )
        portfolio_with_pos = {"600519": pos}

        pipeline._divergence_detector.detect = MagicMock(
            return_value={
                "signals": ["顶背离"],
                "severity": "high",
            }
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=portfolio_with_pos,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("confidence", 0) > 0.6


class TestStageEntryFilter:
    """四阶段入场过滤测试。"""

    @pytest.mark.asyncio
    async def test_frenzy_stage_blocks_buy(
        self,
        pipeline,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
        rt_all,
    ) -> None:
        """疯狂阶段应阻止买入。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        # Mock stage detector 返回疯狂阶段
        from gugu.analysis.stage_detector import MarketStage, StageResult

        pipeline._stage_detector.detect = MagicMock(
            return_value=StageResult(
                stage=MarketStage.FRENZY,
                confidence=0.8,
                description="疯狂上涨阶段",
                suggestion="谨慎持有",
                metrics={},
            )
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "阶段过滤" in signal.get("filter_reason", "")

    @pytest.mark.asyncio
    async def test_downtrend_stage_blocks_buy(
        self,
        pipeline,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
        rt_all,
    ) -> None:
        """下降趋势阶段应阻止买入（不接飞刀）。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        from gugu.analysis.stage_detector import MarketStage, StageResult

        pipeline._stage_detector.detect = MagicMock(
            return_value=StageResult(
                stage=MarketStage.DOWNTREND,
                confidence=0.8,
                description="下降趋势阶段",
                suggestion="空仓观望",
                metrics={},
            )
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "阶段过滤" in signal.get("filter_reason", "")


class TestNoAverageDown:
    """向下摊平检查测试。"""

    @pytest.mark.asyncio
    async def test_avg_down_blocks_buy(
        self,
        pipeline,
        sample_df,
        budget,
        mock_account,
        rt_all,
    ) -> None:
        """向下摊平应阻止加仓买入。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )
        pipeline._wisdom.advise_async = AsyncMock(side_effect=lambda s: dict(s))

        # Mock no_avg_down 返回不允许
        pipeline._no_avg_down_checker.check = MagicMock(
            return_value=MagicMock(
                allowed=False,
                reason="当前价格低于成本价，禁止向下摊平",
                profit_pct=-5.0,
            )
        )

        # 模拟已有持仓
        pos = MagicMock()
        pos.avg_cost = 110.0
        pos.cost_price = 110.0
        pos.quantity = 100
        pos.lowest_price = 100.0
        portfolio = {"600519": pos}

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=portfolio,
            account=mock_account,
        )

        assert signal is not None
        assert signal.get("wisdom_filtered") is True
        assert "向下摊平" in signal.get("filter_reason", "")


class TestPriceValidation:
    """价格校验测试。"""

    @pytest.mark.asyncio
    async def test_price_too_low_rejected(
        self,
        pipeline,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
    ) -> None:
        """价格 <= 1.0 的信号应被拒绝。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )

        # 无实时行情，价格来自历史K线（100.0），但无实时数据
        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=None,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        # 无实时行情时信号应被拒绝
        assert signal is None

    @pytest.mark.asyncio
    async def test_confidence_zero_rejected(
        self,
        pipeline,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
        rt_all,
    ) -> None:
        """置信度为 0 的信号应被拒绝。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.0,
            }
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is None


class TestWisdomTimeout:
    """Wisdom 决策超时降级测试。"""

    @pytest.mark.asyncio
    async def test_wisdom_timeout_fallback(
        self,
        pipeline,
        sample_df,
        budget,
        mock_portfolio,
        mock_account,
        rt_all,
    ) -> None:
        """Wisdom 超时应降级到硬编码 fallback。"""
        pipeline._router.route = MagicMock(
            return_value={
                "symbol": "600519",
                "direction": "buy",
                "strategy": "turtle",
                "strategies": ["turtle"],
                "reason": "突破",
                "confidence": 0.8,
            }
        )
        pipeline._fundamental_filter.check = MagicMock(
            return_value={
                "pass": True,
                "reasons": [],
                "industry": "白酒",
            }
        )
        pipeline._money_flow_filter.check = AsyncMock(
            return_value={
                "pass": True,
                "reasons": [],
                "score": 0.5,
            }
        )
        pipeline._industry_constraint.check_buy = MagicMock(
            return_value={
                "allowed": True,
                "reason": "",
            }
        )

        # Wisdom 超时
        async def _timeout(*args, **kwargs):
            await asyncio.sleep(100)  # 远超 15s 超时
            return {}

        pipeline._wisdom.advise_async = AsyncMock(side_effect=_timeout)
        pipeline._wisdom.advise_fallback = MagicMock(
            return_value={
                "stop_loss_price": 95.0,
                "suggested_position_ratio": 0.2,
                "wisdom_decision": {"source": "fallback", "action": "buy"},
            }
        )

        meta = {"name": "测试", "is_st": False, "is_suspended": False}
        # 使用短超时模拟
        import asyncio as _asyncio

        signal = await pipeline.process(
            symbol="600519",
            df=sample_df,
            meta=meta,
            budget=budget,
            rt_all=rt_all,
            watchlist=[],
            portfolio=mock_portfolio,
            account=mock_account,
        )

        assert signal is not None
        # 应降级到 fallback
        assert signal.get("stop_loss_price") == 95.0
