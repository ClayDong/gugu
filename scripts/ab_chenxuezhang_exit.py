"""chenxuezhang 出场端优化 A/B 回测：legacy vs optimized。

对比维度：
- 平均收益/夏普/最大回撤/胜率/交易数（15股×600日，真实基本面+指数过滤）
"""

import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

TENCENT_KLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q={}"
_CTX = ssl._create_unverified_context()
_INDEX_CODE = "sh000300"

POOL = [
    "sz002472",
    "sz300033",
    "sz002230",
    "sh603501",
    "sz300308",
    "sz300502",
    "sh600519",
    "sz300750",
    "sz002594",
    "sz300059",
    "sz002475",
    "sh600050",
    "sz000977",
    "sh688041",
    "sz002371",
]


def _http_get(url, referer="http://finance.qq.com/"):
    req = urllib.request.Request(url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=_CTX, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_kline(symbol, days=600):
    url = f"{TENCENT_KLINE}?param={symbol},day,,,{days},qfq"
    data = json.loads(_http_get(url))
    node = data.get("data", {}).get(symbol, {})
    kline = node.get("qfqday") or node.get("day") or []
    if not kline:
        return pd.DataFrame()
    rows = []
    for row in kline:
        if len(row) < 6:
            continue
        rows.append(
            [row[0], float(row[1]), float(row[3]), float(row[4]), float(row[2]), float(row[5])]
        )
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    return df.dropna(subset=["open", "high", "low", "close"])


def fetch_quote(symbol):
    try:
        raw = _http_get(TENCENT_QUOTE.format(symbol), referer="https://gu.qq.com/")
        payload = raw.split('"')[1]
        f = payload.split("~")
        if len(f) < 47:
            return {}
        out = {}
        for idx, key in [(38, "turnover_rate"), (39, "pe"), (44, "market_cap")]:
            try:
                out[key] = float(f[idx])
            except (ValueError, IndexError):
                out[key] = None
        if out.get("market_cap") is not None:
            out["market_cap"] *= 1e8
        if out.get("turnover_rate") is not None:
            out["turnover_rate"] /= 100.0
        return out
    except Exception:
        return {}


def main():
    from gugu.backtest import BacktestEngine
    from gugu.strategies.registry import get_strategy

    # 指数
    index_df = fetch_kline(_INDEX_CODE, 700)
    index_df["date"] = pd.to_datetime(index_df["date"])
    index_df["index_return"] = index_df["close"].pct_change()
    print(f"指数 {len(index_df)} 行")

    # 数据
    data = {}
    for sym in POOL:
        df = fetch_kline(sym, 600)
        if df.empty or len(df) < 250:
            print(f"  ⚠️ {sym} 数据不足")
            continue
        df["date"] = pd.to_datetime(df["date"])
        fund = fetch_quote(sym)
        if fund:
            if fund.get("market_cap") is not None:
                df["market_cap"] = fund["market_cap"]
            if fund.get("turnover_rate") is not None:
                df["turnover_rate"] = fund["turnover_rate"]
            if fund.get("pe") is not None:
                df["pe"] = fund["pe"]
        idx_map = index_df.set_index("date")["index_return"]
        df["index_return"] = df["date"].map(idx_map).fillna(0.0)
        data[sym] = df
        time.sleep(0.3)

    results = []
    for mode in ("legacy", "optimized"):
        for sym, df in data.items():
            strategy = get_strategy("chenxuezhang")
            # 覆盖参数
            strategy.params = {**strategy.params, "exit_mode": mode}
            engine = BacktestEngine(
                initial_capital=100000.0,
                commission_rate=0.00025,
                stamp_tax=0.0005,
                slippage=0.002,
            )
            try:
                r = engine.run(strategy, df.copy(), sym, index_df=index_df)
                m = r.metrics
                results.append(
                    {
                        "mode": mode,
                        "symbol": sym,
                        "return": m.get("total_return", float("nan")),
                        "sharpe": m.get("sharpe", float("nan")),
                        "trades": m.get("total_trades", 0),
                        "win_rate": m.get("win_rate", float("nan")),
                        "max_dd": m.get("max_drawdown", float("nan")),
                    }
                )
            except Exception as e:
                results.append({"mode": mode, "symbol": sym, "error": str(e)})
            time.sleep(0.1)

    out = pd.DataFrame(results)
    print("\n=== A/B 对比：chenxuezhang legacy vs optimized ===")
    for mode in ("legacy", "optimized"):
        sub = out[(out["mode"] == mode) & out.get("return", pd.Series(dtype=float)).notna()]
        print(f"\n[{mode}]")
        print(f"  平均收益: {sub['return'].mean()*100:+.2f}%")
        print(f"  平均夏普: {sub['sharpe'].mean():+.3f}")
        print(f"  总交易数: {int(sub['trades'].sum())}")
        print(f"  平均胜率: {sub['win_rate'].mean()*100:.1f}%")
        print(f"  平均最大回撤: {sub['max_dd'].mean()*100:.1f}%")
        print(f"  正收益股票: {(sub['return'] > 0).sum()}/{len(sub)}")

    # 逐股对比
    pivot = out.pivot_table(index="symbol", columns="mode", values="return")
    if "legacy" in pivot and "optimized" in pivot:
        pivot["delta"] = pivot["optimized"] - pivot["legacy"]
        print("\n=== 逐股收益对比（optimized - legacy）===")
        print((pivot * 100).round(2).to_string())

    out.to_csv("scripts/chenxuezhang_ab_result.csv", index=False)
    print("\n已保存 scripts/chenxuezhang_ab_result.csv")


if __name__ == "__main__":
    main()
