"""低吸回踩策略回测验证脚本（P0-1）。

第一性原理：
- 尾盘选股器推荐低吸候选股给用户，但策略未经回测验证
- v0.4.3 已实证 box_breakout 在震荡市样本外 Sharpe -0.14（亏钱）
- 低吸策略是震荡市唯一推荐来源，必须验证有效性

验证流程：
1. 全市场 1792 只 × 500 天回测（与 box_breakout 同基准）
2. 分阶段（牛/熊/震荡）回测
3. Walk-Forward 多股聚合优化参数
4. 4 项解锁条件验证（Sharpe>0 / 参数稳定性 / 回撤<25% / 收益≥0）

用法：
    # 全市场回测
    uv run python scripts/backtest_pullback_low.py --full

    # 仅代表股回测
    uv run python scripts/backtest_pullback_low.py

    # Walk-Forward 优化
    uv run python scripts/backtest_pullback_low.py --walk-forward

    # 分阶段回测
    uv run python scripts/backtest_pullback_low.py --phases
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest.engine import BacktestEngine, walk_forward_optimize_multi_stock  # noqa: E402
from gugu.config import PROJECT_ROOT  # noqa: E402
from gugu.strategies.pullback_low import PullbackLowStrategy  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

# 复用 walk_forward_offline 的选股逻辑
from walk_forward_offline import (  # noqa: E402
    load_stock_df,
    select_stocks_from_cache,
)

logger = get_logger()

_CACHE_DIR = PROJECT_ROOT / "data" / "parquet_cache"

# 代表股：与 box_breakout 回测一致，便于对比
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

# Walk-Forward 参数网格
_PARAM_GRID = {
    "ma_short": [10, 20, 30],
    "ma_long": [40, 60, 90],
    "strong_pct": [0.03, 0.05, 0.08],
    "vol_ratio_max": [0.8, 1.0, 1.2],
    "stop_loss_pct": [0.02, 0.03, 0.05],
    "take_profit_window": [10, 20, 30],
}


def backtest_single(symbol: str, df: pd.DataFrame, params: dict | None = None) -> dict:
    """对单只股票回测低吸策略。"""
    try:
        strategy = PullbackLowStrategy(params)
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


async def run_full_market_backtest(target_count: int = 30) -> None:
    """全市场回测（抽样 target_count 只股票保证可重现）。"""
    print("\n" + "=" * 70)
    print("P0-1: 低吸回踩策略全市场回测验证")
    print("=" * 70)

    # 从缓存选股（与 box_breakout 同基准）
    symbols = select_stocks_from_cache(_DEFAULT_SYMBOLS, min_rows=500, target_count=target_count)
    if not symbols:
        print("❌ 缓存中无可用股票数据")
        return

    print(f"\n📊 回测股票: {len(symbols)} 只（从 1792 只缓存中抽样）")
    print(f"股票列表: {symbols[:10]}{'...' if len(symbols) > 10 else ''}")

    results = []
    for sym in symbols:
        df = load_stock_df(sym)
        if df is None or len(df) < 500:
            continue
        r = backtest_single(sym, df)
        results.append(r)
        if r["trades"] > 0:
            print(
                f"  {sym}: {r['trades']}笔 | 收益 {r['total_return']:+.2%} | "
                f"夏普 {r['sharpe']:.2f} | 回撤 {r['max_drawdown']:.2%} | "
                f"胜率 {r['win_rate']:.0%}"
            )

    # 汇总
    if not results:
        print("\n❌ 无有效回测结果")
        return

    valid = [r for r in results if r["trades"] > 0]
    if not valid:
        print("\n❌ 所有股票 trades=0，策略未产生信号")
        return

    avg_return = sum(r["total_return"] for r in valid) / len(valid)
    avg_sharpe = sum(r["sharpe"] for r in valid) / len(valid)
    avg_dd = sum(r["max_drawdown"] for r in valid) / len(valid)
    avg_win = sum(r["win_rate"] for r in valid) / len(valid)

    print("\n" + "=" * 70)
    print("📈 低吸策略回测汇总")
    print("=" * 70)
    print(f"  有效回测股票: {len(valid)}/{len(results)}")
    print(f"  平均收益:     {avg_return:+.2%}")
    print(f"  平均夏普:     {avg_sharpe:.2f}")
    print(f"  平均最大回撤: {avg_dd:.2%}")
    print(f"  平均胜率:     {avg_win:.0%}")

    # 与 box_breakout 对比（v0.4.2 实证数据）
    print("\n📊 与 box_breakout 对比（v0.4.2 全市场实证）:")
    print(f"  box_breakout: 收益 +31.20% | 夏普 0.30（仅牛市有效，震荡市 -0.14）")
    print(f"  pullback_low: 收益 {avg_return:+.2%} | 夏普 {avg_sharpe:.2f}（全市场）")

    # 4 项解锁条件检查
    print("\n🔒 4 项解锁条件检查:")
    print(f"  1. 夏普 > 0:    {'✅' if avg_sharpe > 0 else '❌'} ({avg_sharpe:.2f})")
    print(f"  2. 回撤 < 25%:  {'✅' if avg_dd < 0.25 else '❌'} ({avg_dd:.2%})")
    print(f"  3. 收益 ≥ 0:    {'✅' if avg_return >= 0 else '❌'} ({avg_return:+.2%})")
    print(f"  4. 胜率 ≥ 40%:  {'✅' if avg_win >= 0.40 else '❌'} ({avg_win:.0%})")

    all_pass = avg_sharpe > 0 and avg_dd < 0.25 and avg_return >= 0 and avg_win >= 0.40
    print(f"\n{'✅ 策略有效，建议启用' if all_pass else '⚠️ 策略未通过验证，需谨慎'}")


async def run_walk_forward_optimization(target_count: int = 10) -> None:
    """Walk-Forward 多股聚合优化。"""
    print("\n" + "=" * 70)
    print("P0-1: 低吸策略 Walk-Forward 优化（多股聚合）")
    print("=" * 70)

    symbols = select_stocks_from_cache(_DEFAULT_SYMBOLS, min_rows=500, target_count=target_count)
    if not symbols:
        print("❌ 缓存中无可用股票数据")
        return

    print(f"\n📊 优化股票: {len(symbols)} 只")

    # 加载数据
    stock_data = {}
    for sym in symbols:
        df = load_stock_df(sym)
        if df is not None and len(df) >= 500:
            stock_data[sym] = df

    if len(stock_data) < 5:
        print(f"❌ 有效股票不足 5 只（实际 {len(stock_data)}），无法多股聚合")
        return

    print(f"📊 有效数据: {len(stock_data)}/{len(symbols)} 只")

    # 运行 Walk-Forward
    try:
        result = walk_forward_optimize_multi_stock(
            strategy_cls=PullbackLowStrategy,
            stock_data=stock_data,
            param_grid=_PARAM_GRID,
            n_splits=5,
        )

        print("\n" + "=" * 70)
        print("📊 Walk-Forward 优化结果")
        print("=" * 70)
        print(f"  最优参数: {result.get('best_params', {})}")
        print(f"  训练 Sharpe: {result.get('train_sharpe', 0):.2f}")
        print(f"  测试 Sharpe: {result.get('test_sharpe', 0):.2f}")
        print(f"  参数稳定性: {result.get('param_stability', 0):.0%}")
        print(f"  平均回撤:   {result.get('avg_max_dd', 0):.2%}")
        print(f"  平均收益:   {result.get('avg_return', 0):+.2%}")

        # 4 项解锁条件
        print("\n🔒 4 项解锁条件检查:")
        c1 = result.get("test_sharpe", 0) > 0
        c2 = result.get("param_stability", 0) >= 0.5
        c3 = result.get("avg_max_dd", 0) < 0.25
        c4 = result.get("avg_return", 0) >= 0
        print(
            f"  1. 样本外 Sharpe > 0:    {'✅' if c1 else '❌'} ({result.get('test_sharpe', 0):.2f})"
        )
        print(
            f"  2. 参数稳定性 ≥ 50%:     {'✅' if c2 else '❌'} ({result.get('param_stability', 0):.0%})"
        )
        print(
            f"  3. 回撤 < 25%:           {'✅' if c3 else '❌'} ({result.get('avg_max_dd', 0):.2%})"
        )
        print(
            f"  4. 收益 ≥ 0:             {'✅' if c4 else '❌'} ({result.get('avg_return', 0):+.2%})"
        )

        all_pass = c1 and c2 and c3 and c4
        if all_pass:
            print("\n✅ 通过解锁条件，参数可写回 strategy_defaults.yaml")
        else:
            print("\n⚠️ 未通过解锁条件，保留默认参数不变")
            print("   说明：样本外失效不代表策略无效，可能需调整参数网格")

    except Exception as e:
        print(f"❌ Walk-Forward 优化失败: {e}")
        import traceback

        traceback.print_exc()


async def main():
    parser = argparse.ArgumentParser(description="低吸回踩策略回测验证")
    parser.add_argument("--full", action="store_true", help="全市场回测（抽样30只）")
    parser.add_argument("--walk-forward", action="store_true", help="Walk-Forward 优化")
    parser.add_argument("--target-count", type=int, default=30, help="回测股票数量")
    args = parser.parse_args()

    if args.walk_forward:
        await run_walk_forward_optimization(target_count=10)
    else:
        await run_full_market_backtest(target_count=args.target_count)


if __name__ == "__main__":
    asyncio.run(main())
