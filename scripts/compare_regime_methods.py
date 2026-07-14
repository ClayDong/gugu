"""比较 4 种市场状态划分方法在 HS300 上的 bull 天数差异。

方法：
1. MarketFilter（生产环境）：MA200 + 趋势向上 + 流动性验证，三重全部满足才 bull
2. 个股 MA200*1.05: close > MA200*1.05（backtest_by_regime.py 用）
3. HS300 MA120*1.05: close > MA120*1.05（diagnose_strategy.py 用）
4. HS300 MA120: close > MA120（最宽松）

用法:
    uv run python scripts/compare_regime_methods.py
"""
from __future__ import annotations

import pandas as pd
from gugu.backtest.engine import BacktestEngine

_HS300_PATH = "data/parquet_cache/hs300_500d.parquet"


def method_market_filter(index_df: pd.DataFrame) -> dict[str, int]:
    """MarketFilter 三重验证：MA200 + 趋势 + 流动性。"""
    engine = BacktestEngine()
    regimes = {}
    for i in range(len(index_df)):
        win = index_df.iloc[:i + 1]
        if len(win) >= 200:
            r = engine._check_market_regime(win)
        else:
            r = "range"
        regimes[str(index_df["date"].iloc[i].date())] = r
    c = {}
    for r in regimes.values():
        c[r] = c.get(r, 0) + 1
    return c


def method_ma200_105(index_df: pd.DataFrame) -> dict[str, int]:
    """close > MA200*1.05 = bull"""
    close = index_df["close"]
    ma200 = close.rolling(200).mean()
    regimes = {}
    for i in range(len(index_df)):
        if i < 200:
            regimes[str(index_df["date"].iloc[i].date())] = "sideways"
        else:
            c = close.iloc[i]
            m = ma200.iloc[i]
            if c > m * 1.05:
                r = "bull"
            elif c < m * 0.95:
                r = "bear"
            else:
                r = "sideways"
            regimes[str(index_df["date"].iloc[i].date())] = r
    c = {}
    for r in regimes.values():
        c[r] = c.get(r, 0) + 1
    return c


def method_ma120_105(index_df: pd.DataFrame) -> dict[str, int]:
    """close > MA120*1.05 = bull（diagnose_strategy.py 用）"""
    close = index_df["close"]
    ma120 = close.rolling(120).mean()
    regimes = {}
    for i in range(len(index_df)):
        if i < 120:
            regimes[str(index_df["date"].iloc[i].date())] = "sideways"
        else:
            c = close.iloc[i]
            m = ma120.iloc[i]
            if c > m * 1.05:
                r = "bull"
            elif c < m * 0.95:
                r = "bear"
            else:
                r = "sideways"
            regimes[str(index_df["date"].iloc[i].date())] = r
    c = {}
    for r in regimes.values():
        c[r] = c.get(r, 0) + 1
    return c


def method_ma120(index_df: pd.DataFrame) -> dict[str, int]:
    """close > MA120 = bull（最宽松）"""
    close = index_df["close"]
    ma120 = close.rolling(120).mean()
    regimes = {}
    for i in range(len(index_df)):
        if i < 120:
            regimes[str(index_df["date"].iloc[i].date())] = "sideways"
        else:
            regimes[str(index_df["date"].iloc[i].date())] = "bull" if close.iloc[i] > ma120.iloc[i] else "sideways"
    c = {}
    for r in regimes.values():
        c[r] = c.get(r, 0) + 1
    return c


def main() -> None:
    index_df = pd.read_parquet(_HS300_PATH)
    index_df["date"] = pd.to_datetime(index_df["date"])
    index_df = index_df.sort_values("date").reset_index(drop=True)
    print(f"HS300 数据: {len(index_df)} 行\n")

    methods = [
        ("MarketFilter 三重验证", method_market_filter(index_df)),
        ("close > MA200*1.05  ", method_ma200_105(index_df)),
        ("close > MA120*1.05  ", method_ma120_105(index_df)),
        ("close > MA120       ", method_ma120(index_df)),
    ]

    print(f"{'方法':<30} {'bull':>8} {'bear':>8} {'range/sideways':>20}")
    print("-" * 70)
    for name, counts in methods:
        bull = counts.get("bull", 0)
        bear = counts.get("bear", 0)
        other = counts.get("range", 0) + counts.get("sideways", 0)
        bull_pct = bull / len(index_df) * 100
        print(f"{name:<30} {bull:>4}({bull_pct:>5.1f}%) {bear:>4}  {other:>4}")


if __name__ == "__main__":
    main()