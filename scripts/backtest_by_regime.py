"""按市场阶段（牛/熊/震荡）分别回测策略。

设计：
1. 用每只股票自身 MA200 划分该股票的"个股阶段"：
   - bull:     close > MA200 * 1.05
   - bear:     close < MA200 * 0.95
   - sideways: 中间
   优点：不依赖外部指数数据（避免数据源限流）。
2. 对每只股票按交易日匹配 regime 切片
3. 在每个 regime 子集上跑策略回测
4. 输出对比报告

用法:
    uv run python scripts/backtest_by_regime.py
    uv run python scripts/backtest_by_regime.py --max-stocks 200
"""

from __future__ import annotations

import argparse
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

_CACHE_DIR = Path("data/parquet_cache")


def classify_regime(stock_df: pd.DataFrame, ma_window: int = 200) -> pd.DataFrame:
    """基于股票自身 MA200 划分每个交易日的市场阶段。

    bull:     close > MA200 * 1.05
    bear:     close < MA200 * 0.95
    sideways: 中间
    """
    df = stock_df.copy()
    df["ma200"] = df["close"].rolling(ma_window, min_periods=1).mean()
    df["regime"] = "sideways"
    df.loc[df["close"] > df["ma200"] * 1.05, "regime"] = "bull"
    df.loc[df["close"] < df["ma200"] * 0.95, "regime"] = "bear"
    return df


def scan_cached_stocks(min_days: int = 500) -> list[str]:
    """扫描缓存目录，返回有 >=min_days 数据的股票代码列表。"""
    if not _CACHE_DIR.exists():
        return []
    codes: set[str] = set()
    for f in _CACHE_DIR.glob("*.parquet"):
        name = f.stem
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
        if days >= min_days and len(code) == 6 and code.isdigit():
            codes.add(code)
    return sorted(codes)


def load_cached_data(code: str) -> pd.DataFrame | None:
    """从缓存加载股票数据（优先最长天数）。"""
    for days in [500, 250, 200, 120, 60, 30, 2]:
        f = _CACHE_DIR / f"{code}_{days}d.parquet"
        if f.exists():
            try:
                df = pd.read_parquet(f)
                if not df.empty:
                    return df
            except Exception:
                pass
    return None


