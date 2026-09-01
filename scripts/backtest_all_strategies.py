"""全市场多策略批量回测脚本。

目标：验证所有启用策略在全市场的有效性，输出对比报告。

设计：
1. 从 akshare 获取全市场股票列表（约 5000 只）
2. 每只股票取 200 天历史数据
3. 对每只股票跑 settings.yaml 中实际启用的策略
4. 汇总每个策略的胜率、收益、夏普、最大回撤
5. 按指标排序输出对比表

用法:
    uv run python scripts/backtest_all_strategies.py
    uv run python scripts/backtest_all_strategies.py --max-stocks 100  # 限制股票数（调试用）
    uv run python scripts/backtest_all_strategies.py --days 200 --workers 10
    uv run python scripts/backtest_all_strategies.py --max-stocks 100 --report  # 回测后生成报告
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
import numpy as np
import pandas as pd

from gugu.backtest import BacktestEngine
from gugu.backtest.metrics import calc_metrics
from gugu.config import get_config
from gugu.data.manager import data_manager
from gugu.research.ledger import append_record, build_record
from gugu.strategies.registry import get_runtime_strategy_names, get_strategy, list_strategies
from gugu.utils.atomic import atomic_write_json
from gugu.utils.log import get_logger

logger = get_logger()

# 性能参数
_STOCK_CONCURRENCY = 10  # 数据获取并发度
_KLINE_TIMEOUT = 15  # 单股 K 线获取超时秒
_MIN_DATA_DAYS = 100  # 最少数据天数（不足跳过）

# 需要计算分布统计的指标
_DISTRIBUTION_METRICS = [
    "total_return",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "total_trades",
    "avg_hold_days",
]


def get_enabled_strategies() -> list[str]:
    """读取当前执行模式经过准入门禁的信号策略。"""
    return get_runtime_strategy_names()


async def fetch_stock_list(max_stocks: int | None = None) -> list[str]:
    """获取全市场股票列表，过滤 ST/北交所/新股。

    优先 akshare 实时接口；失败时降级从 parquet_cache 目录读取已缓存股票代码。
    """
    candidates: list[str] = []
    try:
        spot = await asyncio.to_thread(ak.stock_zh_a_spot_em)
        # 标准化列名
        col_map = {"code": "代码", "name": "名称"}
        spot = spot.rename(columns={k: v for k, v in col_map.items() if k in spot.columns})
        # 过滤
        spot["代码"] = spot["代码"].astype(str)
        mask = ~spot["代码"].str.startswith(("bj", "43", "83", "87", "88", "4", "8"))
        mask &= ~spot["名称"].str.contains("ST|退|N|^C", na=False, regex=True)
        candidates = spot[mask]["代码"].tolist()
        logger.info(f"akshare 获取候选股票: {len(candidates)} 只")
    except Exception as e:
        logger.warning(f"akshare 股票列表获取失败: {e}")

    # 降级：从 parquet_cache 读取已缓存股票代码
    if not candidates:
        cache_dir = Path("data/parquet_cache")
        if cache_dir.exists():
            # 文件名格式：{code}_{days}d.parquet
            # 优先 500d（全量回测），降级 250d
            codes = set()
            for pattern in ("*_500d.parquet", "*_250d.parquet"):
                for p in cache_dir.glob(pattern):
                    code = p.stem.split("_")[0]
                    if not code.startswith(("43", "83", "87", "88", "4", "8", "bj")):
                        codes.add(code)
                if codes:
                    break  # 500d 命中即不再降级
            candidates = sorted(codes)
            logger.info(f"parquet_cache 降级读取候选股票: {len(candidates)} 只")

    if max_stocks and len(candidates) > max_stocks:
        candidates = candidates[:max_stocks]
        logger.info(f"限制为前 {max_stocks} 只股票（调试模式）")

    logger.info(f"最终候选股票: {len(candidates)} 只")
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
    strict_confirmation: bool = False,
) -> tuple[dict[str, list[dict]], dict[str, dict[str, dict]]]:
    """批量回测，返回 (per_strategy_metrics, per_symbol_metrics)。

    Args:
        symbols: 股票代码列表
        strategies: 策略名列表
        days: 回测历史天数
        strict_confirmation: 已废弃，保留仅用于向后兼容。回测引擎已统一
            使用 is_backtest=True 避免未来函数，不再通过此参数模拟确认机制。

    Returns:
        tuple:
            - per_strategy_metrics: {策略名: [metrics_dict, ...]}
            - per_symbol_metrics: {symbol: {策略名: {metrics字段: 值}}}
    """
    # 每个策略一个结果列表
    all_results: dict[str, list[dict]] = {s: [] for s in strategies}
    # 每只股票每个策略的结果
    per_symbol_results: dict[str, dict[str, dict]] = {}
    engine = BacktestEngine(strict_confirmation=strict_confirmation)

    sem = asyncio.Semaphore(_STOCK_CONCURRENCY)

    async def _one(symbol: str) -> tuple[str, dict[str, dict]]:
        async with sem:
            return symbol, await backtest_one_stock(symbol, strategies, days, engine)

    tasks = [_one(sym) for sym in symbols]
    total = len(tasks)
    completed = 0
    t0 = time.time()

    for coro in asyncio.as_completed(tasks):
        symbol, result = await coro
        per_symbol_results[symbol] = {}
        for strategy_name, metrics in result.items():
            if metrics:
                all_results[strategy_name].append(metrics)
                per_symbol_results[symbol][strategy_name] = metrics
            else:
                per_symbol_results[symbol][strategy_name] = {}
        completed += 1
        if completed % 50 == 0 or completed == total:
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (total - completed) / rate if rate > 0 else 0
            logger.info(
                f"回测进度: {completed}/{total} "
                f"({completed / total * 100:.1f}%) "
                f"速率: {rate:.1f}/s "
                f"ETA: {eta / 60:.1f}分钟"
            )

    return all_results, per_symbol_results


def aggregate_metrics(results: list[dict]) -> dict:
    """聚合单策略多股的 metrics，输出统计摘要（含分布原始值）。"""
    if not results:
        return {}

    summary = {}
    for key in _DISTRIBUTION_METRICS:
        values = [r.get(key, 0.0) for r in results if r.get(key) is not None]
        if not values:
            summary[key] = {"mean": 0, "median": 0, "std": 0, "n": 0}
            continue
        series = pd.Series(values)
        summary[key] = {
            "mean": float(series.mean()),
            "median": float(series.median()),
            "std": float(series.std()),
            "p25": float(series.quantile(0.25)),
            "p75": float(series.quantile(0.75)),
            "min": float(series.min()),
            "max": float(series.max()),
            "n": len(values),
            "raw_values": values,  # 保留原始值，用于 t 检验和分布分析
        }

    # 额外统计
    summary["_sample_count"] = len(results)
    summary["_win_rate_mean"] = summary.get("win_rate", {}).get("mean", 0)
    summary["_return_mean"] = summary.get("total_return", {}).get("mean", 0)
    summary["_sharpe_mean"] = summary.get("sharpe", {}).get("mean", 0)
    summary["_sharpe_median"] = summary.get("sharpe", {}).get("median", 0)
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
    """打印对比报告（含分布统计 p25/p75）。"""
    print()
    print("=" * 130)
    print(f"全市场策略回测对比报告 — {date.today()}")
    print(f"股票数: {n_stocks} | 回测天数: {days} | 耗时: {elapsed / 60:.1f} 分钟")
    print("=" * 130)

    # 表头
    print(
        f"{'策略':<25}{'样本':<8}{'总收益%':<20}{'年化%':<12}"
        f"{'夏普':<10}{'最大回撤%':<12}{'胜率%':<10}"
        f"{'盈亏比':<10}{'交易次':<8}{'持仓天':<8}"
    )
    print("-" * 130)

    # 按总收益排序
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
            f"{ret.get('mean', 0) * 100:+.2f}% "
            f"[P25:{ret.get('p25', 0) * 100:+.2f} "
            f"P75:{ret.get('p75', 0) * 100:+.2f}]  "
            f"{ann.get('mean', 0) * 100:+.2f}%    "
            f"{sharpe.get('mean', 0):.2f}      "
            f"{dd.get('mean', 0) * 100:.2f}%      "
            f"{wr.get('mean', 0) * 100:.1f}%      "
            f"{pf.get('mean', 0):.2f}    "
            f"{tt.get('mean', 0):.0f}      "
            f"{hd.get('mean', 0):.1f}"
        )

    print()
    print("说明: [P25 P75] 为收益分布的 25%/75% 分位数")
    print()

    # 策略间对比结论
    print("=" * 130)
    print("策略评估结论")
    print("=" * 130)
    if not sorted_strategies:
        print("无数据")
        return

    best_name, best_summary = sorted_strategies[0]
    worst_name, worst_summary = sorted_strategies[-1]

    print(f"最佳策略: {best_name}")
    print(f"  总收益均值: {best_summary.get('_return_mean', 0) * 100:+.2f}%")
    print(f"  夏普均值: {best_summary.get('_sharpe_mean', 0):.2f}")
    print(f"  夏普中位数: {best_summary.get('_sharpe_median', 0):.2f}")
    print(f"  胜率均值: {best_summary.get('_win_rate_mean', 0) * 100:.1f}%")
    print(f"  盈利比: {best_summary.get('_profitable_ratio', 0) * 100:.0f}%")
    print()
    print(f"最差策略: {worst_name}")
    print(f"  总收益均值: {worst_summary.get('_return_mean', 0) * 100:+.2f}%")
    print(f"  夏普均值: {worst_summary.get('_sharpe_mean', 0):.2f}")
    print()
    print("建议:")
    for name, summary in sorted_strategies:
        ret = summary.get("_return_mean", 0)
        sharpe = summary.get("_sharpe_mean", 0)
        wr = summary.get("_win_rate_mean", 0)
        if ret > 0 and sharpe > 0.5:
            print(f"  ✓ {name}: 收益+{ret * 100:.2f}%, 夏普{sharpe:.2f} → 建议保留")
        elif ret < -0.05 or sharpe < 0:
            print(f"  ✗ {name}: 收益{ret * 100:+.2f}%, 夏普{sharpe:.2f} → 建议禁用或调优")
        else:
            print(f"  ? {name}: 收益{ret * 100:+.2f}%, 夏普{sharpe:.2f} → 边缘，需观察")


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
    all_results, per_symbol_results = await run_batch_backtest(
        symbols, strategies, args.days, strict_confirmation=args.strict
    )

    # 4. 汇总
    all_summaries = {}
    for strategy_name, metrics_list in all_results.items():
        all_summaries[strategy_name] = aggregate_metrics(metrics_list)

    # 5. 打印报告
    elapsed = time.time() - t0
    print_report(all_summaries, len(symbols), args.days, elapsed)

    # 5b. 报告必须使用内存中的完整逐样本值，不能使用落盘截断数据。
    if args.report:
        from scripts.backtest_report import generate_markdown_report, save_report

        report_data = {
            "date": str(date.today()),
            "n_stocks": len(symbols),
            "days": args.days,
            "elapsed_sec": elapsed,
            "strategies": all_summaries,
        }
        report_md = generate_markdown_report(report_data)
        report_path = f"docs/backtest_report_{date.today()}.md"
        save_report(report_md, report_path)
        logger.info(f"对比报告已生成: {report_path}")

    # 6. 保存 JSON 到 docs/ 目录
    today_str = str(date.today())
    json_path = args.output or f"docs/backtest_results_{today_str}.json"
    output_path = Path(json_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 构建聚合输出数据（过滤掉过大的 raw_values 以减小文件体积）
    output_strategies = {}
    for strategy_name, summary in all_summaries.items():
        filtered = {}
        for k, v in summary.items():
            if isinstance(v, dict) and "raw_values" in v:
                d = dict(v)
                d["raw_values"] = (
                    v["raw_values"][:1000] if len(v["raw_values"]) > 1000 else v["raw_values"]
                )
                filtered[k] = d
            else:
                filtered[k] = v
        output_strategies[strategy_name] = filtered

    full_data = {
        "date": today_str,
        "n_stocks": len(symbols),
        "days": args.days,
        "elapsed_sec": elapsed,
        "strategies": output_strategies,
    }
    atomic_write_json(output_path, full_data, indent=2, default=str)
    logger.info(f"回测结果已保存: {output_path}")

    # 6b. 保存 per-symbol JSON（详细原始数据）
    per_symbol_path = Path(f"docs/backtest_raw_{today_str}.json")
    per_symbol_data = {
        "date": today_str,
        "n_stocks": len(symbols),
        "days": args.days,
        "elapsed_sec": elapsed,
        "strategies": strategies,
        "results": per_symbol_results,
    }
    atomic_write_json(per_symbol_path, per_symbol_data, indent=2, default=str)
    logger.info(f"逐股票原始结果已保存: {per_symbol_path}")

    effective_symbols = [symbol for symbol, results in per_symbol_results.items() if results]
    append_record(
        build_record(
            kind="batch_backtest",
            entrypoint="scripts/backtest_all_strategies.py:main_async",
            strategies=strategies,
            symbols=symbols,
            requested_days=args.days,
            effective_symbols=effective_symbols,
            excluded_symbols=sorted(set(symbols) - set(effective_symbols)),
            output_artifact=str(output_path),
            raw_artifact=str(per_symbol_path),
            report_artifact=(report_path if args.report else None),
            elapsed_sec=elapsed,
            summaries=all_summaries,
            strict_confirmation=args.strict,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="全市场多策略批量回测")
    parser.add_argument("--max-stocks", type=int, default=None, help="限制股票数（调试用）")
    parser.add_argument("--days", type=int, default=200, help="回测历史天数")
    parser.add_argument(
        "--strategies", type=str, default=None, help="逗号分隔的策略名，空则用 settings.yaml"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出 JSON 路径（默认: docs/backtest_results_YYYY-MM-DD.json）",
    )
    parser.add_argument(
        "--report", action="store_true", help="回测完成后自动生成 Markdown 对比报告到 docs/ 目录"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：模拟生产环境次日确认机制，回测结果与生产行为一致",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
