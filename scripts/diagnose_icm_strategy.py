"""诊断行业资金强度+动量确认策略（个股适配版 v1.2）。"""

import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/dong/workspace/gugu/src")

from gugu.strategies.industry_capital_momentum import IndustryCapitalMomentumStrategy


def generate_test_data(n: int = 200, trend: str = "uptrend") -> pd.DataFrame:
    """生成测试数据。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")

    if trend == "uptrend":
        base = np.linspace(20, 35, n) + np.random.normal(0, 0.5, n)
    elif trend == "sideways":
        base = 25 + np.cumsum(np.random.normal(0, 0.3, n))
    else:
        base = np.linspace(25, 20, n) + np.random.normal(0, 0.3, n)

    close = np.round(base, 2)
    open_ = close + np.random.normal(0, 0.15, n)
    high = np.maximum(open_, close) + np.abs(np.random.normal(0, 0.2, n))
    low = np.minimum(open_, close) - np.abs(np.random.normal(0, 0.2, n))
    volume = np.random.uniform(500000, 2000000, n)
    amount = volume * close

    df = pd.DataFrame({
        "date": dates,
        "open": np.round(open_, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": close,
        "volume": volume.astype(int),
        "amount": amount,
    })
    return df


def diagnose_conditions(df: pd.DataFrame) -> None:
    """诊断各个入场条件的通过率。"""
    strategy = IndustryCapitalMomentumStrategy()
    result = strategy.generate_signals(df, is_backtest=True)

    print("=" * 60)
    print("行业资金强度+动量确认策略诊断报告 (v1.2)")
    print("=" * 60)
    print(f"\n数据长度: {len(df)} 根K线")
    print(f"买入信号数: {(result['signal'] == 1).sum()}")
    print(f"卖出信号数: {(result['signal'] == -1).sum()}")

    # 检查关键中间指标
    print("\n── 核心条件通过率 ──")

    # MA5/MA20金叉
    ma5 = result["ma5"]
    ma20 = result["ma20"]
    ma5_above = (ma5 > ma20).sum()
    print(f"\nMA5 > MA20 的天数: {ma5_above}/{len(df)} ({100*ma5_above/len(df):.1f}%)")

    # 站上MA5
    above_ma5 = (result["close"] > result["ma5"]).sum()
    print(f"收盘 > MA5 的天数: {above_ma5}/{len(df)} ({100*above_ma5/len(df):.1f}%)")

    # 量能
    vol_ratio = result["vol_ratio"]
    vol_confirm = (vol_ratio >= 1.2).sum()
    print(f"量比 >= 1.2 的天数: {vol_confirm}/{len(df)} ({100*vol_confirm/len(df):.1f}%)")

    # RSI
    rsi = result["rsi"]
    rsi_in_zone = ((rsi >= 35) & (rsi <= 70)).sum()
    print(f"RSI 在 35-70 区间的天数: {rsi_in_zone}/{len(df)} ({100*rsi_in_zone/len(df):.1f}%)")

    # MA5斜率
    ma5_slope = result["ma5_slope"]
    ma_trend_up = (ma5_slope > -0.02).sum()
    print(f"MA5斜率 > -0.02 的天数: {ma_trend_up}/{len(df)} ({100*ma_trend_up/len(df):.1f}%)")

    print("\n── 评分增强项通过率 ──")

    # 量价齐升
    daily_return = result["daily_return"]
    vol_ratio = result["vol_ratio"]
    momentum_score = ((daily_return > 0) & (vol_ratio > 1.0)).sum()
    print(f"\n量价齐升（涨幅>0且量比>1）的天数: {momentum_score}/{len(df)} ({100*momentum_score/len(df):.1f}%)")

    # 成交额占比提升
    amount_ratio = result["amount_ratio"]
    amount_up = (amount_ratio > 1.1).sum()
    print(f"成交额占比 > 1.1 的天数: {amount_up}/{len(df)} ({100*amount_up/len(df):.1f}%)")

    # 个股共振
    stock_up = result["stock_up"]
    resonance = (stock_up.rolling(3).mean() >= 0.5).sum()
    print(f"近3日共振 >= 50% 的天数: {resonance}/{len(df)} ({100*resonance/len(df):.1f}%)")

    # 非过热
    return_20d = result["return_20d"]
    not_hot = (return_20d < 0.25).sum()
    print(f"20日涨幅 < 25% 的天数: {not_hot}/{len(df)} ({100*not_hot/len(df):.1f}%)")

    # 非过度拥挤
    not_crowded = (vol_ratio < 2.5).sum()
    print(f"量比 < 2.5（非拥挤）的天数: {not_crowded}/{len(df)} ({100*not_crowded/len(df):.1f}%)")

    print("\n── 退出条件通过率（v1.2 放宽后）──")

    # 趋势破坏：跌破MA10
    ma10 = result["ma10"]
    exit_trend = (result["close"] < ma10).sum()
    print(f"\n跌破MA10（趋势破坏）的天数: {exit_trend}/{len(df)} ({100*exit_trend/len(df):.1f}%)")

    # 资金退潮
    vol_shrink = (vol_ratio < 0.7).rolling(2).sum() >= 2
    price_decline = (daily_return < 0).rolling(3).sum() >= 3
    exit_drain = (vol_shrink | price_decline).sum()
    print(f"资金退潮（缩量/连跌）的天数: {exit_drain}/{len(df)} ({100*exit_drain/len(df):.1f}%)")

    # 过热止盈（v1.2: 35%和78）
    exit_overheat_return = (return_20d > 0.35).sum()
    exit_overheat_rsi = (rsi > 78).sum()
    print(f"20日涨幅>35%（过热）的天数: {exit_overheat_return}/{len(df)} ({100*exit_overheat_return/len(df):.1f}%)")
    print(f"RSI>78（过热）的天数: {exit_overheat_rsi}/{len(df)} ({100*exit_overheat_rsi/len(df):.1f}%)")

    # 硬止损（v1.2: 12%）
    rolling_max = result["close"].rolling(20).max()
    drawdown = (result["close"] - rolling_max) / rolling_max
    exit_stop = (drawdown < -0.12).sum()
    print(f"回撤>12%（硬止损）的天数: {exit_stop}/{len(df)} ({100*exit_stop/len(df):.1f}%)")

    # 止盈（v1.2: 25%）
    exit_profit = (return_20d > 0.25).sum()
    print(f"20日涨幅>25%（止盈）的天数: {exit_profit}/{len(df)} ({100*exit_profit/len(df):.1f}%)")

    # 最近的指标值
    print("\n── 最近5日指标值 ──")
    recent = result.tail(5)[["close", "ma5", "ma20", "ma10", "vol_ratio", "rsi",
                              "ma5_slope", "amount_ratio", "daily_return", "signal"]]
    print(recent.to_string())

    # 买入信号分布
    buy_signals = result[result["signal"] == 1]
    if len(buy_signals) > 0:
        print(f"\n── 买入信号分布 ──")
        print(f"首次买入信号位置: 第{buy_signals.index[0]}根K线")
        print(f"买入信号平均收盘价: {buy_signals['close'].mean():.2f}")
        print(f"买入信号平均量比: {buy_signals['vol_ratio'].mean():.2f}")
        print(f"买入信号平均RSI: {buy_signals['rsi'].mean():.1f}")

    # 卖出信号分布
    sell_signals = result[result["signal"] == -1]
    if len(sell_signals) > 0:
        print(f"\n── 卖出信号分布 ──")
        print(f"首次卖出信号位置: 第{sell_signals.index[0]}根K线")
        print(f"卖出信号平均收盘价: {sell_signals['close'].mean():.2f}")
        print(f"卖出信号平均量比: {sell_signals['vol_ratio'].mean():.2f}")
        print(f"卖出信号平均RSI: {sell_signals['rsi'].mean():.1f}")


if __name__ == "__main__":
    df = generate_test_data(200, "uptrend")
    diagnose_conditions(df)