def slice_by_regime(stock_df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """按 regime 切分股票数据（基于个股 MA200）。

    返回 {"bull": df, "bear": df, "sideways": df}。
    每段至少 30 行才保留，否则空。
    """
    df_with_regime = classify_regime(stock_df)

    result: dict[str, pd.DataFrame] = {}
    for regime in ["bull", "bear", "sideways"]:
        sub = df_with_regime[df_with_regime["regime"] == regime].drop(columns=["regime", "ma200"])
        sub = sub.reset_index(drop=True)
        if len(sub) >= 30:
            result[regime] = sub
        else:
            result[regime] = pd.DataFrame()
    return result


def backtest_regime(
    code: str,
    df: pd.DataFrame,
    strategies: list[str],
    engine: BacktestEngine,
) -> dict[str, dict]:
    """对一段 df 跑所有策略。"""
    results = {}
    for name in strategies:
        try:
            strategy = get_strategy(name)
            bt_result = engine.run(strategy, df, code)
            results[name] = bt_result.metrics
        except Exception as e:
            logger.debug(f"{code} 策略 {name} 失败: {e}")
            results[name] = {}
    return results


def aggregate(metrics_list: list[dict]) -> dict:
    """聚合指标。"""
    if not metrics_list:
        return {"n": 0}
    n = len(metrics_list)
    returns = [m.get("total_return", 0.0) for m in metrics_list if m]
    sharpes = [m.get("sharpe", 0.0) for m in metrics_list if m]
    win_rates = [m.get("win_rate", 0.0) for m in metrics_list if m]
    drawdowns = [m.get("max_drawdown", 0.0) for m in metrics_list if m]
    trades = [m.get("total_trades", 0.0) for m in metrics_list if m]

    profitable = sum(1 for r in returns if r > 0)

    return {
        "n": n,
        "return_mean": pd.Series(returns).mean() if returns else 0,
        "return_median": pd.Series(returns).median() if returns else 0,
        "sharpe_mean": pd.Series(sharpes).mean() if sharpes else 0,
        "sharpe_median": pd.Series(sharpes).median() if sharpes else 0,
        "win_rate_mean": pd.Series(win_rates).mean() if win_rates else 0,
        "max_dd_mean": pd.Series(drawdowns).mean() if drawdowns else 0,
        "trades_mean": pd.Series(trades).mean() if trades else 0,
        "profitable_ratio": profitable / n if n else 0,
    }


def print_report(
    by_regime: dict[str, dict[str, dict]],
    n_stocks: int,
    regime_summary: dict,
    elapsed: float,
) -> None:
    """打印对比报告。"""
    print()
    print("=" * 130)
    print(f"分市场阶段回测报告 — {date.today()}")
    print(
        f"个股 MA200 阶段分布（聚合所有股票）: bull={regime_summary.get('bull', 0)} 段 "
        f"bear={regime_summary.get('bear', 0)} 段 "
        f"sideways={regime_summary.get('sideways', 0)} 段"
    )
    print(f"股票数: {n_stocks} | 耗时: {elapsed/60:.1f} 分钟")
    print("=" * 130)

    regime_names = {
        "bull": "牛市（个股 close > MA200*1.05）",
        "bear": "熊市（个股 close < MA200*0.95）",
        "sideways": "震荡市（中间）",
    }
    strategies_seen = set()
    for r in ["bull", "bear", "sideways"]:
        strategies_seen.update(by_regime.get(r, {}).keys())

    for regime in ["bull", "bear", "sideways"]:
        print()
        print(f"━━━ {regime_names[regime]} ━━━")
        if regime not in by_regime or not by_regime[regime]:
            print("  无数据")
            continue

        print(
            f"  {'策略':<20}{'样本':<8}{'收益均值%':<14}{'收益中位%':<14}"
            f"{'夏普均值':<12}{'夏普中位':<12}{'胜率%':<10}"
            f"{'盈利比%':<10}{'回撤%':<10}{'交易数':<8}"
        )
        print("  " + "-" * 110)

        # 按 return_mean 降序
        items = sorted(
            by_regime[regime].items(),
            key=lambda x: x[1].get("return_mean", 0),
            reverse=True,
        )
        for name, m in items:
            if m.get("n", 0) == 0:
                print(f"  {name:<20}{'无数据':<8}")
                continue
            print(
                f"  {name:<20}{m['n']:<8}"
                f"{m['return_mean']*100:+.2f}        "
                f"{m['return_median']*100:+.2f}        "
                f"{m['sharpe_mean']:+.2f}       "
                f"{m['sharpe_median']:+.2f}       "
                f"{m['win_rate_mean']*100:.1f}     "
                f"{m['profitable_ratio']*100:.0f}     "
                f"{m['max_dd_mean']*100:.2f}    "
                f"{m['trades_mean']:.0f}"
            )

    print()
    print("=" * 130)
    print("结论判断（基于完整回测）")
    print("=" * 130)
    print()
    print("[策略在不同阶段的适应性]")
    print()
    for strategy in sorted(strategies_seen):
        print(f"  {strategy}:")
        for regime in ["bull", "bear", "sideways"]:
            m = by_regime.get(regime, {}).get(strategy, {})
            n = m.get("n", 0)
            if n == 0:
                print(f"    {regime:<10}: 样本 0")
                continue
            ret = m.get("return_mean", 0)
            sharpe = m.get("sharpe_mean", 0)
            pf = m.get("profitable_ratio", 0)
            verdict = (
                "✓" if (ret > 0.05 and sharpe > 0) else ("✗" if (ret < 0 or sharpe < -0.1) else "?")
            )
            print(
                f"    {regime:<10}: 样本={n:<5} 收益={ret*100:+6.2f}% 夏普={sharpe:+.2f} "
                f"盈利比={pf*100:.0f}%  → {verdict}"
            )
        print()


def resolve_strategy_names(raw: str | None) -> list[str]:
    """解析待回测策略；未指定时读取 settings.yaml 的实际启用策略。"""
    if raw:
        return [name.strip() for name in raw.split(",") if name.strip()]
    cfg = get_config().strategy
    return cfg.get_all_enabled_unique() if cfg.enabled_by_regime else list(cfg.enabled)


def main() -> None:
    parser = argparse.ArgumentParser(description="分市场阶段回测")
    parser.add_argument("--max-stocks", type=int, default=None, help="限制股票数（调试用）")
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help="逗号分隔的策略名（默认读取 settings.yaml 实际启用策略）",
    )
    parser.add_argument("--output", type=str, default="/tmp/bt_regime.json", help="输出 JSON 路径")
    args = parser.parse_args()

    t0 = time.time()
    strategies = resolve_strategy_names(args.strategies)

    # 2. 扫描缓存股票
    codes = scan_cached_stocks(min_days=500)
    if args.max_stocks:
        codes = codes[: args.max_stocks]
        logger.info(f"限制为前 {args.max_stocks} 只（调试模式）")
    logger.info(f"待回测股票: {len(codes)} 只 × {len(strategies)} 策略")

    # 3. 按 regime 切片 + 回测
    engine = BacktestEngine()
    by_regime: dict[str, dict[str, list[dict]]] = {
        "bull": {s: [] for s in strategies},
        "bear": {s: [] for s in strategies},
        "sideways": {s: [] for s in strategies},
    }
    regime_segment_counts: dict[str, int] = {"bull": 0, "bear": 0, "sideways": 0}

    completed = 0
    for code in codes:
        df = load_cached_data(code)
        if df is None or len(df) < 30:
            completed += 1
            continue

        slices = slice_by_regime(df)
        for regime, sub_df in slices.items():
            if sub_df.empty or len(sub_df) < 30:
                continue
            regime_segment_counts[regime] += 1
            results = backtest_regime(code, sub_df, strategies, engine)
            for name, metrics in results.items():
                if metrics:
                    by_regime[regime][name].append(metrics)

        completed += 1
        if completed % 50 == 0 or completed == len(codes):
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            logger.info(
                f"回测进度: {completed}/{len(codes)} "
                f"({completed/len(codes)*100:.1f}%) 速率={rate:.1f}/s"
            )

    # 4. 聚合
    aggregated: dict[str, dict[str, dict]] = {
        regime: {name: aggregate(metrics_list) for name, metrics_list in regimes.items()}
        for regime, regimes in by_regime.items()
    }

    # 5. 打印 + 保存
    elapsed = time.time() - t0
    print_report(aggregated, len(codes), regime_segment_counts, elapsed)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "date": str(date.today()),
                "n_stocks": len(codes),
                "regime_segment_counts": regime_segment_counts,
                "by_regime": aggregated,
                "elapsed_sec": elapsed,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    logger.info(f"报告已保存: {output_path}")


if __name__ == "__main__":
    main()
