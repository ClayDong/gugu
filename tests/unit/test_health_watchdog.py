"""watchdog 健康看门狗单测（2026-08-01 P0）。

覆盖：
1. 信号历史新鲜度检查：交易日有扫描但信号历史零写入 → 告警
2. 心跳新鲜度检查：心跳超时（>15min 未更新）→ 告警
3. 重启熔断：单日重启次数 >3 → 告警
4. 正常状态 → 不告警
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from gugu.health.watchdog import (
    check_scheduler_health,
    check_signal_history_freshness,
    dedupe_alerts,
    SchedulerHealthResult,
    SignalHistoryResult,
)


# ========== 信号历史新鲜度 ==========


def test_signal_history_recent_no_alert(tmp_path: Path):
    """信号历史今天有写入 → 不告警。"""
    hist = tmp_path / "signals_history.jsonl"
    hist.write_text(
        json.dumps({"timestamp": datetime.now().isoformat(), "symbol": "600519"}) + "\n",
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boot.write_text(
        json.dumps({"boot_time": datetime.now().isoformat(), "pid": 123}), encoding="utf-8"
    )
    res = check_signal_history_freshness(history_path=hist, boot_path=boot, data_dir=tmp_path)
    assert isinstance(res, SignalHistoryResult)
    assert res.ok is True, f"近期有写入不应告警: {res}"


def test_signal_history_stale_no_boot_alert(tmp_path: Path):
    """无调度器启动记录（scheduler 从未启动）→ 不告警（系统本来就没跑）。"""
    hist = tmp_path / "signals_history.jsonl"
    hist.write_text(
        json.dumps({"timestamp": (datetime.now() - timedelta(days=5)).isoformat()}) + "\n",
        encoding="utf-8",
    )
    res = check_signal_history_freshness(
        history_path=hist, boot_path=tmp_path / "scheduler_boot.json", data_dir=tmp_path
    )
    assert res.ok is True, "无启动记录时不应误报"


def test_signal_history_stale_with_boot_alerts(tmp_path: Path):
    """调度器已启动（有 boot 记录）但扫描活动也已停止 → 告警。"""
    hist = tmp_path / "signals_history.jsonl"
    hist.write_text(
        json.dumps({"timestamp": (datetime.now() - timedelta(days=3)).isoformat()}) + "\n",
        encoding="utf-8",
    )
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps({"last_scan_at": (datetime.now() - timedelta(days=3)).isoformat()}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boot.write_text(
        json.dumps({"boot_time": datetime.now().isoformat(), "pid": 123}), encoding="utf-8"
    )
    res = check_signal_history_freshness(
        history_path=hist, boot_path=boot, scan_path=scan, data_dir=tmp_path
    )
    assert res.ok is False
    assert "信号扫描" in res.message
    assert res.severity == "high"


def test_signal_history_stale_but_recent_scan_is_healthy(tmp_path: Path):
    """信号历史很旧但最近扫描完成且无有效信号 → 不应误报失忆。"""
    hist = tmp_path / "signals_history.jsonl"
    hist.write_text(
        json.dumps({"timestamp": (datetime.now() - timedelta(days=32)).isoformat()}) + "\n",
        encoding="utf-8",
    )
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps({"last_scan_at": datetime.now().isoformat(), "signals_count": 0}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boot.write_text(
        json.dumps({"boot_time": datetime.now().isoformat(), "pid": 123}), encoding="utf-8"
    )
    res = check_signal_history_freshness(
        history_path=hist, boot_path=boot, scan_path=scan, data_dir=tmp_path
    )
    assert res.ok is True
    assert "扫描正常" in res.message


# ========== 调度器心跳 ==========


def test_heartbeat_fresh_no_alert(tmp_path: Path):
    """心跳 1 分钟前更新 → 正常。"""
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps({"last_cycle_at": datetime.now().isoformat(), "status": "alive"}),
        encoding="utf-8",
    )
    res = check_scheduler_health(
        heartbeat_path=hb, boot_path=tmp_path / "scheduler_boot.json", data_dir=tmp_path
    )
    assert res.ok is True


def test_heartbeat_stale_alerts(tmp_path: Path):
    """心跳 30 分钟未更新 → 告警（调度器可能挂死）。"""
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps(
            {
                "last_cycle_at": (datetime.now() - timedelta(minutes=30)).isoformat(),
                "status": "alive",
            }
        ),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boot.write_text(
        json.dumps({"boot_time": datetime.now().isoformat(), "pid": 123}), encoding="utf-8"
    )
    res = check_scheduler_health(heartbeat_path=hb, boot_path=boot, data_dir=tmp_path)
    assert res.ok is False
    assert "心跳" in res.message
    assert res.severity == "high"


def test_heartbeat_missing_alerts(tmp_path: Path):
    """心跳文件不存在 + 有启动记录 → 告警。"""
    boot = tmp_path / "scheduler_boot.json"
    boot.write_text(
        json.dumps({"boot_time": datetime.now().isoformat(), "pid": 123}), encoding="utf-8"
    )
    res = check_scheduler_health(
        heartbeat_path=tmp_path / "heartbeat.json", boot_path=boot, data_dir=tmp_path
    )
    assert res.ok is False


def test_restart_boom_alerts(tmp_path: Path):
    """单日重启 >3 次 → 告警。"""
    now = datetime.now().replace(microsecond=0)
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps({"last_cycle_at": now.isoformat(), "status": "alive"}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boots = [
        {"boot_time": (now - timedelta(minutes=m)).isoformat(), "pid": 100 + i}
        for i, m in enumerate([1, 2, 3, 5, 8])
    ]
    boot.write_text(json.dumps({"history": boots}), encoding="utf-8")
    res = check_scheduler_health(
        heartbeat_path=hb,
        boot_path=boot,
        data_dir=tmp_path,
        max_restarts_per_day=3,
    )
    assert res.ok is False
    assert "重启" in res.message


def test_restart_storm_escalates_once_per_day(tmp_path: Path):
    """v0.9.32：重启风暴当日首次升级后，同日后续轮次静默（同因不重复升级）。

    2026-08-19 实证：部署导致 4 次启动（无崩溃线索），12:45 升级后
    13:45 对同一根因重复升级。修复后首轮仍升级（新信息），同日后缀
    轮次降为 ok=True（保留计数留痕，不产生升级记录），跨天重置。
    """
    now = datetime.now().replace(microsecond=0)
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps({"last_cycle_at": now.isoformat(), "status": "alive"}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boots = [
        {"boot_time": (now - timedelta(minutes=m)).isoformat(), "pid": 100 + i}
        for i, m in enumerate([1, 2, 3, 5, 8])
    ]
    boot.write_text(json.dumps({"history": boots}), encoding="utf-8")

    # 首轮：无状态文件 → 升级，并持久化当日标记
    res1 = check_scheduler_health(
        heartbeat_path=hb,
        boot_path=boot,
        data_dir=tmp_path,
        max_restarts_per_day=3,
    )
    assert res1.ok is False
    assert "重启风暴" in res1.message
    state_path = tmp_path / "watchdog_restart_storm_state.json"
    assert state_path.exists(), "首次升级应持久化当日标记"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_escalated_date"] == now.date().isoformat()

    # 同日第二轮：同根因 → 静默（ok=True，message 说明已升级过）
    res2 = check_scheduler_health(
        heartbeat_path=hb,
        boot_path=boot,
        data_dir=tmp_path,
        max_restarts_per_day=3,
    )
    assert res2.ok is True, f"同日同因不应重复升级: {res2}"
    assert res2.severity == "medium"
    assert "不重复升级" in res2.message
    assert res2.restarts_today == 5


def test_restart_storm_dedup_resets_next_day(tmp_path: Path):
    """v0.9.32：昨日已升级过重启风暴 → 今日首次达到阈值仍正常升级（跨天重置）。"""
    now = datetime.now().replace(microsecond=0)
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps({"last_cycle_at": now.isoformat(), "status": "alive"}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boots = [
        {"boot_time": (now - timedelta(minutes=m)).isoformat(), "pid": 100 + i}
        for i, m in enumerate([1, 2, 3, 5, 8])
    ]
    boot.write_text(json.dumps({"history": boots}), encoding="utf-8")
    # 状态文件标记为昨天 → 今天应视为首次升级
    state_path = tmp_path / "watchdog_restart_storm_state.json"
    yesterday = (now - timedelta(days=1)).date().isoformat()
    state_path.write_text(json.dumps({"last_escalated_date": yesterday}), encoding="utf-8")
    res = check_scheduler_health(
        heartbeat_path=hb,
        boot_path=boot,
        data_dir=tmp_path,
        max_restarts_per_day=3,
    )
    assert res.ok is False
    assert "重启风暴" in res.message
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_escalated_date"] == now.date().isoformat(), "标记应刷新为今日"


def test_group_alerts_exclude_restart_storm():
    from scripts.health_watchdog import group_alert_messages

    results = [
        SchedulerHealthResult(
            ok=False, severity="high", message="⚠️ 调度器重启风暴：今日已重启 4 次"
        ),
        SchedulerHealthResult(ok=False, severity="high", message="⚠️ 调度器心跳超时：30 分钟未更新"),
    ]

    messages = group_alert_messages(results)

    assert messages == ["⚠️ 调度器心跳超时：30 分钟未更新"]


def test_restart_within_limit_no_alert(tmp_path: Path):
    """单日重启 ≤3 次 → 正常。"""
    now = datetime.now()
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps({"last_cycle_at": now.isoformat(), "status": "alive"}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boots = [
        {"boot_time": (now - timedelta(hours=h)).isoformat(), "pid": 100 + i}
        for i, h in enumerate([1, 2, 3])
    ]
    boot.write_text(json.dumps({"history": boots}), encoding="utf-8")
    res = check_scheduler_health(
        heartbeat_path=hb,
        boot_path=boot,
        data_dir=tmp_path,
        max_restarts_per_day=3,
    )
    assert res.ok is True, "重启 3 次在限额内不应告警"


def test_missing_files_no_boot_no_alert(tmp_path: Path):
    """全无文件（首次安装）→ 不告警（避免安装即报错）。"""
    res = check_scheduler_health(
        heartbeat_path=tmp_path / "heartbeat.json",
        boot_path=tmp_path / "scheduler_boot.json",
        data_dir=tmp_path,
    )
    assert res.ok is True, "无任何运行痕迹时不应误报"


def test_restart_count_no_double_count_top_level(tmp_path: Path):
    """顶层 boot_time 与 history 最后一条相同（_record_boot 双写）→ 不重复计数。

    2026-08-11 回归：_record_boot() 把本次启动同时写入顶层 boot_time 与
    history，原检查两处都计 → 3 次真实启动显示 4 次 → 误报重启风暴全天重发。
    """
    now = datetime.now()
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps({"last_cycle_at": now.isoformat(), "status": "alive"}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    # 3 次启动：10:35 / 10:46 / 11:19，顶层 boot_time 与 history 最后一条相同
    boots = [
        {"boot_time": (now - timedelta(minutes=m)).isoformat(), "pid": 100 + i}
        for i, m in enumerate([1, 2, 3])
    ]
    top_level = boots[-1]["boot_time"]
    boot.write_text(
        json.dumps({"boot_time": top_level, "pid": 103, "history": boots}),
        encoding="utf-8",
    )
    res = check_scheduler_health(
        heartbeat_path=hb,
        boot_path=boot,
        data_dir=tmp_path,
        max_restarts_per_day=3,
    )
    assert res.restarts_today == 3, f"应计 3 次而非 4 次，实际 {res.restarts_today}"
    assert res.ok is True, "3 次真实启动（含当前进程）不应误报重启风暴"


# ========== 告警去重（防 cron 刷屏） ==========


def _make_stale_history_result() -> SignalHistoryResult:
    """构造一个失忆告警结果。"""
    return SignalHistoryResult(
        ok=False,
        severity="high",
        message="⚠️ 信号历史失忆告警：最近一次信号写入距今 5.0 天（>2 天）",
        last_write="2026-07-01T00:00:00",
        boot_time="2026-07-01T00:00:00",
    )


def _make_heartbeat_stale_result() -> SchedulerHealthResult:
    """构造一个心跳超时告警结果。"""
    return SchedulerHealthResult(
        ok=False,
        severity="high",
        message="⚠️ 调度器心跳超时：30 分钟未更新（>15 分钟）——调度器可能挂死，请检查！",
        heartbeat_age_min=30.0,
    )


def test_dedupe_first_alert_pushed(tmp_path: Path):
    """首次告警 → 必须推送。"""
    state = tmp_path / "watchdog_alert_state.json"
    result = _make_stale_history_result()
    to_push = dedupe_alerts([result], state_path=state)
    assert len(to_push) == 1
    assert to_push[0] is result
    assert state.exists(), "推送后应写入状态文件"


def test_dedupe_same_alert_suppressed(tmp_path: Path):
    """同一问题在抑制窗口内 → 抑制（不推送）。"""
    state = tmp_path / "watchdog_alert_state.json"
    result = _make_stale_history_result()
    dedupe_alerts([result], state_path=state)  # 第一次推送
    to_push = dedupe_alerts([result], state_path=state)  # 第二次（同一问题）
    assert to_push == [], "同指纹在抑制窗口内不应重复推送"


def test_dedupe_after_suppress_window_re_pushed(tmp_path: Path):
    """超过抑制窗口 → 重新推送。"""
    state = tmp_path / "watchdog_alert_state.json"
    result = _make_stale_history_result()
    dedupe_alerts([result], state_path=state)
    # 把状态文件里的时间改到 3 小时前（> 默认 120 分钟）
    state_data = json.loads(state.read_text(encoding="utf-8"))
    for fp in state_data:
        state_data[fp]["last_alert_at"] = (datetime.now() - timedelta(hours=3)).isoformat()
    state.write_text(json.dumps(state_data), encoding="utf-8")
    to_push = dedupe_alerts([result], state_path=state)
    assert len(to_push) == 1, "超过抑制窗口应重新告警"


def test_dedupe_healthy_clears_state(tmp_path: Path):
    """恢复健康 → 清除去重状态，下次故障立即重新告警。"""
    state = tmp_path / "watchdog_alert_state.json"
    alert = _make_stale_history_result()
    dedupe_alerts([alert], state_path=state)
    assert state.exists()

    healthy = SignalHistoryResult(ok=True, message="信号历史正常写入")
    to_push = dedupe_alerts([healthy], state_path=state)
    assert to_push == []
    # 状态被清除：再次告警立即推送
    to_push = dedupe_alerts([alert], state_path=state)
    assert len(to_push) == 1, "恢复后再次故障应立即告警"


def test_dedupe_different_problems_independent(tmp_path: Path):
    """不同问题（失忆 vs 心跳超时）独立去重，互不影响。"""
    state = tmp_path / "watchdog_alert_state.json"
    stale = _make_stale_history_result()
    hb = _make_heartbeat_stale_result()
    dedupe_alerts([stale], state_path=state)
    # 心跳超时是新问题 → 应推送
    to_push = dedupe_alerts([stale, hb], state_path=state)
    assert len(to_push) == 1
    assert to_push[0] is hb


def test_dedupe_scheduler_problem_types_separate(tmp_path: Path):
    """心跳缺失/超时/重启风暴是三类问题，分别去重。"""
    state = tmp_path / "watchdog_alert_state.json"
    missing = SchedulerHealthResult(
        ok=False, severity="high", message="⚠️ 调度器心跳缺失：heartbeat.json 不存在或时间戳无效"
    )
    stale = _make_heartbeat_stale_result()
    boom = SchedulerHealthResult(
        ok=False, severity="high", message="⚠️ 调度器重启风暴：今日已重启 4 次（>3 次）"
    )
    dedupe_alerts([missing], state_path=state)
    to_push = dedupe_alerts([missing, stale, boom], state_path=state)
    assert len(to_push) == 2  # 心跳超时 + 重启风暴是新问题，心跳缺失被抑制
    fp_set = {r.message for r in to_push}
    assert any("心跳超时" in m for m in fp_set)
    assert any("重启风暴" in m for m in fp_set)


def test_dedupe_corrupt_state_tolerated(tmp_path: Path):
    """状态文件损坏 → 不崩溃，视为无状态（全部推送）。"""
    state = tmp_path / "watchdog_alert_state.json"
    state.write_text("{corrupt json!!!", encoding="utf-8")
    result = _make_stale_history_result()
    to_push = dedupe_alerts([result], state_path=state)
    assert len(to_push) == 1, "损坏状态应视为首次告警"


def test_dedupe_mixed_healthy_and_alert(tmp_path: Path):
    """健康项 + 告警项混合：健康项清除自己指纹，告警项正常去重。"""
    state = tmp_path / "watchdog_alert_state.json"
    stale = _make_stale_history_result()
    hb_ok = SchedulerHealthResult(ok=True, message="调度器健康")
    dedupe_alerts([stale], state_path=state)
    # 心跳健康（清除心跳类状态，但失忆指纹保留）→ 失忆仍被抑制
    to_push = dedupe_alerts([stale, hb_ok], state_path=state)
    assert to_push == []


def test_dedupe_suppress_minutes_zero_always_push(tmp_path: Path):
    """suppress_minutes=0 → 每次故障都推送（等效关闭去重）。"""
    state = tmp_path / "watchdog_alert_state.json"
    result = _make_stale_history_result()
    dedupe_alerts([result], state_path=state, suppress_minutes=0)
    to_push = dedupe_alerts([result], state_path=state, suppress_minutes=0)
    assert len(to_push) == 1, "suppress=0 时不应抑制"
