"""实盘入口：QMT 实盘交易（阶段四启用）。

用法：
    python scripts/run_live.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()


def main() -> None:
    """实盘入口（阶段四实现）。"""
    logger.error("实盘功能未启用（阶段四）。当前可用：模拟盘 python scripts/run_paper.py")
    logger.info("启用实盘前需完成：")
    logger.info("1. 模拟盘稳定运行 30 天以上")
    logger.info("2. 配置 QMT 环境变量（QMT_PATH/QMT_ACCOUNT_ID/QMT_PASSWORD）")
    logger.info("3. 实现 QmtBroker.connect() 和 order()")
    logger.info("4. 小额资金验证")
    sys.exit(1)


if __name__ == "__main__":
    main()
