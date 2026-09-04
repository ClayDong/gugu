"""watchdog 健康看门狗单测（2026-08-01 P0）。

覆盖：
1. 信号历史新鲜度检查：交易日有扫描但信号历史零写入 → 告警
2. 心跳新鲜度检查：心跳超时（>15min 未更新）→ 告警
3. 重启熔断：单日重启次数 >3 → 告警
4. 正常状态 → 不告警
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from gugu.health.watchdog import (
    check_scheduler_health,
    check_signal_history_freshness,
    check_zero_signal_streak,
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


def test_signal_history_weekend_stale_scan_aligned_no_alert(tmp_path: Path, monkeypatch):
    """(v0.9.33) 周五收盘后跨周末：last_scan_at 停留在周五 15:10（墙钟超 2 天），
    但扫描与最近交易日收盘对齐 → 不应报"信号历史失忆"。

    2026-08-24 实证：周末 + 周一盘前被误报 7 次，驱动 auto_recover 7 次
    无意义重启 scheduler（非交易日无扫描任务，重启不产生扫描）。
    """
    hist = tmp_path / "signals_history.jsonl"
    hist.write_text(
        json.dumps({"timestamp": (datetime.now() - timedelta(days=4)).isoformat()}) + "\n",
        encoding="utf-8",
    )
    scan = tmp_path / "signal_scan.json"
    # 模拟：last_scan_at = 上个交易日 15:10（收盘后尾扫已跑，之后进入周末）
    last_session = (datetime.now() - timedelta(days=3)).date()
    scan.write_text(
        json.dumps({"last_scan_at": datetime.combine(last_session, time(15, 10)).isoformat()}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boot.write_text(
        json.dumps({"boot_time": (datetime.now() - timedelta(days=5)).isoformat(), "pid": 123}),
        encoding="utf-8",
    )
    # last_trading_day 返回上个交易日 → 扫描对齐（15:10 ≥ 15:00）
    monkeypatch.setattr(
        "gugu.utils.calendar.last_trading_day",
        lambda ref=None, max_lookback=15: last_session,
    )
    res = check_signal_history_freshness(
        history_path=hist, boot_path=boot, scan_path=scan, data_dir=tmp_path
    )
    assert res.ok is True, f"周末扫描对齐不应误报失忆: {res}"
    assert res.details.get("calendar_aligned") is True


def test_signal_history_scan_before_last_close_still_alerts(tmp_path: Path, monkeypatch):
    """(v0.9.33) 最近扫描早于最近交易日收盘（如周五 09:25 盘中停摆）→
    日历对齐豁免不适用 → 仍告警（真故障路径不被豁免掩盖）。"""
    hist = tmp_path / "signals_history.jsonl"
    hist.write_text(
        json.dumps({"timestamp": (datetime.now() - timedelta(days=3)).isoformat()}) + "\n",
        encoding="utf-8",
    )
    scan = tmp_path / "signal_scan.json"
    last_session = (datetime.now() - timedelta(days=3)).date()
    scan.write_text(
        json.dumps({"last_scan_at": datetime.combine(last_session, time(9, 25)).isoformat()}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"
    boot.write_text(
        json.dumps({"boot_time": datetime.now().isoformat(), "pid": 123}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "gugu.utils.calendar.last_trading_day",
        lambda ref=None, max_lookback=15: last_session,
    )
    res = check_signal_history_freshness(
        history_path=hist, boot_path=boot, scan_path=scan, data_dir=tmp_path
    )
    assert res.ok is False, f"盘中停摆（扫描早于收盘）应仍告警: {res}"
    assert res.severity == "high"


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


def test_restart_threshold_default_allows_dev_restarts(tmp_path: Path):
    """v0.9.45：默认重启风暴阈值对齐部署常态（8 次/日）。

    2026-09-02 实证：4 次重启全部为优雅部署/开发重启（日志均含
    "调度器已停止"，crash_clue=无崩溃线索），旧阈值 3 误报升级。
    v0.9.41 阈值 3→5 后，09-03/09-04 又连续两天 6 次（同样全部为
    优雅信号退出 + 无崩溃线索）再次误报 → v0.9.45 阈值 5→8。
    真实崩溃循环由心跳陈旧独立指纹捕获，无盲区。
    4 次 / 6 次（正常开发日，用默认阈值）→ 不告警；9 次（超阈值）→ 告警。
    """
    now = datetime.now().replace(microsecond=0)
    hb = tmp_path / "heartbeat.json"
    hb.write_text(
        json.dumps({"last_cycle_at": now.isoformat(), "status": "alive"}),
        encoding="utf-8",
    )
    boot = tmp_path / "scheduler_boot.json"

    # 2026-09-03 修复：boot 时间锚定到当天 09:00（而非 now-N 分钟）。
    # 原实现用 now 往前推，凌晨运行时部分时间戳跨天，restarts_today 只统计
    # "今天"（watchdog 按 .date()==today 过滤），6 次只数到 4 次导致误判。
    _anchor = now.replace(hour=9, minute=0, second=0, microsecond=0)

    def _mk_boots(n: int):
        return [
            {"boot_time": (_anchor - timedelta(minutes=m)).isoformat(), "pid": 1000 + i}
            for i, m in enumerate([1, 3, 5, 8, 12, 20, 30, 45, 60][:n])
        ]

    # 4 次优雅重启（正常开发日）→ 默认阈值不告警
    boot.write_text(json.dumps({"history": _mk_boots(4)}), encoding="utf-8")
    res = check_scheduler_health(heartbeat_path=hb, boot_path=boot, data_dir=tmp_path)
    assert res.ok is True
    assert res.restarts_today == 4

    # 6 次（v0.9.41 曾超出旧阈值 5；v0.9.45 起 6 次为部署常态）→ 不告警
    boot.write_text(json.dumps({"history": _mk_boots(6)}), encoding="utf-8")
    res2 = check_scheduler_health(heartbeat_path=hb, boot_path=boot, data_dir=tmp_path)
    assert res2.ok is True
    assert res2.restarts_today == 6

    # 9 次（超出默认阈值 8）→ 告警
    boot.write_text(json.dumps({"history": _mk_boots(9)}), encoding="utf-8")
    res3 = check_scheduler_health(heartbeat_path=hb, boot_path=boot, data_dir=tmp_path)
    assert res3.ok is False
    assert "重启" in res3.message


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


# ========== 连续 0 信号 streak（v0.9.34 跨交易日重置） ==========


def _make_zero_signal_scan(tmp_path: Path, signals_count: int = 0) -> Path:
    """构造交易时段内的 0 信号扫描现场（扫描新鲜 + 链路健康 + 当日无信号产出）。"""
    scan = tmp_path / "signal_scan.json"
    scan.write_text(
        json.dumps(
            {
                "last_scan_at": datetime.now().isoformat(),
                "signals_count": signals_count,
                "scanned_count": 18,
                "data_health": "ok",
                "data_errors": [],
            }
        ),
        encoding="utf-8",
    )
    hist = tmp_path / "signals_history.jsonl"
    # 无当日信号产出 → 链路健康豁免不生效 → 走 streak 累计分支
    hist.write_text(
        json.dumps({"timestamp": (datetime.now() - timedelta(days=3)).isoformat()}) + "\n",
        encoding="utf-8",
    )
    return scan


def test_zero_signal_streak_resets_on_new_trading_day(tmp_path: Path, monkeypatch):
    """v0.9.34：昨日遗留 streak（48 轮）不应继承到新交易日——开盘首轮归零重计。

    2026-08-24 实证：08-21 周五 48 轮 0 信号 + 周一开盘首轮 → 09:30:30 即升级人工
    （streak=49），而周一策略仍基于周五收盘 bar 判断，信息量为零。
    修复后：跨交易日首查 streak 归零 → 本轮 streak=1（<阈值 3）→ 不告警。
    """
    monkeypatch.setattr("gugu.utils.calendar.is_trading_day", lambda ref=None: True)
    monkeypatch.setattr("gugu.utils.calendar.is_trading_time", lambda now=None: True)
    _make_zero_signal_scan(tmp_path)
    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
    streak_path = tmp_path / "watchdog_zero_signal_streak.json"
    streak_path.write_text(json.dumps({"streak": 48, "streak_date": yesterday}), encoding="utf-8")

    res = check_zero_signal_streak(
        scan_path=tmp_path / "signal_scan.json",
        streak_path=streak_path,
        history_path=tmp_path / "signals_history.jsonl",
    )
    assert res.ok is True, f"跨交易日首轮不应因昨日 streak 误升级: {res}"
    assert res.streak == 1, f"streak 应从 0 重新累计为 1，实际 {res.streak}"
    # 持久化：streak_date 推进到今天
    state = json.loads(streak_path.read_text(encoding="utf-8"))
    assert state["streak_date"] == datetime.now().date().isoformat()
    assert state["streak"] == 1


def test_zero_signal_streak_accumulates_within_same_day(tmp_path: Path, monkeypatch):
    """v0.9.34：同一交易日 streak 正常累计——第 3 轮 0 信号仍照常升级（8/5 检测能力保留）。"""
    monkeypatch.setattr("gugu.utils.calendar.is_trading_day", lambda ref=None: True)
    monkeypatch.setattr("gugu.utils.calendar.is_trading_time", lambda now=None: True)
    _make_zero_signal_scan(tmp_path)
    today = datetime.now().date().isoformat()
    streak_path = tmp_path / "watchdog_zero_signal_streak.json"
    streak_path.write_text(json.dumps({"streak": 2, "streak_date": today}), encoding="utf-8")

    res = check_zero_signal_streak(
        scan_path=tmp_path / "signal_scan.json",
        streak_path=streak_path,
        history_path=tmp_path / "signals_history.jsonl",
    )
    assert res.ok is False, f"同日累计达阈值应升级: {res}"
    assert res.streak == 3
    state = json.loads(streak_path.read_text(encoding="utf-8"))
    assert state["streak_date"] == today
    assert state["streak"] == 3


def test_zero_signal_streak_escalated_once_per_day_kept(tmp_path: Path, monkeypatch):
    """v0.9.34 不破坏 v0.9.30/31：当日已升级 → 同日后续轮次静默且 last_escalated_date 保留。"""
    monkeypatch.setattr("gugu.utils.calendar.is_trading_day", lambda ref=None: True)
    monkeypatch.setattr("gugu.utils.calendar.is_trading_time", lambda now=None: True)
    _make_zero_signal_scan(tmp_path)
    today = datetime.now().date().isoformat()
    streak_path = tmp_path / "watchdog_zero_signal_streak.json"
    streak_path.write_text(
        json.dumps({"streak": 49, "streak_date": today, "last_escalated_date": today}),
        encoding="utf-8",
    )

    res = check_zero_signal_streak(
        scan_path=tmp_path / "signal_scan.json",
        streak_path=streak_path,
        history_path=tmp_path / "signals_history.jsonl",
    )
    assert res.ok is True, "当日已升级过 → 同因不重复升级"
    assert res.severity == "medium"
    state = json.loads(streak_path.read_text(encoding="utf-8"))
    assert state["last_escalated_date"] == today, "last_escalated_date 不得丢失"
    assert state["streak_date"] == today


def test_zero_signal_streak_empty_pool_exempt(tmp_path: Path, monkeypatch):
    """v0.9.37：生产配置 verified_strategies 白名单为空 → 0 策略 0 信号是配置预期，不告警。

    2026-08-26 settings.yaml 收紧 gate-v1 后 production 空池，watchdog 每日 09:40
    准时升级"连续 3 轮 0 信号"噪音（08-25~08-28 连续 4 个交易日实证）。策略池非空时
    （黑屏检测）行为不变——由 test_zero_signal_streak_accumulates_within_same_day 覆盖。
    """
    monkeypatch.setattr("gugu.utils.calendar.is_trading_day", lambda ref=None: True)
    monkeypatch.setattr("gugu.utils.calendar.is_trading_time", lambda now=None: True)
    _make_zero_signal_scan(tmp_path)
    today = datetime.now().date().isoformat()
    streak_path = tmp_path / "watchdog_zero_signal_streak.json"
    # 即使昨日遗留 streak 高达 49，空策略池也必须豁免（不得继承/累计）
    streak_path.write_text(json.dumps({"streak": 49, "streak_date": today}), encoding="utf-8")

    res = check_zero_signal_streak(
        scan_path=tmp_path / "signal_scan.json",
        streak_path=streak_path,
        history_path=tmp_path / "signals_history.jsonl",
        runtime_strategies=[],
    )
    assert res.ok is True, f"空策略池 0 信号应为配置预期，不告警: {res}"
    assert res.streak == 0, f"空策略池不得累计 streak，实际 {res.streak}"
    # 豁免路径不得污染 streak 持久化状态
    state = json.loads(streak_path.read_text(encoding="utf-8"))
    assert state["streak"] == 49, "豁免路径不得改写 streak 状态"


def test_zero_signal_streak_nonempty_pool_no_exemption(tmp_path: Path, monkeypatch):
    """v0.9.37 反向约束：runtime_strategies 非空（策略池有策略）→ 黑屏检测保留，照常升级。

    传 None 与传非空列表语义一致：None 为保守默认（不豁免），[] 才触发豁免。
    """
    monkeypatch.setattr("gugu.utils.calendar.is_trading_day", lambda ref=None: True)
    monkeypatch.setattr("gugu.utils.calendar.is_trading_time", lambda now=None: True)
    _make_zero_signal_scan(tmp_path)
    today = datetime.now().date().isoformat()
    streak_path = tmp_path / "watchdog_zero_signal_streak.json"
    streak_path.write_text(json.dumps({"streak": 2, "streak_date": today}), encoding="utf-8")

    # 策略池非空：第 3 轮仍升级（None 默认路径由 accumulate 测试覆盖，此处验证显式非空池）
    res = check_zero_signal_streak(
        scan_path=tmp_path / "signal_scan.json",
        streak_path=streak_path,
        history_path=tmp_path / "signals_history.jsonl",
        runtime_strategies=["box_breakout"],
    )
    assert res.ok is False, f"非空策略池 0 信号应照常累计告警: {res}"
    assert res.streak == 3
