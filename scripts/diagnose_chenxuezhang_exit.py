"""chenxuezhang 出场逻辑诊断：持仓周期 + 买卖配对分析。

验证假设：R15 尾盘骗线（日涨>2%卖）和 R25 量价背离（价涨量缩卖）导致出场过早，
在题材股上涨初期就砍掉利润（事件研究持有10日 +1.02% vs 引擎回测夏普-0.15）。
"""

import json
import ssl
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

TENCENT_KLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q={}"
_CTX = ssl._create_unverified_context()
_INDEX_CODE = "sh000300"

POOL = ["sz300502", "sz300059", "sz300308", "sh688041", "sz002475"]  # 2正3负代表


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


def enrich(df, symbol):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    fund = fetch_quote(symbol)
    if fund:
        if fund.get("market_cap") is not None:
            df["market_cap"] = fund["market_cap"]
        if fund.get("turnover_rate") is not None:
            df["turnover_rate"] = fund["turnover_rate"]
        if fund.get("pe") is not None:
            df["pe"] = fund["pe"]
    index = fetch_kline(_INDEX_CODE, 700)
    if not index.empty:
        index["date"] = pd.to_datetime(index["date"])
        idx_map = index.set_index("date")["close"]
        df["idx_close"] = df["date"].map(idx_map)
        df["idx_ret"] = df["idx_close"] / df["idx_close"].shift(1) - 1
        df["index_return"] = df["idx_ret"].fillna(0.0)
    return df


def analyze_hold_period(symbol):
    from gugu.strategies.registry import get_strategy

    df = enrich(fetch_kline(symbol), symbol)
    strategy = get_strategy("chenxuezhang")
    sig = strategy.generate_signals(df.copy(), is_backtest=True)

    # 信号统计
    buys = sig.index[sig["signal"] == 1].tolist()
    sells = sig.index[sig["signal"] == -1].tolist()

    # 买卖配对（模拟引擎逻辑：买入后遇卖出即平）
    pairs = []
    open_i = None
    for i in range(len(sig)):
        if sig["signal"].iloc[i] == 1 and open_i is None:
            open_i = i
        elif sig["signal"].iloc[i] == -1 and open_i is not None:
            hold = i - open_i
            ret = df["close"].iloc[i] / df["close"].iloc[open_i] - 1
            pairs.append((hold, ret, df["date"].iloc[open_i], df["date"].iloc[i]))
            open_i = None

    # 卖出信号触发前一日涨幅分布（看是不是"涨2%就卖"）
    sell_day_returns = []
    for i in sells:
        if i > 0:
            r = df["close"].iloc[i] / df["close"].iloc[i - 1] - 1
            sell_day_returns.append(r)

    print(f"\n=== {symbol} ===")
    print(f"买入信号 {len(buys)}，卖出信号 {len(sells)}，配对 {len(pairs)}")
    if pairs:
        holds = [p[0] for p in pairs]
        rets = [p[1] for p in pairs]
        print(
            f"持仓周期: 均值 {np.mean(holds):.1f}日 中位 {np.median(holds):.0f}日 分布 {sorted(holds)[:10]}"
        )
        print(
            f"配对收益: 均值 {np.mean(rets)*100:+.1f}% 正收益 {(np.array(rets) > 0).mean()*100:.0f}%"
        )
        print(
            f"卖出时持仓天数分布: <=2日 {(np.array(holds) <= 2).mean()*100:.0f}% | <=5日 {(np.array(holds) <= 5).mean()*100:.0f}%"
        )
    if sell_day_returns:
        arr = np.array(sell_day_returns)
        print(
            f"卖出日当日涨幅: 均值 {arr.mean()*100:+.2f}% | >2%占比 {(arr > 0.02).mean()*100:.0f}% | >1%占比 {(arr > 0.01).mean()*100:.0f}%"
        )
    return symbol, len(buys), len(sells), pairs, sell_day_returns


def main():
    summary = []
    for sym in POOL:
        try:
            r = analyze_hold_period(sym)
            summary.append(r)
        except Exception as e:
            print(f"  {sym} 诊断失败: {e}")

    print("\n\n=== 汇总 ===")
    print(f"{'代码':<10} {'持仓均值':<8} {'<=2日占比':<10} {'卖出日>2%占比':<12} {'配对正收益'}")
    for sym, nb, ns, pairs, sdr in summary:
        if pairs:
            holds = np.array([p[0] for p in pairs])
            rets = np.array([p[1] for p in pairs])
            sdr_arr = np.array(sdr)
            print(
                f"{sym:<10} {np.mean(holds):<8.1f} {(holds<=2).mean()*100:<10.0f}% "
                f"{(sdr_arr>0.02).mean()*100:<12.0f}% {(rets>0).mean()*100:.0f}%"
            )


if __name__ == "__main__":
    main()
