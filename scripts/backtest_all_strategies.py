"""全市场多策略批量回测脚本。

目标：验证所有启用策略在全市场的有效性，输出对比报告。

设计：
1. 从 akshare 获取全市场股票列表（约 5000 只）
2. 每只股票取 200 天历史数据
3. 对每只股票跑所有启用策略（7 个）
4. 汇总每个策略的胜率、收益、夏普、最大回撤
5. 按指标排序输出对比表

用法:
    uv run python scripts/backtest_all_strategies.py
    uv run python scripts/backtest_all_strategies.py --max-stocks 100  # 限制股票数（调试用）
    uv run python scripts/backtest_all_strategies.py --days 200 --workers 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import akshare as ak
import pandas as pd

from gugu.backtest import BacktestEngine
from gugu.backtest.metrics import calc_metrics
from gugu.config import get_config
from gugu.data.manager import data_manager
from gugu.strategies.registry import get_strategy, list_strategies
from gugu.utils.log import get_logger

logger = get_logger()

# 性能参数
_STOCK_CONCURRENCY = 10  # 数据获取并发度
_KLINE_TIMEOUT = 15      # 单股 K 线获取超时秒
_MIN_DATA_DAYS = 100     # 最少数据天数（不足跳过）


def get_enabled_strategies() -> list[str]:
    """从 settings.yaml 读取启用的策略列表。"""
    try:
        strategies = get_config().strategies.enabled
        if strategies:
            return strategies
    except Exception:
        pass
    # 默认回退
    return ["box_breakout", "turtle", "dual_ma", "bollinger", "rsi_reversal"]


async def fetch_stock_list(max_stocks: int | None = None) -> list[str]:
    """获取全市场股票列表，过滤 ST/北交所/新股。"""
    try:
        spot = await asyncio.to_thread(ak.stock_zh_a_spot_em)
    except Exception as e:
        logger.warning(f"stock_zh_a_spot_em 失败，降级 stock_zh_a_spot: {e}")
        spot = await asyncio.to_thread(ak.stock_zh_a_spot)

    # 标准化列名
    col_map = {
        "code": "代码", "name": "名称",
    }
    spot = spot.rename(columns={k: v for k, v in col_map.items() if k in spot.columns})

    # 过滤
    spot["代码"] = spot["代码"].astype(str)
    mask = ~spot["代码"].str.startswith(("bj", "43", "83", "87", "88", "4", "8"))
    mask &= ~spot["名称"].str.contains("ST|退|N|^C", na=False, regex=True)
    # 排除科创板（688）和创业板（300）— 走势差异大，单独回测更准
    # 这里保留，全市场回测
    candidates = spot[mask]["代码"].tolist()

    if max_stocks and len(candidates) > max_stocks:
        candidates = candidates[:max_stocks]
        logger.info(f"限制为前 {max_stocks} 只股票（调试模式）")

    logger.info(f"全市场候选股票: {len(candidates)} 只")
    return candidates


async def fetch_stock_data(symbol: str, days: int) -> pd.DataFrame | None:
    """获取单股历史数据，失败返回 None。"""
    try:
        async with asyncio.timeout(_KLINE_TIMEOUT):
            dm = data_manager()
            df = await dm.fetch_stock_history(symbol, days=days)
            if len(df) < _MIN_DATA_DAYS:
                return None
            return df
    except (TimeoutError, Exception) as e:
        logger.debug(f"获取 {symbol} 数据失败: {e}")
        return None


async def backtest_one_stock(
    symbol: str,
    strategies: list[str],
    days: int,
    engine: BacktestEngine,
) -> dict[str, dict]:
    """对单股跑所有策略，返回 {策略名: metrics}。"""
    df = await fetch_stock_data(symbol, days)
    if df is None:
        return {}

    results = {}
    for strategy_name in strategies:
        try:
            strategy = get_strategy(strategy_name)
            bt_result = engine.run(strategy, df, symbol)
            results[strategy_name] = bt_result.metrics
        except Exception as e:
            logger.debug(f"{symbol} 策略 {strategy_name} 失败: {e}")
            results[strategy_name] = {}
    return results


async def run_batch_backtest(
    symbols: list[str],
    strategies: list[str],
    days: int,
) -> dict[str, list[dict]]:
    """批量回测，返回 {策略名: [metrics_dict, ...]}。"""
    # 每个策略一个结果列表
    all_results: dict[str, list[dict]] = {s: [] for s in strategies}
    engine = BacktestEngine()

    sem = asyncio.Semaphore(_STOCK_CONCURRENCY)

    async def _one(symbol: str) -> dict[str, dict]:
        async with sem:
            return await backtest_one_stock(symbol, strategies, days, engine)

    tasks = [_one(sym) for sym in symbols]
    total = len(tasks)
    completed = 0
    t0 = time.time()

    for coro in asyncio.as_completed(tasks):
        result = await coro
        for strategy_name, metrics in result.items():
            if metrics:
                all_results[strategy_name].append(metrics)
        completed += 1
        if completed % 50 == 0 or completed == total:
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (total - completed) / rate if rate > 0 else 0
            logger.info(
                f"回测进度: {completed}/{total} "
                f"({completed/total*100:.1f}%) "
                f"速率: {rate:.1f}/s "
                f"ETA: {eta/60:.1f}分钟"
            )

    return all_results


def aggregate_metrics(results: list[dict]) -> dict:
    """聚合单策略多股的 metrics，输出统计摘要。"""
    if not results:
        return {}

    # 提取每个指标
    metric_keys = [
        "total_return", "annual_return", "sharpe", "max_drawdown",
        "win_rate", "profit_factor", "total_trades", "avg_hold_days",
    ]

    summary = {}
    for key in metric_keys:
        values = [r.get(key, 0.0) for r in results if r.get(key) is not None]
        if not values:
            summary[key] = {"mean": 0, "median": 0, "std": 0, "n": 0}
            continue
        series = pd.Series(values)
        summary[key] = {
            "mean": float(series.mean()),
            "median": float(series.median()),
            "std": float(series.std()),
            "min": float(series.min()),
            "max": float(series.max()),
            "n": len(values),
        }

    # 额外统计
    summary["_sample_count"] = len(results)
    summary["_win_rate_mean"] = summary.get("win_rate", {}).get("mean", 0)
    summary["_return_mean"] = summary.get("total_return", {}).get("mean", 0)
    summary["_sharpe_mean"] = summary.get("sharpe", {}).get("mean", 0)
    summary["_max_dd_mean"] = summary.get("max_drawdown", {}).get("mean", 0)
    # 盈利股比例
    profitable = sum(1 for r in results if r.get("total_return", 0) > 0)
    summary["_profitable_ratio"] = profitable / len(results) if results else 0
    return summary


def print_report(
    all_summaries: dict[str, dict],
    n_stocks: int,
    days: int,
    elapsed: float,
) -> None:
    """打印对比报告。"""
    print()
    print("=" * 110)
    print(f"全市场策略回测对比报告 — {date.today()}")
    print(f"股票数: {n_stocks} | 回测天数: {days} | 耗时: {elapsed/60:.1f} 分钟")
    print("=" * 110)

    # 表头
    print(
        f"{'策略':<25}{'样本':<8}{'总收益%':<12}{'年化%':<12}"
        f"{'夏普':<10}{'最大回撤%':<12}{'胜率%':<10}"
        f"{'盈利比%':<10}{'平均交易':<10}{'持仓天数':<10}"
    )
    print("-" * 110)

    # 按"总收益"排序
    sorted_strategies = sorted(
        all_summaries.items(),
        key=lambda x: x[1].get("_return_mean", 0),
        reverse=True,
    )

    for name, summary in sorted_strategies:
        if not summary:
            continue
        n = summary.get("_sample_count", 0)
        if n == 0:
            print(f"{name:<25}{'无数据':<8}")
            continue
        ret = summary.get("total_return", {})
        ann = summary.get("annual_return", {})
        sharpe = summary.get("sharpe", {})
        dd = summary.get("max_drawdown", {})
        wr = summary.get("win_rate", {})
        pf = summary.get("profit_factor", {})
        tt = summary.get("total_trades", {})
        hd = summary.get("avg_hold_days", {})

        print(
            f"{name:<25}{n:<8}"
            f"{ret.get('mean', 0)*100:+.2f}/M{ret.get('median', 0)*100:+.2f}    "
            f"{ann.get('mean', 0)*100:+.2f}    "
            f"{sharpe.get('mean', 0):.2f}      "
            f"{dd.get('mean', 0)*100:.2f}      "
            f"{wr.get('mean', 0)*100:.1f}      "
            f"{summary.get('_profitable_ratio', 0)*100:.0f}        "
            f"{tt.get('mean', 0):.0f}        "
            f"{hd.get('mean', 0):.1f}"
        )

    print()
    print("说明: M=中位数, 盈利比=正收益股票占比")
    print()

    # 策略间对比结论
    print("=" * 110)
    print("策略评估结论")
    print("=" * 110)
    if not sorted_strategies:
        print("无数据")
        return

    best_name, best_summary = sorted_strategies[0]
    worst_name, worst_summary = sorted_strategies[-1]

    print(f"最佳策略: {best_name}")
    print(f"  总收益均值: {best_summary.get('_return_mean', 0)*100:+.2f}%")
    print(f"  夏普均值: {best_summary.get('_sharpe_mean', 0):.2f}")
    print(f"  胜率均值: {best_summary.get('_win_rate_mean', 0)*100:.1f}%")
    print(f"  盈利比: {best_summary.get('_profitable_ratio', 0)*100:.0f}%")
    print()
    print(f"最差策略: {worst_name}")
    print(f"  总收益均值: {worst_summary.get('_return_mean', 0)*100:+.2f}%")
    print(f"  夏普均值: {worst_summary.get('_sharpe_mean', 0):.2f}")
    print()
    print("建议:")
    for name, summary in sorted_strategies:
        ret = summary.get("_return_mean", 0)
        sharpe = summary.get("_sharpe_mean", 0)
        wr = summary.get("_win_rate_mean", 0)
        if ret > 0 and sharpe > 0.5:
            print(f"  ✓ {name}: 收益+{ret*100:.2f}%, 夏普{sharpe:.2f} → 建议保留")
        elif ret < -0.05 or sharpe < 0:
            print(f"  ✗ {name}: 收益{ret*100:+.2f}%, 夏普{sharpe:.2f} → 建议禁用或调优")
        else:
            print(f"  ? {name}: 收益{ret*100:+.2f}%, 夏普{sharpe:.2f} → 边缘，需观察")


async def main_async(args: argparse.Namespace) -> None:
    """主流程。"""
    t0 = time.time()

    # 1. 获取策略列表
    strategies = get_enabled_strategies()
    if args.strategies:
        strategies = args.strategies.split(",")
    logger.info(f"待回测策略: {strategies}")

    # 2. 获取股票列表
    symbols = await fetch_stock_list(args.max_stocks)
    if not symbols:
        logger.error("无法获取股票列表")
        return

    # 3. 批量回测
    logger.info(f"开始批量回测: {len(symbols)} 只股票 × {len(strategies)} 策略 × {args.days} 天")
    all_results = await run_batch_backtest(symbols, strategies, args.days)

    # 4. 汇总
    all_summaries = {}
    for strategy_name, metrics_list in all_results.items():
        all_summaries[strategy_name] = aggregate_metrics(metrics_list)

    # 5. 打印报告
    elapsed = time.time() - t0
    print_report(all_summaries, len(symbols), args.days, elapsed)

    # 6. 保存 JSON
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        full_data = {
            "date": str(date.today()),
            "n_stocks": len(symbols),
            "days": args.days,
            "elapsed_sec": elapsed,
            "strategies": all_summaries,
        }
        output_path.write_text(
            json.dumps(full_data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info(f"报告已保存: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="全市场多策略批量回测")
    parser.add_argument("--max-stocks", type=int, default=None, help="限制股票数（调试用）")
    parser.add_argument("--days", type=int, default=200, help="回测历史天数")
    parser.add_argument("--strategies", type=str, default=None, help="逗号分隔的策略名，空则用 settings.yaml")
    parser.add_argument("--output", type=str, default="data/backtest_report.json", help="输出 JSON 路径")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
