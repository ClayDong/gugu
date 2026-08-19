"""策略验证增强模块：基准对照 + 参数敏感性 + 流动性压力测试 + 滚动窗口验证。

v0.6.4 新增功能：
1. 基准对照：策略收益 vs 沪深300/行业指数
2. 参数扰动稳定性：±10%/20%/50% 参数扰动分析
3. 流动性压力测试：小盘股大单冲击成本模拟
4. 滚动窗口验证：月度滚动回测确认策略稳定性
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

BENCHMARK_HS300 = "000300"


@dataclass
class BenchmarkResult:
    """基准对照结果。"""

    strategy_return: float
    benchmark_return: float
    excess_return: float
    excess_annual: float
    information_ratio: float
    beta: float
    correlation: float


@dataclass
class PerturbationResult:
    """参数扰动稳定性结果。"""

    param_name: str
    base_value: float
    base_return: float
    perturbations: list[dict]
    stability_score: float


@dataclass
class LiquidityStressResult:
    """流动性压力测试结果。"""

    small_cap_return: float
    large_cap_return: float
    liquidity_cost_pct: float
    impact_by_size: dict[str, float]


@dataclass
class RollingWindowResult:
    """滚动窗口验证结果。"""

    windows: list[dict]
    avg_return: float
    avg_sharpe: float
    win_rate: float
    consistency_score: float


def calc_benchmark_comparison(
    strategy_equity: pd.Series,
    benchmark_df: pd.DataFrame,
) -> BenchmarkResult:
    """计算策略与基准（沪深300）的对比指标。"""
    if len(strategy_equity) < 10 or len(benchmark_df) < 10:
        return BenchmarkResult(0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    strategy_returns = strategy_equity.pct_change().dropna()

    benchmark_aligned = benchmark_df.reindex(strategy_equity.index)
    if benchmark_aligned["close"].isna().all():
        return BenchmarkResult(0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    benchmark_returns = benchmark_aligned["close"].pct_change().dropna()

    common_idx = strategy_returns.index.intersection(benchmark_returns.index)
    if len(common_idx) < 10:
        return BenchmarkResult(0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    strategy_ret_aligned = strategy_returns.loc[common_idx]
    benchmark_ret_aligned = benchmark_returns.loc[common_idx]

    strategy_total = (strategy_equity.iloc[-1] / strategy_equity.iloc[0]) - 1
    benchmark_total = (benchmark_aligned["close"].iloc[-1] / benchmark_aligned["close"].iloc[0]) - 1

    excess = strategy_total - benchmark_total

    days = len(common_idx)
    years = days / 252
    excess_annual = (1 + excess) ** (1 / years) - 1 if years > 0 else 0

    tracking_error = (strategy_ret_aligned - benchmark_ret_aligned).std() * np.sqrt(252)
    information_ratio = excess_annual / tracking_error if tracking_error > 0 else 0.0

    cov_matrix = np.cov(strategy_ret_aligned, benchmark_ret_aligned)
    if cov_matrix.shape == (2, 2):
        covariance = cov_matrix[0, 1]
        benchmark_var = cov_matrix[1, 1]
        beta = covariance / benchmark_var if benchmark_var > 0 else 1.0
    else:
        beta = 1.0

    correlation = float(strategy_ret_aligned.corr(benchmark_ret_aligned))
    if np.isnan(correlation):
        correlation = 0.0

    return BenchmarkResult(
        strategy_return=strategy_total,
        benchmark_return=benchmark_total,
        excess_return=excess,
        excess_annual=excess_annual,
        information_ratio=float(information_ratio) if not np.isnan(information_ratio) else 0.0,
        beta=float(beta) if not np.isnan(beta) else 1.0,
        correlation=correlation,
    )


def calc_param_perturbation_stability(
    strategy_class: type,
    df: pd.DataFrame,
    base_params: dict[str, float],
    key_params: list[str],
    perturbation_pcts: list[float] | None = None,
) -> list[PerturbationResult]:
    """计算参数扰动稳定性。"""
    from gugu.backtest.engine import BacktestEngine

    if perturbation_pcts is None:
        perturbation_pcts = [-0.5, -0.2, -0.1, 0.1, 0.2, 0.5]
    # 基准参数必须单独回测，不能用扰动结果均值冒充基准。
    # 即使调用方自定义扰动列表，也强制补入 0%，避免稳定性口径漂移。
    perturbation_pcts = list(perturbation_pcts)
    if 0.0 not in perturbation_pcts:
        perturbation_pcts.append(0.0)

    results = []

    for param_name in key_params:
        if param_name not in base_params:
            continue

        base_value = base_params[param_name]
        perturbations = []

        for pct in perturbation_pcts:
            perturbed_value = base_value * (1 + pct)
            test_params = base_params.copy()
            test_params[param_name] = perturbed_value

            try:
                strategy = strategy_class(params=test_params)
                engine = BacktestEngine(initial_capital=100000.0)
                result = engine.run(strategy, df)
                ret = result.metrics.get("total_return", 0.0)
                perturbations.append({"pct": pct, "value": perturbed_value, "return": ret})
            except Exception:
                perturbations.append({"pct": pct, "value": perturbed_value, "return": None})

        valid_returns = [p["return"] for p in perturbations if p["return"] is not None]
        base_result = next((p for p in perturbations if p["pct"] == 0.0), None)
        base_return = base_result["return"] if base_result is not None else None

        if base_return is None:
            stability_score = 0.0
        elif valid_returns and base_return != 0:
            max_change = max(
                abs(r - base_return) / abs(base_return) for r in valid_returns if r is not None
            )
            stability_score = max(0.0, 1.0 - min(max_change, 1.0))
        elif valid_returns:
            # 基准收益为 0 时，用绝对收益差异评估；不能把零基准误判为稳定。
            max_abs_change = max(abs(r - base_return) for r in valid_returns)
            stability_score = max(0.0, 1.0 - min(max_abs_change, 1.0))
        else:
            stability_score = 0.0

        if base_return is None:
            base_return = 0.0

        results.append(
            PerturbationResult(
                param_name=param_name,
                base_value=base_value,
                base_return=base_return,
                perturbations=perturbations,
                stability_score=stability_score,
            )
        )

    return results


def calc_liquidity_stress_test(
    trades: list[dict],
) -> LiquidityStressResult:
    """计算流动性压力测试。"""
    if not trades:
        return LiquidityStressResult(0.0, 0.0, 0.0, {})

    small_cap_trades = []
    large_cap_trades = []

    for trade in trades:
        price = trade.get("price", 10)
        quantity = trade.get("quantity", 10000)
        trade_amount = price * quantity

        market_cap = trade.get("market_cap")
        if market_cap is None or market_cap <= 0:
            market_cap = trade_amount * 20  # 从成交额估算：假设换手率约5%
            logger.warning(
                f"market_cap missing for trade, estimated from trade_amount: "
                f"{market_cap:.2f} (price={price}, quantity={quantity})"
            )

        if market_cap < 50e8:
            impact_cost = trade_amount * 0.003
            small_cap_trades.append({"amount": trade_amount, "impact": impact_cost})
        elif market_cap > 200e8:
            impact_cost = trade_amount * 0.0005
            large_cap_trades.append({"amount": trade_amount, "impact": impact_cost})
        else:
            impact_cost = trade_amount * 0.0015
            small_cap_trades.append({"amount": trade_amount, "impact": impact_cost})
            large_cap_trades.append({"amount": trade_amount * 2 / 3, "impact": impact_cost * 2 / 3})

    small_total_cost = sum(t["impact"] for t in small_cap_trades)
    large_total_cost = sum(t["impact"] for t in large_cap_trades)

    small_total_amount = sum(t["amount"] for t in small_cap_trades)
    large_total_amount = sum(t["amount"] for t in large_cap_trades)

    small_cost_pct = small_total_cost / small_total_amount if small_total_amount > 100 else 0.003
    large_cost_pct = large_total_cost / large_total_amount if large_total_amount > 100 else 0.0005

    total_cost_pct = (small_cost_pct + large_cost_pct) / 2

    return LiquidityStressResult(
        small_cap_return=-small_cost_pct * len(small_cap_trades),
        large_cap_return=-large_cost_pct * len(large_cap_trades),
        liquidity_cost_pct=total_cost_pct * len(trades),
        impact_by_size={
            "small_cap": small_cost_pct * len(small_cap_trades),
            "mid_cap": small_cost_pct * len(small_cap_trades) * 2 / 3,
            "large_cap": large_cost_pct * len(large_cap_trades),
        },
    )


def calc_rolling_window_validation(
    strategy_class: type,
    df: pd.DataFrame,
    window_months: int = 3,
    step_months: int = 1,
    param_grid: dict[str, list] | None = None,
) -> RollingWindowResult:
    """按真实日期做严格的滚动 train/test 验证。

    每个窗口先在过去 ``window_months`` 的 train 数据上选择参数，随后只在
    紧接着的 ``step_months`` test 数据上统计收益。汇总指标只来自 test。
    """
    from gugu.backtest.engine import BacktestEngine

    if window_months <= 0 or step_months <= 0 or df.empty:
        return RollingWindowResult([], 0.0, 0.0, 0.0, 0.0)

    df_indexed = df.copy()
    if "date" in df_indexed.columns:
        df_indexed["date"] = pd.to_datetime(df_indexed["date"])
        df_indexed = df_indexed.set_index("date")
    if not isinstance(df_indexed.index, pd.DatetimeIndex):
        df_indexed.index = pd.to_datetime(df_indexed.index)
    df_indexed = df_indexed.sort_index()

    train_delta = pd.DateOffset(months=window_months)
    test_delta = pd.DateOffset(months=step_months)
    test_start = df_indexed.index[0] + train_delta
    last_date = df_indexed.index[-1]
    windows: list[dict] = []

    while test_start <= last_date:
        train_start = test_start - train_delta
        test_end = test_start + test_delta
        train_df = df_indexed[(df_indexed.index >= train_start) & (df_indexed.index < test_start)]
        test_df = df_indexed[(df_indexed.index >= test_start) & (df_indexed.index < test_end)]
        if len(train_df) < max(window_months * 20, 20) or len(test_df) < 5:
            test_start = test_end
            continue

        best_params: dict[str, Any] = {}
        train_sharpe = 0.0
        if param_grid:
            import itertools

            best_train_sharpe = float("-inf")
            for values in itertools.product(*param_grid.values()):
                params = dict(zip(param_grid, values, strict=True))
                try:
                    train_result = BacktestEngine(initial_capital=100000.0).run(
                        strategy_class(params), train_df.reset_index(drop=True)
                    )
                    sharpe = float(train_result.metrics.get("sharpe", 0.0))
                    if sharpe > best_train_sharpe:
                        best_train_sharpe = sharpe
                        best_params = params
                except Exception as exc:
                    logger.debug("滚动窗口 train 参数评估失败: {}", exc)
            train_sharpe = best_train_sharpe if best_train_sharpe != float("-inf") else 0.0
        else:
            try:
                train_result = BacktestEngine(initial_capital=100000.0).run(
                    strategy_class(), train_df.reset_index(drop=True)
                )
                train_sharpe = float(train_result.metrics.get("sharpe", 0.0))
            except Exception as exc:
                logger.debug("滚动窗口 train 诊断失败: {}", exc)

        try:
            test_result = BacktestEngine(initial_capital=100000.0).run(
                strategy_class(best_params), test_df.reset_index(drop=True)
            )
            windows.append(
                {
                    "train_start": str(train_df.index[0])[:10],
                    "train_end": str(train_df.index[-1])[:10],
                    "test_start": str(test_df.index[0])[:10],
                    "test_end": str(test_df.index[-1])[:10],
                    "train_sharpe": train_sharpe,
                    "best_params": best_params,
                    "return": test_result.metrics.get("total_return", 0.0),
                    "sharpe": test_result.metrics.get("sharpe", 0.0),
                    "trades": test_result.metrics.get("total_trades", 0),
                }
            )
        except Exception as exc:
            logger.warning("滚动窗口 test 评估失败，跳过该窗口: {}", exc)
        test_start = test_end

    if not windows:
        return RollingWindowResult([], 0.0, 0.0, 0.0, 0.0)

    returns = [w["return"] for w in windows]
    sharpes = [w["sharpe"] for w in windows]
    avg_return = sum(returns) / len(returns)
    avg_sharpe = sum(sharpes) / len(sharpes)
    win_rate = sum(1 for value in returns if value > 0) / len(returns)
    return_std = pd.Series(returns).std() if len(returns) > 1 else max(abs(avg_return), 1e-6)
    consistency_score = max(0.0, min(1.0, abs(avg_return) / (return_std + 1e-6)))

    return RollingWindowResult(
        windows=windows,
        avg_return=avg_return,
        avg_sharpe=avg_sharpe,
        win_rate=win_rate,
        consistency_score=consistency_score,
    )


async def fetch_benchmark_data(
    symbol: str = BENCHMARK_HS300, days: int = 500
) -> pd.DataFrame | None:
    """获取基准指数数据。"""
    try:
        from gugu.data.manager import DataManager

        dm = DataManager()
        df = await dm.fetch_stock_history(symbol, days=days)

        if df is not None and len(df) > 50:
            return df[["close"]] if "close" in df.columns else df

        return None
    except Exception:
        return None


def build_validation_report(
    benchmark_result: BenchmarkResult | None,
    perturbation_results: list[PerturbationResult],
    liquidity_result: LiquidityStressResult | None,
    rolling_result: RollingWindowResult | None,
) -> dict[str, Any]:
    """构建完整的验证报告。"""
    report: dict[str, Any] = {
        "benchmark": None,
        "perturbation": [],
        "liquidity": None,
        "rolling": None,
        "overall_score": 1.0,
        "recommendations": [],
    }

    if benchmark_result and benchmark_result.benchmark_return != 0:
        report["benchmark"] = {
            "excess_return": f"{benchmark_result.excess_return:.2%}",
            "information_ratio": f"{benchmark_result.information_ratio:.2f}",
            "beta": f"{benchmark_result.beta:.2f}",
            "correlation": f"{benchmark_result.correlation:.2f}",
            "verdict": "✅ 超额显著" if benchmark_result.excess_return > 5e-3 else "⚠️ 无超额收益",
        }
        if benchmark_result.excess_return < -5e-2:
            report["recommendations"].append("策略跑输基准，建议优化或更换")

    unstable_params = [r for r in perturbation_results if r.stability_score < 7e-1]

    report["perturbation"] = [
        {
            "param": r.param_name,
            "stability_score": f"{r.stability_score:.2f}",
            "verdict": "⚠️ 不稳定" if r.stability_score < 7e-1 else "✅稳定",
        }
        for r in perturbation_results
    ]
    if unstable_params:
        report["recommendations"].append(
            f"参数{[r.param_name for r in unstable_params]}对结果影响过大，考虑简化逻辑"
        )

    if liquidity_result and liquidity_result.liquidity_cost_pct > 5e-2:
        report["liquidity"] = {
            "cost_pct": f"{liquidity_result.liquidity_cost_pct:.2%}",
            "verdict": "⚠️流动性成本较高",
        }
        report["recommendations"].append("小盘股流动性成本过高，建议增加市值过滤")
    else:
        report["liquidity"] = {"cost_pct": "<5%", "verdict": "✅流动性成本可控"}

    if rolling_result and rolling_result.windows:
        report["rolling"] = {
            "avg_return": f"{rolling_result.avg_return:.2%}",
            "avg_sharpe": f"{rolling_result.avg_sharpe:.2f}",
            "win_rate": f"{rolling_result.win_rate:.2%}",
            "consistency_score": f"{rolling_result.consistency_score:.2f}",
            "verdict": "✅滚动验证通过" if rolling_result.win_rate > 0.6 else "⚠️滚动验证不稳定",
        }
        if rolling_result.win_rate < 0.5:
            report["recommendations"].append("滚动窗口正收益率过低，策略可能存在时序不稳定性")

    scores = []
    if benchmark_result and benchmark_result.excess_return > 5e-3:
        scores.append(0.9)
    elif benchmark_result and benchmark_result.excess_return >= -5e-3:
        scores.append(0.6)
    else:
        scores.append(0.3)

    scores.extend(r.stability_score for r in perturbation_results)

    if rolling_result and rolling_result.windows:
        scores.append(rolling_result.consistency_score)

    report["overall_score"] = sum(scores) / len(scores) if scores else 0.5

    return report
