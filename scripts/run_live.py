"""实盘入口：QMT 实盘交易（阶段四启用）。

用法：
    python scripts/run_live.py              # 默认模式（需 QMT 配置）
    python scripts/run_live.py --signal-only  # 信号监控模式（只通知不下单）
    python scripts/run_live.py --dry-run     # 仅检查 QMT 环境与连接
    python scripts/run_live.py --signal-only --loop 10  # 信号监控模式，每10分钟循环一次
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.config import get_config  # noqa: E402
from gugu.data import data_manager as dm  # noqa: E402
from gugu.engine.main import TradingEngine  # noqa: E402
from gugu.execution.qmt import QmtBroker  # noqa: E402
from gugu.notifier import FeishuNotifier  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()


def _checklist() -> list[str]:
    """QMT 环境检查清单。"""
    cfg = get_config()
    qmt_path = getattr(cfg.execution.live, "broker", "qmt")
    items = []
    items.append(f"运行模式: {cfg.execution.mode}")
    items.append(f"券商接口: {qmt_path}")
    items.append(f"启用的策略: {cfg.strategy.enabled}")
    items.append(f"自选股数量: {len(cfg.watchlist)}")
    return items


async def run_signal_only_cycle(loop_interval_minutes: int = 0) -> None:
    """信号监控模式：运行 TradingEngine 的完整信号扫描流程，但不执行实际下单。

    此模式用于：
    1. 验证数据源连通性
    2. 验证策略信号生成
    3. 验证 Wisdom 决策层
    4. 飞书通知所有信号（不下单）

    与 signal_only 模式的区别：
    - TradingEngine.run_daily_cycle() 在 settings.yaml 中 mode=signal_only 时
      不会下单（见 engine/main.py _process_signal 中的 signal_only 检查）
    - 此函数是独立入口，可周期性循环运行

    Args:
        loop_interval_minutes: 循环间隔分钟数，0=只运行一次
    """
    engine = TradingEngine()
    notifier = FeishuNotifier()

    try:
        await notifier.notify_risk_alert(
            {
                "level": "info",
                "message": "gugu 信号监控模式启动",
                "suggestion": (
                    f"模式: signal_only\n"
                    f"策略: {get_config().strategy.enabled}\n"
                    f"自选股: {len(get_config().watchlist)} 只\n"
                    f"循环间隔: {'单次运行' if loop_interval_minutes == 0 else f'{loop_interval_minutes}分钟'}"
                ),
            }
        )
    except Exception as e:
        logger.warning(f"启动通知失败: {e}")

    while True:
        try:
            logger.info("=" * 50)
            logger.info("信号监控模式：开始交易循环")
            logger.info("=" * 50)
            await engine.run_daily_cycle()
            logger.info("交易循环完成")
        except Exception as e:
            logger.error(f"交易循环异常: {e}")
            try:
                await notifier.notify_error(
                    {
                        "module": "run_live.signal_only",
                        "message": str(e)[:500],
                        "suggestion": "检查日志 logs/gugu_*.log",
                    }
                )
            except Exception:
                pass

        if loop_interval_minutes <= 0:
            break

        logger.info(f"等待 {loop_interval_minutes} 分钟后下一次循环...")
        await asyncio.sleep(loop_interval_minutes * 60)


async def run_qmt_live() -> None:
    """QMT 实盘模式（v0.9.0 实时版）。

    需要：
    1. xtquant SDK 安装
    2. QMT 客户端运行并登录
    3. .env 配置 QMT_PATH/QMT_ACCOUNT_ID/QMT_PASSWORD
    4. settings.yaml execution.mode=live

    v0.9.0 升级：连接后持续运行，盘中每分钟实时推荐 + 自动执行。
    """
    logger.info("QMT 实盘模式启动（v0.9.0 实时）")

    # 检查环境
    broker = QmtBroker()
    connected = broker.connect()
    if not connected:
        logger.error("QMT 连接失败，请检查:")
        logger.error("1. QMT 客户端是否已启动并登录")
        logger.error("2. .env 文件中 QMT_PATH/QMT_ACCOUNT_ID/QMT_PASSWORD 是否正确配置")
        logger.error("3. xtquant SDK 是否已安装")
        logger.error("回退到 signal_only 模式...")
        await run_signal_only_cycle()
        return

    logger.info("QMT 连接成功")
    engine = TradingEngine()

    # 启动交易调度器（含盘中实时推荐 + 定时任务）
    from gugu.engine.scheduler import TradingScheduler

    scheduler = TradingScheduler()
    await scheduler.start()
    logger.info("交易调度器已启动（实时推荐 + 定时任务）")

    # 保持运行
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到退出信号，正在关闭...")
    finally:
        try:
            scheduler._scheduler.shutdown(wait=False)
        except Exception:
            pass
        broker.disconnect()
        logger.info("gugu 实盘已安全退出")


def main(mode: str, loop_interval: int) -> int:
    """主入口。"""
    cfg = get_config()

    # P0 防护：单实例锁，防止与 launchd 管理的 scheduler 并行运行
    from gugu.utils.single_instance import acquire_single_instance_lock

    if not acquire_single_instance_lock(project_root=Path(__file__).resolve().parents[1]):
        logger.error("已有 gugu scheduler 实例在运行，拒绝启动第二个实例")
        return 1

    logger.info("=== gugu 实盘环境检查 ===")
    for item in _checklist():
        logger.info(f"  {item}")

    if mode == "signal_only":
        logger.info("启动信号监控模式（signal_only）")
        asyncio.run(run_signal_only_cycle(loop_interval_minutes=loop_interval))
        return 0
    elif mode == "dry_run":
        logger.info("=== 环境检查清单 ===")
        for item in _checklist():
            logger.info(item)

        # 测试连接
        if cfg.execution.mode == "live":
            broker = QmtBroker()
            connected = broker.connect()
            logger.info(f"QMT 连接测试: {'成功' if connected else '失败'}")
        else:
            logger.info("当前非实盘模式，跳过 QMT 连接测试")

        # 测试数据源
        try:
            from gugu.data import data_manager as dm_test

            test_dm = dm_test.data_manager()
            logger.info(f"数据源状态: {'降级' if test_dm.is_degraded else '正常'}")
        except Exception as e:
            logger.warning(f"数据源测试失败: {e}")

        return 0
    else:
        # live mode
        logger.info("启动实盘模式")
        asyncio.run(run_qmt_live())
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="gugu 实盘入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/run_live.py --signal-only          # 信号监控模式（只通知不下单）
  python scripts/run_live.py --signal-only --loop 5  # 信号监控模式每5分钟循环
  python scripts/run_live.py --dry-run               # 环境检查
  python scripts/run_live.py                          # 实盘模式（需配置 QMT）
        """,
    )
    parser.add_argument("--signal-only", action="store_true", help="信号监控模式（不下单）")
    parser.add_argument("--dry-run", action="store_true", help="仅检查环境，不下单")
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="N",
        help="循环间隔分钟数（signal_only 模式，0=单次）",
    )
    parser.add_argument("--version", action="version", version="gugu 0.6.0")
    args = parser.parse_args()

    if args.dry_run:
        mode = "dry_run"
    elif args.signal_only:
        mode = "signal_only"
    else:
        mode = "live"

    sys.exit(main(mode, loop_interval=args.loop))
