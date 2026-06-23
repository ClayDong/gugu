"""策略回测评估：逐策略回测沪深300成分股，输出胜率/盈亏比/夏普等指标。"""
from __future__ import annotations

import asyncio
import sys

import akshare as ak

from gugu.backtest.engine import BacktestEngine
from gugu.data import data_manager
from gugu.strategies.registry import get_enabled_strategies


async def evaluate(top_n: int = 30) -> None:
    dm = data_manager()
    strategies = get_enabled_strategies()
    engine = BacktestEngine()

    # 获取沪深300成分股
    print("正在获取沪深300成分股...")
    try:
        df_hs300 = ak.index_stock_cons_csindex(symbol="000300")
        codes = df_hs300["成分券代码"].tolist()[:top_n]
        names = df_hs300["成分券名称"].tolist()[:top_n]
    except Exception as e:
        print(f"获取沪深300失败: {e}")
        return

    print(f"共 {len(codes)} 只成分股，{len(strategies)} 个策略\n")

    # 收集结果
    all_results: dict[str, list[dict]] = {s.name: [] for s in strategies}

    for i, (code, name) in enumerate(zip(codes, names)):
        symbol = str(code).strip().zfill(6)
        try:
            df = await dm.fetch_stock_history(symbol, days=250)
            if df.empty or len(df) < 60:
                continue
            for strategy in strategies:
                result = engine.run(strategy, df, symbol=symbol)
                all_results[strategy.name].append({
                    "symbol": symbol,
                    "name": name,
                    "total_return": result.metrics["total_return"],
                    "sharpe": result.metrics["sharpe"],
                    "max_drawdown": result.metrics["max_drawdown"],
                    "win_rate": result.metrics["win_rate"],
                    "profit_factor": result.metrics["profit_factor"],
                    "total_trades": result.metrics["total_trades"],
                })
        except Exception:
            pass

        if (i + 1) % 10 == 0:
            print(f"  已完成 {i+1}/{len(codes)}")

    # 输出汇总
    print("\n" + "=" * 80)
    print(f"{'策略':<18} {'平均收益':>8} {'胜率':>6} {'盈亏比':>6} {'夏普':>6} {'最大回撤':>8} {'平均交易':>6}")
    print("-" * 80)

    for strategy in strategies:
        results = all_results[strategy.name]
        if not results:
            print(f"{strategy.name:<18} 无数据")
            continue

        avg_return = sum(r["total_return"] for r in results) / len(results)
        avg_win = sum(r["win_rate"] for r in results) / len(results)
        avg_pf = sum(r["profit_factor"] for r in results if r["profit_factor"] != float("inf")) / max(1, sum(1 for r in results if r["profit_factor"] != float("inf")))
        avg_sharpe = sum(r["sharpe"] for r in results) / len(results)
        avg_dd = sum(r["max_drawdown"] for r in results) / len(results)
        avg_trades = sum(r["total_trades"] for r in results) / len(results)

        # 标记低效策略
        flag = ""
        if avg_return < 0:
            flag += " ⚠️亏损"
        if avg_win < 0.4:
            flag += " ⚠️低胜率"
        if avg_dd > 0.3:
            flag += " ⚠️高回撤"

        print(
            f"{strategy.name:<18} {avg_return:>+7.2%} {avg_win:>5.1%} "
            f"{avg_pf:>5.2f} {avg_sharpe:>5.3f} {avg_dd:>7.2%} {avg_trades:>5.1f}"
            f"{flag}"
        )

    print("-" * 80)
    print("建议：标记 ⚠️ 的策略考虑禁用或调参，在 settings.yaml 的 strategy.enabled 中配置")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    asyncio.run(evaluate(top_n=n))
