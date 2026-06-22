"""回测入口：对策略进行历史数据回测。

用法：
    python scripts/run_backtest.py --symbol 600519 --strategy turtle --days 120
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 添加 src 到 path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest import BacktestEngine, format_report  # noqa: E402
from gugu.data import data_manager  # noqa: E402
from gugu.notifier import FeishuNotifier  # noqa: E402
from gugu.strategies.registry import get_strategy  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()


async def main(symbol: str, strategy_name: str, days: int, notify: bool) -> None:
    """执行回测。"""
    logger.info(f"开始回测: {symbol} 策略={strategy_name} 天数={days}")

    # 1. 获取数据
    dm = data_manager()
    df = dm.fetch_stock_history(symbol, days=days)
    if df.empty:
        logger.error(f"无法获取 {symbol} 数据")
        return

    logger.info(f"获取 {len(df)} 条历史数据")

    # 2. 运行回测
    strategy = get_strategy(strategy_name)
    engine = BacktestEngine()
    result = engine.run(strategy, df, symbol)

    # 3. 打印报告
    report = format_report(result)
    print(report)

    # 4. 飞书通知（可选）
    if notify:
        from gugu.backtest.report import format_report_dict

        notifier = FeishuNotifier()
        await notifier.notify_backtest(format_report_dict(result))
        logger.info("回测报告已推送到飞书")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gugu 回测")
    parser.add_argument("--symbol", default="600519", help="股票代码")
    parser.add_argument("--strategy", default="turtle", help="策略名称")
    parser.add_argument("--days", type=int, default=120, help="回测天数")
    parser.add_argument("--notify", action="store_true", help="推送飞书")
    args = parser.parse_args()

    asyncio.run(main(args.symbol, args.strategy, args.days, args.notify))
