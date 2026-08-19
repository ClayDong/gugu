"""第一批 P0 防护的针对性测试（2026-08-05）。

覆盖：
1. 单实例锁：已有实例拒绝启动 / 残留锁接管 / GUGU_TEST_MODE 跳过
2. 测试 HTTP 硬隔离：GUGU_TEST_MODE 下真实 client 禁止外发，
   MockTransport 放行（不影响现有测试断言）
3. 健康告警升级：磁盘不足告警 / 数据源降级告警 / 连续 0 信号告警
4. run_all_checks 包含全部五项检查
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from unittest.mock import patch

from gugu.health.watchdog import (
    DataSourceHealthResult,
    DiskHealthResult,
    ZeroSignalResult,
    check_data_source_health,
    check_disk_health,
    check_zero_signal_streak,
    run_all_checks,
)


# ========== 1. 单实例锁 ==========


def test_acquire_lock_when_no_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """无锁文件时获取锁成功。"""
    monkeypatch.delenv("GUGU_TEST_MODE", raising=False)
    from gugu.utils.single_instance import acquire_single_instance_lock

    lock = tmp_path / "scheduler.lock"
    ok = acquire_single_instance_lock(lock_path=lock, project_root=tmp_path)
    assert ok is True
    data = json.loads(lock.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()


def test_acquire_lock_refuses_when_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """已有存活实例的锁 → 拒绝启动。"""
    monkeypatch.delenv("GUGU_TEST_MODE", raising=False)
    from gugu.utils.single_instance import acquire_single_instance_lock

    lock = tmp_path / "scheduler.lock"
    # 伪造一个锁：PID=当前进程（存活），且命令行匹配 gugu 特征
    lock.write_text(
        json.dumps(
            {"pid": os.getpid(), "started_at": "2026-08-05T00:00:00", "cmd": "run_scheduler.py"}
        ),
        encoding="utf-8",
    )
    with patch("gugu.utils.single_instance._process_is_gugu", return_value=True):
        ok = acquire_single_instance_lock(lock_path=lock, project_root=tmp_path)
    assert ok is False


def test_acquire_lock_takes_over_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """残留锁（进程已死）→ 自动接管。"""
    monkeypatch.delenv("GUGU_TEST_MODE", raising=False)
    from gugu.utils.single_instance import acquire_single_instance_lock

    lock = tmp_path / "scheduler.lock"
    # PID=999999 大概率不存在
    lock.write_text(
        json.dumps({"pid": 999999, "started_at": "2026-08-05T00:00:00", "cmd": "run_scheduler.py"}),
        encoding="utf-8",
    )
    ok = acquire_single_instance_lock(lock_path=lock, project_root=tmp_path)
    assert ok is True
    data = json.loads(lock.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()


def test_acquire_lock_skipped_in_test_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GUGU_TEST_MODE 下跳过锁（测试进程可并发）。"""
    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    from gugu.utils.single_instance import acquire_single_instance_lock

    lock = tmp_path / "scheduler.lock"
    lock.write_text(
        json.dumps({"pid": os.getpid(), "cmd": "run_scheduler.py"}),
        encoding="utf-8",
    )
    ok = acquire_single_instance_lock(lock_path=lock, project_root=tmp_path)
    assert ok is True


# ========== 2. 测试 HTTP 硬隔离 ==========


def test_test_mode_blocks_real_http_client(mock_feishu_env):
    """GUGU_TEST_MODE + 真实 client → 禁止外发。"""
    from gugu.notifier.feishu import FeishuNotifier

    notifier = FeishuNotifier()  # 未替换 _client → 真实 httpx client
    assert notifier._test_mode_blocks_http() is True


def test_test_mode_allows_mock_transport(mock_feishu_env):
    """GUGU_TEST_MODE + MockTransport → 放行（测试断言依赖）。"""
    from gugu.notifier.feishu import FeishuNotifier

    notifier = FeishuNotifier()
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 0}))
    )
    notifier._client = mock_client
    try:
        assert notifier._test_mode_blocks_http() is False
    finally:
        import asyncio

        asyncio.run(mock_client.aclose())


@pytest.mark.asyncio
async def test_send_card_noop_in_test_mode(mock_feishu_env):
    """GUGU_TEST_MODE + 真实 client → send_card 返回 False 且不抛异常。"""
    from gugu.notifier.feishu import FeishuNotifier

    notifier = FeishuNotifier()  # 真实 client
    result = await notifier.send_card({"msg_type": "interactive", "card": {"header": {}}})
    assert result is False


