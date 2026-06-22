"""日志配置。基于 loguru，统一格式和输出。"""
from __future__ import annotations

import sys
from typing import Any

from loguru import logger

from gugu.config import PROJECT_ROOT, env

_configured = False


def setup_logging() -> None:
    """初始化日志（进程内只配置一次）。"""
    global _configured
    if _configured:
        return

    level = env().log_level
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    # 控制台输出
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
    )
    # 文件输出
    logger.add(
        log_dir / "gugu_{time:YYYYMMDD}.log",
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
    )
    _configured = True


def get_logger() -> Any:
    """获取 logger 实例。"""
    if not _configured:
        setup_logging()
    return logger
