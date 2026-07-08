"""验证已启用策略的独立回测脚本。

目标：对 settings.yaml 中启用的策略（box_breakout, double_yin）做独立回测验证，
输出每个策略在 100 只股票上的绩效统计。

用法：
    uv run python scripts/verify_enabled_strategies.py
    uv run python scripts/verify_enabled_strategies.py --max-stocks 50
    uv run python scripts/verify_enabled_strategies.py --days 365
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd

from gugu.backtest import BacktestEngine
from gugu.backtest.metrics import calc_metrics
from gugu.config import get_config
from gugu.data.manager import data_manager
from gugu.strategies.registry import get_strategy
from gugu.utils.log import get_logger

logger = get_logger()

# 测试股票池（沪深300成分股 + 自选股，覆盖大中小盘）
_TEST_SYMBOLS = [
    # 自选股
    "002460", "002714", "510300", "510500", "512480", "588000",
    "600519", "300750", "000858", "601318", "000333",
    # 沪深300 代表性股票
    "600036", "601166", "600900", "600276", "000002", "002415",
    "300059", "600887", "601012", "600030", "601398", "600028",
    "601857", "600585", "000651", "000001", "002304", "600809",
    "300015", "601888", "600690", "000538", "600104", "601668",
    "600016", "600019", "601088", "600050", "601628", "601318",
    "600519", "000858", "002007", "300124", "600196", "601211",
    "600837", "601766", "600111", "600010", "601989", "600150",
    "601390", "601186", "600585", "000725", "002230", "300433",
    "600741", "600660", "601633", "600438", "300274", "002129",
    "600745", "603501", "300782", "688981", "688036", "688008",
    "688012", "688126", "688169", "688396", "688561", "688981",
    "002371", "300661", "300750", "002812", "300014", "002460",
    "002709", "300450", "002129", "600703", "002049", "300316",
    "688005", "300751", "002850", "300724", "601899", "600547",
    "002155", "600489", "000630", "600362", "000060", "600516",
    "600392", "600010", "600111", "600259", "600549",
]


async def backtest_strategy(
    strategy_name: str,
    symbols: list[str],
    days: int,
    max_concurrency: int = 10,
) -> dict:
    """对策略在多个股票上回测。

    Returns:
        {strategy, total_stocks, metrics_summary, details}
    """
    dm = data_manager()
    engine = BacktestEngine()
    sem = asyncio.Semaphore(max_concurrency)

    try:
        strategy = get_strategy(strategy_name)
    except Exception as e:
        return {"strategy": strategy_name, "error": f"策略加载失败: {e}"}

    results = []

    async def test_one(symbol: str) -> dict | None:
        async with sem:
            try:
                df = await dm.fetch_stock_history(symbol, days=days)
                if df is None or df.empty or len(df) < 60:
                    return None
                result = engine.run(strategy, df, symbol)
                metrics = calc_metrics(result)
                return {
                    "symbol": symbol,
                    "total_return": metrics.get("total_return", 0),
                    "annual_return": metrics.get("annual_return", 0),
                    "sharpe": metrics.get("sharpe", 0),
                    "max_drawdown": metrics.get("max_drawdown", 0),
                    "win_rate": metrics.get("win_rate", 0),
                    "profit_factor": metrics.get("profit_factor", 0),
                    "total_trades": metrics.get("total_trades", 0),
                    "avg_hold_days": metrics.get("avg_hold_days", 0),
                }
            except Exception as e:
                logger.debug(f"{symbol} 回测失败: {e}")
                return None

    tasks = [test_one(sym) for sym in symbols]
    done = await asyncio.gather(*tasks)
    results = [r for r in done if r is not None]

    if not results:
        return {"strategy": strategy_name, "error": "无有效回测结果", "total_stocks": 0}

    df = pd.DataFrame(results)
    profitable = [r for r in results if r["total_return"] > 0]

    summary = {
        "strategy": strategy_name,
        "total_stocks": len(results),
        "profitable_count": len(profitable),
        "profitable_ratio": round(len(profitable) / len(results), 2),
        "metrics": {
            "total_return": {
                "mean": round(float(df["total_return"].mean()), 4),
                "median": round(float(df["total_return"].median()), 4),
                "std": round(float(df["total_return"].std()), 4),
                "best": round(float(df["total_return"].max()), 4),
                "worst": round(float(df["total_return"].min()), 4),
            },
            "sharpe": {
                "mean": round(float(df["sharpe"].mean()), 4),
                "median": round(float(df["sharpe"].median()), 4),
                "positive_ratio": round(float((df["sharpe"] > 0).mean()), 4),
            },
            "win_rate": {
                "mean": round(float(df["win_rate"].mean()), 4),
                "median": round(float(df["win_rate"].median()), 4),
            },
            "max_drawdown": {
                "mean": round(float(df["max_drawdown"].mean()), 4),
                "median": round(float(df["max_drawdown"].median()), 4),
            },
            "total_trades": {
                "mean": round(float(df["total_trades"].mean()), 1),
                "median": round(float(df["total_trades"].median()), 1),
            },
        },
        "top5": sorted(results, key=lambda x: x["total_return"], reverse=True)[:5],
        "bottom5": sorted(results, key=lambda x: x["total_return"])[:5],
    }
    return summary


def print_report(summary: dict) -> None:
    """打印回测报告。"""
    if "error" in summary:
        print(f"\n❌ {summary.get('strategy')}: {summary['error']}")
        return

    print(f"\n{'='*60}")
    print(f"  策略: {summary['strategy']}")
    print(f"  测试股票: {summary['total_stocks']} 只")
    print(f"  盈利比例: {summary['profitable_count']}/{summary['total_stocks']} ({summary['profitable_ratio']:.0%})")
    print(f"{'='*60}")

    m = summary["metrics"]
    print(f"\n  总收益: 均值 {m['total_return']['mean']:.2%} | "
          f"中位数 {m['total_return']['median']:.2%} | "
          f"最佳 {m['total_return']['best']:.2%} | "
          f"最差 {m['total_return']['worst']:.2%}")
    print(f"  夏普比: 均值 {m['sharpe']['mean']:.2f} | "
          f"中位数 {m['sharpe']['median']:.2f} | "
          f"夏普>0占比 {m['sharpe']['positive_ratio']:.0%}")
    print(f"  胜  率: 均值 {m['win_rate']['mean']:.1%} | "
          f"中位数 {m['win_rate']['median']:.1%}")
    print(f"  最大回撤: 均值 {m['max_drawdown']['mean']:.2%} | "
          f"中位数 {m['max_drawdown']['median']:.2%}")
    print(f"  交易次数: 均值 {m['total_trades']['mean']:.0f} | "
          f"中位数 {m['total_trades']['median']:.0f}")

    print(f"\n  🏆 TOP5:")
    for r in summary["top5"]:
        print(f"    {r['symbol']}: 收益 {r['total_return']:.2%} | "
              f"夏普 {r['sharpe']:.2f} | 胜率 {r['win_rate']:.0%}")

    print(f"\n  📉 BOTTOM5:")
    for r in summary["bottom5"]:
        print(f"    {r['symbol']}: 收益 {r['total_return']:.2%} | "
              f"夏普 {r['sharpe']:.2f} | 胜率 {r['win_rate']:.0%}")
    print()


async def main():
    parser = argparse.ArgumentParser(description="验证已启用策略回测")
    parser.add_argument("--max-stocks", type=int, default=100, help="最大测试股票数")
    parser.add_argument("--days", type=int, default=365, help="回测天数")
    parser.add_argument("--output", type=str, default="", help="输出JSON文件路径")
    args = parser.parse_args()

    # 获取已启用策略
    try:
        enabled = list(get_config().strategies.enabled)
    except Exception:
        enabled = ["box_breakout", "double_yin"]

    symbols = _TEST_SYMBOLS[:args.max_stocks]
    print(f"\n📊 验证已启用策略: {enabled}")
    print(f"📈 测试股票: {len(symbols)} 只, 回测天数: {args.days}")

    all_results = {}
    for strategy_name in enabled:
        print(f"\n⏳ 回测 {strategy_name}...")
        start = time.time()
        summary = await backtest_strategy(strategy_name, symbols, args.days)
        elapsed = time.time() - start
        summary["elapsed_seconds"] = round(elapsed, 2)
        print_report(summary)
        all_results[strategy_name] = summary

    # 策略对比
    print(f"\n{'='*60}")
    print("  策略对比")
    print(f"{'='*60}")
    print(f"  {'策略':<20} {'盈利比':>8} {'收益均值':>10} {'夏普均值':>10} {'胜率均值':>8} {'交易次数':>8}")
    print(f"  {'-'*64}")
    for name, s in all_results.items():
        if "error" in s:
            continue
        m = s["metrics"]
        print(f"  {name:<20} {s['profitable_ratio']:>7.0%} "
              f"{m['total_return']['mean']:>9.2%} "
              f"{m['sharpe']['mean']:>9.2f} "
              f"{m['win_rate']['mean']:>7.1%} "
              f"{m['total_trades']['mean']:>7.0f}")

    # 输出JSON
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n💾 结果已保存到: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())