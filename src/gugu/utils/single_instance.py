"""单实例锁：防止 scheduler 多实例并行运行（2026-08-05 P0 防护）。

背景：2026-08-05 实证——手动启动的 scheduler 与 launchd 管理的 scheduler
并行跑了 12 小时+，导致重复扫描/重复通知/状态文件竞争。本模块提供
进程级互斥锁：同一时间只允许一个 scheduler 实例运行。

设计要点：
- 锁文件写入 PID + 启动时间 + 命令行
- 获取锁时验证锁内 PID 是否仍存活（os.kill(pid, 0)）且命令行匹配
  gugu 特征（防止 PID 被系统复用后误判"已在运行"）
- 锁文件损坏/残留（进程已死）→ 自动接管
- GUGU_TEST_MODE 下跳过（测试进程可并发创建 scheduler 实例）
- 进程退出时释放锁文件（atexit 注册 + 显式 release）

用法：
    from gugu.utils.single_instance import acquire_single_instance_lock
    if not acquire_single_instance_lock():
        sys.exit(1)  # 已有实例在运行
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("gugu.single_instance")

# 锁文件位置（相对项目根）
DEFAULT_LOCK_PATH = "data/scheduler.lock"

# gugu scheduler 进程命令行特征（用于防止 PID 复用误判）
_GUGU_CMD_MARKERS = (
    "run_scheduler",
    "engine.scheduler",
    "run_live.py",
)


def _process_alive(pid: int) -> bool:
    """检查 PID 对应的进程是否存活。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限（更安全：视为存活）
    except OSError:
        return False
    return True


def _process_is_gugu(pid: int) -> bool:
    """验证 PID 对应进程是否为 gugu scheduler（防止 PID 复用误判）。"""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        cmd = (result.stdout or "").strip()
    except Exception:
        return True  # ps 失败时保守处理：视为 gugu（拒绝启动比误放行安全）
    return any(marker in cmd for marker in _GUGU_CMD_MARKERS)


def _read_lock(lock_path: Path) -> dict | None:
    """安全读取锁文件内容。"""
    try:
        if not lock_path.exists():
            return None
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def acquire_single_instance_lock(
    lock_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> bool:
    """获取单实例锁。

    Returns:
        True = 获得锁（可继续启动）
        False = 已有实例在运行（应退出）
    """
    if os.environ.get("GUGU_TEST_MODE"):
        logger.debug("GUGU_TEST_MODE: 跳过单实例锁")
        return True

    root = Path(project_root) if project_root else Path.cwd()
    path = Path(lock_path) if lock_path else root / DEFAULT_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    # 检查现有锁
    existing = _read_lock(path)
    if existing:
        pid = int(existing.get("pid", 0) or 0)
        if pid > 0 and _process_alive(pid) and _process_is_gugu(pid):
            started = existing.get("started_at", "?")
            cmd = (existing.get("cmd", "") or "")[:80]
            logger.error(
                "单实例锁被占用：已有 scheduler 实例运行 (PID=%s, started=%s, cmd=%s)。"
                "拒绝启动，避免双实例并行。",
                pid,
                started,
                cmd,
            )
            return False
        # 进程已死 → 残留锁，接管
        logger.warning("检测到残留锁文件（PID=%s 已不在运行），自动接管", pid)

    # 写入新锁
    payload = {
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cmd": " ".join(__import__("sys").argv[:3]),
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("写入单实例锁失败（不阻塞启动）: %s", e)

    # 进程退出时释放锁（避免残留锁误伤下次启动）
    atexit.register(release_single_instance_lock, path)
    logger.info("单实例锁已获取: %s (PID=%s)", path, os.getpid())
    return True


def release_single_instance_lock(lock_path: str | Path) -> None:
    """释放单实例锁（仅当锁属于当前进程时删除）。"""
    path = Path(lock_path)
    try:
        existing = _read_lock(path)
        if existing and int(existing.get("pid", 0) or 0) == os.getpid():
            path.unlink(missing_ok=True)
            logger.info("单实例锁已释放: %s", path)
    except Exception as e:
        logger.debug("释放单实例锁失败（忽略）: %s", e)
