"""健康看门狗：守护 gugu 的"沉默失效"防线（2026-08-01 P0）。

三大师评审共识：系统最大的敌人是静默失效——调度器在跑、日志在写，
但信号结果没落盘、无人察觉（7/16~7/31 真实事故：信号历史 11 天零写入）。

提供两类检查：
1. check_signal_history_freshness：信号历史写入看门狗
   - 调度器已启动（有 boot 记录）+ 信号历史超过阈值未写入 → 告警
   - "系统活着但失忆了"
2. check_scheduler_health：调度器健康心跳
   - 心跳超时（>15min 未更新）→ 调度器可能挂死 → 告警
   - 单日重启次数 >3 → 重启风暴 → 告警
"""

from __future__ import annotations

import contextlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gugu.utils.atomic import atomic_write_json

# 默认阈值
HEARTBEAT_STALE_MINUTES = 15  # 心跳超过 15 分钟未更新视为挂死
SIGNAL_HISTORY_STALE_DAYS = 2  # 交易日信号历史超过 2 天未写入视为失忆
MAX_RESTARTS_PER_DAY = 3  # 单日重启超过 3 次视为重启风暴
DEFAULT_BOOT_FILE = "scheduler_boot.json"
DEFAULT_HEARTBEAT_FILE = "heartbeat.json"
DEFAULT_HISTORY_FILE = "signals_history.jsonl"
DEFAULT_SCAN_FILE = "signal_scan.json"
# 告警去重（2026-08-01 P2）：同一问题在抑制窗口内不重复推送，防 cron 刷屏
DEFAULT_ALERT_STATE_FILE = "watchdog_alert_state.json"
DEFAULT_SUPPRESS_MINUTES = 120  # 同一问题默认 2 小时内只告警一次
# 重启风暴当日同因去重（v0.9.32）：记录当日首次升级日期，跨天自动重置
DEFAULT_RESTART_STORM_STATE_FILE = "watchdog_restart_storm_state.json"


@dataclass
class HealthCheckResult:
    """健康检查结果基类。"""

    ok: bool
    severity: str = "info"  # info / medium / high
    message: str = ""
    details: dict = field(default_factory=dict)

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


# ========== 通用工具 ==========


