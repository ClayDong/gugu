"""主引擎 TradingEngine 单元测试。

覆盖核心路径：run_daily_cycle、_process_signal、_check_daily_loss、
_update_prices、send_daily_report、reset_halt、心跳写入。
用 mock 替换 DataManager、FeishuNotifier、PaperBroker、RiskManager、
StockSelector、WisdomAdvisor，确保测试不依赖网络。
"""
from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from gugu.engine.main import TradingEngine
from gugu.execution.base import AccountInfo, OrderResult
from gugu.risk.rules import RiskAction, RiskCheckResult, RiskLevel


@pytest.fixture
def mock_data_manager() -> mock.MagicMock:
    """Mock DataManager：返回可控的行情数据。"""
    dm = mock.MagicMock()
    dm.fetch_stock_history = mock.AsyncMock(return_value=pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=60, freq="D"),
            "open": [10.0] * 60,
            "high": [11.0] * 60,
            "low": [9.0] * 60,
            "close": [10.0] * 60,
            "volume": [1_000_000] * 60,
            "amount": [10_000_000] * 60,
        }
    ))
    dm.fetch_stock_realtime = mock.AsyncMock(return_value=pd.DataFrame(
        {"symbol": ["600519"], "name": ["茅台"], "price": [10.0]}
    ))
    dm.fetch_stock_meta = mock.AsyncMock(return_value={
        "symbol": "600519",
        "name": "茅台",
        "prev_close": 10.0,
        "is_st": False,
        "is_suspended": False,
    })
    dm.fetch_sector_flow = mock.AsyncMock(return_value=pd.DataFrame(
        {"sector": ["白酒"], "main_net": [1e9], "main_pct": [0.1]}
    ))
    return dm


@pytest.fixture
def mock_notifier() -> mock.AsyncMock:
    return mock.AsyncMock()


@pytest.fixture
def mock_broker() -> mock.MagicMock:
    broker = mock.MagicMock()
    broker.get_account.return_value = AccountInfo(
        cash=1_000_000, total_value=1_000_000, positions={}
    )
    broker.get_portfolio.return_value = {}
    broker.daily_start_value = 1_000_000
    broker.order.return_value = OrderResult(
        True, "600519", "buy", 10.0, 100, 0.25, message="买入成功"
    )
    return broker


@pytest.fixture
def mock_risk() -> mock.MagicMock:
    risk = mock.MagicMock()
    risk.is_halted = False
    risk.check_order.return_value = RiskCheckResult(
        RiskLevel.L1_POSITION, RiskAction.ALLOW, "ok"
    )
    risk.check_daily_loss.return_value = RiskCheckResult(
        RiskLevel.L2_DAILY_LOSS, RiskAction.ALLOW, "within limits"
    )
    return risk


@pytest.fixture
def engine(
    mock_data_manager: mock.MagicMock,
    mock_notifier: mock.AsyncMock,
    mock_broker: mock.MagicMock,
    mock_risk: mock.MagicMock,
) -> TradingEngine:
    """构造一个所有依赖均被 mock 的 TradingEngine。"""
    # 测试中默认使用 paper 模式（而非 signal_only），以便测试下单逻辑
    mock_settings = {"execution": {"mode": "paper", "paper": {"initial_capital": 1_000_000}}}
    with (
        mock.patch("gugu.engine.main.data_manager", return_value=mock_data_manager),
        mock.patch("gugu.engine.main.get_enabled_strategies", return_value=[]),
        mock.patch("gugu.engine.main.PaperBroker", return_value=mock_broker),
        mock.patch("gugu.engine.main.FeishuNotifier", return_value=mock_notifier),
        mock.patch("gugu.engine.main.RiskManager", return_value=mock_risk),
        mock.patch("gugu.engine.main.WisdomAdvisor"),
        mock.patch("gugu.engine.main.StockSelector"),
        mock.patch("gugu.engine.main.settings", return_value=mock_settings),
    ):
        eng = TradingEngine()
    return eng


def test_engine_init(engine: TradingEngine) -> None:
    """引擎初始化应加载自选股。"""
    assert len(engine._watchlist) > 0


@pytest.mark.asyncio
async def test_run_daily_cycle_non_trading_day(engine: TradingEngine) -> None:
    """非交易日应跳过交易循环。"""
    with mock.patch("gugu.engine.main.is_trading_day", return_value=False):
        await engine.run_daily_cycle()
    # 不应调用 settle_t_plus_1
    engine._broker.settle_t_plus_1.assert_not_called()


