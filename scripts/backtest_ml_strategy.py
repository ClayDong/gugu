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
from gugu.backtest import BacktestEngine, calc_metrics  # noqa: E402
from gugu.data.manager import data_manager  # noqa: E402
from gugu.strategies.registry import get_strategy  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()

_STOCK_CONCURRENCY = 10
_KLINE_TIMEOUT = 30
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
        # 股票池过滤：排除低流动性（成交额 < 5 亿）和高波动（换手率 > 30%）
        if "成交额" in spot.columns:
            mask &= spot["成交额"].astype(float, errors="ignore") > 500_000_000
        if "换手率" in spot.columns:
            mask &= spot["换手率"].astype(float, errors="ignore") < 30.0
        candidates = spot[mask]["代码"].tolist()
        logger.info(f"akshare 获取候选股票（过滤后）: {len(candidates)} 只")
    except Exception as e:
        logger.warning(f"akshare 股票列表获取失败: {e}")

    if not candidates:
        cache_dir = Path("data/parquet_cache")
        if cache_dir.exists():
            codes = set()
            # 累积所有可用的缓存文件，不 break（优先 365d 因为数据量最多）
            for pattern in ("*_365d.parquet", "*_500d.parquet", "*_300d.parquet", "*_250d.parquet"):
                for p in cache_dir.glob(pattern):
                    code = p.stem.split("_")[0]
                    if not code.startswith(("43", "83", "87", "88", "4", "8", "bj")):
                        codes.add(code)
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


async def run_ml_backtest(
    symbols: list[str],
    days: int,
    train_ratio: float = 0.6,
    strategy_params: dict | None = None,
) -> dict:
    """对多只股票跑 ML 策略回测（仅评估测试期）。

    第一性原理：
    - 模型用前 train_ratio 的数据训练，必须在后 (1-train_ratio) 的数据上测试
    - 否则存在 train/test leak：模型见过测试期的数据，回测结果虚高
    - 实现：engine 在全量数据上运行（保证因子有足够历史），
      但只统计测试期内的 trades 和 equity_curve，重新计算 metrics
    """
    logger.info(f"开始 ML 策略回测，{len(symbols)} 只股票，{days} 天，train_ratio={train_ratio}")
    if strategy_params:
        logger.info(f"策略参数覆盖: {strategy_params}")

    engine = BacktestEngine()
    strategy = get_strategy("ml_factor")
    if strategy_params:
        strategy.params.update(strategy_params)

    all_metrics: list[dict] = []
    per_stock: list[dict] = []
    sem = asyncio.Semaphore(_STOCK_CONCURRENCY)

    async def _one(symbol: str) -> dict | None:
        async with sem:
            df = await fetch_stock_data(symbol, days)
            if df is None:
                logger.debug(f"{symbol}: 数据获取失败")
                return None
            try:
                result = engine.run(strategy, df, symbol)

                # 仅评估测试期：过滤 trades 和 equity_curve
                split_idx = int(len(df) * train_ratio)
                if split_idx < len(df) - 10:
                    split_date = pd.Timestamp(df.iloc[split_idx]["date"])
                    test_trades = [t for t in result.trades if pd.Timestamp(t.date) >= split_date]
                    test_equity = result.equity_curve[result.equity_curve.index >= split_date]
                    if len(test_equity) > 1:
                        test_metrics = calc_metrics(test_equity, test_trades)
                    else:
                        test_metrics = {
                            "total_trades": 0,
                            "total_return": 0.0,
                            "sharpe": 0.0,
                            "max_drawdown": 0.0,
                            "win_rate": 0.0,
                            "avg_hold_days": 0.0,
                        }
                else:
                    test_metrics = result.metrics

                logger.info(
                    f"{symbol}: trades={test_metrics.get('total_trades', 0)}, "
                    f"return={test_metrics.get('total_return', 0):.4f}"
                )
                return {"symbol": symbol, "metrics": test_metrics}
            except Exception as e:
                logger.warning(f"{symbol} ML 回测失败: {e}")
                return None

    tasks = [_one(sym) for sym in symbols]
    t0 = time.time()
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        r = await coro
        if r:
            all_metrics.append(r["metrics"])
            per_stock.append(
                {
                    "symbol": r["symbol"],
                    "trades": r["metrics"].get("total_trades", 0),
                    "return": round(r["metrics"].get("total_return", 0), 4),
                    "sharpe": round(r["metrics"].get("sharpe", 0), 4),
                    "max_drawdown": round(r["metrics"].get("max_drawdown", 0), 4),
                    "win_rate": round(r["metrics"].get("win_rate", 0), 4),
                }
            )
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
        "per_stock": sorted(per_stock, key=lambda x: x["return"], reverse=True),
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


