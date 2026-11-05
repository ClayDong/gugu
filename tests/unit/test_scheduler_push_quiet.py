"""调度推送降噪契约测试（UX 优化 2026-11-05）。

背景：用户反馈早中晚推送内容重复、没必要天天发。排查后确认三类噪音：

1. 15:25 信号 T+1 次日验证在「样本数不足」时仍生成灰卡进 digest，
   收盘 15:40 刷出来就是一条「暂无次日验证」的空汇总；
2. 9:35 尾盘选股次日验证在昨日无选股记录时也推一条空卡；
3. 15:20 信号绩效卡走 send_business_card 直接单发，与 15:25 次日验证
   主题重叠（都在讲信号胜率/收益）。该项当前被 `scheduler.tasks.signal_performance=false`
   关闭，但仍需改成 digest 聚合，防止日后重新开启时又变成收盘两条。

本文件把上述三条固化为契约：
- 无实质内容 → 只记日志，不推送；
- 有内容 → 正常推送，且绩效/次日验证共用同一 digest 聚合键；
- 「没有新信息」与「取数失败」必须区分：前者静默，后者仍走 notify_error。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from gugu.engine.scheduler_jobs import SchedulerJobsMixin, _report_has_data


class _Host(SchedulerJobsMixin):
    """最小宿主：只提供 jobs 需要的 _engine。"""

    def __init__(self, notifier: mock.AsyncMock) -> None:
        self._engine = SimpleNamespace(notifier=notifier)


@pytest.fixture
def notifier() -> mock.AsyncMock:
    n = mock.AsyncMock()
    # 这些方法在 job 里被 await，返回值需要是可比较的真值
    n.notify_screener_validation = mock.AsyncMock(return_value=True)
    n.notify_signal_performance = mock.AsyncMock(return_value=True)
    n.notify_signal_daily_validation = mock.AsyncMock(return_value=True)
    n.notify_error = mock.AsyncMock(return_value=True)
    n.send_business_card = mock.AsyncMock(return_value=True)
    return n


@pytest.fixture
def host(notifier: mock.AsyncMock) -> _Host:
    return _Host(notifier)


@pytest.fixture(autouse=True)
def _trading_day() -> None:
    """所有用例默认在交易日运行，避免被日历短路。"""
    with mock.patch("gugu.engine.scheduler_jobs.is_trading_day", return_value=True):
        yield


# ========== _report_has_data 判定本身 ==========


def test_report_has_data_rejects_non_dict() -> None:
    assert _report_has_data(None) is False
    assert _report_has_data([]) is False
    assert _report_has_data("report") is False


def test_report_has_data_rejects_message_only_report() -> None:
    """带 message 的是「无数据说明」，不是可推送内容。"""
    assert _report_has_data({"total_signals": 99, "message": "行情获取失败"}) is False


def test_report_has_data_threshold_is_three() -> None:
    """阈值必须与 formatter 的灰卡阈值一致，否则会出现「有数据却发灰卡」。"""
    assert _report_has_data({"total_signals": 2}) is False
    assert _report_has_data({"total_signals": 3}) is True


def test_report_has_data_rejects_missing_or_bad_total() -> None:
    assert _report_has_data({}) is False
    assert _report_has_data({"total_signals": None}) is False
    assert _report_has_data({"total_signals": "abc"}) is False


def test_report_has_data_tolerates_numeric_string() -> None:
    """报告经 JSON 往返后数字可能变字符串，不能因此误判为无数据。"""
    assert _report_has_data({"total_signals": "5"}) is True


def test_threshold_matches_performance_card_gray_boundary() -> None:
    """formatter 在 total<3 时出灰卡；_report_has_data 必须同界，避免自相矛盾。"""
    from gugu.notifier.formatter import build_signal_performance_card
    from gugu.notifier.formatter_reports import build_signal_daily_validation_card

    for build in (build_signal_performance_card, build_signal_daily_validation_card):
        gray = str(build({"total_signals": 2, "message": "样本数不足（2），暂无数据"}))
        assert "样本数不足" in gray

    report_below = {"total_signals": 2}
    report_at = {"total_signals": 3, "buy_count": 3, "sell_count": 0}
    assert _report_has_data(report_below) is False
    assert _report_has_data(report_at) is True


# ========== 9:35 尾盘选股次日验证 ==========


@pytest.mark.asyncio
async def test_validate_screener_without_results_does_not_push(host, notifier) -> None:
    """昨日无选股记录时，不应再推一条「无验证结果」空卡。"""
    with mock.patch(
        "gugu.engine.scheduler_jobs.validate_yesterday_screener",
        new_callable=mock.AsyncMock,
        return_value=[],
    ):
        await host._safe_validate_screener()

    notifier.notify_screener_validation.assert_not_awaited()
    notifier.notify_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_screener_with_results_still_pushes(host, notifier) -> None:
    """有验证样本时必须照常推送，降噪不能把有效信息也一起吞掉。"""
    results = [
        SimpleNamespace(open_premium=1.2, intraday_high_premium=2.4, stop_loss_hit=False),
        SimpleNamespace(open_premium=-0.5, intraday_high_premium=0.8, stop_loss_hit=True),
    ]
    with mock.patch(
        "gugu.engine.scheduler_jobs.validate_yesterday_screener",
        new_callable=mock.AsyncMock,
        return_value=results,
    ):
        await host._safe_validate_screener()

    notifier.notify_screener_validation.assert_awaited_once_with(results)


@pytest.mark.asyncio
async def test_validate_screener_failure_still_alerts(host, notifier) -> None:
    """取数异常属于故障，必须继续告警——静默的是「没新信息」，不是「出错了」。"""
    with mock.patch(
        "gugu.engine.scheduler_jobs.validate_yesterday_screener",
        new_callable=mock.AsyncMock,
        side_effect=RuntimeError("行情接口超时"),
    ):
        await host._safe_validate_screener()

    notifier.notify_screener_validation.assert_not_awaited()
    notifier.notify_error.assert_awaited_once()
    payload = notifier.notify_error.await_args.args[0]
    assert "尾盘选股验证异常" in payload["message"]


# ========== 15:20 信号绩效 ==========


@pytest.mark.asyncio
async def test_signal_performance_without_samples_does_not_push(host, notifier) -> None:
    tracker = mock.MagicMock()
    tracker.generate_report = mock.AsyncMock(
        return_value={"total_signals": 0, "message": "样本数不足（0），暂无绩效数据"}
    )
    with mock.patch("gugu.analysis.signal_tracker.SignalTracker", return_value=tracker):
        await host._safe_signal_performance()

    notifier.notify_signal_performance.assert_not_awaited()
    notifier.send_business_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_signal_performance_goes_through_digest_not_direct_send(host, notifier) -> None:
    """绩效卡必须走 notify_signal_performance（digest），不能再直接单发。"""
    report = {
        "total_signals": 8,
        "buy_count": 6,
        "sell_count": 2,
        "forward_5d_signal_hit_rate": 0.55,
    }
    tracker = mock.MagicMock()
    tracker.generate_report = mock.AsyncMock(return_value=report)
    with mock.patch("gugu.analysis.signal_tracker.SignalTracker", return_value=tracker):
        await host._safe_signal_performance()

    notifier.notify_signal_performance.assert_awaited_once_with(report)
    notifier.send_business_card.assert_not_awaited()


# ========== 15:25 信号次日验证 ==========


@pytest.mark.asyncio
async def test_signal_daily_validation_without_samples_does_not_push(host, notifier) -> None:
    tracker = mock.MagicMock()
    tracker.generate_daily_validation_report = mock.AsyncMock(
        return_value={"total_signals": 1, "message": "样本数不足（1），暂无次日验证"}
    )
    with mock.patch("gugu.analysis.signal_tracker.SignalTracker", return_value=tracker):
        await host._safe_signal_daily_validation()

    notifier.notify_signal_daily_validation.assert_not_awaited()


@pytest.mark.asyncio
async def test_signal_daily_validation_with_samples_pushes(host, notifier) -> None:
    report = {"total_signals": 5, "valid_count": 5, "win_rate": 0.6}
    tracker = mock.MagicMock()
    tracker.generate_daily_validation_report = mock.AsyncMock(return_value=report)
    with mock.patch("gugu.analysis.signal_tracker.SignalTracker", return_value=tracker):
        await host._safe_signal_daily_validation()

    notifier.notify_signal_daily_validation.assert_awaited_once_with(report)


@pytest.mark.asyncio
async def test_signal_daily_validation_failure_still_alerts(host, notifier) -> None:
    tracker = mock.MagicMock()
    tracker.generate_daily_validation_report = mock.AsyncMock(
        side_effect=RuntimeError("行情源不可用")
    )
    with mock.patch("gugu.analysis.signal_tracker.SignalTracker", return_value=tracker):
        await host._safe_signal_daily_validation()

    notifier.notify_error.assert_awaited_once()
    assert "信号次日验证异常" in notifier.notify_error.await_args.args[0]["message"]
