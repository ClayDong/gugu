"""原子文件写入工具（P0 修复）。

防止进程在写入过程中崩溃导致文件截断/损坏。
策略：写到临时文件 → fsync → os.replace（原子替换）。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
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


def atomic_write_json(path: str | Path, data: Any, **kwargs: Any) -> None:
    """原子写入 JSON 文件。

    Args:
        path: 目标文件路径
        data: 要序列化的对象
        **kwargs: 传给 json.dumps 的参数（如 ensure_ascii, indent）
    """
    content = json.dumps(data, **kwargs)
    atomic_write_text(path, content)
