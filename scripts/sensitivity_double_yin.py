"""double_yin 策略参数敏感性分析：止损 0-5%，止盈 8-15%。

分析目标：找到最优的止损/止盈参数组合，使得策略在风险可控的前提下最大化收益。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest.engine import BacktestEngine  # noqa: E402
from gugu.data import data_manager  # noqa: E402
from gugu.strategies.double_yin import DoubleYinMoneyBagStrategy  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()

# 参数敏感性分析范围
STOP_LOSS_RANGE = [0.0, 0.01, 0.02, 0.03, 0.04, 0.05]  # 0% - 5%
TAKE_PROFIT_RANGE = [0.08, 0.10, 0.12, 0.15]  # 8% - 15%

# 测试股票池
TEST_STOCKS = ["600519", "000858", "002714", "300750", "002371", "002594"]
DAYS = 500

# 基准参数
BASE_PARAMS = {
    "ma_trend_window": 20,
    "ma_long_window": 60,
    "ma_slope_window": 5,
    "first_yin_vol_ratio_max": 0.9,
    "second_yin_vol_ratio_max": 0.6,
    "doji_body_max": 0.01,
    "small_yang_max_gain": 0.03,
    "breakout_vol_ratio_min": 2.0,
    "breakout_pct_min": 0.0,
    "min_confidence": 0.0,
    "stop_loss_pct": 0.0,
    "take_profit_pct": 0.08,
    "max_upper_shadow_pct": 0.02,
    "stagnation_vol_ratio": 2.5,
}


async def run_sensitivity_analysis() -> None:
    """运行参数敏感性分析。"""
    dm = data_manager()
    print(f"获取 {len(TEST_STOCKS)} 只股票历史数据（{DAYS} 天）...")
    stock_dfs = await dm.fetch_stock_history_batch(TEST_STOCKS, days=DAYS)
    stock_dfs = {s: df for s, df in stock_dfs.items() if not df.empty and len(df) >= 120}
    print(f"成功获取 {len(stock_dfs)} 只股票数据")

    if len(stock_dfs) < 3:
        print(f"有效股票数 {len(stock_dfs)} < 3，无法运行分析")
        return

    results: list[dict] = []
    total_combos = len(STOP_LOSS_RANGE) * len(TAKE_PROFIT_RANGE)
    combo_idx = 0

    for stop_loss in STOP_LOSS_RANGE:
        for take_profit in TAKE_PROFIT_RANGE:
            combo_idx += 1
            print(f"\n[{combo_idx}/{total_combos}] 止损={stop_loss:.0%}, 止盈={take_profit:.0%}")

            params = BASE_PARAMS.copy()
            params["stop_loss_pct"] = stop_loss
            params["take_profit_pct"] = take_profit

            engine = BacktestEngine()
            combo_results: list[dict] = []

            for symbol, df in stock_dfs.items():
                try:
                    strategy = DoubleYinMoneyBagStrategy(params)
                    bt_result = engine.run(strategy, df, symbol=symbol)
                    metrics = bt_result.metrics
                    combo_results.append(
                        {
                            "symbol": symbol,
                            "trades": metrics.get("total_trades", 0),
                            "return": metrics.get("total_return", 0),
                            "sharpe": metrics.get("sharpe", 0),
                            "max_drawdown": metrics.get("max_drawdown", 0),
                            "win_rate": metrics.get("win_rate", 0),
                        }
                    )
                except Exception as e:
                    logger.warning(f"回测失败 {symbol}: {e}")
                    continue

            if combo_results:
                avg_trades = sum(r["trades"] for r in combo_results) / len(combo_results)
                avg_return = sum(r["return"] for r in combo_results) / len(combo_results)
                avg_sharpe = sum(r["sharpe"] for r in combo_results) / len(combo_results)
                avg_max_dd = sum(r["max_drawdown"] for r in combo_results) / len(combo_results)
                avg_win_rate = sum(r["win_rate"] for r in combo_results) / len(combo_results)

                results.append(
                    {
                        "stop_loss_pct": stop_loss,
                        "take_profit_pct": take_profit,
                        "avg_trades": avg_trades,
                        "avg_return": avg_return,
                        "avg_sharpe": avg_sharpe,
                        "avg_max_drawdown": avg_max_dd,
                        "avg_win_rate": avg_win_rate,
                        "n_stocks": len(combo_results),
                    }
                )

                print(
                    f"  交易数: {avg_trades:.1f}, 收益: {avg_return:.2%}, "
                    f"Sharpe: {avg_sharpe:.4f}, 回撤: {avg_max_dd:.2%}, 胜率: {avg_win_rate:.2%}"
                )

    # 输出汇总结果
    print("\n" + "=" * 80)
    print("参数敏感性分析结果汇总")
    print("=" * 80)

    # 按 Sharpe 排序
    results_sorted = sorted(results, key=lambda x: x["avg_sharpe"], reverse=True)

    print(
        f"\n{'止损':>6} {'止盈':>6} {'交易数':>8} {'收益':>10} {'Sharpe':>10} {'最大回撤':>10} {'胜率':>8}"
    )
    print("-" * 70)
    for r in results_sorted:
        print(
            f"{r['stop_loss_pct']:>5.0%} {r['take_profit_pct']:>5.0%} "
            f"{r['avg_trades']:>8.1f} {r['avg_return']:>9.2%} "
            f"{r['avg_sharpe']:>10.4f} {r['avg_max_drawdown']:>9.2%} {r['avg_win_rate']:>7.2%}"
        )

    # 最优参数推荐
    best = results_sorted[0] if results_sorted else None
    if best:
        print(f"\n最优参数组合:")
        print(f"  止损: {best['stop_loss_pct']:.0%}")
        print(f"  止盈: {best['take_profit_pct']:.0%}")
        print(f"  平均交易数: {best['avg_trades']:.1f}")
        print(f"  平均收益: {best['avg_return']:.2%}")
        print(f"  平均 Sharpe: {best['avg_sharpe']:.4f}")
        print(f"  平均最大回撤: {best['avg_max_drawdown']:.2%}")
        print(f"  平均胜率: {best['avg_win_rate']:.2%}")

    # 输出 DataFrame 格式便于分析
    df_results = pd.DataFrame(results)
    df_results = df_results.sort_values("avg_sharpe", ascending=False)
    print("\n\nDataFrame 格式结果:")
    print(df_results.to_string(index=False))


if __name__ == "__main__":
    asyncio.run(run_sensitivity_analysis())
