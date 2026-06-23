"""TradingScheduler 单元测试。

覆盖调度器的任务配置、安全执行（_safe_run/_safe_report）、
异常捕获以及生命周期（start/shutdown）。
仅 mock 外部依赖（TradingEngine、is_trading_day、asyncio.sleep），
不 mock 被测的 TradingScheduler 业务逻辑。
"""
from __future__ import annotations

from unittest import mock

import pytest

from gugu.engine.scheduler import TradingScheduler


@pytest.fixture
def mock_engine() -> mock.AsyncMock:
    """Mock TradingEngine：避免真实初始化带来的副作用。"""
    return mock.AsyncMock()


@pytest.fixture
def scheduler(mock_engine: mock.AsyncMock) -> TradingScheduler:
    """构造一个引擎被 mock 的 TradingScheduler。"""
    with mock.patch(
        "gugu.engine.scheduler.TradingEngine", return_value=mock_engine
    ):
        return TradingScheduler()


def test_setup_creates_scheduled_jobs(scheduler: TradingScheduler) -> None:
    """setup() 后应创建 7 个定时任务（4 个盘中扫描 + 3 个日报）。"""
    scheduler.setup()
    jobs = scheduler._scheduler.get_jobs()
    # 升级后：4 个盘中扫描 + 3 个日报
    assert len(jobs) == 7
    job_ids = {j.id for j in jobs}
    assert "scan_1" in job_ids
    assert "scan_2" in job_ids
    assert "scan_3" in job_ids
    assert "scan_4" in job_ids
    assert "report_morning" in job_ids
    assert "report_noon" in job_ids
    assert "report_close" in job_ids


@pytest.mark.asyncio
async def test_safe_run_skips_non_trading_day(
    scheduler: TradingScheduler, mock_engine: mock.AsyncMock
) -> None:
    """非交易日 _safe_run 应直接返回，不调用 engine.run_daily_cycle。"""
    with mock.patch("gugu.engine.scheduler.is_trading_day", return_value=False):
        await scheduler._safe_run()
    mock_engine.run_daily_cycle.assert_not_called()


@pytest.mark.asyncio
async def test_safe_run_catches_exception(
    scheduler: TradingScheduler, mock_engine: mock.AsyncMock
) -> None:
    """_safe_run 应捕获 engine.run_daily_cycle 抛出的异常，不向上抛出。"""
    mock_engine.run_daily_cycle.side_effect = RuntimeError("engine boom")
    with mock.patch("gugu.engine.scheduler.is_trading_day", return_value=True):
        # 不应抛异常
        await scheduler._safe_run()
    mock_engine.run_daily_cycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_safe_report_catches_exception(
    scheduler: TradingScheduler, mock_engine: mock.AsyncMock
) -> None:
    """_safe_report 应捕获 engine.send_daily_report 抛出的异常，不向上抛出。"""
    mock_engine.send_daily_report.side_effect = RuntimeError("report boom")
    with mock.patch("gugu.engine.scheduler.is_trading_day", return_value=True):
        # 不应抛异常
        await scheduler._safe_report("close")
    mock_engine.send_daily_report.assert_awaited_once()
    # 验证 period 参数被正确传递
    mock_engine.send_daily_report.assert_awaited_with("close")


@pytest.mark.asyncio
async def test_start_and_shutdown_lifecycle(
    scheduler: TradingScheduler, mock_engine: mock.AsyncMock
) -> None:
    """start() 应启动调度器，收到中断信号后调用 shutdown() 优雅退出。"""
    # 监视 _scheduler.shutdown 是否被调用（仍执行真实逻辑）
    with (
        mock.patch.object(
            scheduler._scheduler,
            "shutdown",
            wraps=scheduler._scheduler.shutdown,
        ) as shutdown_spy,
        mock.patch(
            "gugu.engine.scheduler.asyncio.sleep",
            side_effect=KeyboardInterrupt,
        ),
        mock.patch("gugu.engine.scheduler.is_trading_day", return_value=False),
    ):
        await scheduler.start()
    # 验证调度器与引擎均已关闭
    shutdown_spy.assert_called_once()
    mock_engine.shutdown.assert_awaited_once()