async def train_global_model(symbols: list[str], days: int, train_ratio: float = 0.6) -> dict:
    """v4: 用多只股票数据联合训练全局 ML 模型。

    第一性原理：
    - v3 的问题：模型用第一只股票数据训练，对其他股票泛化差
    - 20 只股票中大部分 0 买入信号，因为模型只见过 1 只股票的数据
    - 修复：汇总所有股票的因子和标签，联合训练一个全局模型

    v4.2 修复 train/test leak：
    - 仅用前 train_ratio（默认 60%）的数据训练模型
    - 后 40% 数据用于回测测试期，模型从未见过
    - 对抗性审查：如果不做时间分割，模型训练时已看过测试期收益 → 回测虚高

    对抗性审查：
    - 副作用1：forward returns 不能跨股票计算（shift(-5) 会把股票 A 和 B 的价格混在一起）
      修复：按股票分别计算标签，然后合并
    - 副作用2：不同股票的因子分布不同，合并后可能引入噪声
      缓解：用分位数标签（top 40%/bottom 40%），每只股票内部归一化
    """
    logger.info(f"开始全局模型训练，{len(symbols)} 只股票，{days} 天，train_ratio={train_ratio}")

    factory = AlphaFactory()
    model = MLFactorModel(forward_days=5)

    stock_factors: list[pd.DataFrame] = []
    stock_closes: list[pd.Series] = []

    sem = asyncio.Semaphore(_STOCK_CONCURRENCY)

    async def _one(symbol: str) -> None:
        async with sem:
            df = await fetch_stock_data(symbol, days)
            if df is None:
                return
            # v4.2: 仅用前 train_ratio 的数据训练，防止 train/test leak
            split_idx = int(len(df) * train_ratio)
            df_train = df.iloc[:split_idx]
            rows: list[dict[str, float]] = []
            for i in range(len(df_train)):
                chunk = df_train.iloc[: i + 1]
                if len(chunk) < 30:
                    rows.append({})
                    continue
                factors = factory.compute_all(chunk)
                row = {name: f.value for name, f in factors.items()}
                rows.append(row)
            stock_factors.append(pd.DataFrame(rows))
            stock_closes.append(df_train["close"].reset_index(drop=True))

    tasks = [_one(sym) for sym in symbols]
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        await coro
        if (i + 1) % 10 == 0:
            logger.info(f"全局模型训练进度: {i + 1}/{len(tasks)}")

    if not stock_factors:
        logger.warning("全局模型训练无数据")
        return {"status": "no_data"}

    # 按股票分别计算标签，然后合并
    # 避免跨股票 shift(-5) 导致标签错误
    all_labels: list[pd.Series] = []
    for fdf, close in zip(stock_factors, stock_closes, strict=True):
        if len(fdf) < 30 or len(close) < 30:
            continue
        stock_labels = model._prepare_labels_quantile(close)
        # 截断到因子矩阵长度
        all_labels.append(stock_labels.iloc[: len(fdf)])

    if not all_labels:
        logger.warning("全局模型训练无有效标签")
        return {"status": "no_labels"}

    combined_factors = pd.concat(stock_factors, ignore_index=True)
    combined_labels = pd.concat(all_labels, ignore_index=True)

    # 截断到相同长度
    min_len = min(len(combined_factors), len(combined_labels))
    combined_factors = combined_factors.iloc[:min_len]
    combined_labels = combined_labels.iloc[:min_len]

    factor_cols = [
        c
        for c in combined_factors.columns
        if c not in ("date", "close", "open", "high", "low", "volume", "amount")
        and pd.api.types.is_numeric_dtype(combined_factors[c])
    ]

    if len(combined_factors) < 100 or not factor_cols:
        logger.warning(f"全局模型训练样本不足: {len(combined_factors)}")
        return {"status": "insufficient_samples"}

    # 先用混合 close 计算 IC（近似，跨股票 IC 有噪声但方向仍有效）
    all_close: list[float] = []
    for close in stock_closes:
        all_close.extend(close.tolist())
    close_series = pd.Series(all_close[:min_len])

    train_result = model.train(
        combined_factors,
        close=close_series,
        factor_columns=factor_cols,
        labels=combined_labels,
    )

    if train_result.get("status") == "trained":
        model.save()
        logger.info(f"全局 ML 模型训练完成并保存: {train_result}")
    else:
        logger.warning(f"全局模型训练失败: {train_result}")

    return train_result


