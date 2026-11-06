"""gugu 自动修复闭环（Auto-Recovery，2026-08-10 P0）。

watchdog 检测到告警后，不再直接推送给用户，而是先走：
    根因分析 → 安全修复 → 验证 → （失败才升级人工）

安全边界（对抗性审查）：
1. 只做可逆、幂等、低风险动作：重启 scheduler（launchd 管理）、清理垃圾文件
   （旧日志/__pycache__/.bak）、重新探测数据源。
   绝不修改策略配置、绝不删除业务数据（signals_history/live_positions/paper state）。
2. 修复冷却：同一指纹在冷却窗口内不重复修复（防修复风暴）。
3. 修复上限：每类修复每日最多 MAX_FIXES_PER_DAY 次，防止修复本身变成新故障。
4. 失败升级：无法自动修复或修复后验证失败 → 返回 escalation=True，
   由调用方推送带根因分析的完整告警。
5. 测试模式：GUGU_TEST_MODE 下所有修复动作短路（不真重启/不真删文件），
   符合 gugu 测试隔离铁律（不得产生生产副作用）。
6. 审计日志：data/auto_recover_log.jsonl 记录每次修复的根因/动作/结果/验证，
   供事后复盘与"同类告警是否反复"统计。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gugu.config import PROJECT_ROOT
from gugu.health.watchdog import (
    DEFAULT_BOOT_FILE,
    DEFAULT_HEARTBEAT_FILE,
    DEFAULT_SCAN_FILE,
    DEFAULT_SCAN_STREAK_FILE,
    HealthCheckResult,
    _alert_fingerprint,
)

# 修复冷却窗口（秒）：同一指纹修复后 60 分钟内不重复修复
DEFAULT_FIX_COOLDOWN_SEC = 3600
# 每类修复每日上限
DEFAULT_MAX_FIXES_PER_DAY = 5
# 审计日志
DEFAULT_RECOVER_LOG = "auto_recover_log.jsonl"
# 修复状态（冷却/次数）
DEFAULT_FIX_STATE_FILE = "auto_recover_state.json"
# 清理旧日志的保留天数
LOG_RETAIN_DAYS = 3
# 清理 .bak 的保留天数
BAK_RETAIN_DAYS = 7
# launchd 服务名
LAUNCHD_LABEL = "com.gugu.scheduler"

# 数据源探测样本（真实拉取验证连通性）
PROBE_SYMBOLS = ["600519", "000001"]


def _run_sync(coro):
    """在同步上下文执行协程（AutoRecover.run 为同步入口）。

    2026-08-10 修复：_probe_data_sources 直接对 DataManager.fetch_stock_history
    （async 方法）返回的 coroutine 调 len() → TypeError: object of type 'coroutine'
    has no len() → 探测永远失败 → 数据源告警无条件升级人工（当日 3 次假升级实证）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中事件循环（正常路径：health_watchdog.py 同步 main）
        return asyncio.run(coro)
    # 已有运行中事件循环：不能同步阻塞，丢到新线程执行（防御性兜底）
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=60)


@dataclass
class FixResult:
    """一次自动修复的结果。"""

    fingerprint: str  # 告警指纹（同 watchdog._alert_fingerprint）
    ok: bool  # 修复链路是否成功（含验证）
    root_cause: str  # 根因分析结论
    action: str = ""  # 执行了什么动作
    verified: bool = False  # 修复后验证是否通过
    escalation: bool = False  # 是否需要升级人工（无法修复/修复失败）
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "ok": self.ok,
            "root_cause": self.root_cause,
            "action": self.action,
            "verified": self.verified,
            "escalation": self.escalation,
            "detail": self.detail,
        }


