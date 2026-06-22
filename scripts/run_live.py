"""实盘入口：QMT 实盘交易（阶段四启用）。

用法：
    python scripts/run_live.py --dry-run   # 仅检查 QMT 环境与连接（不下单）
    python scripts/run_live.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.config import env  # noqa: E402
from gugu.execution.qmt import QmtBroker  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()


def _checklist() -> list[str]:
    """QMT 环境检查清单。"""
    cfg = env()
    items = []
    items.append(f"QMT_PATH: {'已配置' if cfg.qmt_path else '未配置'}")
    items.append(f"QMT_ACCOUNT_ID: {'已配置' if cfg.qmt_account_id else '未配置'}")
    items.append(f"QMT_PASSWORD: {'已配置' if cfg.qmt_password else '未配置'}")
    return items


def main(dry_run: bool = False) -> int:
    """实盘入口（阶段四实现）。"""
    logger.error("实盘功能未启用（阶段四）。当前可用：模拟盘 python scripts/run_paper.py")
    logger.info("启用实盘前需完成：")
    logger.info("1. 模拟盘稳定运行 30 天以上")
    logger.info("2. 配置 QMT 环境变量（QMT_PATH/QMT_ACCOUNT_ID/QMT_PASSWORD）")
    logger.info("3. 实现 QmtBroker.connect() 和 order()")
    logger.info("4. 小额资金验证")

    if dry_run:
        logger.info("=== QMT 环境检查（dry-run）===")
        for item in _checklist():
            logger.info(item)
        broker = QmtBroker()
        connected = broker.connect()
        logger.info(f"QMT 连接测试: {'成功' if connected else '失败（预期，阶段四未实现）'}")
        return 0

    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gugu 实盘")
    parser.add_argument("--dry-run", action="store_true", help="仅检查 QMT 环境，不下单")
    parser.add_argument("--version", action="version", version="gugu 0.1.0")
    args = parser.parse_args()

    sys.exit(main(dry_run=args.dry_run))