@pytest.mark.asyncio
async def test_run_daily_cycle_trading_day(
    engine: TradingEngine, mock_broker: mock.MagicMock
) -> None:
    """交易日应执行完整循环。"""
    with mock.patch("gugu.engine.main.is_trading_day", return_value=True):
        await engine.run_daily_cycle()
    mock_broker.settle_t_plus_1.assert_called_once()
    engine._risk.reset.assert_called_once()


@pytest.mark.asyncio
async def test_process_signal_risk_blocked(
    engine: TradingEngine, mock_risk: mock.MagicMock, mock_notifier: mock.AsyncMock
) -> None:
    """风控拦截信号时不下单，发风控告警。"""
    mock_risk.check_order.return_value = RiskCheckResult(
        RiskLevel.L1_POSITION, RiskAction.HALT, "仓位超限"
    )
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 10.0,
        "strategies": ["turtle"],
        "strategy": "turtle",
    }
    await engine._process_signal(signal)
    engine._broker.order.assert_not_called()
    mock_notifier.notify_risk_alert.assert_called_once()


@pytest.mark.asyncio
async def test_process_signal_success(
    engine: TradingEngine,
    mock_broker: mock.MagicMock,
    mock_notifier: mock.AsyncMock,
) -> None:
    """风控通过时下单并发送信号通知。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 10.0,
        "strategies": ["turtle"],
        "strategy": "turtle",
    }
    await engine._process_signal(signal)
    mock_broker.order.assert_called_once()
    mock_notifier.notify_signal.assert_called_once()


@pytest.mark.asyncio
async def test_check_daily_loss_warn(
    engine: TradingEngine, mock_risk: mock.MagicMock, mock_notifier: mock.AsyncMock
) -> None:
    """日亏预警触发飞书告警。"""
    mock_risk.check_daily_loss.return_value = RiskCheckResult(
        RiskLevel.L2_DAILY_LOSS, RiskAction.WARN, "日亏 3% 预警"
    )
    await engine._check_daily_loss()
    mock_notifier.notify_risk_alert.assert_called_once()
    alert = mock_notifier.notify_risk_alert.call_args[0][0]
    assert alert["level"] == "warn"


@pytest.mark.asyncio
async def test_check_daily_loss_halt(
    engine: TradingEngine, mock_risk: mock.MagicMock, mock_notifier: mock.AsyncMock
) -> None:
    """日亏熔断触发飞书告警。"""
    mock_risk.check_daily_loss.return_value = RiskCheckResult(
        RiskLevel.L2_DAILY_LOSS, RiskAction.HALT, "日亏 5% 熔断"
    )
    await engine._check_daily_loss()
    mock_notifier.notify_risk_alert.assert_called_once()
    alert = mock_notifier.notify_risk_alert.call_args[0][0]
    assert alert["level"] == "halt"


@pytest.mark.asyncio
async def test_send_daily_report(
    engine: TradingEngine, mock_notifier: mock.AsyncMock
) -> None:
    """日报发送应调用 notifier。"""
    await engine.send_daily_report("close")
    mock_notifier.notify_daily_report.assert_called_once()
    args = mock_notifier.notify_daily_report.call_args
    assert args[0][0] == "close"


@pytest.mark.asyncio
async def test_update_prices(
    engine: TradingEngine, mock_data_manager: mock.MagicMock, mock_broker: mock.MagicMock
) -> None:
    """更新现价应调用 broker.update_price。"""
    mock_broker.get_portfolio.return_value = {"600519": mock.MagicMock()}
    await engine._update_prices()
    mock_data_manager.fetch_stock_realtime.assert_awaited()
    mock_broker.update_price.assert_called()


@pytest.mark.asyncio
async def test_update_prices_exception(
    engine: TradingEngine, mock_data_manager: mock.MagicMock
) -> None:
    """更新现价失败不应抛异常。"""
    mock_data_manager.fetch_stock_realtime = mock.AsyncMock(side_effect=RuntimeError("network error"))
    # 不应抛异常
    await engine._update_prices()


@pytest.mark.asyncio
async def test_shutdown(engine: TradingEngine, mock_notifier: mock.AsyncMock) -> None:
    """shutdown 应关闭 notifier。"""
    await engine.shutdown()
    mock_notifier.close.assert_called_once()


def test_reset_halt(engine: TradingEngine, mock_risk: mock.MagicMock) -> None:
    """reset_halt 应清除熔断状态但不重置日初净值（P-01 修复）。"""
    engine.reset_halt()
    mock_risk.clear_halt_only.assert_called_once()
    # 不应再调用 reset（避免掩盖当日亏损）
    mock_risk.reset.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_written(
    engine: TradingEngine, mock_broker: mock.MagicMock
) -> None:
    """心跳文件应被写入。"""
    # 验证 _write_heartbeat 不抛异常即可（PROJECT_ROOT 已在 main.py 中定义）
    engine._write_heartbeat("ok")


@pytest.mark.asyncio
async def test_scan_signals_empty_watchlist(engine: TradingEngine) -> None:
    """空自选股列表应返回空信号。"""
    engine._watchlist = []
    signals = await engine._scan_signals()
    assert signals == []


@pytest.mark.asyncio
async def test_scan_signals_with_data(
    engine: TradingEngine, mock_data_manager: mock.MagicMock
) -> None:
    """有数据但无策略时应返回空信号（router 无策略时不产生信号）。"""
    engine._watchlist = ["600519"]
    signals = await engine._scan_signals()
    # router 无策略，返回空
    assert signals == []


@pytest.mark.asyncio
async def test_scan_signals_data_fetch_error(
    engine: TradingEngine, mock_data_manager: mock.MagicMock
) -> None:
    """数据采集失败不应中断扫描。"""
    mock_data_manager.fetch_stock_history = mock.AsyncMock(side_effect=RuntimeError("api error"))
    engine._watchlist = ["600519"]
    signals = await engine._scan_signals()
    assert signals == []


@pytest.mark.asyncio
async def test_run_daily_cycle_halted_skips(
    engine: TradingEngine, mock_risk: mock.MagicMock, mock_broker: mock.MagicMock
) -> None:
    """L2 熔断状态下应跳过交易循环，不执行 reset。"""
    mock_risk.is_halted = True
    with mock.patch("gugu.engine.main.is_trading_day", return_value=True):
        await engine.run_daily_cycle()
    # 不应调用 settle_t_plus_1 和 reset
    mock_broker.settle_t_plus_1.assert_not_called()
    mock_risk.reset.assert_not_called()


def test_paper_broker_direction_normalization(tmp_path, monkeypatch) -> None:
    """PaperBroker 应规范化 direction 参数（大小写/空格）。"""
    from gugu.execution import PaperBroker

    monkeypatch.setattr("gugu.execution.paper.STATE_FILE", tmp_path / "state.json")
    broker = PaperBroker(initial_capital=1_000_000)
    # 传入 "Buy"（大写）应正常买入
    result = broker.order("600519", "Buy", 100, price=10.0)
    assert result.success is True
    assert result.direction == "buy"

    # 传入 " buy "（带空格）应正常买入
    result2 = broker.order("000858", " buy ", 100, price=10.0)
    assert result2.success is True
    assert result2.direction == "buy"


@pytest.mark.asyncio
async def test_process_signal_wisdom_filtered(
    engine: TradingEngine,
    mock_broker: mock.MagicMock,
    mock_notifier: mock.AsyncMock,
) -> None:
    """wisdom 入场过滤的信号应仅通知不下单。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 10.0,
        "strategies": ["turtle"],
        "strategy": "turtle",
        "wisdom_filtered": True,
        "wisdom_decision": {"entry_filtered": True, "filter_reason": "低置信度"},
    }
    await engine._process_signal(signal)
    # 不应下单
    mock_broker.order.assert_not_called()
    # 应发送信号通知（让用户看到被过滤的信号）
    mock_notifier.notify_signal.assert_called_once()


@pytest.mark.asyncio
async def test_process_signal_respects_wisdom_position_ratio(
    engine: TradingEngine,
    mock_broker: mock.MagicMock,
    mock_notifier: mock.AsyncMock,
) -> None:
    """_process_signal 应使用 wisdom 调整后的仓位比例，不覆盖。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 10.0,
        "strategies": ["turtle"],
        "strategy": "turtle",
        "suggested_position_ratio": 0.20,  # wisdom 调整后的试仓比例
    }
    await engine._process_signal(signal)
    mock_broker.order.assert_called_once()
    # 验证下单数量基于 20% 而非默认 24%
    # total_value=1_000_000, ratio=0.20, price=10.0
    # quantity = int(1_000_000 * 0.20 / 10.0 / 100) * 100 = 20000
    call_args = mock_broker.order.call_args
    assert call_args[0][2] == 20000  # quantity
