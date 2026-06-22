"""模拟盘入口：启动交易引擎，模拟盘运行。

用法：
    python scripts/run_paper.py            # 运行一次交易循环
    python scripts/run_paper.py --daemon   # 守护进程模式（定时调度）
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


async def run_once() -> None:
    """运行一次交易循环。"""
    engine = TradingEngine()
    await engine.run_daily_cycle()
    await engine.send_daily_report("close")


async def run_daemon() -> None:
    """守护进程模式：定时调度。"""
    scheduler = TradingScheduler()
    await scheduler.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gugu 模拟盘")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式")
    args = parser.parse_args()

    if args.daemon:
        asyncio.run(run_daemon())
    else:
        asyncio.run(run_once())
