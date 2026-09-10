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
from gugu.research.ledger import append_record, build_record
from gugu.strategies.registry import get_runtime_strategy_names, get_strategy
from gugu.utils.atomic import atomic_write_json
from gugu.utils.log import get_logger

logger = get_logger()


def get_enabled_strategies() -> list[str]:
    """读取当前执行模式经过准入门禁的信号策略。"""
    return get_runtime_strategy_names()


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
    """从缓存加载数据，优先用最长天数的缓存。

    v0.4.2 修复: 加入 500 到搜索列表头部，否则 1786 个 _500d.parquet 文件不会被加载。
    """
    for days in [500, 365, 250, 200, 120, 60, 30, 2]:
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
    index_df: pd.DataFrame | None = None,
) -> dict[str, dict]:
    """对单股跑所有策略。"""
    results = {}
    for strategy_name in strategies:
        try:
            strategy = get_strategy(strategy_name)
            bt_result = engine.run(strategy, df, code, index_df=index_df)
            results[strategy_name] = bt_result.metrics
        except Exception as e:
            logger.debug(f"{code} 策略 {strategy_name} 失败: {e}")
            results[strategy_name] = {}
    return results


def run_batch_backtest(
    stocks: list[tuple[str, int]],
    strategies: list[str],
    cache_dir: Path,
    index_df: pd.DataFrame | None = None,
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

        result = backtest_one_stock(code, df, strategies, engine, index_df=index_df)
        for strategy_name, metrics in result.items():
            if metrics:
                all_results[strategy_name].append(metrics)

        completed += 1
        if completed % 20 == 0 or completed == total:
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            logger.info(
                f"回测进度: {completed}/{total} ({completed / total * 100:.1f}%) 速率: {rate:.1f}/s"
            )

    return all_results


def aggregate_metrics(results: list[dict]) -> dict:
    """聚合单策略多股的 metrics。"""
    if not results:
        return {}

    metric_keys = [
        "total_return",
        "annual_return",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "profit_factor",
        "total_trades",
        "avg_hold_days",
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
    print(f"股票数: {n_stocks} | 耗时: {elapsed / 60:.1f} 分钟")
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
            f"{ret.get('mean', 0) * 100:+.2f}/M{ret.get('median', 0) * 100:+.2f}    "
            f"{ann.get('mean', 0) * 100:+.2f}      "
            f"{sharpe.get('mean', 0):.2f}        "
            f"{dd.get('mean', 0) * 100:.2f}        "
            f"{wr.get('mean', 0) * 100:.1f}      "
            f"{summary.get('_profitable_ratio', 0) * 100:.0f}        "
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
        print(
            f"     样本={n} | 收益={ret * 100:+.2f}% | 夏普={sharpe:.2f} | 胜率={wr * 100:.1f}% | 盈利比={pf * 100:.0f}% | 回撤={dd * 100:.2f}%"
        )

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
            print(f"  ✓ {name}: 收益+{ret * 100:.2f}%, 夏普{sharpe:.2f} → 建议保留（有效）")
        elif ret < -0.05 or sharpe < 0:
            print(f"  ✗ {name}: 收益{ret * 100:+.2f}%, 夏普{sharpe:.2f} → 建议禁用或调优（无效）")
        else:
            print(f"  ? {name}: 收益{ret * 100:+.2f}%, 夏普{sharpe:.2f} → 边缘，需观察")


def main() -> None:
    parser = argparse.ArgumentParser(description="全市场多策略批量回测（离线版）")
    parser.add_argument("--max-stocks", type=int, default=None, help="限制股票数（调试用）")
    parser.add_argument(
        "--min-days", type=int, default=120, help="最少缓存天数要求（默认 120，确保至少覆盖半年）"
    )
    parser.add_argument("--strategies", type=str, default=None, help="逗号分隔的策略名")
    parser.add_argument(
        "--output", type=str, default="data/backtest_report.json", help="输出 JSON 路径"
    )
    parser.add_argument("--notify", action="store_true", help="回测完成后推送飞书报告")
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
        stocks = stocks[: args.max_stocks]
        logger.info(f"限制为前 {args.max_stocks} 只股票（调试模式）")

    logger.info(f"可用缓存股票: {len(stocks)} 只 (min_days={args.min_days})")

    # 加载 HS300 指数数据（用于 market filter 回测集成）
    index_df = None
    cache_dir_obj = Path("data/parquet_cache")
    hs300_path = cache_dir_obj / "hs300_500d.parquet"
    if hs300_path.exists():
        try:
            index_df = pd.read_parquet(hs300_path)
            index_df["date"] = pd.to_datetime(index_df["date"])
            index_df = index_df.sort_values("date").reset_index(drop=True)
            logger.info(f"加载 HS300 指数数据: {len(index_df)} 行")
        except Exception as e:
            logger.warning(f"加载 HS300 指数数据失败: {e}")

    # 3. 批量回测
    logger.info(f"开始批量回测: {len(stocks)} 只股票 × {len(strategies)} 策略")
    all_results = run_batch_backtest(stocks, strategies, cache_dir, index_df=index_df)

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
    atomic_write_json(output_path, full_data, indent=2, default=str)
    logger.info(f"报告已保存: {output_path}")

    append_record(
        build_record(
            kind="offline_batch_backtest",
            entrypoint="scripts/backtest_offline.py:main",
            strategies=strategies,
            symbols=[code for code, _ in stocks],
            requested_min_days=args.min_days,
            effective_symbol_count=len(stocks),
            output_artifact=str(output_path),
            elapsed_sec=elapsed,
            summaries=all_summaries,
        )
    )

    # 7. 飞书通知（v0.7.1 新增）
    if args.notify:
        _send_feishu_report(full_data, strategies)


def _send_feishu_report(full_data: dict, strategies: list[str]) -> None:
    """推送批量回测报告到飞书。"""
    try:
        import asyncio
        from gugu.notifier.feishu import FeishuNotifier

        summaries = full_data.get("strategies", {})
        summary_lines = []
        for s_name in strategies:
            s = summaries.get(s_name, {})
            if not s:
                summary_lines.append(f"{s_name}: 无数据")
                continue
            ret = s.get("_return_mean", 0)
            sharpe = s.get("_sharpe_mean", 0)
            dd = s.get("_max_dd_mean", 0)
            wr = s.get("_win_rate_mean", 0)
            n = s.get("_sample_count", 0)
            pf = s.get("_profitable_ratio", 0)
            summary_lines.append(
                f"{s_name}: 样本{n} 收益{ret * 100:+.2f}% "
                f"夏普{sharpe:.2f} 回撤{dd * 100:.2f}% "
                f"胜率{wr * 100:.1f}% 盈利比{pf * 100:.0f}%"
            )

        report_text = "\n".join(summary_lines)
        feishu_report = {
            "title": "全市场离线回测报告",
            "date": full_data["date"],
            "n_stocks": full_data["n_stocks"],
            "elapsed_min": round(full_data["elapsed_sec"] / 60, 1),
            "strategies": report_text,
        }

        async def _send():
            notifier = FeishuNotifier()
            # 复用回测报告模板
            from gugu.notifier.formatter import format_backtest_report

            card = format_backtest_report(feishu_report)
            await notifier.send_card(card)

        asyncio.run(_send())
        logger.info("飞书回测报告推送成功")
    except Exception as e:
        logger.warning(f"飞书推送失败: {e}")


if __name__ == "__main__":
    main()
