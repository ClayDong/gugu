"""健康检查结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HealthCheckResult:
    """健康检查结果基类。"""

    ok: bool
    severity: str = "info"
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class SignalHistoryResult(HealthCheckResult):
    """信号历史新鲜度检查结果。"""

    last_write: str = ""
    boot_time: str = ""


@dataclass
class SchedulerHealthResult(HealthCheckResult):
    """调度器健康检查结果。"""

    heartbeat_age_min: float = 0.0
    restarts_today: int = 0


@dataclass
class DiskHealthResult(HealthCheckResult):
    """磁盘空间检查结果。"""

    free_bytes: int = 0
    total_bytes: int = 0


@dataclass
class DataSourceHealthResult(HealthCheckResult):
    """数据源健康检查结果。"""

    failure_rate: float = 0.0
    total_events: int = 0
    failures: int = 0


@dataclass
class ZeroSignalResult(HealthCheckResult):
    """连续 0 信号检查结果。"""

    streak: int = 0
    last_scan_at: str = ""