# ========== 3. 健康告警升级 ==========


def test_disk_health_low_space_alerts(tmp_path: Path):
    """磁盘剩余空间低于阈值 → 告警。"""
    result = check_disk_health(data_dir=tmp_path, min_free_bytes=10**18)
    assert isinstance(result, DiskHealthResult)
    assert result.ok is False
    assert result.severity == "high"


def test_disk_health_ok(tmp_path: Path):
    """磁盘空间充足 → 正常。"""
    result = check_disk_health(data_dir=tmp_path, min_free_bytes=1)
    assert result.ok is True


def test_data_source_health_high_failure_alerts(tmp_path: Path):
    """当日失败率超阈值 → 告警。"""
    events = tmp_path / "source_health_events.jsonl"
    today = datetime.now().date().isoformat()
    lines = []
    for i in range(6):
        lines.append(
            json.dumps(
                {"timestamp": f"{today}T09:{i:02d}:00", "source": "sina", "event": "failure"}
            )
        )
    for i in range(4):
        lines.append(
            json.dumps(
                {"timestamp": f"{today}T09:{i + 10:02d}:00", "source": "sina", "event": "success"}
            )
        )
    events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = check_data_source_health(data_dir=tmp_path, failure_rate_threshold=0.5)
    assert isinstance(result, DataSourceHealthResult)
    assert result.ok is False
    assert result.failure_rate == 0.6


def test_data_source_health_ok(tmp_path: Path):
    """失败率低 → 正常。"""
    events = tmp_path / "source_health_events.jsonl"
    today = datetime.now().date().isoformat()
    lines = [
        json.dumps({"timestamp": f"{today}T09:00:00", "source": "sina", "event": "success"}),
        json.dumps({"timestamp": f"{today}T09:01:00", "source": "sina", "event": "success"}),
        json.dumps({"timestamp": f"{today}T09:02:00", "source": "sina", "event": "fallback_used"}),
    ]
    events.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = check_data_source_health(data_dir=tmp_path)
    assert result.ok is True


def test_data_source_low_failure_many_degraded_medium_not_high(tmp_path: Path):
    """2026-08-10 修复：失败率正常但降级事件多 → medium 提示，不误报 high。

    背景：熔断振荡单日产生多次 degraded 记录（每次熔断触发一条），
    次数多≠数据源故障——失败率才是主指标。旧逻辑 degraded>=10 即 high，
    修复后失败率 1% + 降级 13 次不应再触发 high 告警。
    """
    events = tmp_path / "source_health_events.jsonl"
    today = datetime.now().date().isoformat()
    lines = []
    # 100 条 success（失败率 0%）
    for i in range(100):
        lines.append(
            json.dumps(
                {"timestamp": f"{today}T09:{i % 60:02d}:00", "source": "sina", "event": "success"}
            )
        )
    # 15 条 degraded（超过旧阈值 10，但失败率仍 0%）
    for i in range(15):
        lines.append(
            json.dumps(
                {"timestamp": f"{today}T10:{i:02d}:00", "source": "akshare", "event": "degraded"}
            )
        )
    events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = check_data_source_health(data_dir=tmp_path)
    assert result.ok is False, "降级频繁应提示"
    assert result.severity == "medium", "失败率正常时降级频繁应为 medium 而非 high"
    assert result.failure_rate == 0.0


def test_zero_signal_streak_alerts_after_threshold(tmp_path: Path):
    """连续 N 轮 0 信号 → 告警（交易时段内）。"""
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps({"last_scan_at": datetime.now().isoformat(), "signals_count": 0}),
        encoding="utf-8",
    )
    # 前 2 轮不告警（streak 1、2），第 3 轮达到阈值
    with (
        patch("gugu.utils.calendar.is_trading_day", return_value=True),
        patch("gugu.utils.calendar.is_trading_time", return_value=True),
    ):
        for expected_ok in (True, True, False):
            result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
            assert result.ok is expected_ok, f"streak 轮次判定错误: {result}"
            assert isinstance(result, ZeroSignalResult)


def test_zero_signal_streak_resets_on_signal(tmp_path: Path):
    """有信号 → streak 清零。"""
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps({"last_scan_at": datetime.now().isoformat(), "signals_count": 2}),
        encoding="utf-8",
    )
    result = check_zero_signal_streak(data_dir=tmp_path)
    assert result.ok is True
    assert result.streak == 0


