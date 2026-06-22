"""数据流穿透审计追踪脚本。

用 fixture 数据走完完整买入信号链路，打印每个模块的输入输出实际值。
不 mock 业务逻辑模块，仅用 fixture 数据代替网络请求。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.config import PROJECT_ROOT, settings
from gugu.data.manager import DataManager
from gugu.engine.signal_router import SignalRouter
from gugu.execution import PaperBroker
from gugu.risk import RiskManager
from gugu.strategies.registry import get_enabled_strategies
from gugu.wisdom import WisdomAdvisor


# ========== Fixture 数据 ==========
def make_fixture_df(days: int = 60, base_price: float = 150.0, trend: str = "down_then_up") -> pd.DataFrame:
    """生成模拟行情数据，确保能触发布林带/RSI 买入信号。"""
    import numpy as np
    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=days)
    if trend == "down_then_up":
        # 前 40 天下跌，后 20 天上涨——触发布林带下轨买入
        prices = [base_price * (1 - 0.005 * i) for i in range(40)] + \
                 [base_price * (1 - 0.005 * 40) * (1 + 0.008 * i) for i in range(20)]
    else:
        prices = [base_price] * days
    close = pd.Series(prices[:days], dtype=float)
    high = close * 1.02
    low = close * 0.98
    open_ = close * 0.999
    volume = pd.Series([1_000_000] * days, dtype=float)
    amount = close * volume

    return pd.DataFrame({
        "date": dates[:days],
        "open": open_.values,
        "high": high.values,
        "low": low.values,
        "close": close.values,
        "volume": volume.values,
        "amount": amount.values,
    })


def trace_buy_signal_pipeline():
    """追踪买入信号完整链路。"""
    print("=" * 70)
    print("链路1：买入信号完整生成链路（DataManager → SignalRouter → WisdomAdvisor → RiskManager → PaperBroker）")
    print("=" * 70)

    # 1. DataManager 输出
    fixture_df = make_fixture_df(60, 150.0, "down_then_up")
    print(f"\n[DataManager 输出] shape={fixture_df.shape}")
    print(f"  close 范围: {fixture_df['close'].min():.2f} ~ {fixture_df['close'].max():.2f}")
    print(f"  close 最后5个: {fixture_df['close'].tail().tolist()}")
    print(f"  是否有0值: {(fixture_df['close'] == 0).any()}")
    print(f"  是否有负值: {(fixture_df['close'] < 0).any()}")

    # 2. SignalRouter 输出
    strategies = get_enabled_strategies()
    print(f"\n[SignalRouter 输入] 启用策略: {[s.name for s in strategies]}")
    router = SignalRouter(strategies)
    signal = router.route(fixture_df, "000858", name="五粮液")
    print(f"[SignalRouter 输出] signal={signal}")
    if signal:
        print(f"  direction={signal.get('direction')}")
        print(f"  confidence={signal.get('confidence')}")
        print(f"  strategies={signal.get('strategies')}")
        print(f"  suggested_position_ratio={signal.get('suggested_position_ratio')} ← 关键字段！")
        print(f"  price={signal.get('price')} ← 关键字段！")
    else:
        print("  ⚠️ 无信号产生！")

    if not signal:
        # 强制构造一个信号继续追踪
        print("\n  ⚠️ fixture 数据未触发策略信号，构造模拟信号继续追踪...")
        signal = {
            "symbol": "000858",
            "name": "五粮液",
            "direction": "buy",
            "confidence": 0.72,
            "strategy": "bollinger",
            "strategies": ["bollinger"],
            "reason": "策略 bollinger 触发 buy 信号",
        }

    # 3. WisdomAdvisor 输出
    print(f"\n[WisdomAdvisor 输入] suggested_position_ratio={signal.get('suggested_position_ratio')}")
    print(f"  price={signal.get('price')}")
    print(f"  confidence={signal.get('confidence')}")

    # 模拟 TradingEngine._scan_signals 中的流程：先设基础比例，再调 wisdom
    max_ratio = settings().get("risk", {}).get("max_position_ratio", 0.30)
    signal["suggested_position_ratio"] = max_ratio * 0.8
    signal["price"] = float(fixture_df["close"].iloc[-1])
    signal["prev_close"] = float(fixture_df["close"].iloc[-2]) if len(fixture_df) >= 2 else signal["price"]
    signal["is_st"] = False
    signal["is_suspended"] = False
    print(f"  [设基础比例后] suggested_position_ratio={signal['suggested_position_ratio']:.2%}")

    wisdom = WisdomAdvisor()
    enhanced = wisdom.advise(signal)
    print(f"\n[WisdomAdvisor 输出]")
    print(f"  suggested_position_ratio={enhanced.get('suggested_position_ratio')}")
    print(f"  wisdom_filtered={enhanced.get('wisdom_filtered')}")
    print(f"  stop_loss_price={enhanced.get('stop_loss_price')}")
    print(f"  wisdom_decision={enhanced.get('wisdom_decision')}")
    if enhanced.get("wisdom_decision", {}).get("adjusted_position_ratio") is not None:
        orig = max_ratio * 0.8
        adj = enhanced["wisdom_decision"]["adjusted_position_ratio"]
        print(f"  仓位调整: {orig:.2%} → {adj:.2%} ({enhanced['wisdom_decision'].get('position_strategy', '')})")

    # 4. RiskManager 检查
    risk = RiskManager()
    broker = PaperBroker(initial_capital=1_000_000)
    account = broker.get_account()

    suggested_ratio = enhanced.get("suggested_position_ratio", 0.0)
    target_value = account.total_value * suggested_ratio
    price = enhanced.get("price", 0)
    quantity = int(target_value / price / 100) * 100 if price > 0 else 0

    print(f"\n[RiskManager 输入]")
    print(f"  symbol=000858, direction=buy, quantity={quantity}, price={price:.2f}")
    print(f"  suggested_ratio={suggested_ratio:.2%}, target_value={target_value:.2f}")

    risk_result = risk.check_order(
        symbol="000858",
        direction="buy",
        quantity=quantity,
        price=price,
        portfolio=broker.get_portfolio(),
        cash=account.cash,
        prev_close=signal.get("prev_close"),
        is_st=False,
        is_suspended=False,
    )
    print(f"[RiskManager 输出] allowed={risk_result.allowed}, message={risk_result.message}")

    # 5. PaperBroker 下单
    if risk_result.allowed:
        order_result = broker.order("000858", "buy", quantity, price)
        print(f"\n[PaperBroker 输出] success={order_result.success}")
        print(f"  fill_price={order_result.price:.2f}, quantity={order_result.quantity}")
        print(f"  commission={order_result.commission:.2f}")
    else:
        print(f"\n[PaperBroker] 风控拦截，不下单")

    # 最终账户状态
    final_account = broker.get_account()
    print(f"\n[最终账户状态]")
    print(f"  cash={final_account.cash:,.2f}")
    print(f"  total_value={final_account.total_value:,.2f}")
    print(f"  positions={list(final_account.positions.keys())}")

    return enhanced


def trace_filtered_signal_pipeline():
    """追踪低置信度信号被入场过滤的链路。"""
    print("\n\n" + "=" * 70)
    print("链路2：入场过滤信号链路（低置信度信号应仅通知不下单）")
    print("=" * 70)

    wisdom = WisdomAdvisor()
    low_conf_signal = {
        "symbol": "600519",
        "name": "贵州茅台",
        "direction": "buy",
        "confidence": 0.35,  # 低置信度
        "strategy": "turtle",
        "strategies": ["turtle"],
        "reason": "突破20日高点",
        "suggested_position_ratio": 0.24,
        "price": 1500.0,
    }

    print(f"\n[WisdomAdvisor 输入] confidence={low_conf_signal['confidence']}")
    enhanced = wisdom.advise(low_conf_signal)
    print(f"[WisdomAdvisor 输出]")
    print(f"  wisdom_filtered={enhanced.get('wisdom_filtered')}")
    print(f"  wisdom_decision={enhanced.get('wisdom_decision')}")

    # 模拟 TradingEngine._process_signal 的逻辑
    if enhanced.get("wisdom_filtered"):
        print(f"\n[TradingEngine._process_signal] 检测到 wisdom_filtered=True → 仅通知不下单 ✅")
    else:
        print(f"\n[TradingEngine._process_signal] ⚠️ 未检测到 wisdom_filtered → 会继续下单！BUG！")

    return enhanced


if __name__ == "__main__":
    trace_buy_signal_pipeline()
    trace_filtered_signal_pipeline()
