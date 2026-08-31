"""TradingScheduler 单元测试。

覆盖调度器的任务配置、安全执行（_safe_run/_safe_report）、
异常捕获以及生命周期（start/shutdown）。
仅 mock 外部依赖（TradingEngine、is_trading_day、asyncio.sleep），
不 mock 被测的 TradingScheduler 业务逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

from gugu.engine.scheduler import TradingScheduler


@pytest.fixture
def mock_engine() -> mock.AsyncMock:
    """Mock TradingEngine：避免真实初始化带来的副作用。"""
    engine = mock.AsyncMock()
    engine.market_filter = mock.MagicMock()
    engine.market_filter.check_market = mock.AsyncMock(return_value={"regime": "bull"})
    engine.notifier = mock.AsyncMock()
    return engine


@pytest.fixture
def scheduler(mock_engine: mock.AsyncMock) -> TradingScheduler:
    """构造一个引擎被 mock 的 TradingScheduler。

    v0.9.33+：TradingScheduler 接受 engine 注入，不再需要 patch。
    """
    return TradingScheduler(engine=mock_engine)


def test_setup_creates_scheduled_jobs(scheduler: TradingScheduler) -> None:
    """setup() 后应创建定时任务（scan+report+retry+macro）。

    任务数量随 settings.yaml scheduler.tasks 开关变化：
    当前关闭的任务（false）不会注册对应 job：
    - flow_afternoon / perspective_daily / altalpha_fetch
    - sentiment_daily / perspective_afternoon / concept_daily
    - signal_performance / alpha_summary / market_temp
    - signal_noon
    - macro_morning / macro_preamarket / macro_noon / macro_close
    v0.7.5 P0-14修复：减少推送频次，关闭非核心任务
    """
    scheduler.setup()
    jobs = scheduler._scheduler.get_jobs()
    # 4 scan + 1 rsi_scan + 1 screener + 1 screener_validate + 1 screener_validate_eod
    # + 1 screener_auto_tune + 2 range_watchlist + 1 report_close + 1 retry
    # + 1 cache_cleanup + 1 weekly_optimize + 1 heuristic_optimize + 1 weekly_review
    # + 1 intraday_monitor + 1 config_hot_reload
    # + 1 portfolio_report + 1 signal_daily_validation + 1 signal_weekly = 22
    # 当前 settings 的四个宏观开关均为 false，因此宏观任务不注册。
    assert len(jobs) == 22
    job_ids = {j.id for j in jobs}
    assert "intraday_monitor" in job_ids
    assert "scan_1" in job_ids
    assert "scan_2" in job_ids
    assert "scan_3" in job_ids
    assert "scan_4" in job_ids
    assert "rsi_scan_1330" in job_ids
    assert "screener_1430" in job_ids
    assert "screener_validate_0935" in job_ids
    assert "screener_validate_1505" in job_ids
    assert "screener_auto_tune_1510" in job_ids
    assert "report_close" in job_ids
    assert "retry_notify" in job_ids
    assert "macro_early" not in job_ids
    assert "macro_morning" not in job_ids
    assert "macro_noon" not in job_ids
    assert "macro_close" not in job_ids
    # P1-7: weekly_optimize_0300 周参数优化任务
    assert "weekly_optimize_0300" in job_ids
    # 启发式优化任务（每月 1/15 日 04:00）
    assert "heuristic_optimize_0400" in job_ids
    # P2-8: weekly_review_2000 周复盘推送任务
    assert "weekly_review_2000" in job_ids
    # 持仓认知分析任务（15:35 收盘后推送）
    assert "portfolio_report_1535" in job_ids
    # 缓存清理任务
    assert "cache_cleanup_0700" in job_ids
    # 市场温度计在 settings.yaml 中关闭
    assert "market_temp_0825" not in job_ids
    # 信号绩效反馈任务
    assert "signal_daily_validation_1525" in job_ids
    assert "signal_weekly_2030" in job_ids
    # 配置热加载检测
    assert "config_hot_reload" in job_ids
    # flow jobs should NOT exist (merged into macro or disabled)
    assert "flow_morning" not in job_ids
    assert "flow_close" not in job_ids
    # settings.yaml 中关闭的任务不应注册
    assert "afternoon_flow_1430" not in job_ids  # flow_afternoon: false
    assert "altalpha_fetch_1800" not in job_ids  # altalpha_fetch: false
    assert "perspective_report_0830" not in job_ids  # perspective_daily: false
    assert "sentiment_daily_2030" not in job_ids  # sentiment_daily: false
    assert "signal_performance_1520" not in job_ids  # signal_performance: false


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
    mock_engine.notifier.notify_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_safe_afternoon_flow_notifies_error(
    scheduler: TradingScheduler, mock_engine: mock.AsyncMock
) -> None:
    with (
        mock.patch("gugu.engine.scheduler.is_trading_day", return_value=True),
        mock.patch("gugu.engine.scheduler_jobs.is_trading_day", return_value=True),
        mock.patch(
            "gugu.notifier.flow_report.run_afternoon_report", side_effect=RuntimeError("flow boom")
        ),
    ):
        await scheduler._safe_afternoon_flow()
    mock_engine.notifier.notify_error.assert_awaited_once()


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
    # 验证调度器与引擎均已关闭，心跳任务也已回收
    shutdown_spy.assert_called_once()
    mock_engine.shutdown.assert_awaited_once()
    assert scheduler._heartbeat_task is not None
    assert scheduler._heartbeat_task.done()


@pytest.mark.asyncio
async def test_optimize_params_uses_sys_executable_not_uv(
    scheduler: TradingScheduler,
) -> None:
    """周参数优化子进程应使用 sys.executable 而非 "uv run python"（v0.9.35）。

    launchd 环境 PATH 不含 uv（实证 2026-08-23 03:00 [Errno 2] 失败），
    调度器运行于项目 venv（sys.executable = .venv/bin/python），
    直接调用无需 uv/PATH 依赖。
    """
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured.append(list(cmd))
        return mock.MagicMock(returncode=0, stdout="ok", stderr="")

    with (
        mock.patch(
            "gugu.engine.scheduler_jobs.get_enabled_strategies",
            return_value=[mock.MagicMock(name="box_breakout")],
        ),
        mock.patch(
            "gugu.analysis.optimization_pools.get_strategy_stock_pool",
            return_value="600519,000858",
        ),
        mock.patch(
            "gugu.config.PROJECT_ROOT",
            Path(__file__).resolve().parents[2],
        ),
        mock.patch(
            "gugu.engine.scheduler_jobs.subprocess.run",
            side_effect=fake_run,
        ),
    ):
        await scheduler._safe_optimize_params()

    assert captured, "应至少调用一次周参数优化子进程"
    for cmd in captured:
        assert cmd[0] == sys.executable, f"应使用 sys.executable，实际: {cmd[0]}"
        assert "uv" not in cmd, f"不应依赖 uv，实际 cmd: {cmd}"
        assert "optimize_params.py" in cmd[1]
