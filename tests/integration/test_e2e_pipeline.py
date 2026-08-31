"""端到端集成测试骨架：验证核心模块导入与基本数据流。

本测试验证所有核心模块可正常导入及基本数据模型正常工作。
不发起任何网络请求或 akshare API 调用 —— 所有外部依赖均被 monkeypatch 隔离。

覆盖范围：
1. test_imports — 验证 10+ 核心模块导入
2. test_config_loading — 配置加载与基本结构
3. test_signal_creation — Signal 模型构造
4. test_data_types_import — 基础数据模型导入
5. test_perspective_engine — PerspectiveEngine 实例化
6. test_signal_pipeline_construction — SignalPipeline 带 mock 依赖构建
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

# ──────────────────────────────────────────────────────────
# 全局 monkeypatch：阻止所有网络相关的导入/调用
# ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻止测试中的任何网络/IO 调用。

    通过 mock 易触发的网络模块确保所有测试在无网络环境下运行。
    由于 AkshareCollector / SinaCollector 的抽象方法在类上定义，patch 需要
    使用正确的方法名称，并在导入后执行（延迟绑定）。这里使用惰性方式：
    只 patch httpx，它是最底层的网络依赖。
    """
    # 阻止 httpx 请求（底层网络）
    monkeypatch.setattr("httpx.Client", MagicMock())
    monkeypatch.setattr("httpx.AsyncClient", MagicMock())


# ──────────────────────────────────────────────────────────
# 测试 1：验证 10+ 核心模块可正常导入
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_imports() -> None:
    """验证 10+ 核心模块导入不报错。"""
    # 配置层
    import gugu.config  # noqa: F811
    from gugu.config import AppConfig, DataConfig, get_config  # noqa: F811

    # 模型层
    from gugu.models.signal import Direction, OrderResult, Signal  # noqa: F811
    from gugu.models.position import Position  # noqa: F811

    # 数据层
    from gugu.data.manager import DataManager  # noqa: F811
    from gugu.data.quality import ValidationRule  # noqa: F811

    # 引擎层
    from gugu.engine.signal_pipeline import SignalPipeline  # noqa: F811
    from gugu.engine.signal_router import SignalRouter  # noqa: F811
    from gugu.engine.main import TradingEngine  # noqa: F811

    # 策略层
    from gugu.strategies.base import Strategy  # noqa: F811
    from gugu.strategies.breakout import BoxBreakoutStrategy  # noqa: F811
    from gugu.strategies.mean_revert import BollingerStrategy  # noqa: F811

    # 分析层
    from gugu.analysis.perspectives import PerspectiveEngine  # noqa: F811
    from gugu.analysis.regime_detector import MultiPeriodRegimeDetector  # noqa: F811
    from gugu.analysis.stage_detector import StageDetector  # noqa: F811

    # 风控层
    from gugu.risk.manager import RiskManager  # noqa: F811

    # 执行层
    from gugu.execution.paper import PaperBroker  # noqa: F811

    # 过滤层
    from gugu.filters.fundamental import FundamentalFilter  # noqa: F811
    from gugu.filters.money_flow import MoneyFlowFilter  # noqa: F811
    from gugu.filters.industry_constraint import IndustryConstraint  # noqa: F811

    # 通知层
    from gugu.notifier.feishu import FeishuNotifier  # noqa: F811

    # 智慧决策层
    from gugu.wisdom import WisdomAdvisor  # noqa: F811

    # 工具层
    from gugu.utils.log import get_logger  # noqa: F811

    # 如果以上导入均成功，测试通过
    assert AppConfig is not None
    assert Signal is not None
    assert Direction is not None
    assert len(Direction.__args__) == 2  # type: ignore[attr-defined]


# ──────────────────────────────────────────────────────────
# 测试 2：验证核心配置加载
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_loading() -> None:
    """核心配置应成功加载，返回 AppConfig 实例。"""
    from gugu.config import get_config

    cfg = get_config()
    assert cfg is not None
    assert hasattr(cfg, "risk")
    assert hasattr(cfg, "strategy")
    assert hasattr(cfg, "data")
    assert hasattr(cfg, "execution")
    assert hasattr(cfg, "feishu")


# ──────────────────────────────────────────────────────────
# 测试 3：验证 Signal 强类型数据模型
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_creation() -> None:
    """Signal 对象应能正确构造并序列化。"""
    from gugu.models.signal import Signal

    s = Signal(symbol="600519", direction="buy", price=150.0)
    assert s.symbol == "600519"
    assert s.direction == "buy"
    assert s.price == 150.0
    assert s.strategy == ""
    assert s.confidence == 1.0

    # to_dict 序列化
    d = s.to_dict()
    assert d["symbol"] == "600519"
    assert d["direction"] == "buy"
    assert d["price"] == 150.0

    # from_dict 反序列化
    s2 = Signal.from_dict(d)
    assert s2.symbol == "600519"
    assert s2.direction == "buy"
    assert s2.price == 150.0


# ──────────────────────────────────────────────────────────
# 测试 4：验证基础数据模型导入
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_types_import() -> None:
    """基础数据模型应可正常导入。"""
    from gugu.models.signal import Direction, FusionMode, OrderResult

    # Direction 类型是 Literal["buy", "sell"]
    assert Direction is not None

    # OrderResult
    order = OrderResult(success=True, symbol="600519", direction="buy", quantity=100, price=150.0)
    assert order.success is True
    assert order.symbol == "600519"

    # OrderResult to_dict
    d = order.to_dict()
    assert d["success"] is True
    assert d["quantity"] == 100

    # FusionMode
    import gugu.models.signal as signal_module

    assert hasattr(signal_module, "FusionMode")
    assert FusionMode is not None