def test_zero_signal_skips_when_scan_stale(tmp_path: Path):
    """扫描不新鲜（>30 分钟）→ 不累计（由心跳检查覆盖）。"""
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps(
            {"last_scan_at": (datetime.now() - timedelta(hours=2)).isoformat(), "signals_count": 0}
        ),
        encoding="utf-8",
    )
    result = check_zero_signal_streak(data_dir=tmp_path)
    assert result.ok is True  # 不告警


def test_zero_signal_skips_off_trading_hours(tmp_path: Path):
    """非交易时段 → 不累计 streak（收盘后 0 信号是正常的，防误报）。"""
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps({"last_scan_at": datetime.now().isoformat(), "signals_count": 0}),
        encoding="utf-8",
    )
    with (
        patch("gugu.utils.calendar.is_trading_day", return_value=True),
        patch("gugu.utils.calendar.is_trading_time", return_value=False),
    ):
        result = check_zero_signal_streak(data_dir=tmp_path)
    assert result.ok is True
    assert "非交易时段" in result.message
    # streak 不应累计
    streak_file = tmp_path / "watchdog_zero_signal_streak.json"
    assert not streak_file.exists() or json.loads(streak_file.read_text())["streak"] == 0


def test_zero_signal_counts_only_during_trading(tmp_path: Path):
    """交易时段内 0 信号才累计 streak。"""
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps({"last_scan_at": datetime.now().isoformat(), "signals_count": 0}),
        encoding="utf-8",
    )
    with (
        patch("gugu.utils.calendar.is_trading_day", return_value=True),
        patch("gugu.utils.calendar.is_trading_time", return_value=True),
    ):
        result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
    assert result.ok is True  # streak=1 未达阈值
    assert result.streak == 1


def test_zero_signal_escalates_once_per_day(tmp_path: Path):
    """v0.9.30：0 信号升级当日只发生一次（同因不重复升级，跨天重置）。

    2026-08-19 实证：黑屏根因修复后 streak 未重置，10:55 冷却到期对同一根因
    二次升级 → 用户每小时收到重复升级。升级信息价值在首次，同日后续轮次静默。
    """
    scan = tmp_path / "signal_scan.json"
    streak_file = tmp_path / "watchdog_zero_signal_streak.json"
    scan.write_text(
        json.dumps({"last_scan_at": datetime.now().isoformat(), "signals_count": 0}),
        encoding="utf-8",
    )
    with (
        patch("gugu.utils.calendar.is_trading_day", return_value=True),
        patch("gugu.utils.calendar.is_trading_time", return_value=True),
    ):
        # 1-2 轮：未达阈值（ok=True）；第 3 轮：升级（ok=False）
        for expected_ok in (True, True, False):
            result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
            assert result.ok is expected_ok, f"轮次判定错误: {result}"
        assert result.severity == "high"
        # 持久化了 last_escalated_date
        state = json.loads(streak_file.read_text(encoding="utf-8"))
        assert state.get("last_escalated_date") == datetime.now().date().isoformat()

        # 第 4+ 轮（同日）：同因已升级 → 静默不升级
        result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
        assert result.ok is True, "当日已升级过 0 信号，后续轮次应静默"
        assert "不重复升级" in result.message

        # v0.9.31 回归：第 4 轮覆盖写后 last_escalated_date 必须保留，
        # 否则第 5 轮会因字段丢失再次升级（2026-08-19 09:55→10:55 二次升级实证）
        state = json.loads(streak_file.read_text(encoding="utf-8"))
        assert (
            state.get("last_escalated_date") == datetime.now().date().isoformat()
        ), "同因不重复升级轮次不应覆盖丢失 last_escalated_date"
        result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
        assert result.ok is True, "字段保留后第 5 轮仍应静默不升级"
        assert "不重复升级" in result.message
        state = json.loads(streak_file.read_text(encoding="utf-8"))
        assert state.get("last_escalated_date") == datetime.now().date().isoformat()

        # 跨天：last_escalated_date 为昨天 → 允许再次升级
        state["last_escalated_date"] = (datetime.now() - timedelta(days=1)).date().isoformat()
        streak_file.write_text(json.dumps(state), encoding="utf-8")
        result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
        assert result.ok is False, "跨天后应允许再次升级"


