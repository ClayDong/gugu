#!/usr/bin/env python3
"""多股聚合回测脚本 for weekly_combo strategy."""

import subprocess
import sys

STOCKS = [
    "600519",
    "002714",
    "002371",
    "300750",
    "600276",
    "002594",
    "600438",
    "000858",
    "002230",
    "601012",
    "603259",
    "002472",
    "300760",
]


def run_backtest(symbol: str, days: int = 500) -> dict | None:
    """运行单个股票的回测并解析结果。"""
    cmd = [
        sys.executable,
        "scripts/run_backtest.py",
        "--strategy",
        "weekly_combo",
        "--symbol",
        symbol,
        "--days",
        str(days),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = proc.stdout + proc.stderr

    for line in output.split("\n"):
        if "trades=" in line and "total_return=" in line:
            try:
                trades = int(line.split("trades=")[1].split()[0])
                ret = float(line.split("total_return=")[1].split("%")[0])
                sharpe = float(line.split("sharpe=")[1].split()[0])
                dd = float(line.split("max_drawdown=")[1].split("%")[0])
                return {
                    "symbol": symbol,
                    "trades": trades,
                    "return": ret,
                    "sharpe": sharpe,
                    "drawdown": dd,
                }
            except (ValueError, IndexError):
                pass
    return None


def main():
    results = []
    for sym in STOCKS:
        r = run_backtest(sym)
        if r:
            results.append(r)
            print(
                f"{r['symbol']}: trades={r['trades']:2d}, return={r['return']:+.2f}%, sharpe={r['sharpe']:+.2f}, max_dd={r['drawdown']:.2f}%"
            )
        else:
            print(f"{sym}: 无结果")

    print("=" * 70)
    if not results:
        print("没有成功回测的股票")
        return

    avg_ret = sum(r["return"] for r in results) / len(results)
    avg_sharpe = sum(r["sharpe"] for r in results) / len(results)
    avg_dd = sum(r["drawdown"] for r in results) / len(results)
    total_trades = sum(r["trades"] for r in results)
    profitable = sum(1 for r in results if r["return"] > 0)

    print(f"测试股票数: {len(results)}")
    print(f"总交易数: {total_trades}")
    print(f"盈利股票: {profitable}/{len(results)} ({profitable/len(results)*100:.0f}%)")
    print(f"平均收益: {avg_ret:+.2f}%")
    print(f"平均夏普: {avg_sharpe:+.2f}")
    print(f"平均最大回撤: {avg_dd:.2f}%")


if __name__ == "__main__":
    main()
