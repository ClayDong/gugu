"""ML 多因子策略全市场回测脚本。

目标：验证 ml_factor 策略在全市场的有效性，输出准入门禁评估报告。

流程：
1. 获取全市场股票列表（限制 100-300 只，适配 8GB RAM）
2. 每只股票取 500 天历史数据
3. 对每只股票跑 ml_factor 策略
4. 汇总胜率、收益、夏普、最大回撤
5. 输出准入门禁评估结果

用法:
    uv run python scripts/backtest_ml_strategy.py --max-stocks 50
    uv run python scripts/backtest_ml_strategy.py --max-stocks 100 --days 500 --report
    uv run python scripts/backtest_ml_strategy.py --max-stocks 300 --output data/ml_backtest_report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.analysis.alpha_factory import AlphaFactory  # noqa: E402
from gugu.analysis.factor_ic import FactorICAnalyzer  # noqa: E402
from gugu.analysis.ml_factor import MLFactorModel  # noqa: E402
from gugu.backtest import BacktestEngine  # noqa: E402
from gugu.data.manager import data_manager  # noqa: E402
from gugu.strategies.registry import get_strategy  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()

_STOCK_CONCURRENCY = 5
_KLINE_TIMEOUT = 15
_MIN_DATA_DAYS = 200


async def fetch_stock_list(max_stocks: int) -> list[str]:
    """获取股票列表，优先从缓存读取。"""
    import akshare as ak

    candidates: list[str] = []
    try:
        spot = await asyncio.to_thread(ak.stock_zh_a_spot_em)
        spot["代码"] = spot["代码"].astype(str)
        mask = ~spot["代码"].str.startswith(("bj", "43", "83", "87", "88", "4", "8"))
        mask &= ~spot["名称"].str.contains("ST|退|N|^C", na=False, regex=True)
        candidates = spot[mask]["代码"].tolist()
        logger.info(f"akshare 获取候选股票: {len(candidates)} 只")
    except Exception as e:
        logger.warning(f"akshare 股票列表获取失败: {e}")

    if not candidates:
        cache_dir = Path("data/parquet_cache")
        if cache_dir.exists():
            codes = set()
            for pattern in ("*_500d.parquet", "*_250d.parquet"):
                for p in cache_dir.glob(pattern):
                    code = p.stem.split("_")[0]
                    if not code.startswith(("43", "83", "87", "88", "4", "8", "bj")):
                        codes.add(code)
                if codes:
                    break
            candidates = sorted(codes)
            logger.info(f"parquet_cache 降级读取: {len(candidates)} 只")

    if len(candidates) > max_stocks:
        candidates = candidates[:max_stocks]
        logger.info(f"限制为前 {max_stocks} 只股票")

    return candidates


async def fetch_stock_data(symbol: str, days: int) -> pd.DataFrame | None:
    """获取单股历史数据。"""
    try:
        async with asyncio.timeout(_KLINE_TIMEOUT):
            dm = data_manager()
            df = await dm.fetch_stock_history(symbol, days=days)
            if len(df) < _MIN_DATA_DAYS:
                return None
            return df
    except Exception as e:
        logger.debug(f"获取 {symbol} 数据失败: {e}")
        return None


async def run_ic_analysis(symbols: list[str], days: int) -> dict:
    """对多只股票跑因子 IC 分析。"""
    logger.info(f"开始因子 IC 分析，{len(symbols)} 只股票，{days} 天")

    factory = AlphaFactory()
    analyzer = FactorICAnalyzer(forward_days=5)

    all_factor_rows: list[dict[str, float]] = []
    all_close: list[float] = []

    sem = asyncio.Semaphore(_STOCK_CONCURRENCY)

    async def _one(symbol: str) -> None:
        async with sem:
            df = await fetch_stock_data(symbol, days)
            if df is None:
                return
            for i in range(len(df)):
                chunk = df.iloc[: i + 1]
                if len(chunk) < 30:
                    continue
                factors = factory.compute_all(chunk)
                row = {name: f.value for name, f in factors.items()}
                row["close"] = float(chunk["close"].iloc[-1])
                all_factor_rows.append(row)
                all_close.append(float(chunk["close"].iloc[-1]))

    tasks = [_one(sym) for sym in symbols]
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        await coro
        if (i + 1) % 10 == 0:
            logger.info(f"IC 分析进度: {i + 1}/{len(tasks)}")

    if not all_factor_rows:
        logger.warning("IC 分析无数据")
        return {"status": "no_data"}

    factor_df = pd.DataFrame(all_factor_rows)
    factor_cols = [c for c in factor_df.columns if c != "close"]

    results = analyzer.analyze(factor_df, factor_cols)
    effective = analyzer.get_effective_factors(results, ic_threshold=0.03, ir_threshold=0.3)
    summary = analyzer.summary(results)

    logger.info(f"IC 分析完成: {summary['effective_count']}/{summary['total_factors']} 个有效因子")
    logger.info(f"有效因子: {effective}")
    logger.info("Top 5 因子:")
    for item in summary["top_5_by_ic"]:
        logger.info(f"  {item['name']}: IC={item['ic_mean']}, IR={item['ir']}")

    return {
        "status": "ok",
        "summary": summary,
        "effective_factors": effective,
    }


async def run_ml_backtest(symbols: list[str], days: int) -> dict:
    """对多只股票跑 ML 策略回测。"""
    logger.info(f"开始 ML 策略回测，{len(symbols)} 只股票，{days} 天")

    engine = BacktestEngine()
    strategy = get_strategy("ml_factor")

    all_metrics: list[dict] = []
    sem = asyncio.Semaphore(_STOCK_CONCURRENCY)

    async def _one(symbol: str) -> dict | None:
        async with sem:
            df = await fetch_stock_data(symbol, days)
            if df is None:
                logger.debug(f"{symbol}: 数据获取失败")
                return None
            try:
                result = engine.run(strategy, df, symbol)
                logger.info(f"{symbol}: trades={result.metrics.get('total_trades', 0)}, return={result.metrics.get('total_return', 0):.4f}")
                return result.metrics
            except Exception as e:
                logger.warning(f"{symbol} ML 回测失败: {e}")
                return None

    tasks = [_one(sym) for sym in symbols]
    t0 = time.time()
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        m = await coro
        if m:
            all_metrics.append(m)
        if (i + 1) % 10 == 0:
            elapsed = time.time() - t0
            logger.info(f"回测进度: {i + 1}/{len(tasks)}, 已用 {elapsed:.0f}s")

    if not all_metrics:
        logger.warning("ML 回测无结果")
        return {"status": "no_results"}

    # 汇总
    returns = [m.get("total_return", 0) for m in all_metrics]
    sharpes = [m.get("sharpe", 0) for m in all_metrics]
    drawdowns = [m.get("max_drawdown", 0) for m in all_metrics]
    win_rates = [m.get("win_rate", 0) for m in all_metrics]
    trades = [m.get("total_trades", 0) for m in all_metrics]

    total_trades = sum(trades)
    profitable = sum(1 for r in returns if r > 0)

    summary = {
        "status": "ok",
        "samples": len(all_metrics),
        "total_trades": total_trades,
        "avg_return": round(float(np.mean(returns)), 4),
        "median_return": round(float(np.median(returns)), 4),
        "avg_sharpe": round(float(np.mean(sharpes)), 4),
        "max_drawdown": round(float(np.max(drawdowns)), 4),
        "avg_win_rate": round(float(np.mean(win_rates)), 4),
        "profitable_ratio": round(profitable / len(returns), 4),
        "avg_hold_days": round(float(np.mean([m.get("avg_hold_days", 0) for m in all_metrics])), 1),
    }

    logger.info(f"ML 回测完成: {summary}")
    return summary


def evaluate_gate(metrics: dict) -> dict:
    """评估准入门禁。"""
    thresholds = {
        "total_trades": (100, "higher"),
        "avg_return": (0.0, "higher"),
        "median_return": (0.0, "higher"),
        "avg_sharpe": (0.0, "higher"),
        "max_drawdown": (0.25, "lower"),
        "profitable_ratio": (0.50, "higher"),
    }

    checks = {}
    failed = []
    for key, (threshold, direction) in thresholds.items():
        value = metrics.get(key, 0)
        if direction == "higher":
            passed = float(value) >= threshold
        else:
            passed = float(value) <= threshold
        checks[key] = {"value": value, "threshold": threshold, "passed": passed}
        if not passed:
            failed.append(key)

    return {
        "passed": not failed,
        "failed_checks": failed,
        "checks": checks,
    }


async def main_async(max_stocks: int, days: int, output: str, report: bool) -> None:
    """主入口。"""
    symbols = await fetch_stock_list(max_stocks)
    if not symbols:
        logger.error("无法获取股票列表")
        return

    # Step 1: 因子 IC 分析
    logger.info("=" * 60)
    logger.info("Step 1: 因子 IC 分析")
    logger.info("=" * 60)
    ic_result = await run_ic_analysis(symbols, days)

    # Step 2: ML 策略回测
    logger.info("=" * 60)
    logger.info("Step 2: ML 策略全市场回测")
    logger.info("=" * 60)
    bt_result = await run_ml_backtest(symbols, days)

    # Step 3: 准入门禁评估
    logger.info("=" * 60)
    logger.info("Step 3: 准入门禁评估")
    logger.info("=" * 60)
    gate = evaluate_gate(bt_result) if bt_result.get("status") == "ok" else {"passed": False}

    result = {
        "date": str(pd.Timestamp.now().date()),
        "max_stocks": max_stocks,
        "days": days,
        "ic_analysis": ic_result,
        "backtest": bt_result,
        "gate": gate,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info(f"报告已保存: {out_path}")

    if report:
        print("\n" + "=" * 60)
        print("ML 策略准入门禁报告")
        print("=" * 60)
        print(f"股票数: {max_stocks}")
        print(f"回测天数: {days}")
        if bt_result.get("status") == "ok":
            print(f"总交易数: {bt_result['total_trades']}")
            print(f"平均收益: {bt_result['avg_return']:.2%}")
            print(f"中位收益: {bt_result['median_return']:.2%}")
            print(f"平均夏普: {bt_result['avg_sharpe']:.4f}")
            print(f"最大回撤: {bt_result['max_drawdown']:.2%}")
            print(f"胜率: {bt_result['avg_win_rate']:.2%}")
            print(f"盈利比: {bt_result['profitable_ratio']:.2%}")
        print(f"\n门禁通过: {'是' if gate.get('passed') else '否'}")
        if gate.get("failed_checks"):
            print(f"未通过项: {', '.join(gate['failed_checks'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ML 多因子策略全市场回测")
    parser.add_argument("--max-stocks", type=int, default=50, help="最大股票数（默认 50）")
    parser.add_argument("--days", type=int, default=500, help="回测天数（默认 500）")
    parser.add_argument("--output", type=str, default="", help="输出报告 JSON 路径")
    parser.add_argument("--report", action="store_true", help="打印文本报告")
    args = parser.parse_args()

    asyncio.run(main_async(args.max_stocks, args.days, args.output, args.report))


if __name__ == "__main__":
    main()
