"""信号监控入口：启动信号监控系统。

用法：
    python scripts/run_paper.py            # 运行一次信号扫描
    python scripts/run_paper.py --daemon   # 守护进程模式（定时调度，保活）
    python scripts/run_paper.py --report  # 单次模式结束后发送信号汇总
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.engine.main import TradingEngine  # noqa: E402
from gugu.engine.scheduler import TradingScheduler  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()


async def run_once(send_report: bool = False, reset_halt: bool = False) -> None:
    """运行一次信号扫描。"""
    engine = TradingEngine()
    logger.info(f"运行模式: {engine._exec_mode}")
    if reset_halt:
        engine.reset_halt()
    try:
        await engine.run_daily_cycle()
        if send_report:
            await engine.send_daily_report("close")
    finally:
        await engine.shutdown()


async def run_daemon() -> None:
    """守护进程模式：定时调度，保活运行。"""
    scheduler = TradingScheduler()
    logger.info("启动信号监控守护进程...")
    try:
        await scheduler.start()
    finally:
        await scheduler.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gugu 信号监控系统")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式（保活）")
    parser.add_argument("--report", action="store_true", help="单次模式结束后发送信号汇总")
    parser.add_argument("--reset-halt", action="store_true", help="重置 L2 熔断状态后执行")
    parser.add_argument("--version", action="version", version="gugu 0.1.0")
    args = parser.parse_args()

    if args.daemon:
        asyncio.run(run_daemon())
    else:
        asyncio.run(run_once(send_report=args.report, reset_halt=args.reset_halt))
