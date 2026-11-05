"""auto_recover 自动修复闭环单测（2026-08-10 P0 自愈）。

覆盖：
1. run()：健康项跳过、告警项修复、冷却/每日上限
2. 修复器路由：心跳/重启风暴/信号历史/磁盘/数据源/0信号
3. 安全边界：GUGU_TEST_MODE 下所有修复动作短路（不真重启/不真删文件）
4. 失败升级：无法自动修复 → escalation=True
5. 审计日志：auto_recover_log.jsonl 记录根因/动作/结果
6. 脚本 --auto-fix 模式：修复失败才推送，全部自愈返回 0
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from gugu.health.auto_recover import AutoRecover, FixResult
from gugu.health.watchdog import (
    DiskHealthResult,
    SchedulerHealthResult,
    SignalHistoryResult,
    ZeroSignalResult,
    _alert_fingerprint,
)


# ========== fixture ==========


@pytest.fixture
def rec(tmp_path: Path):
    """测试模式 AutoRecover（所有动作短路，不碰生产）。"""
    return AutoRecover(data_dir=tmp_path, test_mode=True, fix_cooldown_sec=0)


@pytest.fixture
def heartbeat_stale_result(tmp_path: Path) -> SchedulerHealthResult:
    return SchedulerHealthResult(
        ok=False,
        severity="high",
        message="⚠️ 调度器心跳超时：25 分钟未更新（>15 分钟）——调度器可能挂死，请检查！",
        heartbeat_age_min=25.0,
    )


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ========== run() 基本流程 ==========


def test_run_healthy_skipped(rec: AutoRecover):
    """健康项不进入修复流程。"""
    ok = SchedulerHealthResult(ok=True, message="调度器健康")
    fixes = rec.run([ok])
    assert fixes == []


def test_run_alert_fixed(rec: AutoRecover, heartbeat_stale_result):
    """心跳告警在测试模式短路 → 视为自愈成功（不升级人工）。"""
    fixes = rec.run([heartbeat_stale_result])
    assert len(fixes) == 1
    f = fixes[0]
    assert f.ok is True
    assert f.escalation is False
    assert "测试模式" in f.root_cause
    assert "[测试模式]" in f.action


def test_run_audit_log_written(rec: AutoRecover, heartbeat_stale_result):
    """修复后审计日志落盘。"""
    rec.run([heartbeat_stale_result])
    log = rec.recover_log
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["fingerprint"] == "SchedulerHealthResult:heartbeat_stale"
    assert entry["ok"] is True
    assert "root_cause" in entry and "action" in entry


# ========== 冷却与每日上限 ==========


def test_cooldown_suppresses_repeat(tmp_path: Path, heartbeat_stale_result):
    """冷却窗口内同一指纹不重复修复。"""
    rec = AutoRecover(data_dir=tmp_path, test_mode=True, fix_cooldown_sec=3600)
    first = rec.run([heartbeat_stale_result])
    assert len(first) == 1
    # 冷却窗口内第二次 → 跳过
    second = rec.run([heartbeat_stale_result])
    assert second == []


def test_cooldown_expired_allows_fix(tmp_path: Path, heartbeat_stale_result):
    """冷却窗口过期后允许再次修复。"""
    rec = AutoRecover(data_dir=tmp_path, test_mode=True, fix_cooldown_sec=0)
    assert len(rec.run([heartbeat_stale_result])) == 1
    assert len(rec.run([heartbeat_stale_result])) == 1


def test_daily_limit_stops_fixes(tmp_path: Path, heartbeat_stale_result):
    """同一指纹每日修复次数上限。"""
    rec = AutoRecover(data_dir=tmp_path, test_mode=True, fix_cooldown_sec=0, max_fixes_per_day=2)
    assert len(rec.run([heartbeat_stale_result])) == 1
    assert len(rec.run([heartbeat_stale_result])) == 1
    # 第 3 次超上限 → 跳过
    assert rec.run([heartbeat_stale_result]) == []


def test_daily_limit_resets_next_day(tmp_path: Path, heartbeat_stale_result):
    """跨天重置每日计数。"""
    rec = AutoRecover(data_dir=tmp_path, test_mode=True, fix_cooldown_sec=0, max_fixes_per_day=1)
    rec.run([heartbeat_stale_result])
    # 伪造昨天的状态
    state = rec._state()
    for v in state.values():
        v["day"] = (datetime.now() - timedelta(days=1)).date().isoformat()
    rec._save_state(state)
    # 今天可再次修复
    assert len(rec.run([heartbeat_stale_result])) == 1


# ========== 修复器路由 ==========


def test_restart_storm_escalates(rec: AutoRecover, tmp_path: Path):
    """重启风暴：无自动修复动作（避免加剧）→ 升级人工。"""
    _write_json(
        tmp_path / "scheduler_boot.json",
        {"boot_time": datetime.now().isoformat(), "history": []},
    )
    res = SchedulerHealthResult(
        ok=False,
        severity="high",
        message="⚠️ 调度器重启风暴：今日已重启 4 次（>3 次）——可能存在网络错误风暴/崩溃循环，请检查日志！",
        restarts_today=4,
    )
    fixes = rec.run([res])
    assert len(fixes) == 1
    f = fixes[0]
    assert f.ok is False
    assert f.escalation is True
    assert "重启风暴" in f.root_cause or "今日启动" in f.root_cause


def test_disk_low_short_circuit_in_test_mode(rec: AutoRecover):
    """磁盘告警在测试模式短路，不真删文件。"""
    res = DiskHealthResult(
        ok=False,
        severity="high",
        message="⚠️ 磁盘空间不足：剩余 512MB（<1024MB）",
        free_bytes=512 * 1024 * 1024,
    )
    fixes = rec.run([res])
    assert len(fixes) == 1
    assert fixes[0].ok is True
    assert "测试模式" in fixes[0].action


def test_zero_signal_escalates_when_scan_running(tmp_path: Path):
    """扫描在跑但 0 信号（策略/置信度层）→ 无法自动修复，升级人工。"""
    _write_json(
        tmp_path / "signal_scan.json",
        {"last_scan_at": datetime.now().isoformat(), "signals_count": 0, "data_health": "ok"},
    )
    res = ZeroSignalResult(
        ok=False, severity="high", message="⚠️ 连续 3 轮扫描 0 信号（3 轮阈值）", streak=3
    )
    # 真实模式（非短路），mock 底层探测
    rec = AutoRecover(data_dir=tmp_path, test_mode=False, fix_cooldown_sec=0)
    rec._probe_data_sources = lambda: {"ok": False, "detail": "mock 探测失败"}
    fixes = rec.run([res])
    assert len(fixes) == 1
    f = fixes[0]
    assert f.escalation is True
    assert "无自动修复方案" in f.action


def test_probe_data_sources_closes_temporary_manager(monkeypatch, tmp_path: Path):
    """数据源探测结束后必须关闭临时 DataManager。"""
    import gugu.data.manager as manager_module

    class FakeDataManager:
        closed = False

        async def fetch_stock_history(self, symbol: str, days: int = 5):
            return None

        def close(self):
            self.closed = True

    fake = FakeDataManager()
    monkeypatch.setattr(manager_module, "DataManager", lambda: fake)
    rec = AutoRecover(data_dir=tmp_path, test_mode=True)

    result = rec._probe_data_sources()

    assert result == {"ok": False, "detail": "全部探测样本不可用"}
    assert fake.closed is True


def test_unknown_alert_type_escalates(rec: AutoRecover):
    """未知告警类型 → 升级人工（fail-safe）。"""
    from gugu.health.watchdog import HealthCheckResult

    class UnknownResult(HealthCheckResult):
        pass

    res = UnknownResult(ok=False, severity="high", message="全新未知检查项告警")
    fixes = rec._fix_for(res)
    assert fixes.escalation is True
    assert "未知告警类型" in fixes.root_cause


# ========== 测试模式安全边界 ==========


def test_test_mode_env_var_short_circuits(tmp_path: Path, heartbeat_stale_result, monkeypatch):
    """GUGU_TEST_MODE 环境变量自动启用短路（不依赖显式传参）。"""
    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    rec = AutoRecover(data_dir=tmp_path, fix_cooldown_sec=0)
    fixes = rec.run([heartbeat_stale_result])
    assert fixes[0].ok is True
    assert "[测试模式]" in fixes[0].action


def test_production_mode_not_short_circuited(tmp_path: Path, heartbeat_stale_result, monkeypatch):
    """非测试模式 → 真实执行（这里验证分支结构，不真重启）。"""
    monkeypatch.delenv("GUGU_TEST_MODE", raising=False)
    rec = AutoRecover(data_dir=tmp_path, test_mode=False, fix_cooldown_sec=0)
    # mock 底层动作，避免真重启
    rec._process_alive = lambda: True
    rec._kickstart = lambda force=False: True
    rec._verify_heartbeat = lambda: (True, {"heartbeat_age_min": 0.1})
    fixes = rec.run([heartbeat_stale_result])
    assert fixes[0].ok is True
    assert "强制重启" in fixes[0].action


# ========== _cleanup_safe_files 安全边界 ==========


def test_cleanup_removes_old_logs_keeps_fresh(tmp_path: Path, monkeypatch):
    """清理旧日志，保留新日志；测试模式不执行真实清理。"""
    monkeypatch.setattr("gugu.health.auto_recover.PROJECT_ROOT", tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "gugu_old.log"
    old.write_text("x" * 100)
    # 伪造旧 mtime
    import os as _os

    old_ts = (datetime.now() - timedelta(days=10)).timestamp()
    _os.utime(old, (old_ts, old_ts))
    fresh = log_dir / "gugu_fresh.log"
    fresh.write_text("y" * 100)

    rec = AutoRecover(data_dir=tmp_path, test_mode=False)
    # 直接调用底层清理（绕过测试模式短路）验证过滤逻辑
    rec._cleanup_safe_files()
    assert not old.exists(), "旧日志应被清理"
    assert fresh.exists(), "新日志应保留"


def test_cleanup_never_touches_business_data(tmp_path: Path, monkeypatch):
    """清理绝不删除业务数据文件。"""
    monkeypatch.setattr("gugu.health.auto_recover.PROJECT_ROOT", tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in (
        "signals_history.jsonl",
        "live_positions.json",
        "paper_broker_state.json",
        "heartbeat.json",
    ):
        (data_dir / name).write_text("{}", encoding="utf-8")

    rec = AutoRecover(data_dir=data_dir, test_mode=False)
    rec._cleanup_safe_files()
    for name in (
        "signals_history.jsonl",
        "live_positions.json",
        "paper_broker_state.json",
        "heartbeat.json",
    ):
        assert (data_dir / name).exists(), f"业务数据 {name} 不应被清理"


# ========== 脚本 --auto-fix 模式 ==========


def test_script_auto_fix_returns_zero_when_all_healed(rec: AutoRecover, tmp_path: Path):
    """全部自愈成功 → 退出码 0（不推送人工告警）。

    2026-11-04 整改：原用例只调 rec.run 后 ``assert True``，既没验证退出码
    也没验证修复结果，是零断言假绿。现在同时验证真实修复结果与脚本返回码。
    """
    from scripts.health_watchdog import _auto_fix_mode

    class Args:
        quiet = True
        notify_auto_fixed = False

    res = SchedulerHealthResult(
        ok=False, severity="high", message="⚠️ 调度器心跳超时：25 分钟未更新（>15 分钟）"
    )
    # 1) 真实自愈路径：告警必须产出 FixResult，且不需要人工升级
    fixes = rec.run([res])
    assert len(fixes) == 1
    assert fixes[0].ok is True
    assert fixes[0].escalation is False

    # 2) 脚本 --auto-fix 退出码语义：全部自愈 → 0，且不推送告警
    healed = FixResult(
        fingerprint="scheduler_heartbeat",
        ok=True,
        root_cause="心跳超时（test_mode 短路）",
        action="记录并跳过实际重启",
        verified=True,
        escalation=False,
    )
    with mock.patch("scripts.health_watchdog.AutoRecover") as mock_cls:
        mock_cls.return_value.run.return_value = [healed]
        with mock.patch("scripts.health_watchdog.push_alerts") as mock_push:
            exit_code = _auto_fix_mode([res], tmp_path, Args())
    assert exit_code == 0
    mock_push.assert_not_called()


def test_fingerprint_mapping(heartbeat_stale_result):
    """指纹映射稳定（与 watchdog 去重一致）。"""
    assert _alert_fingerprint(heartbeat_stale_result) == "SchedulerHealthResult:heartbeat_stale"
