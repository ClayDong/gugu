"""数据源健康监控：记录各数据源成功/失败/降级/恢复事件，支撑稳定性可观测性。

核心设计：
- 事件驱动：DataManager 在请求成功、失败、降级、恢复时调用本模块
- 持久化到 JSONL：data/source_health_events.jsonl（按日期滚动）
- 每日摘要：get_daily_summary() 汇总当天各 source 的健康指标
- 静默优先：降级/恢复事件只记录，不主动发用户通知，仅在收盘日报中附带摘要
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from gugu.config import PROJECT_ROOT
from gugu.utils.atomic import atomic_write_json
from gugu.utils.log import get_logger

logger = get_logger()

_EVENTS_FILE = Path(PROJECT_ROOT) / "data" / "source_health_events.jsonl"
_SUMMARY_FILE = Path(PROJECT_ROOT) / "data" / "source_health_daily.json"
_MAX_LINES = 5000


@dataclass
class SourceHealthEvent:
    """单个数据源健康事件。"""

    timestamp: str
    source: str
    event: str  # success / failure / degraded / recovered / fallback_used
    method: str = ""
    detail: str = ""


class DataSourceHealthMonitor:
    """数据源健康监控器。

    线程安全：内部使用 RLock 保护事件缓冲区和文件写入。
    """

    _instance: DataSourceHealthMonitor | None = None
    _lock = threading.RLock()

    def __new__(cls) -> DataSourceHealthMonitor:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._buffer: list[SourceHealthEvent] = []
            self._buffer_lock = threading.RLock()
            self._events_file = _EVENTS_FILE
            self._summary_file = _SUMMARY_FILE
            self._events_file.parent.mkdir(parents=True, exist_ok=True)
            self._initialized = True

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _record(self, source: str, event: str, method: str = "", detail: str = "") -> None:
        evt = SourceHealthEvent(
            timestamp=self._now(),
            source=source,
            event=event,
            method=method,
            detail=detail,
        )
        with self._buffer_lock:
            self._buffer.append(evt)
            # 每累计 20 条或 60 秒写入一次，降低 IO
            should_flush = len(self._buffer) >= 20
        if should_flush:
            self.flush()

    def record_success(self, source: str, method: str = "") -> None:
        """记录数据源成功事件。"""
        self._record(source, "success", method=method)

    def record_failure(self, source: str, method: str = "", detail: str = "") -> None:
        """记录数据源失败事件。"""
        self._record(source, "failure", method=method, detail=detail)
        logger.warning("[source-health] {} {} 失败: {}", source, method, detail)

    def record_degraded(self, source: str, fallback: str, method: str = "") -> None:
        """记录主源降级事件。"""
        self._record(source, "degraded", method=method, detail=f"fallback->{fallback}")
        logger.warning("[source-health] {} 降级到 {} ({})", source, fallback, method)

    def record_recovered(self, source: str, method: str = "") -> None:
        """记录主源恢复事件。"""
        self._record(source, "recovered", method=method)
        logger.info("[source-health] {} 已恢复 ({})", source, method)

    def record_fallback_used(self, source: str, method: str = "") -> None:
        """记录降级源被成功使用事件。"""
        self._record(source, "fallback_used", method=method)

    def flush(self) -> None:
        """将缓冲区事件持久化到 JSONL。"""
        with self._buffer_lock:
            if not self._buffer:
                return
            events = self._buffer[:]
            self._buffer.clear()

        try:
            self._events_file.parent.mkdir(parents=True, exist_ok=True)
            with self._events_file.open("a", encoding="utf-8") as f:
                for evt in events:
                    f.write(json.dumps(asdict(evt), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("数据源健康事件写入失败: {}", e)

    def _rotate_if_needed(self) -> None:
        """超过最大行数时按日期归档。"""
        try:
            if not self._events_file.exists():
                return
            with self._events_file.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= _MAX_LINES:
                return
            backup = self._events_file.with_suffix(f".jsonl.{date.today().isoformat()}")
            backup.write_text("".join(lines), encoding="utf-8")
            self._events_file.write_text("", encoding="utf-8")
            logger.info("[source-health] 事件日志轮转: {} 行 -> {}", len(lines), backup)
        except Exception as e:
            logger.warning("数据源健康日志轮转失败: {}", e)

    def get_daily_summary(self, day: date | None = None) -> dict[str, Any]:
        """获取指定日期的数据源健康摘要。

        Returns:
            {
                "date": "2026-07-31",
                "sources": {
                    "sina": {"success": 120, "failure": 3, "fallback_used": 0, "degraded": 1, "recovered": 1},
                    "akshare": {"success": 45, "failure": 2, "fallback_used": 15, ...},
                    ...
                },
                "total_requests": 188,
                "failure_rate": 0.027,
                "degraded_minutes": 12.5,
                "primary_source": "sina",
            }
        """
        self.flush()
        self._rotate_if_needed()

        target = day or date.today()
        target_str = target.isoformat()
        stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        degraded_at: dict[str, float | None] = {}
        recovered_at: dict[str, float | None] = {}
        total = 0
        failures = 0

        try:
            if not self._events_file.exists():
                return self._empty_summary(target)
            with self._events_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = evt.get("timestamp", "")
                    if not ts.startswith(target_str):
                        continue
                    src = evt.get("source", "unknown")
                    event = evt.get("event", "")
                    stats[src][event] += 1
                    if event in ("success", "failure", "fallback_used"):
                        total += 1
                    if event == "failure":
                        failures += 1
                    if event == "degraded":
                        degraded_at[src] = datetime.fromisoformat(ts).timestamp()
                        recovered_at[src] = None
                    if event == "recovered" and degraded_at.get(src) is not None:
                        recovered_at[src] = datetime.fromisoformat(ts).timestamp()
        except Exception as e:
            logger.warning("读取数据源健康事件失败: {}", e)
            return self._empty_summary(target)

        # 计算降级时长
        degraded_seconds = 0.0
        for src, start in degraded_at.items():
            end = recovered_at.get(src)
            if start is None:
                continue
            if end is None:
                # 未恢复，按当天结束时或当前时间算
                end = time.time()
            degraded_seconds += max(0.0, end - start)

        return {
            "date": target_str,
            "sources": {k: dict(v) for k, v in stats.items()},
            "total_requests": total,
            "failure_rate": failures / total if total > 0 else 0.0,
            "degraded_minutes": round(degraded_seconds / 60.0, 1),
            "primary_source": "sina",  # 与当前配置保持一致，后续可从 DataManager 注入
        }

    def _empty_summary(self, day: date) -> dict[str, Any]:
        return {
            "date": day.isoformat(),
            "sources": {},
            "total_requests": 0,
            "failure_rate": 0.0,
            "degraded_minutes": 0.0,
            "primary_source": "sina",
        }

    def save_daily_summary(self, day: date | None = None) -> dict[str, Any]:
        """生成并持久化每日摘要。"""
        summary = self.get_daily_summary(day)
        try:
            self._summary_file.parent.mkdir(parents=True, exist_ok=True)
            existing: dict[str, Any] = {}
            if self._summary_file.exists():
                try:
                    existing = json.loads(self._summary_file.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            existing[summary["date"]] = summary
            atomic_write_json(self._summary_file, existing)
        except Exception as e:
            logger.warning("保存数据源健康摘要失败: {}", e)
        return summary


def get_source_health_monitor() -> DataSourceHealthMonitor:
    """获取全局单例。"""
    return DataSourceHealthMonitor()
