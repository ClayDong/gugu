"""source_health 事件轮转后统计不丢失 回归测试（v0.9.52）。

背景（2026-09-29 实证）：事件文件 >5000 行触发轮转归档到
``source_health_events.jsonl.<date>`` 并清空实时文件，
旧实现的 get_daily_summary / alert_triage 只读实时文件 → 轮转当天统计被误报为 0/0
（实际 9 failures / 1161 requests = 0.78%，被分诊标成 0%）。
修复：聚合实时文件 + 全部历史归档文件，按目标日期时间戳过滤。

安全：所有用例使用 tmp_path 隔离的独立实例，不触碰生产 data/ 目录与全局单例。
"""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta

import pytest

from gugu.data.source_health import DataSourceHealthMonitor


def _fresh_monitor(tmp_path) -> DataSourceHealthMonitor:
    """构造指向 tmp_path 的隔离监控实例（绕过全局单例）。"""
    m = object.__new__(DataSourceHealthMonitor)
    m._buffer = []
    m._buffer_lock = threading.RLock()
    m._events_file = tmp_path / "source_health_events.jsonl"
    m._summary_file = tmp_path / "source_health_daily.json"
    m._initialized = True
    return m


def _write_events(monitor: DataSourceHealthMonitor, day: str, n: int) -> None:
    lines = []
    for i in range(n):
        evt = {
            "timestamp": f"{day}T09:{i % 60:02d}:{(i // 60) % 60:02d}",
            "source": "sina" if i % 5 else "all",
            "event": "failure" if i % 100 == 0 else "success",
        }
        lines.append(json.dumps(evt, ensure_ascii=False))
    with monitor._events_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class TestRotationSummary:
    """轮转后当日摘要仍然完整（核心回归）。"""

    def test_daily_summary_preserved_after_rotation(self, tmp_path):
        """>5000 行触发轮转后，get_daily_summary 必须仍统计出当日全部事件。"""
        monitor = _fresh_monitor(tmp_path)
        today = date.today().isoformat()
        # 5001 行 → 超过 _MAX_LINES=5000 触发轮转
        n = 5001
        _write_events(monitor, today, n)

        summary = monitor.get_daily_summary(date.today())

        # 轮转发生：实时文件被清空，归档文件生成
        assert monitor._events_file.exists()
        assert monitor._events_file.read_text(encoding="utf-8") == ""
        archive = tmp_path / f"source_health_events.jsonl.{today}"
        assert archive.exists()

        # 统计不丢失：总请求数 = n（success + failure），失败数 = i%100==0 共 51 个（i=0..5000）
        assert summary["total_requests"] == n
        assert summary["failure_rate"] == pytest.approx(51 / n)
        assert summary["sources"]["all"]["failure"] == 51
        assert summary["sources"]["sina"]["success"] > 0

    def test_summary_merges_live_events_after_rotation(self, tmp_path):
        """轮转后追加到实时文件的新事件也要计入当日摘要（归档 + 实时聚合）。"""
        monitor = _fresh_monitor(tmp_path)
        today = date.today().isoformat()
        _write_events(monitor, today, 5001)
        # 轮转后追加 10 条（实时文件）
        _write_events(monitor, today, 10)

        summary = monitor.get_daily_summary(date.today())

        assert summary["total_requests"] == 5011

    def test_history_day_in_archive_still_queryable(self, tmp_path):
        """历史日期事件被卷入新归档后，get_daily_summary(历史日) 仍可查。"""
        monitor = _fresh_monitor(tmp_path)
        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        # 昨天 100 条 + 今天 5001 条（同一文件 → 一起被归档到今日归档）
        _write_events(monitor, yesterday, 100)
        _write_events(monitor, today.isoformat(), 5001)

        hist = monitor.get_daily_summary(today - timedelta(days=1))
        assert hist["total_requests"] == 100

    def test_old_archive_duplicate_free(self, tmp_path):
        """归档事件不会被二次计数（实时文件轮转后被清空，无重叠）。"""
        monitor = _fresh_monitor(tmp_path)
        today = date.today().isoformat()
        _write_events(monitor, today, 5001)

        s1 = monitor.get_daily_summary(date.today())
        s2 = monitor.get_daily_summary(date.today())

        # 两次查询结果一致（轮转后实时文件为空，不会叠加计数）
        assert s1["total_requests"] == s2["total_requests"] == 5001

    def test_no_rotation_no_regression(self, tmp_path):
        """未达轮转阈值时行为与旧版一致。"""
        monitor = _fresh_monitor(tmp_path)
        today = date.today().isoformat()
        _write_events(monitor, today, 100)

        summary = monitor.get_daily_summary(date.today())

        assert summary["total_requests"] == 100
        assert not list(tmp_path.glob("source_health_events.jsonl.*")), "不应产生归档"

    def test_empty_day_returns_empty_summary(self, tmp_path):
        """目标日期无事件时返回空摘要（不抛异常）。"""
        monitor = _fresh_monitor(tmp_path)
        summary = monitor.get_daily_summary(date.today() - timedelta(days=30))
        assert summary["total_requests"] == 0
        assert summary["sources"] == {}
