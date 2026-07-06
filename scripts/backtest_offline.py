"""全市场多策略批量回测（离线版）。

针对网络受限环境，优先使用本地 parquet 缓存数据，避免依赖在线 API。
若无缓存则尝试 akshare，失败跳过该股票。

设计：
1. 扫描 data/parquet_cache/ 获取有缓存的股票列表
2. 加载每只股票的 longest 缓存（250d > 60d > 2d）
3. 对每只股票跑所有启用策略
4. 汇总每个策略的胜率、收益、夏普、最大回撤
5. 输出对比报告 + 保存 JSON

用法:
    uv run python scripts/backtest_offline.py
    uv run python scripts/backtest_offline.py --max-stocks 50
    uv run python scripts/backtest_offline.py --min-days 200  # 只用 >=200 天数据的股票
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date
from pathlib import Path

import pandas as pd

from gugu.backtest import BacktestEngine
from gugu.config import get_config
from gugu.strategies.registry import get_strategy
from gugu.utils.log import get_logger

logger = get_logger()


def get_enabled_strategies() -> list[str]:
    """从 settings.yaml 读取启用的策略列表。

    注意: config model 可能只返回部分策略（默认值），
    这里直接读 yaml 文件确保获取全部启用策略。
    """
    import yaml
    from pathlib import Path

    # 优先直接读 yaml 文件（避免 config model 默认值覆盖）
    yaml_path = Path("config/settings.yaml")
    if yaml_path.exists():
        try:
            with open(yaml_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            strategies = cfg.get("strategy", {}).get("enabled", [])
            if strategies:
                return strategies
        except Exception as e:
            logger.warning(f"读取 settings.yaml 失败: {e}")

    # 回退到 config model
    try:
        strategies = get_config().strategy.enabled
        if strategies:
            return strategies
    except Exception:
        pass
    return ["box_breakout", "turtle", "dual_ma", "bollinger", "rsi_reversal"]


def scan_cached_stocks(cache_dir: Path, min_days: int) -> list[tuple[str, int]]:
    """扫描缓存目录，返回 [(code, days_available), ...] 按可用天数降序。"""
    if not cache_dir.exists():
        return []

    # 统计每只股票的可用缓存天数
    stock_max_days: dict[str, int] = {}
    for f in cache_dir.glob("*.parquet"):
        # 文件名格式: 600519_250d.parquet / 600519_60d.parquet
        name = f.stem  # 600519_250d
        parts = name.split("_")
        if len(parts) != 2:
            continue
        code, days_str = parts
        if not days_str.endswith("d"):
            continue
        try:
            days = int(days_str[:-1])
        except ValueError:
            continue
        # 取最大可用天数
        if code not in stock_max_days or days > stock_max_days[code]:
            stock_max_days[code] = days

    # 过滤最低天数要求
    result = [(c, d) for c, d in stock_max_days.items() if d >= min_days]
    result.sort(key=lambda x: x[1], reverse=True)
    return result


def load_cached_data(cache_dir: Path, code: str) -> pd.DataFrame | None:
    """从缓存加载数据，优先用最长天数的缓存。"""
    for days in [250, 200, 120, 60, 30, 2]:
        f = cache_dir / f"{code}_{days}d.parquet"
        if f.exists():
            try:
                df = pd.read_parquet(f)
                if not df.empty:
                    return df
            except Exception as e:
                logger.debug(f"读取 {f} 失败: {e}")
    return None


def backtest_one_stock(
    code: str,
    df: pd.DataFrame,
    strategies: list[str],
    engine: BacktestEngine,
) -> dict[str, dict]:
    """对单股跑所有策略。"""
    results = {}
    for strategy_name in strategies:
        try:
            strategy = get_strategy(strategy_name)
            bt_result = engine.run(strategy, df, code)
            results[strategy_name] = bt_result.metrics
        except Exception as e:
            logger.debug(f"{code} 策略 {strategy_name} 失败: {e}")
            results[strategy_name] = {}
    return results


def run_batch_backtest(
    stocks: list[tuple[str, int]],
    strategies: list[str],
    cache_dir: Path,
) -> dict[str, list[dict]]:
    """批量回测。"""
    all_results: dict[str, list[dict]] = {s: [] for s in strategies}
    engine = BacktestEngine()

    total = len(stocks)
    completed = 0
    t0 = time.time()

    for code, days in stocks:
        df = load_cached_data(cache_dir, code)
        if df is None or len(df) < 30:
            completed += 1
            continue

        result = backtest_one_stock(code, df, strategies, engine)
        for strategy_name, metrics in result.items():
            if metrics:
                all_results[strategy_name].append(metrics)

        completed += 1
        if completed % 20 == 0 or completed == total:
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            logger.info(
                f"回测进度: {completed}/{total} "
                f"({completed/total*100:.1f}%) "
                f"速率: {rate:.1f}/s"
            )

    return all_results


def aggregate_metrics(results: list[dict]) -> dict:
    """聚合单策略多股的 metrics。"""
    if not results:
        return {}

    metric_keys = [
        "total_return", "annual_return", "sharpe", "max_drawdown",
        "win_rate", "profit_factor", "total_trades", "avg_hold_days",
    ]

    summary = {}
    for key in metric_keys:
        values = [r.get(key, 0.0) for r in results if r.get(key) is not None]
        if not values:
            summary[key] = {"mean": 0, "median": 0, "n": 0}
            continue
        series = pd.Series(values)
        summary[key] = {
            "mean": float(series.mean()),
            "median": float(series.median()),
            "std": float(series.std()) if len(series) > 1 else 0,
            "min": float(series.min()),
            "max": float(series.max()),
            "n": len(values),
        }

    summary["_sample_count"] = len(results)
    summary["_win_rate_mean"] = summary.get("win_rate", {}).get("mean", 0)
    summary["_return_mean"] = summary.get("total_return", {}).get("mean", 0)
    summary["_sharpe_mean"] = summary.get("sharpe", {}).get("mean", 0)
    summary["_max_dd_mean"] = summary.get("max_drawdown", {}).get("mean", 0)
    profitable = sum(1 for r in results if r.get("total_return", 0) > 0)
    summary["_profitable_ratio"] = profitable / len(results) if results else 0
    return summary


def print_report(
    all_summaries: dict[str, dict],
    n_stocks: int,
    elapsed: float,
) -> None:
    """打印对比报告。"""
    print()
    print("=" * 130)
    print(f"全市场策略回测对比报告（离线缓存数据）— {date.today()}")
    print(f"股票数: {n_stocks} | 耗时: {elapsed/60:.1f} 分钟")
    print("=" * 130)

    # 表头
    print(
        f"{'策略':<25}{'样本':<8}{'总收益%':<14}{'年化%':<14}"
        f"{'夏普':<14}{'最大回撤%':<14}{'胜率%':<10}"
        f"{'盈利比%':<10}{'平均交易':<10}{'持仓天':<10}"
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
        tt = summary.get("total_trades", {})
        hd = summary.get("avg_hold_days", {})

        print(
            f"{name:<25}{n:<8}"
            f"{ret.get('mean', 0)*100:+.2f}/M{ret.get('median', 0)*100:+.2f}    "
            f"{ann.get('mean', 0)*100:+.2f}      "
            f"{sharpe.get('mean', 0):.2f}        "
            f"{dd.get('mean', 0)*100:.2f}        "
            f"{wr.get('mean', 0)*100:.1f}      "
            f"{summary.get('_profitable_ratio', 0)*100:.0f}        "
            f"{tt.get('mean', 0):.0f}        "
            f"{hd.get('mean', 0):.1f}"
        )

    print()
    print("说明: M=中位数, 盈利比=正收益股票占比")
    print()

    # 策略间对比结论
    print("=" * 130)
    print("策略评估结论")
    print("=" * 130)
    if not sorted_strategies:
        print("无数据")
        return

    print()
    print("[策略排名（按总收益均值降序）]")
    print()
    for i, (name, summary) in enumerate(sorted_strategies, 1):
        ret = summary.get("_return_mean", 0)
        sharpe = summary.get("_sharpe_mean", 0)
        wr = summary.get("_win_rate_mean", 0)
        pf = summary.get("_profitable_ratio", 0)
        dd = summary.get("_max_dd_mean", 0)
        n = summary.get("_sample_count", 0)
        print(f"  #{i} {name}")
        print(f"     样本={n} | 收益={ret*100:+.2f}% | 夏普={sharpe:.2f} | 胜率={wr*100:.1f}% | 盈利比={pf*100:.0f}% | 回撤={dd*100:.2f}%")

    print()
    print("[建议]")
    for name, summary in sorted_strategies:
        ret = summary.get("_return_mean", 0)
        sharpe = summary.get("_sharpe_mean", 0)
        wr = summary.get("_win_rate_mean", 0)
        n = summary.get("_sample_count", 0)
        if n < 5:
            print(f"  ? {name}: 样本仅 {n} 只，数据不足以下结论")
        elif ret > 0.05 and sharpe > 0.5:
            print(f"  ✓ {name}: 收益+{ret*100:.2f}%, 夏普{sharpe:.2f} → 建议保留（有效）")
        elif ret < -0.05 or sharpe < 0:
            print(f"  ✗ {name}: 收益{ret*100:+.2f}%, 夏普{sharpe:.2f} → 建议禁用或调优（无效）")
        else:
            print(f"  ? {name}: 收益{ret*100:+.2f}%, 夏普{sharpe:.2f} → 边缘，需观察")


def main() -> None:
    parser = argparse.ArgumentParser(description="全市场多策略批量回测（离线版）")
    parser.add_argument("--max-stocks", type=int, default=None, help="限制股票数（调试用）")
    parser.add_argument("--min-days", type=int, default=60, help="最少缓存天数要求（默认 60）")
    parser.add_argument("--strategies", type=str, default=None, help="逗号分隔的策略名")
    parser.add_argument("--output", type=str, default="data/backtest_report.json", help="输出 JSON 路径")
    args = parser.parse_args()

    t0 = time.time()

    # 1. 策略列表
    strategies = get_enabled_strategies()
    if args.strategies:
        strategies = args.strategies.split(",")
    logger.info(f"待回测策略: {strategies}")

    # 2. 扫描缓存
    cache_dir = Path("data/parquet_cache")
    stocks = scan_cached_stocks(cache_dir, args.min_days)
    if not stocks:
        logger.error("缓存目录无数据，请先运行 fetch_stock_history 缓存数据")
        return

    if args.max_stocks and len(stocks) > args.max_stocks:
        stocks = stocks[:args.max_stocks]
        logger.info(f"限制为前 {args.max_stocks} 只股票（调试模式）")

    logger.info(f"可用缓存股票: {len(stocks)} 只 (min_days={args.min_days})")

    # 3. 批量回测
    logger.info(f"开始批量回测: {len(stocks)} 只股票 × {len(strategies)} 策略")
    all_results = run_batch_backtest(stocks, strategies, cache_dir)

    # 4. 汇总
    all_summaries = {}
    for strategy_name, metrics_list in all_results.items():
        all_summaries[strategy_name] = aggregate_metrics(metrics_list)

    # 5. 打印报告
    elapsed = time.time() - t0
    print_report(all_summaries, len(stocks), elapsed)

    # 6. 保存 JSON
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    full_data = {
        "date": str(date.today()),
        "n_stocks": len(stocks),
        "min_days": args.min_days,
        "elapsed_sec": elapsed,
        "strategies": all_summaries,
    }
    output_path.write_text(
        json.dumps(full_data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info(f"报告已保存: {output_path}")


if __name__ == "__main__":
    main()