class AutoRecover:
    """自动修复器：对告警结果逐项做根因分析 + 修复 + 验证。

    用法：
        recover = AutoRecover(data_dir=PROJECT_ROOT / "data")
        fixes = recover.run(results)   # results 来自 run_all_checks()
        # fixes 中 escalation=True 的才需要推送人工告警
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        fix_cooldown_sec: int = DEFAULT_FIX_COOLDOWN_SEC,
        max_fixes_per_day: int = DEFAULT_MAX_FIXES_PER_DAY,
        test_mode: bool | None = None,
    ) -> None:
        self.data_dir = data_dir or (PROJECT_ROOT / "data")
        self.fix_cooldown_sec = fix_cooldown_sec
        self.max_fixes_per_day = max_fixes_per_day
        self.recover_log = self.data_dir / DEFAULT_RECOVER_LOG
        self.state_file = self.data_dir / DEFAULT_FIX_STATE_FILE
        # 测试模式：显式传入优先，否则读环境变量
        if test_mode is None:
            test_mode = os.environ.get("GUGU_TEST_MODE") == "1"
        self.test_mode = test_mode

    # ========== 对外入口 ==========

    def run(self, results: list[HealthCheckResult]) -> list[FixResult]:
        """对告警项逐项修复。健康项跳过。

        返回每个告警的修复结果。调用方只需推送 escalation=True 的项。
        """
        fixes: list[FixResult] = []
        for result in results:
            if result.ok:
                continue
            fp = _alert_fingerprint(result)
            # 1) 冷却 + 每日上限原子检查（通过才记录本次修复）
            if not self._can_fix(fp):
                continue
            # 2) 根因分析 + 修复
            fix = self._fix_for(result)
            # 4) 记录审计
            self._record(fix)
            fixes.append(fix)
        return fixes

    # ========== 分诊：按指纹路由到修复器 ==========

    def _fix_for(self, result: HealthCheckResult) -> FixResult:
        fp = _alert_fingerprint(result)
        msg = result.message
        # 精确指纹路由优先（避免消息关键词误判——如 0 信号消息也含"扫描"）
        if fp.endswith("zero_signal_streak"):
            return self._fix_zero_signal(fp, result)
        if fp.endswith("data_source_degraded"):
            return self._fix_data_source(fp, result)
        if fp.endswith("disk_low"):
            return self._fix_disk(fp, result)
        if fp.endswith("restart_storm"):
            return self._fix_restart_storm(fp, result)
        if fp.endswith("heartbeat_missing") or fp.endswith("heartbeat_stale"):
            return self._fix_heartbeat(fp, result)
        if fp.endswith("signal_history_stale"):
            return self._fix_signal_history(fp, result)
        # 兜底：消息关键词辅助（未知指纹但消息可辨识）
        if "心跳" in msg:
            return self._fix_heartbeat(fp, result)
        if "重启风暴" in msg:
            return self._fix_restart_storm(fp, result)
        if "失忆" in msg or "信号历史" in msg:
            return self._fix_signal_history(fp, result)
        # 未知类型：无法自动修复，升级人工
        return FixResult(
            fingerprint=fp,
            ok=False,
            root_cause=f"未知告警类型，无自动修复方案: {msg[:100]}",
            escalation=True,
        )

    # ========== 修复器 ==========

    def _fix_heartbeat(self, fp: str, result: HealthCheckResult) -> FixResult:
        """心跳缺失/超时：调度器可能挂死 → 重启 scheduler。

        根因分析：先看进程是否存活（launchctl + 锁文件），再看心跳文件。
        """
        # --- 根因分析 ---
        hb = self._read_json(DEFAULT_HEARTBEAT_FILE)
        hb_ts = hb.get("last_cycle_at") if hb else None
        lock = self._read_json("scheduler.lock")
        proc_alive = self._process_alive()
        root_cause = (
            f"心跳文件最后更新={hb_ts or '无'}；进程存活={proc_alive}；"
            f"锁={lock.get('pid') if lock else '无'}"
        )

        if self.test_mode:
            return FixResult(
                fingerprint=fp,
                ok=True,
                root_cause=f"[测试模式] {root_cause}",
                action="[测试模式] 跳过真实重启",
                verified=True,
            )

        # --- 修复动作 ---
        if proc_alive:
            # 进程活着但心跳超时 → 卡死，kill 后由 launchd 拉起（KeepAlive）
            self._kickstart(force=True)
            action = "进程存活但心跳超时 → launchctl kickstart -k 强制重启"
        else:
            # 进程死了 → launchd KeepAlive 应已拉起；若无，手动 kickstart
            self._kickstart(force=False)
            action = "进程不存在 → launchctl kickstart 拉起 scheduler"

        # --- 验证 ---
        ok, detail = self._verify_heartbeat()
        return FixResult(
            fingerprint=fp,
            ok=ok,
            root_cause=root_cause,
            action=action,
            verified=ok,
            escalation=not ok,
            detail=detail,
        )

    def _fix_restart_storm(self, fp: str, result: HealthCheckResult) -> FixResult:
        """重启风暴：不自动重启（避免加剧），而是分析日志找根因。

        重启风暴通常是症状不是病根——真正要修的是崩溃原因。
        自动修复动作：采集最近崩溃原因摘要，供人工/后续判断。
        """
        boot = self._read_json(DEFAULT_BOOT_FILE)
        history = boot.get("history", []) if boot else []
        today = datetime.now().date().isoformat()
        today_boots = [b for b in history if str(b.get("boot_time", "")).startswith(today)]
        root_cause = f"今日启动 {len(today_boots)} 次；最近启动记录: {today_boots[-3:] if today_boots else '无'}"

        # 尝试从日志提取崩溃线索
        crash_clue = self._last_crash_clue()
        detail = {"crash_clue": crash_clue}

        # 重启风暴没有安全的自愈动作（自动重启 = 火上浇油）→ 升级人工
        return FixResult(
            fingerprint=fp,
            ok=False,
            root_cause=root_cause,
            action="重启风暴无自动修复动作（避免重启风暴加剧）；已采集崩溃线索",
            verified=False,
            escalation=True,
            detail=detail,
        )

    def _fix_signal_history(self, fp: str, result: HealthCheckResult) -> FixResult:
        """信号历史失忆：系统活着但扫描/落盘停摆。

        根因分支：
        A. 扫描链路停摆（signal_scan.json 陈旧）→ 重启 scheduler
        B. 扫描在跑但历史不落盘 → 通常是 record_signal_history 异常或数据源问题，
           重启 scheduler 刷新状态
        """
        scan = self._read_json(DEFAULT_SCAN_FILE)
        scan_ts = scan.get("last_scan_at") if scan else None
        root_cause = f"信号历史陈旧；signal_scan.last_scan_at={scan_ts or '无'}"

        if self.test_mode:
            return FixResult(
                fingerprint=fp,
                ok=True,
                root_cause=f"[测试模式] {root_cause}",
                action="[测试模式] 跳过真实重启",
                verified=True,
            )

        # --- 修复动作：重启 scheduler（刷新扫描/落盘链路） ---
        self._kickstart(force=False)
        action = "信号历史失忆 → launchctl kickstart 重启 scheduler 刷新扫描链路"

        # --- 验证：等心跳新鲜（重启后 scheduler 会写心跳） ---
        ok, detail = self._verify_heartbeat()
        return FixResult(
            fingerprint=fp,
            ok=ok,
            root_cause=root_cause,
            action=action,
            verified=ok,
            escalation=not ok,
            detail=detail,
        )

    def _fix_disk(self, fp: str, result: HealthCheckResult) -> FixResult:
        """磁盘不足：清理安全垃圾（旧日志/__pycache__/旧 .bak）。

        安全边界：只删 logs/*.log（>LOG_RETAIN_DAYS）、项目 __pycache__、
        以及 data/*.bak*（>BAK_RETAIN_DAYS）。绝不删业务数据。
        """
        usage = shutil.disk_usage(str(self.data_dir))
        before_free = usage.free
        root_cause = f"磁盘剩余 {before_free / 1024 / 1024:.0f}MB"

        if self.test_mode:
            return FixResult(
                fingerprint=fp,
                ok=True,
                root_cause=f"[测试模式] {root_cause}",
                action="[测试模式] 跳过真实清理",
                verified=True,
            )

        freed = self._cleanup_safe_files()
        after_free = shutil.disk_usage(str(self.data_dir)).free
        ok = after_free > 1 * 1024 * 1024 * 1024  # 1GB
        return FixResult(
            fingerprint=fp,
            ok=ok,
            root_cause=root_cause,
            action=f"清理旧日志/缓存/旧备份，释放 {freed / 1024 / 1024:.0f}MB"
            f"（{before_free / 1024 / 1024:.0f}MB → {after_free / 1024 / 1024:.0f}MB）",
            verified=ok,
            escalation=not ok,
            detail={"freed_bytes": freed, "before_free": before_free, "after_free": after_free},
        )

    def _fix_data_source(self, fp: str, result: HealthCheckResult) -> FixResult:
        """数据源降级：先探测真实连通性，恢复则自愈，仍失败则升级。

        数据源降级本身有四级冗余自动降级（sina→eastmoney→akshare→tencent），
        自动修复动作 = 重新探测 + 报告当前可用源。
        """
        root_cause = (
            f"数据源失败率 {result.details.get('failure_rate', '?')}，"
            f"失败 {result.details.get('failures', '?')}/{result.details.get('total_events', '?')} 次"
        )

        if self.test_mode:
            return FixResult(
                fingerprint=fp,
                ok=True,
                root_cause=f"[测试模式] {root_cause}",
                action="[测试模式] 跳过真实探测",
                verified=True,
            )

        probe = self._probe_data_sources()
        if probe["ok"]:
            return FixResult(
                fingerprint=fp,
                ok=True,
                root_cause=root_cause,
                action=f"数据源探测恢复：{probe['detail']}",
                verified=True,
                detail=probe,
            )
        return FixResult(
            fingerprint=fp,
            ok=False,
            root_cause=root_cause,
            action=f"数据源探测仍失败：{probe['detail']}",
            verified=False,
            escalation=True,
            detail=probe,
        )

    def _fix_zero_signal(self, fp: str, result: HealthCheckResult) -> FixResult:
        """连续 0 信号：先看数据源/扫描状态定位根因分支。

        0 信号三态（v0.9.8 P0-2）：
        A. 数据源故障/择时降级 → 数据源探测
        B. 扫描停摆 → 重启 scheduler
        C. 策略未触发/置信度过滤 → 无自动修复，升级人工（策略层问题）
        """
        scan = self._read_json(DEFAULT_SCAN_FILE)
        data_health = scan.get("data_health") if scan else None
        data_errors = scan.get("data_errors") if scan else None
        streak = (self._read_json(DEFAULT_SCAN_STREAK_FILE) or {}).get("streak", 0)
        root_cause = (
            f"连续 {streak} 轮 0 信号；data_health={data_health}；data_errors={data_errors}"
        )

        if self.test_mode:
            return FixResult(
                fingerprint=fp,
                ok=True,
                root_cause=f"[测试模式] {root_cause}",
                action="[测试模式] 跳过真实修复",
                verified=True,
            )

        # 分支 A：数据源问题 → 探测（探通则自愈，探不通升级）
        if data_health == "degraded" or data_errors:
            probe = self._probe_data_sources()
            if probe["ok"]:
                return FixResult(
                    fingerprint=fp,
                    ok=True,
                    root_cause=root_cause,
                    action=f"数据源已恢复，0 信号由数据源故障导致 → 自愈：{probe['detail']}",
                    verified=True,
                    detail=probe,
                )
            return FixResult(
                fingerprint=fp,
                ok=False,
                root_cause=root_cause,
                action=f"数据源仍不可用：{probe['detail']}",
                verified=False,
                escalation=True,
                detail=probe,
            )

        # 分支 B：扫描停摆（scan 陈旧）→ 重启 scheduler
        if scan and scan.get("last_scan_at"):
            try:
                from datetime import datetime as _dt

                last_scan = _dt.fromisoformat(scan["last_scan_at"])
                age_min = (datetime.now() - last_scan).total_seconds() / 60.0
            except (ValueError, TypeError):
                age_min = 999
            if age_min > 30:
                self._kickstart(force=False)
                ok, detail = self._verify_heartbeat()
                return FixResult(
                    fingerprint=fp,
                    ok=ok,
                    root_cause=root_cause,
                    action="扫描停摆 → 重启 scheduler",
                    verified=ok,
                    escalation=not ok,
                    detail=detail,
                )

        # 分支 C：策略/置信度层 → 无法自动修复，升级人工
        return FixResult(
            fingerprint=fp,
            ok=False,
            root_cause=root_cause,
            action="扫描在跑但 0 信号（策略未触发/置信度过滤/择时保守），无自动修复方案",
            verified=False,
            escalation=True,
            detail={"streak": streak, "data_health": data_health},
        )

    # ========== 底层动作（测试模式外真实执行） ==========

    def _kickstart(self, force: bool = False) -> bool:
        """重启 launchd 管理的 scheduler。

        force=True: kickstart -k（先 kill 再拉起）；force=False: kickstart（无则拉起）。
        """
        cmd = [
            "launchctl",
            "kickstart",
            "-k" if force else "",
            f"gui/{os.getuid()}/{LAUNCHD_LABEL}",
        ]
        cmd = [c for c in cmd if c]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return proc.returncode == 0
        except Exception:
            return False

    def _process_alive(self) -> bool:
        """检查 scheduler 进程是否存活（launchctl list + 锁文件双重验证）。"""
        try:
            proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
            if LAUNCHD_LABEL in proc.stdout:
                return True
        except Exception:
            pass
        lock = self._read_json("scheduler.lock")
        if lock and lock.get("pid"):
            try:
                os.kill(int(lock["pid"]), 0)
                return True
            except (OSError, ValueError):
                pass
        return False

    def _verify_heartbeat(self) -> tuple[bool, dict]:
        """验证心跳是否恢复新鲜（<=15 分钟）。等待最多 45 秒。"""
        hb = None
        for _ in range(9):
            hb = self._read_json(DEFAULT_HEARTBEAT_FILE)
            ts = hb.get("last_cycle_at") if hb else None
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    age_min = (datetime.now() - dt).total_seconds() / 60.0
                    if age_min <= 15:
                        return True, {"heartbeat_age_min": round(age_min, 1), "last_cycle_at": ts}
                except (ValueError, TypeError):
                    pass
            time.sleep(5)
        return False, {"heartbeat": hb}

    def _probe_data_sources(self) -> dict:
        """探测数据源连通性：真实拉取一只股票的历史行情。"""
        dm = None
        try:
            from gugu.data.manager import DataManager

            dm = DataManager()
            for symbol in PROBE_SYMBOLS:
                df = _run_sync(dm.fetch_stock_history(symbol, days=5))
                if df is not None and len(df) > 0:
                    return {
                        "ok": True,
                        "detail": f"{symbol} 行情可用（{len(df)} 条）",
                        "probe_symbol": symbol,
                    }
        except Exception as e:
            return {"ok": False, "detail": f"探测异常: {e}"}
        finally:
            if dm is not None:
                with contextlib.suppress(Exception):
                    dm.close()
        return {"ok": False, "detail": "全部探测样本不可用"}

    def _cleanup_safe_files(self) -> int:
        """清理安全垃圾，返回释放字节数。

        安全清单（只删可再生成的产物）：
        - logs/*.log 且 mtime > LOG_RETAIN_DAYS（日志可再生成）
        - 项目下 __pycache__（字节码可再生成）
        - data/*.bak* 且 mtime > BAK_RETAIN_DAYS（备份可删）
        """
        freed = 0
        root = PROJECT_ROOT

        # 1) 旧日志
        log_dir = root / "logs"
        if log_dir.exists():
            for f in log_dir.glob("*.log"):
                try:
                    if (
                        datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)
                    ).days > LOG_RETAIN_DAYS:
                        freed += f.stat().st_size
                        f.unlink(missing_ok=True)
                except OSError:
                    pass

        # 2) __pycache__（仅项目 src/ 与 scripts/ 下，不碰 .venv）
        for base in (root / "src", root / "scripts", root / "tests"):
            if base.exists():
                for pyc in base.rglob("__pycache__"):
                    try:
                        for f in pyc.glob("*"):
                            if f.is_file():
                                freed += f.stat().st_size
                        shutil.rmtree(pyc, ignore_errors=True)
                    except OSError:
                        pass

        # 3) 旧 .bak
        if self.data_dir.exists():
            for f in self.data_dir.glob("*.bak*"):
                try:
                    if (
                        datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)
                    ).days > BAK_RETAIN_DAYS:
                        freed += f.stat().st_size
                        f.unlink(missing_ok=True)
                except OSError:
                    pass

        return freed

    def _last_crash_clue(self) -> str:
        """从最近日志提取崩溃线索（最后几处 Traceback/ERROR 摘要）。"""
        log_dir = PROJECT_ROOT / "logs"
        clues: list[str] = []
        if log_dir.exists():
            logs = sorted(log_dir.glob("gugu_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            for log in logs[:2]:
                try:
                    lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
                    for i, line in enumerate(lines):
                        if "Traceback" in line or "ERROR" in line or "CRITICAL" in line:
                            ctx = lines[max(0, i - 1) : i + 3]
                            clues.append(" | ".join(c.strip() for c in ctx)[:300])
                except OSError:
                    continue
        return clues[:3] if clues else "无崩溃线索"

    # ========== 状态/审计 ==========

    def _read_json(self, name: str) -> dict | None:
        path = self.data_dir / name
        try:
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _state(self) -> dict:
        try:
            if self.state_file.exists():
                return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_state(self, state: dict) -> None:
        with contextlib.suppress(OSError):
            self.state_file.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _can_fix(self, fp: str) -> bool:
        """冷却 + 每日上限原子检查；通过则记录本次修复并返回 True。

        状态结构：
            {fp: {"last_fix_at": iso, "day": "YYYY-MM-DD", "fix_count": N}}
        """
        state = self._state()
        today = datetime.now().date().isoformat()
        rec = state.get(fp) or {}

        # 跨天重置计数
        if rec.get("day") != today:
            rec = {"day": today, "fix_count": 0}

        # 冷却检查：同指纹冷却窗口内不重复修
        last = rec.get("last_fix_at")
        if last:
            try:
                age_min = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60.0
                if age_min * 60 < self.fix_cooldown_sec:
                    return False
            except (ValueError, TypeError):
                pass

        # 每日上限检查
        if int(rec.get("fix_count", 0)) >= self.max_fixes_per_day:
            return False

        # 原子记录本次修复（冷却从动作前开始计）
        rec["last_fix_at"] = datetime.now().isoformat()
        rec["fix_count"] = int(rec.get("fix_count", 0)) + 1
        state[fp] = rec
        self._save_state(state)
        return True

    def _record(self, fix: FixResult) -> None:
        """审计日志：追加写 auto_recover_log.jsonl。"""
        with contextlib.suppress(OSError):
            entry = {"ts": datetime.now().isoformat(), **fix.to_dict()}
            with self.recover_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
