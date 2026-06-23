"""策略/wisdom 效果对比脚本（BIZ-03 修复）。

对同一策略 + 同一数据，分别启用/禁用 wisdom 决策层跑回测，
输出对比报告，量化 wisdom 的边际贡献。

用法：
    python scripts/compare_wisdom.py --symbol 600519 --strategy turtle --days 120
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 添加 src 到 path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest import BacktestEngine  # noqa: E402
from gugu.data import data_manager  # noqa: E402
from gugu.strategies.registry import get_strategy  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()


def _format_metrics(metrics: dict, label: str) -> str:
    """格式化指标为可读字符串。"""
    # P1-r 修复：字段名与 BacktestResult.metrics 对齐
    # 实际字段：total_return/annual_return/sharpe/max_drawdown/win_rate/profit_factor/total_trades
    return (
        f"  [{label}]\n"
        f"    总收益:     {metrics.get('total_return', 0):.2%}\n"
        f"    年化收益:   {metrics.get('annual_return', 0):.2%}\n"
        f"    夏普比率:   {metrics.get('sharpe', 0):.4f}\n"
        f"    最大回撤:   {metrics.get('max_drawdown', 0):.2%}\n"
        f"    胜率:       {metrics.get('win_rate', 0):.2%}\n"
        f"    盈亏比:     {metrics.get('profit_factor', 0):.2f}\n"
        f"    交易次数:   {int(metrics.get('total_trades', 0))}\n"
    )


async def main(symbol: str, strategy_name: str, days: int) -> None:
    """对比启用/禁用 wisdom 的回测结果。"""
    logger.info(f"开始对比: {symbol} 策略={strategy_name} 天数={days}")

    # 1. 获取数据
    dm = data_manager()
    df = await dm.fetch_stock_history(symbol, days=days)
    if df.empty:
        logger.error(f"无法获取 {symbol} 数据")
        return

    logger.info(f"获取 {len(df)} 条历史数据")

    # 2. 禁用 wisdom 回测（基线）
    strategy = get_strategy(strategy_name)
    engine_no_wisdom = BacktestEngine(enable_wisdom=False)
    result_no_wisdom = engine_no_wisdom.run(strategy, df, symbol)

    # 3. 启用 wisdom 回测
    strategy2 = get_strategy(strategy_name)
    engine_with_wisdom = BacktestEngine(enable_wisdom=True)
    result_with_wisdom = engine_with_wisdom.run(strategy2, df, symbol)

    # 4. 输出对比报告
    print("\n" + "=" * 60)
    print(f"  策略/wisdom 效果对比报告")
    print(f"  股票: {symbol} | 策略: {strategy_name} | 天数: {days}")
    print("=" * 60)

    print("\n【指标对比】")
    print(_format_metrics(result_no_wisdom.metrics, "禁用 wisdom（基线）"))
    print(_format_metrics(result_with_wisdom.metrics, "启用 wisdom"))

    # 5. 计算边际贡献
    print("【wisdom 边际贡献】")
    delta_return = result_with_wisdom.metrics.get("total_return", 0) - result_no_wisdom.metrics.get(
        "total_return", 0
    )
    delta_sharpe = result_with_wisdom.metrics.get("sharpe", 0) - result_no_wisdom.metrics.get(
        "sharpe", 0
    )
    delta_drawdown = result_with_wisdom.metrics.get(
        "max_drawdown", 0
    ) - result_no_wisdom.metrics.get("max_drawdown", 0)
    delta_trades = result_with_wisdom.metrics.get("total_trades", 0) - result_no_wisdom.metrics.get(
        "total_trades", 0
    )

    print(f"  收益变化:     {delta_return:+.2%}")
    print(f"  夏普变化:     {delta_sharpe:+.4f}")
    print(f"  回撤变化:     {delta_drawdown:+.2%}（负值=回撤减小=改善）")
    print(f"  交易次数变化: {int(delta_trades):+d}")

    print("\n【结论】")
    if delta_return > 0 and delta_sharpe > 0:
        print("  ✅ wisdom 提升了收益与风险调整后收益，建议启用")
    elif delta_return > 0:
        print("  ⚠️ wisdom 提升了收益但夏普未改善，需进一步评估")
    elif delta_drawdown < 0:
        print("  ⚠️ wisdom 降低了回撤但收益未提升，适合保守策略")
    else:
        print("  ❌ wisdom 未带来正向贡献，需检查 skill 配置或参数")

    print("\n" + "=" * 60)
    print("  对比完成。详细数据见 data/signals_history.jsonl")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gugu 策略/wisdom 效果对比")
    parser.add_argument("--symbol", default="600519", help="股票代码")
    parser.add_argument("--strategy", default="turtle", help="策略名称")
    parser.add_argument("--days", type=int, default=120, help="回测天数")
    parser.add_argument("--version", action="version", version="gugu 0.1.0")
    args = parser.parse_args()

    asyncio.run(main(args.symbol, args.strategy, args.days))
