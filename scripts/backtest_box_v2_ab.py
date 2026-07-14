"""box_breakout v1 vs v2 A/B 对比回测。

比较 v1（原版：卖出量能确认+无趋势过滤+无最大持仓）和 v2（优化版）的表现差异。

用法:
    uv run python scripts/backtest_box_v2_ab.py --max-stocks 50
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import numpy as np
import pandas as pd

from gugu.backtest import BacktestEngine
from gugu.strategies.breakout import BoxBreakoutStrategy
from gugu.utils.log import get_logger

logger = get_logger()

_CACHE_DIR = Path("data/parquet_cache")


def load_cached_stocks(max_stocks: int, min_days: int = 200) -> dict[str, pd.DataFrame]:
    """从 parquet 缓存加载股票数据。"""
    stocks: dict[str, pd.DataFrame] = {}
    for p in sorted(_CACHE_DIR.glob("*_365d.parquet")):
        code = p.stem.split("_")[0]
        if code.startswith(("43", "83", "87", "88", "4", "8", "bj")):
            continue
        try:
            df = pd.read_parquet(p)
            if len(df) >= min_days:
                stocks[code] = df
                if len(stocks) >= max_stocks:
                    break
        except Exception:
            continue
    logger.info(f"加载 {len(stocks)} 只缓存股票")
    return stocks


def run_v1(stock_dfs: dict[str, pd.DataFrame]) -> list[dict]:
    """v1 配置：原版参数（无趋势过滤、卖出量能确认、无最大持仓）。

    通过 params 覆盖来模拟 v1 行为：
    - trend_filter=False → 不做趋势过滤
    - max_hold_days=0 → 不做时间止损
    注意：卖出量能确认的 bug 已在 v2 代码中修复，无法通过参数恢复。
    因此 v1 结果实际上是 "v1卖出修复 + v1趋势/持仓"，用于隔离各项优化的贡献。
    """
    # v1 参数：关闭 v2 新增功能
    v1_params = {
        "trend_filter": False,
        "max_hold_days": 0,
    }
    strategy = BoxBreakoutStrategy(v1_params)
    engine = BacktestEngine()
    results: list[dict] = []
    for symbol, df in stock_dfs.items():
        try:
            result = engine.run(strategy, df, symbol)
            results.append({
                "symbol": symbol,
                "trades": len(result.trades),
                "return": result.metrics.get("total_return", 0),
                "sharpe": result.metrics.get("sharpe", 0),
                "max_drawdown": result.metrics.get("max_drawdown", 0),
                "win_rate": result.metrics.get("win_rate", 0),
            })
        except Exception as e:
            logger.debug(f"{symbol} v1 失败: {e}")
    return results


def run_v2(stock_dfs: dict[str, pd.DataFrame]) -> list[dict]:
    """v2 配置：趋势过滤 + 最大持仓 + 卖出修复。"""
    strategy = BoxBreakoutStrategy()  # 使用默认 v2 参数
    engine = BacktestEngine()
    results: list[dict] = []
    for symbol, df in stock_dfs.items():
        try:
            result = engine.run(strategy, df, symbol)
            results.append({
                "symbol": symbol,
                "trades": len(result.trades),
                "return": result.metrics.get("total_return", 0),
                "sharpe": result.metrics.get("sharpe", 0),
                "max_drawdown": result.metrics.get("max_drawdown", 0),
                "win_rate": result.metrics.get("win_rate", 0),
            })
        except Exception as e:
            logger.debug(f"{symbol} v2 失败: {e}")
    return results


def run_v2_no_trend(stock_dfs: dict[str, pd.DataFrame]) -> list[dict]:
    """v2 卖出修复 + 最大持仓，但无趋势过滤。

    用于隔离"卖出修复"的独立贡献。
    """
    params = {"trend_filter": False, "max_hold_days": 30}
    strategy = BoxBreakoutStrategy(params)
    engine = BacktestEngine()
    results: list[dict] = []
    for symbol, df in stock_dfs.items():
        try:
            result = engine.run(strategy, df, symbol)
            results.append({
                "symbol": symbol,
                "trades": len(result.trades),
                "return": result.metrics.get("total_return", 0),
                "sharpe": result.metrics.get("sharpe", 0),
                "max_drawdown": result.metrics.get("max_drawdown", 0),
                "win_rate": result.metrics.get("win_rate", 0),
            })
        except Exception as e:
            logger.debug(f"{symbol} v2_no_trend 失败: {e}")
    return results


def summarize(results: list[dict], label: str) -> dict:
    """汇总结果。"""
    if not results:
        return {"label": label, "samples": 0}
    returns = [r["return"] for r in results]
    sharpes = [r["sharpe"] for r in results]
    drawdowns = [r["max_drawdown"] for r in results]
    trades = [r["trades"] for r in results]
    profitable = sum(1 for r in returns if r > 0)
    stocks_with_trades = sum(1 for t in trades if t > 0)
    return {
        "label": label,
        "samples": len(results),
        "stocks_with_trades": stocks_with_trades,
        "total_trades": sum(trades),
        "avg_return": round(float(np.mean(returns)), 4),
        "median_return": round(float(np.median(returns)), 4),
        "avg_sharpe": round(float(np.mean(sharpes)), 4),
        "max_drawdown": round(float(np.max(drawdowns)), 4),
        "profitable_ratio": round(profitable / len(returns), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="box_breakout v1 vs v2 A/B 回测")
    parser.add_argument("--max-stocks", type=int, default=50)
    parser.add_argument("--min-days", type=int, default=200)
    args = parser.parse_args()

    stock_dfs = load_cached_stocks(args.max_stocks, args.min_days)
    if not stock_dfs:
        logger.error("无缓存数据")
        return

    print(f"\n{'='*70}")
    print(f"box_breakout v1 vs v2 A/B 对比回测")
    print(f"股票数: {len(stock_dfs)} | 最小数据天数: {args.min_days}")
    print(f"{'='*70}\n")

    # 运行三组实验
    t0 = time.time()
    logger.info("运行 v1（卖出修复+无趋势+无持仓限制）...")
    v1_results = run_v1(stock_dfs)

    logger.info("运行 v2_no_trend（卖出修复+无趋势+最大持仓30天）...")
    v2_nt_results = run_v2_no_trend(stock_dfs)

    logger.info("运行 v2（卖出修复+趋势过滤+最大持仓30天）...")
    v2_results = run_v2(stock_dfs)
    elapsed = time.time() - t0

    # 汇总
    v1_sum = summarize(v1_results, "v1: 卖出修复+无趋势+无持仓限制")
    v2_nt_sum = summarize(v2_nt_results, "v2_no_trend: 卖出修复+无趋势+持仓30天")
    v2_sum = summarize(v2_results, "v2: 卖出修复+趋势过滤+持仓30天")

    print(f"\n{'='*70}")
    print(f"A/B 对比结果（耗时 {elapsed:.0f}s）")
    print(f"{'='*70}")
    print(f"{'指标':<20} {'v1(卖出修复+无趋势)':<25} {'v2_NT(+持仓30天)':<25} {'v2(全优化)':<25}")
    print(f"{'-'*95}")
    for key in ["stocks_with_trades", "total_trades", "avg_return", "median_return",
                "avg_sharpe", "max_drawdown", "profitable_ratio"]:
        v1_val = v1_sum.get(key, "N/A")
        v2nt_val = v2_nt_sum.get(key, "N/A")
        v2_val = v2_sum.get(key, "N/A")
        print(f"{key:<20} {str(v1_val):<25} {str(v2nt_val):<25} {str(v2_val):<25}")

    # 逐股对比
    print(f"\n{'='*70}")
    print("逐股对比（仅有交易的股票）")
    print(f"{'='*70}")
    print(f"{'股票':<10} {'v1交易':>6} {'v1收益':>8} {'v2交易':>6} {'v2收益':>8} {'差异':>8}")
    print(f"{'-'*50}")
    for v1, v2 in zip(v1_results, v2_results, strict=True):
        if v1["trades"] > 0 or v2["trades"] > 0:
            diff = v2["return"] - v1["return"]
            print(f"{v1['symbol']:<10} {v1['trades']:>6} {v1['return']*100:>7.2f}% {v2['trades']:>6} {v2['return']*100:>7.2f}% {diff*100:>+7.2f}%")


if __name__ == "__main__":
    main()
