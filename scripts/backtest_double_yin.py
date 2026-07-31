"""双阴钱袋子策略回测验证脚本。

第一性原理：
- 文章战法是实战派经验总结，未经回测验证不能直接上线
- 双阴钱袋子要求3K组合（双阴+倍量反转），信号稀疏，需多股验证
- 与 pullback_low 对比：哪个策略在震荡市更有效？

验证流程：
1. 全市场 30 只抽样 × 500 天回测
2. 与 pullback_low 对比（同基准）
3. 4 项解锁条件验证

用法：
    uv run python scripts/backtest_double_yin.py
    uv run python scripts/backtest_double_yin.py --target-count 50
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest.engine import BacktestEngine  # noqa: E402
from gugu.config import PROJECT_ROOT  # noqa: E402
from gugu.strategies.double_yin import DoubleYinMoneyBagStrategy  # noqa: E402
from gugu.strategies.pullback_low import PullbackLowStrategy  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

from walk_forward_offline import (  # noqa: E402
    load_stock_df,
    select_stocks_from_cache,
)

logger = get_logger()

_DEFAULT_SYMBOLS = [
    "600519",
    "000858",
    "601318",
    "600036",
    "002714",
    "300750",
    "000333",
    "600276",
    "601012",
    "601888",
]


def backtest_single(
    symbol: str, df: pd.DataFrame, strategy_cls, params: dict | None = None
) -> dict:
    """对单只股票回测指定策略。"""
    try:
        strategy = strategy_cls(params)
        engine = BacktestEngine(initial_capital=1_000_000)
        result = engine.run(strategy, df, symbol=symbol)
        return {
            "symbol": symbol,
            "trades": len(result.trades),
            "total_return": result.metrics.get("total_return", 0),
            "sharpe": result.metrics.get("sharpe", 0),
            "max_drawdown": result.metrics.get("max_drawdown", 0),
            "win_rate": result.metrics.get("win_rate", 0),
        }
    except Exception as e:
        logger.warning(f"回测 {symbol} 失败: {e}")
        return {
            "symbol": symbol,
            "trades": 0,
            "total_return": 0,
            "sharpe": 0,
            "max_drawdown": 0,
            "win_rate": 0,
        }


async def run_comparison_backtest(target_count: int = 30) -> None:
    """对比回测 double_yin vs pullback_low。"""
    print("\n" + "=" * 80)
    print("双阴钱袋子策略 vs 低吸回踩策略 对比回测验证")
    print("=" * 80)

    symbols = select_stocks_from_cache(_DEFAULT_SYMBOLS, min_rows=500, target_count=target_count)
    if not symbols:
        print("❌ 缓存中无可用股票数据")
        return

    print(f"\n📊 回测股票: {len(symbols)} 只")

    double_yin_results = []
    pullback_results = []

    for sym in symbols:
        df = load_stock_df(sym)
        if df is None or len(df) < 500:
            continue

        # 双阴钱袋子策略
        dy_r = backtest_single(sym, df, DoubleYinMoneyBagStrategy)
        double_yin_results.append(dy_r)

        # 低吸回踩策略（对比基准）
        pl_r = backtest_single(sym, df, PullbackLowStrategy)
        pullback_results.append(pl_r)

        if dy_r["trades"] > 0 or pl_r["trades"] > 0:
            print(
                f"  {sym}: 双阴 {dy_r['trades']}笔 收益{dy_r['total_return']:+.2%} 夏普{dy_r['sharpe']:.2f} | "
                f"低吸 {pl_r['trades']}笔 收益{pl_r['total_return']:+.2%} 夏普{pl_r['sharpe']:.2f}"
            )

    # 汇总对比
    print("\n" + "=" * 80)
    print("📈 两个策略回测对比汇总")
    print("=" * 80)

    dy_valid = [r for r in double_yin_results if r["trades"] > 0]
    pl_valid = [r for r in pullback_results if r["trades"] > 0]

    def _summary(results: list, name: str) -> dict:
        if not results:
            print(f"  {name}: 无有效回测结果")
            return {}
        avg_return = sum(r["total_return"] for r in results) / len(results)
        avg_sharpe = sum(r["sharpe"] for r in results) / len(results)
        avg_dd = sum(r["max_drawdown"] for r in results) / len(results)
        avg_win = sum(r["win_rate"] for r in results) / len(results)
        total_trades = sum(r["trades"] for r in results)
        print(
            f"  {name}: {len(results)}/{len(double_yin_results)} 只有效 | "
            f"总交易 {total_trades}笔 | "
            f"平均收益 {avg_return:+.2%} | "
            f"夏普 {avg_sharpe:.2f} | "
            f"回撤 {avg_dd:.2%} | "
            f"胜率 {avg_win:.0%}"
        )
        return {
            "avg_return": avg_return,
            "avg_sharpe": avg_sharpe,
            "avg_dd": avg_dd,
            "avg_win": avg_win,
            "total_trades": total_trades,
        }

    print()
    dy_stats = _summary(dy_valid, "双阴钱袋子")
    pl_stats = _summary(pl_valid, "低吸回踩  ")

    # 4 项解锁条件检查
    if dy_stats:
        print("\n🔒 双阴钱袋子策略 4 项解锁条件检查:")
        c1 = dy_stats["avg_sharpe"] > 0
        c2 = dy_stats["avg_dd"] < 0.25
        c3 = dy_stats["avg_return"] >= 0
        c4 = dy_stats["avg_win"] >= 0.40
        print(f"  1. 夏普 > 0:    {'✅' if c1 else '❌'} ({dy_stats['avg_sharpe']:.2f})")
        print(f"  2. 回撤 < 25%:  {'✅' if c2 else '❌'} ({dy_stats['avg_dd']:.2%})")
        print(f"  3. 收益 ≥ 0:    {'✅' if c3 else '❌'} ({dy_stats['avg_return']:+.2%})")
        print(f"  4. 胜率 ≥ 40%:  {'✅' if c4 else '❌'} ({dy_stats['avg_win']:.0%})")
        all_pass = c1 and c2 and c3 and c4
        print(
            f"\n{'✅ 双阴钱袋子策略有效，建议启用' if all_pass else '⚠️ 策略未通过验证，需调整参数或禁用'}"
        )

    # 对比结论
    if dy_stats and pl_stats:
        print("\n📊 两个策略对比结论:")
        if dy_stats["avg_sharpe"] > pl_stats["avg_sharpe"]:
            print("  ✅ 双阴钱袋子夏普更高，信号质量更优")
        else:
            print("  ⚠️ 双阴钱袋子夏普低于低吸回踩，可能信号过于稀疏")
        if dy_stats["avg_return"] > pl_stats["avg_return"]:
            print("  ✅ 双阴钱袋子收益更高")
        else:
            print("  ⚠️ 双阴钱袋子收益低于低吸回踩")
        if dy_stats["total_trades"] < pl_stats["total_trades"]:
            print(
                f"  📊 双阴钱袋子交易次数更少（{dy_stats['total_trades']} vs {pl_stats['total_trades']}）"
            )


async def main():
    parser = argparse.ArgumentParser(description="双阴钱袋子策略回测验证")
    parser.add_argument("--target-count", type=int, default=30, help="回测股票数量")
    args = parser.parse_args()

    await run_comparison_backtest(target_count=args.target_count)


if __name__ == "__main__":
    asyncio.run(main())