def _read_json(path: Path) -> dict | None:
    """安全读取 JSON 文件，失败返回 None。"""
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _parse_time(value: str | None) -> datetime | None:
    """解析 ISO 时间字符串，失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _age_minutes(dt: datetime | None) -> float | None:
    """距离现在的分钟数。"""
    if dt is None:
        return None
    return (datetime.now() - dt).total_seconds() / 60.0


def _load_boot_record(boot_path: Path) -> tuple[datetime | None, list[dict]]:
    """加载调度器启动记录。返回 (最新启动时间, 历史启动列表)。

    boot_latest 优先取顶层 boot_time；若缺失（旧格式只写 history），
    取 history 中最新一条的 boot_time。
    """
    data = _read_json(boot_path)
    if not data:
        return None, []
    history = data.get("history", [])
    latest = _parse_time(data.get("boot_time"))
    if latest is None and history:
        # 兜底：history 里最新一条
        times = [_parse_time(b.get("boot_time")) for b in history]
        times = [t for t in times if t is not None]
        if times:
            latest = max(times)
    return latest, history


# ========== 1. 信号历史写入看门狗 ==========


def check_signal_history_freshness(
    history_path: Path | None = None,
    boot_path: Path | None = None,
    scan_path: Path | None = None,
    data_dir: Path | None = None,
    stale_days: int = SIGNAL_HISTORY_STALE_DAYS,
) -> SignalHistoryResult:
    """检查信号历史是否新鲜。

    判定逻辑：
    - 调度器从未启动（无 boot 记录）→ 不告警（系统本来就没跑）
    - 信号历史为空文件 → 不告警（首次部署）
    - 最近一次信号写入距今 > stale_days 天 且 调度器已启动 → 告警
      （= 系统活着但信号没落盘，静默失效）
    """
    data_dir = data_dir or Path("data")
    history_path = history_path or data_dir / DEFAULT_HISTORY_FILE
    boot_path = boot_path or data_dir / DEFAULT_BOOT_FILE
    scan_path = scan_path or data_dir / DEFAULT_SCAN_FILE

    boot_latest, _ = _load_boot_record(boot_path)
    if boot_latest is None:
        # 调度器从未启动，系统本来就没跑，不算失忆
        return SignalHistoryResult(ok=True, message="调度器无启动记录，跳过信号历史检查")

    if not history_path.exists() or history_path.stat().st_size == 0:
        return SignalHistoryResult(ok=True, message="信号历史为空，跳过检查")

    # 读取最后一条记录的时间戳
    last_ts: datetime | None = None
    try:
        with history_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_time(rec.get("timestamp"))
                if ts is not None:
                    last_ts = ts
    except OSError:
        return SignalHistoryResult(
            ok=False,
            severity="high",
            message=f"信号历史文件读取失败: {history_path}",
        )

    if last_ts is None:
        return SignalHistoryResult(ok=True, message="信号历史无有效时间戳，跳过检查")

    age_days = (datetime.now() - last_ts).total_seconds() / 86400.0
    if age_days <= stale_days:
        return SignalHistoryResult(ok=True, message="信号历史正常写入")

    scan = _read_json(scan_path)
    scan_ts = _parse_time(scan.get("last_scan_at")) if scan else None
    scan_age_days = (
        (datetime.now() - scan_ts).total_seconds() / 86400.0 if scan_ts is not None else None
    )
    if scan_age_days is not None and scan_age_days <= stale_days:
        return SignalHistoryResult(
            ok=True,
            message=(
                f"信号扫描正常，最近 {scan.get('signals_count', 0)} 条有效信号；"
                "期间无需要落盘的信号"
            ),
            last_write=last_ts.isoformat(),
            boot_time=boot_latest.isoformat(),
            details={
                "age_days": round(age_days, 1),
                "scan_age_days": round(scan_age_days, 1),
                "signals_count": scan.get("signals_count", 0),
            },
        )

    return SignalHistoryResult(
        ok=False,
        severity="high",
        message=(
            f"⚠️ 信号扫描失忆告警：最近一次扫描距今 "
            f"{scan_age_days:.1f} 天（>{stale_days} 天），"
            "调度器心跳仍在但扫描链路未完成——系统活着但扫描已停！"
            if scan_age_days is not None
            else "⚠️ 信号扫描记录缺失：无法确认扫描链路是否仍在运行"
        ),
        last_write=last_ts.isoformat(),
        boot_time=boot_latest.isoformat(),
        details={
            "age_days": round(age_days, 1),
            "scan_age_days": round(scan_age_days, 1) if scan_age_days is not None else None,
            "stale_days": stale_days,
        },
    )


# ========== 2. 调度器健康心跳 ==========


def check_scheduler_health(
    heartbeat_path: Path | None = None,
    boot_path: Path | None = None,
    data_dir: Path | None = None,
    heartbeat_stale_minutes: int = HEARTBEAT_STALE_MINUTES,
    max_restarts_per_day: int = MAX_RESTARTS_PER_DAY,
) -> SchedulerHealthResult:
    """检查调度器健康状态：心跳新鲜度 + 重启风暴。

    判定逻辑：
    - 无 boot 记录 → 不告警（系统未运行，非故障）
    - 心跳文件不存在 或 心跳超时 → 告警（调度器可能挂死）
    - 单日重启次数 > max → 告警（重启风暴，网络错误风暴等）
    """
    data_dir = data_dir or Path("data")
    heartbeat_path = heartbeat_path or data_dir / DEFAULT_HEARTBEAT_FILE
    boot_path = boot_path or data_dir / DEFAULT_BOOT_FILE

    boot_latest, boot_history = _load_boot_record(boot_path)
    if boot_latest is None:
        return SchedulerHealthResult(ok=True, message="调度器无启动记录，跳过健康检查")

    # 1) 心跳检查
    hb = _read_json(heartbeat_path)
    hb_ts = _parse_time(hb.get("last_cycle_at")) if hb else None
    hb_age = _age_minutes(hb_ts)

    if hb_age is None:
        return SchedulerHealthResult(
            ok=False,
            severity="high",
            message="⚠️ 调度器心跳缺失：heartbeat.json 不存在或时间戳无效——调度器可能已挂死！",
            details={"boot_time": boot_latest.isoformat()},
        )
    if hb_age > heartbeat_stale_minutes:
        return SchedulerHealthResult(
            ok=False,
            severity="high",
            message=(
                f"⚠️ 调度器心跳超时：{hb_age:.0f} 分钟未更新"
                f"（>{heartbeat_stale_minutes} 分钟）——调度器可能挂死，请检查！"
            ),
            heartbeat_age_min=round(hb_age, 1),
            details={"boot_time": boot_latest.isoformat()},
        )

    # 2) 重启风暴检查
    today = datetime.now().date()
    restarts_today = 0
    for b in boot_history:
        bt = _parse_time(b.get("boot_time"))
        if bt is not None and bt.date() == today:
            restarts_today += 1
    # 防重复计数（2026-08-11 修复）：_record_boot() 把本次启动同时写入
    # 顶层 boot_time 与 history，原实现两处都计 → 当前进程被数两次 →
    # 3 次真实启动显示 4 次 → 误报重启风暴全天重发（14:40/15:45 实证）。
    # 修复：仅当顶层 boot_time 不在 history 中（旧格式只写顶层）才补计。
    boot_data = _read_json(boot_path) if boot_path.exists() else None
    top_level_boot_time = boot_data.get("boot_time") if boot_data else None
    top_level_ts = _parse_time(top_level_boot_time) if top_level_boot_time else None
    if top_level_ts is not None and top_level_ts.date() == today:
        already_in_history = any(
            _parse_time(b.get("boot_time")) == top_level_ts for b in boot_history
        )
        if not already_in_history:
            restarts_today += 1

    if restarts_today > max_restarts_per_day:
        # v0.9.32：重启风暴当日同因只升级一次（与 ZeroSignal v0.9.30/31 同款模式）。
        # 背景：2026-08-19 上午为部署 v0.9.29-31 修复连续启动 4 次（均为
        # launchd kickstart/部署重启，crash_clue=无崩溃线索，历史常态 3-4 次/日），
        # auto-fix 路径无任何去重 → 12:45 首次升级后 13:45 对同一根因重复升级
        # （"今日启动 4 次"），噪音全天重复。修复：当日首次升级持久化
        # last_escalated_date，后续轮次降为 ok=True/severity=medium 静默留痕，
        # 跨天自动重置（新一天首次达到阈值仍正常升级）。
        # 注意：真正的崩溃循环仍会被心跳陈旧检查（独立指纹）捕获，无盲区。
        storm_state_path = data_dir / DEFAULT_RESTART_STORM_STATE_FILE
        storm_state = _read_json(storm_state_path) or {}
        today = datetime.now().date().isoformat()
        storm_state_payload = {
            "last_escalated_date": today,
            "restarts_today": restarts_today,
            "updated_at": datetime.now().isoformat(),
        }
        if storm_state.get("last_escalated_date") == today:
            with contextlib.suppress(OSError):
                atomic_write_json(storm_state_path, storm_state_payload, ensure_ascii=False)
            return SchedulerHealthResult(
                ok=True,
                severity="medium",
                message=(
                    f"调度器今日已重启 {restarts_today} 次，但今日已升级过"
                    f"重启风暴告警（{today}），同因不重复升级"
                ),
                heartbeat_age_min=round(hb_age, 1),
                restarts_today=restarts_today,
                details={"max_restarts": max_restarts_per_day},
            )
        with contextlib.suppress(OSError):
            atomic_write_json(storm_state_path, storm_state_payload, ensure_ascii=False)
        return SchedulerHealthResult(
            ok=False,
            severity="high",
            message=(
                f"⚠️ 调度器重启风暴：今日已重启 {restarts_today} 次"
                f"（>{max_restarts_per_day} 次）——可能存在网络错误风暴/崩溃循环，请检查日志！"
            ),
            restarts_today=restarts_today,
            details={"max_restarts": max_restarts_per_day},
        )

    return SchedulerHealthResult(
        ok=True,
        message="调度器健康",
        heartbeat_age_min=round(hb_age, 1),
        restarts_today=restarts_today,
    )


# ========== 组合检查 ==========


def run_all_checks(
    data_dir: Path | None = None,
    heartbeat_stale_minutes: int = HEARTBEAT_STALE_MINUTES,
    signal_stale_days: int = SIGNAL_HISTORY_STALE_DAYS,
    max_restarts_per_day: int = MAX_RESTARTS_PER_DAY,
) -> list[HealthCheckResult]:
    """运行全部健康检查，返回结果列表（不告警的也在列）。

    v0.9.x P0 防护扩展（2026-08-05）：新增磁盘空间 / 数据源降级 /
    连续 0 信号三项检查，覆盖「静默失效」的三种典型形态：
    - 磁盘满 → 所有持久化静默失败
    - 数据源全降级 → 0 信号 / 0 选股
    - 信号链路断裂 → 连续 0 信号
    """
    data_dir = data_dir or Path("data")
    return [
        check_scheduler_health(
            data_dir=data_dir,
            heartbeat_stale_minutes=heartbeat_stale_minutes,
            max_restarts_per_day=max_restarts_per_day,
        ),
        check_signal_history_freshness(data_dir=data_dir, stale_days=signal_stale_days),
        check_disk_health(data_dir=data_dir),
        check_data_source_health(data_dir=data_dir),
        check_zero_signal_streak(data_dir=data_dir),
    ]


# ========== 2.5 磁盘空间检查（2026-08-05 P0 防护） ==========
# 事故：磁盘 100% 满（94MB 剩余）时 PaperBroker 成交但状态持久化写失败
# （Errno 28），heartbeat.json 同步写失败 886 次累积——全部只打 WARNING，
# 用户毫不知情。本检查将磁盘不足升级为主动告警。


@dataclass
class DiskHealthResult(HealthCheckResult):
    """磁盘空间检查结果。"""

    free_bytes: int = 0
    total_bytes: int = 0


# 磁盘剩余空间告警阈值（字节）：默认 1GB
DEFAULT_MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024


def check_disk_health(
    data_dir: Path | None = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> DiskHealthResult:
    """检查数据目录所在磁盘的剩余空间。

    磁盘满会导致：状态文件写失败、心跳写失败、日志丢失——
    系统看似运行实则所有持久化静默失败（2026-08-05 实证 886 次写失败）。
    """
    data_dir = data_dir or Path("data")
    try:
        import shutil

        usage = shutil.disk_usage(str(data_dir))
        free = usage.free
        if free < min_free_bytes:
            free_mb = free / 1024 / 1024
            return DiskHealthResult(
                ok=False,
                severity="high",
                message=(
                    f"⚠️ 磁盘空间不足：剩余 {free_mb:.0f}MB "
                    f"（<{min_free_bytes / 1024 / 1024:.0f}MB）——"
                    "状态文件/心跳/日志写入可能失败，请立即清理磁盘！"
                ),
                free_bytes=free,
                total_bytes=usage.total,
                details={
                    "free_bytes": free,
                    "total_bytes": usage.total,
                    "free_mb": round(free / 1024 / 1024, 1),
                },
            )
        return DiskHealthResult(
            ok=True,
            message=f"磁盘空间正常（剩余 {free / 1024 / 1024 / 1024:.1f}GB）",
            free_bytes=free,
            total_bytes=usage.total,
        )
    except Exception as e:
        return DiskHealthResult(
            ok=True,
            message=f"磁盘检查失败，跳过: {e}",
            details={"error": str(e)},
        )


# ========== 2.6 数据源降级检查（2026-08-05 P0 防护） ==========
# 事故：8/4 数据源故障（全市场快照为空）导致尾盘选股静默失败无记录。
# DataSourceHealthMonitor 已将 success/failure/degraded/recovered 事件
# 持久化到 source_health_events.jsonl（按日期滚动）——本检查读取当日事件，
# 失败率超阈值时升级告警。


@dataclass
class DataSourceHealthResult(HealthCheckResult):
    """数据源健康检查结果。"""

    failure_rate: float = 0.0
    total_events: int = 0
    failures: int = 0


DEFAULT_EVENTS_FILE = "source_health_events.jsonl"
# 当日失败率告警阈值：失败事件占全部事件比例超过 50% → 数据源严重降级
DEFAULT_FAILURE_RATE_THRESHOLD = 0.5


def check_data_source_health(
    data_dir: Path | None = None,
    events_file: str = DEFAULT_EVENTS_FILE,
    failure_rate_threshold: float = DEFAULT_FAILURE_RATE_THRESHOLD,
) -> DataSourceHealthResult:
    """检查数据源健康：读取当日 source_health_events.jsonl 统计失败率。

    数据源降级本身会被 DataManager 自动切换 fallback（设计内行为），
    但「全部数据源连续失败/全市场快照为空」会导致静默 0 信号/0 选股——
    必须升级为主动告警（2026-08-05 实证）。
    """
    data_dir = data_dir or Path("data")
    events_path = data_dir / events_file

    if not events_path.exists():
        return DataSourceHealthResult(ok=True, message="无数据源健康事件记录（首次部署）")

    today_prefix = datetime.now().date().isoformat()
    total = 0
    failures = 0
    degraded = 0
    try:
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = evt.get("timestamp", "")
                if not ts.startswith(today_prefix):
                    continue
                event = evt.get("event", "")
                if event in ("success", "failure", "fallback_used", "degraded"):
                    total += 1
                if event == "failure":
                    failures += 1
                if event == "degraded":
                    degraded += 1
    except OSError:
        return DataSourceHealthResult(
            ok=False,
            severity="medium",
            message=f"数据源健康事件文件读取失败: {events_path}",
        )

    if total == 0:
        return DataSourceHealthResult(ok=True, message="今日暂无数据源事件（可能非交易时段）")

    failure_rate = failures / total
    if failure_rate >= failure_rate_threshold:
        return DataSourceHealthResult(
            ok=False,
            severity="high",
            message=(
                f"⚠️ 数据源严重降级：今日失败率 {failure_rate:.0%} "
                f"（{failures}/{total}），降级事件 {degraded} 次——"
                "可能导致 0 信号/0 选股（8/4 事故同款），请检查数据源！"
            ),
            failure_rate=failure_rate,
            total_events=total,
            failures=failures,
            details={
                "failure_rate": round(failure_rate, 3),
                "total_events": total,
                "failures": failures,
                "degraded": degraded,
            },
        )
    if degraded >= 15:
        # 降级事件次数过多（但失败率正常）→ medium 级提示，不是 high。
        # 2026-08-10 修复：熔断振荡会单日产生多次 degraded 记录（每次熔断触发一条），
        # 次数多≠数据源故障——失败率才是主指标；此处仅提示异常频繁降级。
        return DataSourceHealthResult(
            ok=False,
            severity="medium",
            message=(
                f"⚠️ 数据源降级频繁：今日降级事件 {degraded} 次，"
                f"但失败率 {failure_rate:.1%} 正常——多为瞬时限流/熔断振荡，继续观察"
            ),
            failure_rate=failure_rate,
            total_events=total,
            failures=failures,
            details={
                "failure_rate": round(failure_rate, 3),
                "total_events": total,
                "failures": failures,
                "degraded": degraded,
            },
        )
    return DataSourceHealthResult(
        ok=True,
        message=f"数据源健康（今日失败率 {failure_rate:.1%}，降级 {degraded} 次）",
        failure_rate=failure_rate,
        total_events=total,
        failures=failures,
    )


# ========== 2.7 连续 0 信号检查（2026-08-05 P0 防护） ==========
# 事故：30 天 0 信号（指数代码误映射→择时降级→信号全拦）期间，
# signal_scan.json 每天 signals_count=0 但系统从未主动告警。
# 本检查：交易时段内连续 N 轮扫描 0 信号 → 告警。
# 注意：非交易时段/非交易日 0 信号是正常的（无扫描活动），不告警。


@dataclass
class ZeroSignalResult(HealthCheckResult):
    """连续 0 信号检查结果。"""

    streak: int = 0
    last_scan_at: str = ""


DEFAULT_SCAN_STREAK_FILE = "watchdog_zero_signal_streak.json"
# 连续 0 信号告警阈值：连续 3 轮扫描（每轮 5 分钟 → 15 分钟）无信号
DEFAULT_ZERO_SIGNAL_STREAK_THRESHOLD = 3
# 扫描新鲜度：signal_scan.json 的 last_scan_at 距今超过 30 分钟视为扫描未运行
DEFAULT_SCAN_FRESH_MINUTES = 30


def _has_today_signal(history_path: Path) -> bool:
    """信号历史今日是否有产出（链路可用性证明）。

    信号按时间顺序 append 写入，今日记录必在文件末尾——只读末尾 200 行。
    读取失败视为无产出（保守，不影响告警路径）。
    """
    try:
        if not history_path.exists() or history_path.stat().st_size == 0:
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        with history_path.open("r", encoding="utf-8") as f:
            tail = deque(f, maxlen=200)
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("timestamp", "")
            if isinstance(ts, str) and ts[:10] == today:
                return True
    except OSError:
        return False
    return False


def check_zero_signal_streak(
    data_dir: Path | None = None,
    scan_path: Path | None = None,
    streak_path: Path | None = None,
    history_path: Path | None = None,
    threshold: int = DEFAULT_ZERO_SIGNAL_STREAK_THRESHOLD,
    fresh_minutes: int = DEFAULT_SCAN_FRESH_MINUTES,
) -> ZeroSignalResult:
    """检查交易时段内是否连续多轮扫描 0 信号。

    判定逻辑：
    - signal_scan.json 不存在 → 跳过（无扫描活动）
    - last_scan_at 距今 > fresh_minutes → 扫描未运行（由心跳检查覆盖），不累计
    - signals_count > 0 → 正常，streak 清零
    - signals_count == 0 且扫描新鲜 → streak + 1；streak >= threshold → 告警
    - **链路健康豁免（v0.9.14）**：扫描在跑（scanned_count>0）+ 数据健康
      （data_health=ok 且无 data_errors）+ 当日已有信号产出 → 0 信号是市场状态
      （震荡市策略未触发/融合无方向），非链路故障，不累计不告警。
      真故障路径（数据源挂/扫描停摆/全天 0 信号）完全保留。
    """
    data_dir = data_dir or Path("data")
    scan_path = scan_path or data_dir / DEFAULT_SCAN_FILE
    streak_path = streak_path or data_dir / DEFAULT_SCAN_STREAK_FILE
    history_path = history_path or data_dir / DEFAULT_HISTORY_FILE

    scan = _read_json(scan_path)
    if not scan or not scan.get("last_scan_at"):
        return ZeroSignalResult(ok=True, message="无扫描记录，跳过 0 信号检查")

    # 关键：仅交易时段判定（2026-08-05 修复）。
    # 非交易时段/非交易日扫描 0 信号是正常的（收盘后/周末无市场活动），
    # 若此时累计 streak 会导致收盘后的正常扫描误报"连续 0 信号"。
    # 真正需要告警的场景是盘中（9:30-15:00）连续多轮无信号。
    try:
        from gugu.utils.calendar import is_trading_day, is_trading_time

        if not is_trading_day() or not is_trading_time():
            return ZeroSignalResult(
                ok=True,
                message="非交易时段，跳过 0 信号检查",
                last_scan_at=scan.get("last_scan_at", ""),
            )
    except Exception:
        pass  # 日历判断失败时降级为仅靠扫描新鲜度（保守）

    last_ts = _parse_time(scan.get("last_scan_at"))
    age_min = _age_minutes(last_ts)
    if age_min is None or age_min > fresh_minutes:
        # 扫描不新鲜 → 可能是调度器挂了（心跳检查覆盖）或非交易时段
        return ZeroSignalResult(
            ok=True,
            message=f"扫描已停止（最近扫描 {age_min:.0f} 分钟前），由心跳检查覆盖",
            last_scan_at=scan.get("last_scan_at", ""),
        )

    # 读取当前 streak 状态
    state = _read_json(streak_path) or {}
    streak = int(state.get("streak", 0) or 0)
    # v0.9.30：当日是否发生 0 信号升级（首次升级置 True，持久化 last_escalated_date）
    escalated_today = False

    signals_count = int(scan.get("signals_count", 0) or 0)
    if signals_count > 0:
        streak = 0  # 有信号 → 清零
        result = ZeroSignalResult(
            ok=True,
            message=f"信号正常产出（最近一轮 {signals_count} 条）",
            streak=0,
            last_scan_at=scan.get("last_scan_at", ""),
        )
    else:
        # 链路健康豁免（v0.9.14，2026-08-11 实证）：
        # 扫描在跑（scanned_count>0）+ 数据健康（data_health=ok 且无 data_errors）
        # + 当日已有信号产出 → 0 信号是市场状态（震荡市策略未触发/融合无方向），
        # 非链路故障。不累计 streak、不告警——避免"策略未触发"被误判为
        # 8/5 四根因链事故同款而每小时升级人工（当日 7 次误升级噪音）。
        # 真故障路径（数据源挂 → data_health!=ok；扫描停摆 → last_scan_at 陈旧；
        # 全天 0 信号 → 当日无产出）完全保留。
        chain_healthy = (
            scan.get("data_health") == "ok"
            and int(scan.get("scanned_count", 0) or 0) > 0
            and not scan.get("data_errors")
        )
        if chain_healthy and _has_today_signal(history_path):
            streak = 0
            result = ZeroSignalResult(
                ok=True,
                message=(
                    "扫描链路健康且当日已有信号产出，本轮 0 信号为市场状态"
                    "（策略未触发/震荡市），不累计不告警"
                ),
                streak=0,
                last_scan_at=scan.get("last_scan_at", ""),
            )
        else:
            streak += 1
            if streak >= threshold:
                today = datetime.now().date().isoformat()
                if state.get("last_escalated_date") == today:
                    # v0.9.30：当日已升级过一次 0 信号（根因已上报），同因不重复升级。
                    # 背景：2026-08-19 黑屏根因修复后 streak 未重置（持续累计），
                    # 冷却 60 分钟到期后 10:55 再次升级同一根因 → 用户每小时收到
                    # 重复升级。保留首次升级的信息价值，同日后续轮次静默（跨天重置）。
                    result = ZeroSignalResult(
                        ok=True,
                        severity="medium",
                        message=(
                            f"连续 {streak} 轮 0 信号，但今日已升级过 0 信号告警"
                            f"（{today}），同因不重复升级"
                        ),
                        streak=streak,
                        last_scan_at=scan.get("last_scan_at", ""),
                    )
                else:
                    escalated_today = True
                    result = ZeroSignalResult(
                        ok=False,
                        severity="high",
                        message=(
                            f"⚠️ 连续 {streak} 轮扫描 0 信号（{threshold} 轮阈值）——"
                            "择时降级/置信度过滤/数据源故障都可能导致此现象"
                            "（8/5 四根因链事故同款），请排查！"
                        ),
                        streak=streak,
                        last_scan_at=scan.get("last_scan_at", ""),
                    )
            else:
                result = ZeroSignalResult(
                    ok=True,
                    message=f"连续 {streak} 轮 0 信号（阈值 {threshold}，未达告警线）",
                    streak=streak,
                    last_scan_at=scan.get("last_scan_at", ""),
                )

    # 持久化 streak（写失败不阻塞告警）
    with contextlib.suppress(OSError):
        payload: dict = {"streak": streak, "updated_at": datetime.now().isoformat()}
        if escalated_today:
            payload["last_escalated_date"] = datetime.now().date().isoformat()
        elif state.get("last_escalated_date"):
            # v0.9.31：保留已有 last_escalated_date，防覆盖丢失。
            # 背景：v0.9.30 仅在 escalated_today=True 时写入该字段，"同因不重复升级"
            # 轮次（escalated_today=False）覆盖写不带它 → 字段丢失 → 下一轮再次升级
            # （2026-08-19 实证：09:55 首次升级后 10:55 二次升级同一根因）。
            payload["last_escalated_date"] = state["last_escalated_date"]
        streak_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    return result


# ========== 3. 告警去重（防 cron 刷屏） ==========
def _alert_fingerprint(result: HealthCheckResult) -> str:
    """生成告警指纹：同一检查项的同一类问题视为同一指纹。

    消息内容可能随时间变化（如\"距今 5.2 天\"→\"6.1 天\"），
    因此指纹只用检查项类别，不用完整消息。
    """
    cls_name = type(result).__name__
    if isinstance(result, SignalHistoryResult):
        return f"{cls_name}:signal_history_stale"
    if isinstance(result, SchedulerHealthResult):
        # 心跳缺失/超时/重启风暴是三类不同问题，分别去重
        if "心跳缺失" in result.message:
            return f"{cls_name}:heartbeat_missing"
        if "心跳超时" in result.message:
            return f"{cls_name}:heartbeat_stale"
        if "重启风暴" in result.message:
            return f"{cls_name}:restart_storm"
        return f"{cls_name}:scheduler_other"
    if isinstance(result, DiskHealthResult):
        return f"{cls_name}:disk_low"
    if isinstance(result, DataSourceHealthResult):
        return f"{cls_name}:data_source_degraded"
    if isinstance(result, ZeroSignalResult):
        return f"{cls_name}:zero_signal_streak"
    return f"{cls_name}:{result.message[:40]}"


def dedupe_alerts(
    results: list[HealthCheckResult],
    state_path: Path | None = None,
    suppress_minutes: int = DEFAULT_SUPPRESS_MINUTES,
    data_dir: Path | None = None,
) -> list[HealthCheckResult]:
    """对检查结果做告警去重，返回需要推送的告警列表。

    规则：
    - 健康项（ok）清除其去重状态（恢复后下次再故障立即告警，不被抑制窗口卡住）
    - 告警项：同指纹在 suppress_minutes 内已推送过 → 抑制（不返回）
    - 首次告警 / 指纹变化 / 超过抑制窗口 → 返回并更新状态

    状态文件结构：
        {fingerprint: {"last_alert_at": iso_timestamp}}
    """
    data_dir = data_dir or Path("data")
    state_path = state_path or data_dir / DEFAULT_ALERT_STATE_FILE

    # 加载现有状态
    state: dict[str, dict] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    now = datetime.now()
    alerts = [r for r in results if not r.ok]

    # 1) 健康项清除状态
    healthy_fingerprints = {_alert_fingerprint(r) for r in results if r.ok}
    for fp in healthy_fingerprints:
        state.pop(fp, None)

    # 2) 过滤告警
    to_push: list[HealthCheckResult] = []
    changed = False
    for alert in alerts:
        fp = _alert_fingerprint(alert)
        last = state.get(fp)
        last_ts = _parse_time(last.get("last_alert_at")) if last else None
        suppressed = False
        if last_ts is not None:
            age_min = (now - last_ts).total_seconds() / 60.0
            if age_min < suppress_minutes:
                suppressed = True
        if not suppressed:
            to_push.append(alert)
            state[fp] = {"last_alert_at": now.isoformat()}
            changed = True

    # 3) 写回状态（仅在变化时，减少 IO）
    if changed or healthy_fingerprints:
        with contextlib.suppress(OSError):
            # 状态写失败不阻塞告警推送
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return to_push


if __name__ == "__main__":
    # 手动运行：python -m gugu.health.watchdog
    for r in run_all_checks():
        status = "✅" if r.ok else f"❌ [{r.severity}]"
        print(f"{status} {r.message}")
