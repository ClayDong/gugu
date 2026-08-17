#!/usr/bin/env python3
"""交易调度器启动入口。

用法:
    uv run python scripts/run_scheduler.py

该脚本启动 APScheduler 调度器，按 config/settings.yaml 中配置的定时任务执行：
- 盘中扫描（09:30 / 10:30 / 13:05 / 14:30）
- 尾盘选股（14:31）
- 盘后资金流决策报告（14:32）
- 日报/宏观简报/基金监控等

调度器以前台进程运行，建议配合 nohup / screen / tmux 使用：
    nohup uv run python scripts/run_scheduler.py > logs/scheduler.log 2>&1 &
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 确保项目根目录在 PYTHONPATH 中
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gugu.engine.scheduler import TradingScheduler


def main() -> None:
    """启动交易调度器守护进程。"""
    logging.basicConfig(level=logging.INFO)

    # P0 防护：单实例锁，防止与 launchd/手动启动的实例并行运行
    from gugu.utils.single_instance import acquire_single_instance_lock

    if not acquire_single_instance_lock(project_root=ROOT):
        sys.exit(1)

    scheduler = TradingScheduler()
    try:
        import asyncio

        asyncio.run(scheduler.start())
    except KeyboardInterrupt:
        print("\n收到中断信号，调度器退出")


if __name__ == "__main__":
    main()
