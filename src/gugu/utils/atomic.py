"""原子文件写入工具（P0 修复）。

防止进程在写入过程中崩溃导致文件截断/损坏。
策略：写到临时文件 → fsync → os.replace（原子替换）。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any


def atomic_write_text(path: str | Path, content: str) -> None:
    """原子写入文本文件。

    流程：写到同目录临时文件 → fsync → os.replace 原子替换。
    os.replace 在 POSIX 上是原子的，崩溃时要么是旧文件要么是新文件，
    不会出现半截写入的中间状态。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录临时文件（os.replace 在跨文件系统时非原子，必须同目录）
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # 异常时清理临时文件
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def append_jsonl(path: str | Path, record: Any, *, max_lines: int = 10000) -> None:
    """Append one strict JSONL record under a cross-process sidecar lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines.append(
                json.dumps(
                    _sanitize_json(record),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
            atomic_write_text(path, "\n".join(lines[-max_lines:]) + "\n")
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _sanitize_json(value: Any) -> Any:
    """Normalize non-finite values while preserving common JSON-adjacent types."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, (datetime, date, Path)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if hasattr(value, "item"):
        with contextlib.suppress(Exception):
            return _sanitize_json(value.item())
    return value


def atomic_write_json(path: str | Path, data: Any, **kwargs: Any) -> None:
    """Write strict JSON atomically; NaN/Infinity become JSON null."""
    options = dict(kwargs)
    options.setdefault("allow_nan", False)
    options.setdefault("ensure_ascii", False)
    content = json.dumps(_sanitize_json(data), **options)
    atomic_write_text(path, content)
