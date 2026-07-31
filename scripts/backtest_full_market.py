"""全市场回测脚本：验证策略在全市场的表现"""

import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import akshare as ak
import pandas as pd
from tqdm import tqdm

from gugu.backtest.engine import BacktestEngine
from gugu.strategies.registry import get_strategy_cls
from gugu.data import data_manager
from gugu.utils.log import get_logger

logger = get_logger()


def get_full_stock_list() -> list[str]:
    """获取全市场股票列表（排除ST和北交所）"""
    df = ak.stock_info_a_code_name()
    # 排除ST股和北交所（8开头）
    df = df[~df["name"].str.contains("ST|退", na=False)]
    df = df[~df["code"].str.startswith(("8", "4"))]  # 北交所/退市
    codes = df["code"].tolist()
    logger.info(f"全市场股票数量（排除ST/北交所）: {len(codes)}")
    return codes


def backtest_strategy_on_stocks(
    strategy_name: str,
    stock_codes: list[str],
    days: int = 300,
    initial_cash: float = 100000.0,
) -> dict:
    """对一批股票运行策略回测"""
    strategy_cls = get_strategy_cls(strategy_name)
    if strategy_cls is None:
        raise ValueError(f"策略 '{strategy_name}' 未注册")

    results = []
    trade_counts = []

    for code in tqdm(stock_codes, desc=f"回测 {strategy_name}"):
        try:
            # 获取数据
            df = data_manager.fetch_stock_history(code, days=days)
            if df is None or len(df) < 60:
                continue

            # 运行回测
            engine = BacktestEngine(
                strategy=strategy_cls(),
                initial_cash=initial_cash,
            )
            result = engine.run(df, symbol=code)

            trades = result.get("total_trades", 0)
            total_return = result.get("total_return", 0.0)
            sharpe = result.get("sharpe_ratio", 0.0)
            max_dd = result.get("max_drawdown", 0.0)

            if trades > 0:
                results.append(
                    {
                        "code": code,
                        "trades": trades,
                        "return": total_return,
                        "sharpe": sharpe,
                        "max_drawdown": max_dd,
                    }
                )
                trade_counts.append(trades)

        except Exception as e:
            continue

    return {
        "strategy": strategy_name,
        "total_stocks": len(stock_codes),
        "stocks_with_trades": len(results),
        "total_trades": sum(trade_counts),
        "avg_trades_per_stock": sum(trade_counts) / len(results) if results else 0,
        "results": results,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="全市场回测策略")
    parser.add_argument("--strategy", required=True, help="策略名称")
    parser.add_argument("--limit", type=int, default=500, help="最多测试股票数量")
    parser.add_argument("--days", type=int, default=300, help="回测天数")
    args = parser.parse_args()

    # 获取全市场股票列表
    all_stocks = get_full_stock_list()

    # 限制数量（避免测试时间过长）
    stocks_to_test = all_stocks[: args.limit]
    logger.info(f"将测试 {len(stocks_to_test)} 只股票")

    # 运行回测
    result = backtest_strategy_on_stocks(
        args.strategy,
        stocks_to_test,
        days=args.days,
    )

    # 输出结果
    print("\n" + "=" * 60)
    print(f"全市场回测结果: {result['strategy']}")
    print("=" * 60)
    print(f"测试股票数: {result['total_stocks']}")
    print(f"有交易的股票数: {result['stocks_with_trades']}")
    print(f"总交易次数: {result['total_trades']}")
    print(f"平均每只股票交易次数: {result['avg_trades_per_stock']:.2f}")

    if result["results"]:
        # 按收益排序
        sorted_results = sorted(result["results"], key=lambda x: x["return"], reverse=True)

        print("\n收益最高的10只股票:")
        for r in sorted_results[:10]:
            print(f"  {r['code']}: {r['return']:.2%} ({r['trades']}笔交易)")

        print("\n收益最低的10只股票:")
        for r in sorted_results[-10:]:
            print(f"  {r['code']}: {r['return']:.2%} ({r['trades']}笔交易)")

        # 统计
        returns = [r["return"] for r in result["results"]]
        sharpes = [r["sharpe"] for r in result["results"] if r["sharpe"] > 0]

        print(f"\n统计:")
        print(f"  平均收益: {sum(returns)/len(returns):.2%}")
        print(f"  正收益比例: {sum(1 for r in returns if r > 0) / len(returns):.1%}")
        print(f"  平均夏普(正): {sum(sharpes)/len(sharpes):.2f}" if sharpes else "  无正夏普")

    return result


if __name__ == "__main__":
    main()
