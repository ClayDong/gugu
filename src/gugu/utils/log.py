"""日志配置。基于 loguru，统一格式和输出。"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

from gugu.config import PROJECT_ROOT, env

_configured = False

# 日志文件日期前缀（与文件名模板保持一致）
_LOG_DATE_RE = re.compile(r"gugu_(\d{8})\.log")


def _rotate_by_size_or_date(message: str, file: Any) -> bool:
    """文件轮转条件：10MB 大小上限 或 跨日（文件名日期 != 今天）。

    ⚠️ 2026-08-11 修复（告警评估员）：loguru 0.7.3 的 {time:YYYYMMDD} 文件名
    只在创建/大小轮转时求值，不会在午夜自动切换——跨日运行的 scheduler
    （如 8/10 16:35 启动）会一直写 gugu_20260810.log，导致
    alert_triage / 巡检 grep 找不到"今日日志"（实证：8/11 无 gugu_20260811.log）。
    修复：轮转条件增加日期比对，跨日即轮转到新日期文件。
    """
    # 大小轮转（保留原 10MB 上限）
    try:
        file.seek(0, 2)
        if file.tell() + len(message) > 10 * 1024 * 1024:
            return True
    except (OSError, ValueError):
        pass
    # 跨日轮转
    name = getattr(file, "name", "") or ""
    m = _LOG_DATE_RE.search(name)
    return bool(m and m.group(1) != time.strftime("%Y%m%d"))


def setup_logging() -> None:
    """初始化日志（进程内只配置一次）。"""
    global _configured
    if _configured:
        return

    level = env().log_level
    # 测试模式：日志文件重定向到临时目录，禁止写生产 logs/（2026-08-10 实证：
    # pytest 进程把测试日志写入生产 logs/gugu_YYYYMMDD.log，污染当日 ERROR 统计、
    # 造成告警分诊 142 条假 ERROR；conftest 只 patch gugu.config.PROJECT_ROOT 属性，
    # 本模块 import 时已绑定生产路径，必须在此显式隔离）
    if os.environ.get("GUGU_TEST_MODE") == "1":
        log_dir = Path(tempfile.gettempdir()) / "gugu_test_logs"
    else:
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
        rotation=_rotate_by_size_or_date,
        retention="30 days",
        encoding="utf-8",
    )
    _configured = True


def get_logger() -> Any:
    """获取 logger 实例。"""
    if not _configured:
        setup_logging()
    return logger
