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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 默认阈值
HEARTBEAT_STALE_MINUTES = 15  # 心跳超过 15 分钟未更新视为挂死
SIGNAL_HISTORY_STALE_DAYS = 2  # 交易日信号历史超过 2 天未写入视为失忆
MAX_RESTARTS_PER_DAY = 3  # 单日重启超过 3 次视为重启风暴
DEFAULT_BOOT_FILE = "scheduler_boot.json"
DEFAULT_HEARTBEAT_FILE = "heartbeat.json"
DEFAULT_HISTORY_FILE = "signals_history.jsonl"
# 告警去重（2026-08-01 P2）：同一问题在抑制窗口内不重复推送，防 cron 刷屏
DEFAULT_ALERT_STATE_FILE = "watchdog_alert_state.json"
DEFAULT_SUPPRESS_MINUTES = 120  # 同一问题默认 2 小时内只告警一次


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
    if age_days > stale_days:
        return SignalHistoryResult(
            ok=False,
            severity="high",
            message=(
                f"⚠️ 信号历史失忆告警：最近一次信号写入距今 {age_days:.1f} 天"
                f"（>{stale_days} 天），调度器在运行但信号未落盘——系统活着但失忆了！"
            ),
            last_write=last_ts.isoformat(),
            boot_time=boot_latest.isoformat(),
            details={"age_days": round(age_days, 1), "stale_days": stale_days},
        )
    return SignalHistoryResult(ok=True, message="信号历史正常写入")


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
    # boot_latest 若来自 history 兜底（顶层无 boot_time），已在上面计入，不再重复
    boot_data = _read_json(boot_path) if boot_path.exists() else None
    top_level_boot_time = boot_data.get("boot_time") if boot_data else None
    if boot_latest.date() == today and top_level_boot_time:
        restarts_today += 1

    if restarts_today > max_restarts_per_day:
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
    """运行全部健康检查，返回结果列表（不告警的也在列）。"""
    data_dir = data_dir or Path("data")
    return [
        check_scheduler_health(
            data_dir=data_dir,
            heartbeat_stale_minutes=heartbeat_stale_minutes,
            max_restarts_per_day=max_restarts_per_day,
        ),
        check_signal_history_freshness(data_dir=data_dir, stale_days=signal_stale_days),
    ]


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
