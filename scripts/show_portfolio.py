"""持仓查看：展示当前账户、持仓、交易记录。

用法：
    python scripts/show_portfolio.py             # 查看持仓汇总
    python scripts/show_portfolio.py --trades    # 查看交易记录
    python scripts/show_portfolio.py --all       # 查看全部
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.execution import PaperBroker  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()


def show_portfolio(broker: PaperBroker) -> None:
    """展示账户汇总和持仓详情。"""
    account = broker.get_account()
    portfolio = broker.get_portfolio()

    print("\n" + "=" * 60)
    print("  gugu 持仓报告")
    print("=" * 60)

    # 账户汇总
    print("\n📊 账户汇总")
    print(f"  总资产: ¥{account.total_value:,.2f}")
    print(f"  现金:   ¥{account.cash:,.2f}")
    print(f"  持仓市值: ¥{account.total_value - account.cash:,.2f}")
    print(f"  持仓数量: {len(account.positions)} 只")

    # 日初净值
    try:
        start_value = broker.daily_start_value
        profit = account.total_value - start_value
        profit_pct = profit / start_value if start_value > 0 else 0
        profit_str = f"+¥{profit:,.2f}" if profit >= 0 else f"-¥{abs(profit):,.2f}"
        profit_pct_str = f"+{profit_pct:.2%}" if profit >= 0 else f"{profit_pct:.2%}"
        print(f"  日初净值: ¥{start_value:,.2f}")
        print(f"  当日盈亏: {profit_str} ({profit_pct_str})")
    except Exception:
        pass

    # 持仓详情
    if portfolio:
        print("\n📈 持仓详情")
        print(f"  {'代码':<8} {'数量':>8} {'可用':>8} {'成本':>10} {'现价':>10} {'市值':>12} {'盈亏':>12}")
        print("  " + "-" * 80)
        for symbol, pos in portfolio.items():
            profit = pos.profit
            profit_str = f"+{profit:,.2f}" if profit >= 0 else f"{profit:,.2f}"
            print(
                f"  {symbol:<8} {pos.quantity:>8} {pos.available:>8} "
                f"{pos.avg_cost:>10.2f} {pos.current_price:>10.2f} "
                f"{pos.market_value:>12.2f} {profit_str:>12}"
            )
    else:
        print("\n📭 当前无持仓")

    print("\n" + "=" * 60)


def show_trades(broker: PaperBroker) -> None:
    """展示交易记录。"""
    trades = broker.trades

    print("\n" + "=" * 60)
    print("  gugu 交易记录")
    print("=" * 60)

    if not trades:
        print("\n📭 无交易记录")
        print("\n" + "=" * 60)
        return

    print(f"\n📋 共 {len(trades)} 笔交易")
    print(f"  {'日期':<12} {'代码':<8} {'方向':<6} {'价格':>10} {'数量':>8} {'佣金':>10} {'印花税':>10}")
    print("  " + "-" * 80)
    for t in trades:
        direction = "买入" if t["direction"] == "buy" else "卖出"
        print(
            f"  {t['date']:<12} {t['symbol']:<8} {direction:<6} "
            f"{t['price']:>10.2f} {t['quantity']:>8} "
            f"{t['commission']:>10.2f} {t['stamp_tax']:>10.2f}"
        )

    print("\n" + "=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="gugu 持仓查看")
    parser.add_argument("--trades", action="store_true", help="查看交易记录")
    parser.add_argument("--all", action="store_true", help="查看全部（持仓+交易记录）")
    parser.add_argument("--version", action="version", version="gugu 0.1.0")
    args = parser.parse_args()

    broker = PaperBroker()

    if args.all:
        show_portfolio(broker)
        show_trades(broker)
    elif args.trades:
        show_trades(broker)
    else:
        show_portfolio(broker)


if __name__ == "__main__":
    main()