# ──────────────────────────────────────────────────────────
# 测试 5：验证 PerspectiveEngine 可实例化
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_perspective_engine_instantiation() -> None:
    """PerspectiveEngine 应可无参数实例化。"""
    from gugu.analysis.perspectives import ALL_PERSPECTIVES, DEFAULT_WEIGHTS, PerspectiveEngine

    engine = PerspectiveEngine()
    assert engine is not None
    assert engine._perspectives == ALL_PERSPECTIVES
    assert engine._weights == DEFAULT_WEIGHTS

    # 带自定义视角/权重
    custom_engine = PerspectiveEngine(
        perspectives=["buffett", "lynch"],
        weights={"buffett": 0.6, "lynch": 0.4},
    )
    assert custom_engine._perspectives == ["buffett", "lynch"]
    assert custom_engine._weights["buffett"] == 0.6

    # 验证常量已定义
    assert len(ALL_PERSPECTIVES) == 10
    assert "buffett" in ALL_PERSPECTIVES
    assert "technical" in ALL_PERSPECTIVES


# ──────────────────────────────────────────────────────────
# 测试 6：验证 SignalPipeline 可带 mock 依赖构造
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_pipeline_construction() -> None:
    """SignalPipeline 应能用 mock 依赖成功构建。"""
    from gugu.analysis.position_controller import PositionController
    from gugu.analysis.regime_detector import MultiPeriodRegimeDetector
    from gugu.engine.signal_pipeline import SignalPipeline
    from gugu.engine.signal_router import SignalRouter
    from gugu.wisdom import WisdomAdvisor

    # 所有依赖使用 MagicMock
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
    assert pipeline._regime_detector is regime
    assert pipeline._position_controller is pos_ctrl


@pytest.mark.asyncio
async def test_signal_pipeline_processes_real_router_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 SignalRouter 与 SignalPipeline.process 应能组合产出观察信号。"""
    from gugu.analysis.position_controller import PositionBudget
    from gugu.engine.signal_pipeline import SignalPipeline
    from gugu.engine.signal_router import SignalRouter

    dates = pd.bdate_range("2024-01-01", periods=60)
    close = pd.Series([100.0 + i * 0.2 for i in range(60)], dtype=float)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": [1_000_000] * 60,
            "amount": close * 1_000_000,
        }
    )

    strategy = MagicMock()
    strategy.name = "fixture_strategy"
    strategy.generate_signals.return_value = df.assign(
        signal=[0] * 59 + [1], confidence=[0.0] * 59 + [0.8]
    )

    dm = MagicMock()
    dm.is_degraded = False
    wisdom = MagicMock()
    wisdom.advise_fallback.return_value = {
        "wisdom_filtered": False,
        "suggested_position_ratio": 0.2,
        "stop_loss_price": 95.0,
        "take_profit": {"price": 110.0},
    }
    fundamental = MagicMock()
    fundamental.check.return_value = {"pass": True, "industry": "测试行业"}
    money_flow = MagicMock()
    money_flow.check = AsyncMock(return_value={"pass": True, "reasons": []})
    industry = MagicMock()
    industry.check_buy.return_value = {"allowed": True, "reason": "ok"}
    regime_detector = MagicMock()
    position_controller = MagicMock()

    pipeline = SignalPipeline(
        data_manager=dm,
        signal_router=SignalRouter([strategy], fusion_rule="any", min_confidence=0.0),
        wisdom_advisor=wisdom,
        regime_detector=regime_detector,
        position_controller=position_controller,
        fundamental_filter=fundamental,
        money_flow_filter=money_flow,
        industry_constraint=industry,
        exec_mode="signal_only",
    )
    pipeline._scan_with_perspectives = False
    pipeline._holder_concentration_filter = None
    pipeline._altalpha_filter = None
    pipeline._check_sector_rotation = AsyncMock(return_value={"allowed": True, "reason": "ok"})
    monkeypatch.setattr(pipeline, "_detect_stage_and_danger", MagicMock())

    signal = await pipeline.process(
        symbol="600519",
        df=df,
        meta={"name": "测试股票", "is_st": False, "is_suspended": False},
        budget=PositionBudget(0.8, 0.3, 800_000, 5, "test"),
        rt_all=pd.DataFrame(
            {
                "symbol": ["600519"],
                "price": [111.0],
            }
        ),
        watchlist=["600519"],
        portfolio={},
        account=MagicMock(total_value=1_000_000, cash=800_000),
        regime="bull",
    )

    assert signal is not None
    assert signal["symbol"] == "600519"
    assert signal["direction"] == "buy"
    assert signal["strategy"] == "fixture_strategy"
    assert signal["price"] == 111.0
    assert signal["price_is_realtime"] is True
    assert signal["data_quality"] == "fresh_valid"
    assert signal["stop_loss_price"] == 95.0
    strategy.generate_signals.assert_called_once()
    wisdom.advise_fallback.assert_called_once()
    money_flow.check.assert_awaited_once_with("600519")


# ──────────────────────────────────────────────────────────
# 测试 7：验证策略注册表与策略枚举
# ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_strategy_registry_import() -> None:
    """策略注册表应可导入并列出启用策略。"""
    from gugu.strategies.manifest import StrategyManifest
    from gugu.strategies.registry import get_enabled_strategies, list_strategies

    # 元数据
    strategies = list_strategies()
    assert len(strategies) > 0

    # 加载清单
    manifest = StrategyManifest()
    assert manifest is not None

    # 启用策略
    enabled = get_enabled_strategies()
    assert enabled is not None