def test_zero_signal_exempt_when_chain_healthy_and_today_signal(tmp_path: Path):
    """链路健康豁免（v0.9.14）：扫描在跑+数据健康+当日已有信号产出
    → 0 信号是市场状态（震荡市策略未触发），不累计不告警。

    2026-08-11 实证：上午 4 个 double_yin 信号后，午后震荡市 0 信号
    被误升级人工 7 次——本测试固化"市场状态≠链路故障"判定。
    """
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps(
            {
                "last_scan_at": datetime.now().isoformat(),
                "signals_count": 0,
                "scanned_count": 18,
                "data_health": "ok",
                "data_errors": [],
            }
        ),
        encoding="utf-8",
    )
    hist = tmp_path / "signals_history.jsonl"
    hist.write_text(
        json.dumps(
            {"timestamp": datetime.now().isoformat(), "symbol": "600519", "direction": "buy"}
        )
        + "\n",
        encoding="utf-8",
    )
    with (
        patch("gugu.utils.calendar.is_trading_day", return_value=True),
        patch("gugu.utils.calendar.is_trading_time", return_value=True),
    ):
        # 连续多轮调用也不累计（豁免后 streak 保持 0）
        for _ in range(5):
            result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
            assert result.ok is True, f"链路健康豁免应不告警: {result}"
            assert result.streak == 0
            assert "市场状态" in result.message


def test_zero_signal_alerts_when_chain_healthy_but_no_today_signal(tmp_path: Path):
    """链路健康但当日全天 0 信号产出 → 仍告警（8/5 四根因链事故同款保留）。

    当日无任何信号落盘说明链路从未证明可用——可能是择时降级/置信度过滤
    等静默故障，必须走原告警路径。
    """
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps(
            {
                "last_scan_at": datetime.now().isoformat(),
                "signals_count": 0,
                "scanned_count": 18,
                "data_health": "ok",
                "data_errors": [],
            }
        ),
        encoding="utf-8",
    )
    # 无 signals_history.jsonl（当日无产出）
    with (
        patch("gugu.utils.calendar.is_trading_day", return_value=True),
        patch("gugu.utils.calendar.is_trading_time", return_value=True),
    ):
        for expected_ok in (True, True, False):
            result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
            assert result.ok is expected_ok, f"全天 0 信号应累计告警: {result}"


def test_zero_signal_alerts_when_data_degraded_with_today_signal(tmp_path: Path):
    """数据源不健康（data_health=degraded）+ 当日有信号 → 不豁免，仍累计告警。

    数据源故障导致的 0 信号必须告警，链路健康豁免只在 data_health=ok 时生效。
    """
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps(
            {
                "last_scan_at": datetime.now().isoformat(),
                "signals_count": 0,
                "scanned_count": 18,
                "data_health": "degraded",
                "data_errors": ["flow breaker active"],
            }
        ),
        encoding="utf-8",
    )
    hist = tmp_path / "signals_history.jsonl"
    hist.write_text(
        json.dumps(
            {"timestamp": datetime.now().isoformat(), "symbol": "600519", "direction": "buy"}
        )
        + "\n",
        encoding="utf-8",
    )
    with (
        patch("gugu.utils.calendar.is_trading_day", return_value=True),
        patch("gugu.utils.calendar.is_trading_time", return_value=True),
    ):
        for expected_ok in (True, True, False):
            result = check_zero_signal_streak(data_dir=tmp_path, threshold=3, fresh_minutes=30)
            assert result.ok is expected_ok, f"数据降级不应豁免: {result}"


def test_run_all_checks_contains_all_five(tmp_path: Path):
    """run_all_checks 包含全部五项检查。"""
    results = run_all_checks(data_dir=tmp_path)
    types = {type(r).__name__ for r in results}
    assert "SchedulerHealthResult" in types
    assert "SignalHistoryResult" in types
    assert "DiskHealthResult" in types
    assert "DataSourceHealthResult" in types
    assert "ZeroSignalResult" in types


# ========== fixture ==========


@pytest.fixture
def mock_feishu_env():
    """Patch env() 为已配置状态（与 test_feishu_notifier 同款）。"""
    with patch("gugu.notifier.feishu.env") as mock_env:
        mock_env.return_value.feishu_app_id = "app-id"
        mock_env.return_value.feishu_app_secret = "secret"
        mock_env.return_value.feishu_chat_id = "system-chat-id"
        mock_env.return_value.feishu_business_chat_id = "business-chat-id"
        mock_env.return_value.feishu_webhook = (
            "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
        )
        yield mock_env