async def main_async(
    max_stocks: int,
    days: int,
    output: str,
    report: bool,
    train_ratio: float = 0.6,
    strategy_params: dict | None = None,
) -> None:
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

    # Step 2: v4 全局模型训练（仅用前 train_ratio 数据）
    logger.info("=" * 60)
    logger.info(f"Step 2: v4.2 全局模型训练（多股票联合训练，train_ratio={train_ratio}）")
    logger.info("=" * 60)
    # 清除旧模型缓存，确保用新数据训练
    from gugu.analysis.ml_factor import MLFactorModel as _MF

    old_model = _MF()
    if old_model.MODEL_PATH.exists():
        old_model.MODEL_PATH.unlink()
        logger.info(f"已清除旧模型缓存: {old_model.MODEL_PATH}")
    train_result = await train_global_model(symbols, days, train_ratio=train_ratio)

    # Step 3: ML 策略回测（仅评估后 1-train_ratio 测试期）
    logger.info("=" * 60)
    logger.info(f"Step 3: ML 策略全市场回测（仅评估测试期，train_ratio={train_ratio}）")
    logger.info("=" * 60)
    bt_result = await run_ml_backtest(
        symbols, days, train_ratio=train_ratio, strategy_params=strategy_params
    )

    # Step 4: 准入门禁评估
    logger.info("=" * 60)
    logger.info("Step 4: 准入门禁评估")
    logger.info("=" * 60)
    gate = evaluate_gate(bt_result) if bt_result.get("status") == "ok" else {"passed": False}

    result = {
        "date": str(pd.Timestamp.now().date()),
        "max_stocks": max_stocks,
        "days": days,
        "train_ratio": train_ratio,
        "ic_analysis": ic_result,
        "global_model_training": train_result,
        "backtest": bt_result,
        "gate": gate,
    }

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=lambda o: int(o)
            if isinstance(o, np.integer)
            else float(o)
            if isinstance(o, np.floating)
            else str(o),
        )
    )

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
                default=lambda o: int(o)
                if isinstance(o, np.integer)
                else float(o)
                if isinstance(o, np.floating)
                else str(o),
            )
            + "\n",
            encoding="utf-8",
        )
        logger.info(f"报告已保存: {out_path}")

    if report:
        print("\n" + "=" * 60)
        print("ML 策略准入门禁报告")
        print("=" * 60)
        print(f"股票数: {max_stocks}")
        print(f"回测天数: {days}")
        print(
            f"训练比例: {result.get('train_ratio', 0.6):.0%} 训练 / {1 - result.get('train_ratio', 0.6):.0%} 测试"
        )
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
    parser.add_argument(
        "--train-ratio",
        type=float,
        default=0.6,
        help="训练集比例（默认 0.6，即 60%% 训练 40%% 测试）",
    )
    parser.add_argument(
        "--buy-threshold", type=float, default=None, help="覆盖买入阈值（默认 0.66）"
    )
    parser.add_argument(
        "--sell-threshold", type=float, default=None, help="覆盖卖出阈值（默认 0.34）"
    )
    parser.add_argument("--cooldown-days", type=int, default=None, help="覆盖冷却期天数（默认 16）")
    args = parser.parse_args()

    strategy_params: dict = {}
    if args.buy_threshold is not None:
        strategy_params["buy_threshold"] = args.buy_threshold
    if args.sell_threshold is not None:
        strategy_params["sell_threshold"] = args.sell_threshold
    if args.cooldown_days is not None:
        strategy_params["cooldown_days"] = args.cooldown_days

    asyncio.run(
        main_async(
            args.max_stocks,
            args.days,
            args.output,
            args.report,
            args.train_ratio,
            strategy_params or None,
        )
    )


if __name__ == "__main__":
    main()
