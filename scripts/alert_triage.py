#!/usr/bin/env python3
"""gugu 告警分诊采集脚本（2026-08-10）。

给"告警评估员"agent 用的数据收集器：把当前系统异常清单整理成结构化文本，
agent 拿到这份摘要后做智能评估（该修就修、不该修就放过）。

输出协议（cron script 模式，stdout 注入 agent prompt）：
- 全部健康 → 首行 "HEALTHY"
- 有异常 → 分节列出：心跳/告警状态/自愈记录/当日ERROR/信号扫描/数据源/磁盘

用法:
    uv run python scripts/alert_triage.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gugu.config import PROJECT_ROOT
from gugu.utils.calendar import is_trading_day, is_trading_time

DATA = PROJECT_ROOT / "data"
LOGS = PROJECT_ROOT / "logs"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# loguru 行首时间戳（2026-09-11 09:36:18 | ERROR | ...）
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _stale_error_note(error_lines: list[str]) -> str:
    """标注「早于当前实例启动」的历史 ERROR（已由重启修复，非新告警）。

    背景：修复+重启后，当日日志里修复前的 ERROR 行仍会被反复分诊，
    导致每轮评估重复处置同一条历史告警（2026-09-11 连续 3 轮）。
    这里只做**展示层**标注，不改变 issues 判定，复现的 ERROR 仍会照常上报。
    """
    boot = _read_json(DATA / "scheduler_boot.json") or {}
    boot_ts = str(boot.get("boot_time", "")).replace("T", " ")[:19]
    if not boot_ts:
        return ""
    stamps = [m.group(1) for line in error_lines if (m := TS_RE.match(line.strip()))]
    if not stamps or max(stamps) >= boot_ts:
        return ""
    return (
        f"   ⚠️ 以上 ERROR 时间戳均早于当前实例启动（{boot_ts}），"
        f"疑似已由重启修复的**历史残留**；若重启后仍复现请按新告警处置"
    )


def _read_json(path: Path) -> dict | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_jsonl_tail(path: Path, n: int = 20) -> list[dict]:
    """读 jsonl 最后 n 条（从尾部扫描，容忍损坏行）。"""
    out: list[dict] = []
    try:
        if not path.exists():
            return out
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out[-n:]
    except OSError:
        return out


def _today_log_path() -> Path | None:
    if not LOGS.exists():
        return None
    p = LOGS / f"gugu_{datetime.now().strftime('%Y%m%d')}.log"
    return p if p.exists() else None


def main() -> int:
    sections: list[str] = []
    issues: list[str] = []
    now = datetime.now()

    # ========== 1. 心跳 ==========
    hb = _read_json(DATA / "heartbeat.json")
    if hb and hb.get("last_cycle_at"):
        try:
            age_min = (now - datetime.fromisoformat(hb["last_cycle_at"])).total_seconds() / 60.0
            status = "✅ 新鲜" if age_min <= 15 else f"⚠️ 超时 {age_min:.0f} 分钟"
            if age_min > 15:
                issues.append("心跳超时")
        except (ValueError, TypeError):
            status = "⚠️ 时间戳无效"
            issues.append("心跳时间戳无效")
        sections.append(f"1. 心跳: {status} (last_cycle_at={hb.get('last_cycle_at')})")
    else:
        sections.append("1. 心跳: ⚠️ 缺失/无记录")
        issues.append("心跳缺失")

    # ========== 2. watchdog 告警状态（最近 24h 触发过的） ==========
    alert_state = _read_json(DATA / "watchdog_alert_state.json") or {}
    active_alerts = []
    for fp, rec in alert_state.items():
        ts = rec.get("last_alert_at", "")
        try:
            if ts and (now - datetime.fromisoformat(ts)).total_seconds() < 86400:
                active_alerts.append(f"{fp} (最后告警 {ts[:19]})")
        except (ValueError, TypeError):
            continue
    if active_alerts:
        issues.append("watchdog 24h 内有告警")
        sections.append("2. watchdog 近24h告警: " + " | ".join(active_alerts))
    else:
        sections.append("2. watchdog 近24h告警: 无")

    # ========== 3. 自愈记录（最近 24h escalation / 反复修复） ==========
    rec_log = DATA / "auto_recover_log.jsonl"
    if rec_log.exists():
        entries = _read_jsonl_tail(rec_log, 50)
        recent = [e for e in entries if e.get("ts", "").startswith(now.strftime("%Y-%m-%d"))]
        if recent:
            escd = [e for e in recent if e.get("escalation")]
            fixed = [e for e in recent if e.get("ok") and not e.get("escalation")]
            lines = [f"今日自愈: 成功 {len(fixed)} 次, 升级人工 {len(escd)} 次"]
            for e in recent[-5:]:
                mark = "❌升级" if e.get("escalation") else ("✅自愈" if e.get("ok") else "⚠️")
                lines.append(
                    f"  {mark} [{e.get('fingerprint')}] {e.get('ts', '')[:19]} 根因: {str(e.get('root_cause'))[:120]}"
                )
            if escd:
                issues.append("自愈升级了问题")
            sections.append("3. 自愈记录: " + "\n".join(lines))
        else:
            sections.append("3. 自愈记录: 今日无")
    else:
        sections.append("3. 自愈记录: 无日志")

    # ========== 4. 当日 ERROR 统计 ==========
    log_path = _today_log_path()
    if log_path:
        try:
            lines = ANSI_RE.sub(
                "", log_path.read_text(encoding="utf-8", errors="ignore")
            ).splitlines()
            errors = [l for l in lines if "ERROR" in l or "CRITICAL" in l or "Traceback" in l]
            if errors:
                # 精确口径：条数只统计 loguru 日志行头的 ERROR/CRITICAL，
                # Traceback 行仅作上下文（旧口径把两者都计数 → 1 次崩溃报「2 条」）
                entries = [l for l in errors if "| ERROR" in l or "| CRITICAL" in l]
                tb_count = len(errors) - len(entries)
                if not entries:  # 非 loguru 格式（外部脚本写入）兜底，保持原口径
                    entries, tb_count = errors, 0
                label = f"当日日志 {len(entries)} 条 ERROR/异常"
                if tb_count:
                    label += f"（另有 {tb_count} 行 Traceback 上下文）"
                issues.append(label)
                samples = errors[-5:]
                sections.append(
                    f"4. {label} (最近5条):\n" + "\n".join(f"  {s[:160]}" for s in samples)
                )
                note = _stale_error_note(entries)
                if note:
                    sections.append(note)
            else:
                sections.append("4. 当日日志: 无 ERROR")
        except OSError:
            sections.append("4. 当日日志: 读取失败")
    else:
        sections.append("4. 当日日志: 无今日日志文件")

    # ========== 5. 信号扫描状态 ==========
    scan = _read_json(DATA / "signal_scan.json")
    if scan:
        scan_ts = scan.get("last_scan_at", "")
        try:
            scan_age_min = (
                (now - datetime.fromisoformat(scan_ts)).total_seconds() / 60.0 if scan_ts else 999
            )
        except (ValueError, TypeError):
            scan_age_min = 999
        dh = scan.get("data_health", "?")
        de = scan.get("data_errors") or []
        sig = scan.get("signals_count", 0)
        parts = [
            f"last_scan_at={scan_ts or '无'} (距今 {scan_age_min:.0f} 分钟)",
            f"signals_count={sig}",
            f"data_health={dh}",
        ]
        if de:
            parts.append(f"data_errors={de}")
            # flow_breaker_active = 资金流熔断冷却标记（v0.9.13），预期行为非故障，
            # 仅展示不构成 issue（与 watchdog 数据源判定口径一致）。
            real_errors = [d for d in de if d != "flow_breaker_active"]
            if real_errors:
                issues.append(f"信号扫描 data_errors: {real_errors}")
        if scan_age_min > 30:
            # 非交易时段停摆是预期行为（午休/收盘后/周末不扫描），仅交易时段判定
            # （与 watchdog v0.9.8 "0 信号检查必须只交易时段判定" 同源教训，2026-08-10 补）
            if is_trading_day(now.date()) and is_trading_time(now):
                # 开盘首轮竞态（2026-08-13 实证）：日循环首个扫描 09:30 启动、
                # 全量 ~09:32-09:33 完成，但 last_scan_at 只在整轮完成后写入 →
                # 09:30-09:40 之间分诊必然看到昨日时间戳（首轮扫描进行中，扫描链路
                # 日志/心跳均正常即非停摆）。心跳超时另有独立 issue，不会因此漏报。
                if now.hour == 9 and now.minute < 40:
                    parts.append("(开盘首轮扫描进行中，跳过停摆判定)")
                else:
                    # 午休间隔（11:30-13:00）设计不扫描，从 gap 中扣除午休时长后
                    # 再判定是否真正停摆（2026-08-26 修复：消除午休后瞬态误报）。
                    # 如扫描覆盖跨日（如周末后首日），也一并考虑。
                    effective_age = scan_age_min
                    if scan_ts:
                        try:
                            scan_dt = datetime.fromisoformat(scan_ts)
                            LUNCH_START = time(11, 30)
                            LUNCH_END = time(13, 0)
                            # 午休时长：扫描后到午休开始 + 午休结束到现在的完整 90 分钟
                            if scan_dt.time() <= LUNCH_START and now.time() >= LUNCH_END:
                                effective_age -= 90.0
                        except (ValueError, TypeError):
                            pass
                    if effective_age > 30:
                        issues.append("信号扫描停摆(>30分钟)")
                    else:
                        parts.append("(午休间隔，非停摆)")
        sections.append("5. 信号扫描: " + "; ".join(parts))
    else:
        sections.append("5. 信号扫描: 无记录")

    # ========== 6. 数据源失败率（当日） ==========
    ev = DATA / "source_health_events.jsonl"
    if ev.exists():
        today_prefix = now.date().isoformat()
        total = fails = 0
        # 轮转（>5000 行）会把当日事件归档到 source_health_events.jsonl.<date>，
        # 实时文件被清空 → 只读实时文件会漏统计（实测 2026-09-29 轮转后漏报 0/0）。
        # 因此同时聚合历史归档文件，按当日时间戳过滤。
        event_files = [ev]
        try:
            event_files += sorted(
                DATA.glob("source_health_events.jsonl.*"),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            pass
        try:
            for path in event_files:
                if not path.exists():
                    continue
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if e.get("timestamp", "").startswith(today_prefix):
                            total += 1
                            if e.get("event") == "failure":
                                fails += 1
            rate = fails / total if total else 0
            flag = "⚠️" if rate >= 0.5 else "✅"
            if rate >= 0.5:
                issues.append(f"数据源失败率 {rate:.0%}")
            sections.append(f"6. 数据源: {flag} 今日失败率 {rate:.0%} ({fails}/{total})")
        except OSError:
            sections.append("6. 数据源: 读取失败")
    else:
        sections.append("6. 数据源: 无事件记录")

    # ========== 7. 磁盘 ==========
    try:
        usage = shutil.disk_usage(str(DATA))
        free_gb = usage.free / 1024**3
        flag = "✅" if free_gb >= 1 else "⚠️"
        if free_gb < 1:
            issues.append("磁盘空间不足")
        sections.append(f"7. 磁盘: {flag} 剩余 {free_gb:.1f}GB")
    except OSError:
        sections.append("7. 磁盘: 检查失败")

    # ========== 8. 调度器进程/锁 ==========
    lock = _read_json(DATA / "scheduler.lock")
    lock_info = f"锁PID={lock.get('pid')}" if lock else "无锁文件"
    sections.append(f"8. 调度器: {lock_info}")

    # ========== 输出 ==========
    if issues:
        print("⚠️ gugu 告警分诊（有异常，请评估）")
        print("=" * 50)
        print("异常清单: " + "; ".join(issues))
        print()
        print("\n".join(sections))
        print("=" * 50)
        print("评估指引: 逐条判断根因 → 需要优化代码/配置就动手改（改完跑相关测试），")
        print("已知无害/设计行为/瞬时降级就放过并记录理由；无法判断或需用户决策→升级飞书。")
        return 1
    print("HEALTHY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
