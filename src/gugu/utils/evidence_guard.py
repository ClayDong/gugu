"""前向证据文件写入守卫。

`data/signals_history.jsonl` 与 `data/paper_execution_ledger.jsonl` 是
`check_forward_evidence.py` / `check_drift.py` 的判定依据，也就是「策略是否
真的在前向跑过」的唯一证据源。

2026-11-05 实测发现：这两个文件里躺着测试夹具——

- signals_history.jsonl：symbol 000001/000002/000003、strategies ["s"]、
  时间戳 2026-01-01..03，明显是测试构造的合成信号；
- paper_execution_ledger.jsonl：symbol 600001、strategy 为空、
  data_quality=unknown 的一条假成交。

根因是部分模块在**导入期**执行 ``from gugu.config import PROJECT_ROOT``，
把路径常量固化下来；conftest patch ``gugu.config.PROJECT_ROOT`` 无法影响
已绑定的副本。逐个模块补 patch 属于打地鼠，新模块一加就会重新漏。

因此改为在写入点加结构性守卫：测试模式下，任何指向仓库 ``data/`` 目录的
证据文件写入都会被重定向到进程临时目录。这样无论调用方是谁、路径怎么算错，
测试夹具都不可能落进生产证据文件。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from gugu.utils.log import get_logger

logger = get_logger()

# 受保护的前向证据文件名（只保护证据文件，不干扰其它运行时状态）
EVIDENCE_FILENAMES = frozenset(
    {
        "signals_history.jsonl",
        "paper_execution_ledger.jsonl",
    }
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_DATA_DIR = _REPO_ROOT / "data"
_redirect_dir: Path | None = None


def _is_inside_repo_data(path: Path) -> bool:
    try:
        path.resolve().relative_to(_REPO_DATA_DIR.resolve())
    except (ValueError, OSError):
        return False
    return True


def _redirect_root() -> Path:
    global _redirect_dir
    if _redirect_dir is None:
        _redirect_dir = Path(tempfile.mkdtemp(prefix="gugu_evidence_"))
    return _redirect_dir


def guard_evidence_path(path: str | Path) -> Path:
    """返回实际可写的证据文件路径。

    非测试模式原样返回；测试模式下若目标落在仓库 ``data/`` 内，则重定向到
    临时目录，并记一条 warning 以便发现「本该显式隔离却依赖默认路径」的调用方。
    已经指向临时目录的调用（测试显式传 tmp_path）直接放行。
    """
    target = Path(path)
    if target.name not in EVIDENCE_FILENAMES:
        return target
    if os.environ.get("GUGU_TEST_MODE") != "1":
        return target
    if not _is_inside_repo_data(target):
        return target

    redirected = _redirect_root() / target.name
    logger.warning(
        "测试模式下拦截对生产证据文件的写入：{} → {}（证据文件不得被测试夹具污染）",
        target,
        redirected,
    )
    return redirected


def reset_redirect_dir() -> None:
    """清理测试期重定向目录（供测试夹具显式调用）。"""
    global _redirect_dir
    _redirect_dir = None
