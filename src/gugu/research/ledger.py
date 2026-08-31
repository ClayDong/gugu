"""可审计的研究运行账本。

账本只记录顶层回测/优化运行，不进入回测热路径，也不参与策略准入或配置写回。
每条记录必须能被严格 JSON 解析；NaN/Infinity 会被规范化为 null。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gugu import config as gugu_config
from gugu.utils.log import get_logger

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX production path
    fcntl = None  # type: ignore[assignment]


LEDGER_SCHEMA_VERSION = 1


def _default_ledger_path() -> Path:
    """Resolve the project path at write time so tests and workers can isolate it."""
    return gugu_config.PROJECT_ROOT / "data" / "research_ledger.jsonl"


logger = get_logger()


def new_run_id() -> str:
    """Create a collision-resistant research run identifier."""
    return uuid.uuid4().hex


def runtime_context() -> dict[str, Any]:
    """Collect reproducibility metadata without making research decisions."""
    context: dict[str, Any] = {"git_commit": None, "git_dirty": None}
    try:
        root = str(gugu_config.PROJECT_ROOT)
        commit = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if commit.returncode == 0:
            context["git_commit"] = commit.stdout.strip() or None
        status = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if status.returncode == 0:
            context["git_dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        logger.debug("无法采集研究运行 git 元数据")
    return context


def _sanitize(value: Any) -> Any:
    """Convert values to strict-JSON-compatible primitives recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if hasattr(value, "item"):
        with contextlib.suppress(Exception):
            return _sanitize(value.item())
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


@contextlib.contextmanager
def _locked_append(path: Path) -> Iterator[list[str]]:
    """Read and update a JSONL file while serializing writers on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            yield lines
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def append_record(record: dict[str, Any], path: str | Path | None = None) -> Path | None:
    """Append one sanitized record atomically.

    Ledger failures are deliberately non-fatal: research results and Gate decisions
    must not change because observability storage is unavailable.
    """
    target = Path(path) if path is not None else _default_ledger_path()
    sanitized = _sanitize(record)
    if not isinstance(sanitized, dict):
        raise TypeError("研究账本记录必须是 dict")
    sanitized.setdefault("schema_version", LEDGER_SCHEMA_VERSION)
    sanitized.setdefault("run_id", new_run_id())
    sanitized.setdefault("recorded_at", datetime.now(UTC).isoformat())
    try:
        line = json.dumps(sanitized, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with _locked_append(target) as lines:
            lines.append(line)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, target)
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        return target
    except Exception as exc:
        logger.warning("研究账本写入失败（不影响研究结果）: {}", exc)
        return None


def build_record(
    *,
    kind: str,
    entrypoint: str,
    strategy: str | None = None,
    strategies: list[str] | None = None,
    symbols: list[str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a standard run-level record with reproducibility metadata."""
    symbol_list = [str(symbol) for symbol in (symbols or [])]
    record: dict[str, Any] = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "run_id": new_run_id(),
        "recorded_at": datetime.now(UTC).isoformat(),
        "kind": kind,
        "entrypoint": entrypoint,
        "strategy": strategy,
        "strategies": strategies or ([strategy] if strategy else []),
        "symbols": symbol_list,
        "symbol_count": len(symbol_list),
    }
    record.update(runtime_context())
    record.update(fields)
    return record
