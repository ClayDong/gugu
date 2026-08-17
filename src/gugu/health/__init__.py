"""健康看门狗模块（P0 静默失效防线）。"""

from gugu.health.auto_recover import AutoRecover, FixResult
from gugu.health.watchdog import (
    check_data_source_health,
    check_disk_health,
    check_scheduler_health,
    check_signal_history_freshness,
    check_zero_signal_streak,
    run_all_checks,
)

__all__ = [
    "AutoRecover",
    "FixResult",
    "check_scheduler_health",
    "check_signal_history_freshness",
    "check_disk_health",
    "check_data_source_health",
    "check_zero_signal_streak",
    "run_all_checks",
]
