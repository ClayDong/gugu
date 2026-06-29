"""P0 认知引擎回测验证脚本。

对比"启用认知引擎"vs"不启用认知引擎"的回测结果，
验证四阶段判断、移动止损、危险信号、禁止摊平是否确实改善了回测指标。

用法：
    python scripts/validate_cognitive.py --symbol 600519 --strategy turtle --days 250
    python scripts/validate_cognitive.py --symbol 600519 --all-strategies  # 对比所有策略
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest import BacktestEngine  # noqa: E402
from gugu.data import data_manager  # noqa: E402
from gugu.strategies.registry import get_strategy, get_enabled_strategies  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()


def _fmt_metrics(metrics: dict, label: str) -> str:
    """格式化指标文本。"""
    return (
        f"  [{label}]\n"
        f"    总收益:     {metrics.get('total_return', 0):.2%}\n"
        f"    年化收益:   {metrics.get('annual_return', 0):.2%}\n"
        f"    夏普比率:   {metrics.get('sharpe', 0):.4f}\n"
        f"    最大回撤:   {metrics.get('max_drawdown', 0):.2%}\n"
        f"    胜率:       {metrics.get('win_rate', 0):.2%}\n"
        f"    盈亏比:     {metrics.get('profit_factor', 0):.2f}\n"
        f"    交易次数:   {int(metrics.get('total_trades', 0))}\n"
        f"    cognitive_log: {metrics.get('cognitive_events', 0)} 次\n"
    )


async def validate_one(symbol: str, strategy_name: str, days: int = 250) -> dict:
    """对单策略单股票执行对比回测。"""
    dm = data_manager()
    df = await dm.fetch_stock_history(symbol, days=days)
    if df.empty:
        logger.error(f"无法获取 {symbol} 数据")
        return {}

    print(f"\n{'=' * 60}")
    print(f"  股票: {symbol} | 策略: {strategy_name} | 数据: {len(df)} 条")
    print(f"{'=' * 60}")

    # 基线：无认知引擎
    strategy = get_strategy(strategy_name)
    base = BacktestEngine(enable_cognitive_engine=False)
    result_base = base.run(strategy, df, symbol)
    print(_fmt_metrics(result_base.metrics, "无认知引擎（基线）"))

    # 对比：启用认知引擎
    strategy2 = get_strategy(strategy_name)
    cog = BacktestEngine(enable_cognitive_engine=True)
    result_cog = cog.run(strategy2, df, symbol)
    print(_fmt_metrics(result_cog.metrics, "启用认知引擎"))

    # 差异
    delta_return = result_cog.metrics.get("total_return", 0) - result_base.metrics.get("total_return", 0)
    delta_sharpe = result_cog.metrics.get("sharpe", 0) - result_base.metrics.get("sharpe", 0)
    delta_dd = result_cog.metrics.get("max_drawdown", 0) - result_base.metrics.get("max_drawdown", 0)
    delta_trades = result_cog.metrics.get("total_trades", 0) - result_base.metrics.get("total_trades", 0)

    print("  【认知引擎边际贡献】")
    print(f"    收益变化:     {delta_return:+.2%}")
    print(f"    夏普变化:     {delta_sharpe:+.4f}")
    print(f"    回撤变化:     {delta_dd:+.2%}（负值=回撤减小=改善）")
    print(f"    交易次数变化: {delta_trades:+d}")
    print(f"    cognitive_log: {len(result_cog.cognitive_log)} 条")

    # 输出 cognitive_log 摘要
    if result_cog.cognitive_log:
        from collections import Counter
        action_counts = Counter(e.get("action", "") for e in result_cog.cognitive_log)
        print(f"    认知引擎动作: {dict(action_counts)}")

    conclusion = ""
    if delta_return > 0 and delta_sharpe > 0:
        conclusion = "✅ 认知引擎提升了收益与风险调整后收益"
    elif delta_return > 0 and delta_dd < 0:
        conclusion = "✅ 认知引擎提升了收益并降低了回撤"
    elif delta_dd < 0:
        conclusion = "⚠️ 认知引擎降低了回撤但收益未提升，适合保守场景"
    else:
        conclusion = "❌ 认知引擎未带来正向贡献，需检查参数"
    print(f"  【结论】{conclusion}")

    return {
        "symbol": symbol,
        "strategy": strategy_name,
        "base_return": result_base.metrics.get("total_return", 0),
        "cog_return": result_cog.metrics.get("total_return", 0),
        "delta_return": delta_return,
        "delta_sharpe": delta_sharpe,
        "delta_drawdown": delta_dd,
        "delta_trades": delta_trades,
        "cognitive_events": len(result_cog.cognitive_log),
        "conclusion": conclusion,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="gugu P0 认知引擎回测验证")
    parser.add_argument("--symbol", default="600519", help="股票代码")
    parser.add_argument("--strategy", default="", help="策略名称，默认使用已启用策略")
    parser.add_argument("--all-strategies", action="store_true", help="对比所有已启用策略")
    parser.add_argument("--days", type=int, default=250, help="回测天数")
    parser.add_argument("--grid", action="store_true", help="参数网格搜索（对止损比例和浪谷窗口）")
    args = parser.parse_args()

    if args.all_strategies:
        strategies = [s.name for s in get_enabled_strategies()]
    elif args.strategy:
        strategies = [args.strategy]
    else:
        strategies = [s.name for s in get_enabled_strategies()]

    results = []
    for sname in strategies:
        try:
            r = await validate_one(args.symbol, sname, args.days)
            if r:
                results.append(r)
        except Exception as e:
            logger.error(f"回测 {sname} 失败: {e}")

    # 网格搜索（可选）
    if args.grid and results:
        print(f"\n\n{'=' * 60}")
        print("  参数网格搜索：初始止损比例 × 浪谷窗口")
        print(f"{'=' * 60}")
        from gugu.backtest.engine import BacktestEngine

        strategy = get_strategy(strategies[0])
        dm = data_manager()
        df = await dm.fetch_stock_history(args.symbol, days=args.days)
        if not df.empty:
            best_return, best_params = -999, {}
            for stop_pct in [0.05, 0.08, 0.10, 0.12, 0.15]:
                for wave in [3, 5, 7]:
                    # 通过 enable_cognitive_engine + 调整默认参数模拟
                    engine = BacktestEngine(enable_cognitive_engine=True)
                    # 修改引擎默认参数（此处简化，直接改类属性）
                    from gugu.analysis.trailing_stop import TrailingStopEngine
                    orig_stop = TrailingStopEngine.DEFAULT_INITIAL_STOP_PCT
                    orig_wave = TrailingStopEngine.WAVE_WINDOW
                    TrailingStopEngine.DEFAULT_INITIAL_STOP_PCT = stop_pct
                    TrailingStopEngine.WAVE_WINDOW = wave
                    try:
                        result = engine.run(strategy, df, args.symbol)
                        ret = result.metrics.get("total_return", 0)
                        print(f"  止损{stop_pct:.0%} 浪谷{wave}d → 收益{ret:.2%}")
                        if ret > best_return:
                            best_return = ret
                            best_params = {"stop_pct": stop_pct, "wave": wave}
                    finally:
                        TrailingStopEngine.DEFAULT_INITIAL_STOP_PCT = orig_stop
                        TrailingStopEngine.WAVE_WINDOW = orig_wave
            if best_params:
                print(f"\n  最优参数: {best_params} → 收益{best_return:.2%}")

    # 汇总
    print(f"\n\n{'=' * 60}")
    print(f"  验证汇总: {args.symbol} × {len(results)} 策略")
    print(f"{'=' * 60}")
    for r in results:
        print(f"  {r['strategy']}: 基线{r['base_return']:.2%} → 认知{r['cog_return']:.2%} ({r['delta_return']:+.2%}) | {r['conclusion']}")


if __name__ == "__main__":
    asyncio.run(main())