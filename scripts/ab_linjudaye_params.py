"""linjudaye 引擎级 A/B：旧参数 vs 校准参数（带时间止损，熔断已清，2026-08-01）。"""

import json
import logging
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
logging.disable(logging.CRITICAL)  # 静默 loguru 熔断刷屏

from gugu.backtest import BacktestEngine
from gugu.strategies.blogger_rules.linjudaye import LinJuDaYeStrategy

TENCENT_KLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE = "https://qt.gtimg.cn/q={}"
_CTX = ssl._create_unverified_context()
_INDEX = "sh000300"

POOL_40 = [
    "sz002230",
    "sz300308",
    "sz300502",
    "sh688041",
    "sz002371",
    "sh603501",
    "sz000977",
    "sz300661",
    "sz002049",
    "sz300496",
    "sz300033",
    "sz002475",
    "sz300059",
    "sz002415",
    "sz300418",
    "sz300750",
    "sz002594",
    "sz300274",
    "sz002460",
    "sz300014",
    "sh600519",
    "sz000858",
    "sz300760",
    "sh600276",
    "sz000333",
    "sh600030",
    "sz000001",
    "sh601318",
    "sz002142",
    "sz002472",
    "sh600050",
    "sz000725",
    "sh601012",
    "sz002648",
    "sh600900",
    "sz300124",
    "sh601899",
    "sz002601",
    "sh600036",
]
POOL_40 = list(dict.fromkeys(POOL_40))


def _http_get(url, referer="http://finance.qq.com/"):
    req = urllib.request.Request(url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"})
    return (
        urllib.request.urlopen(req, context=_CTX, timeout=20)
        .read()
        .decode("utf-8", errors="ignore")
    )


def fetch_kline(symbol, days=600):
    url = f"{TENCENT_KLINE}?param={symbol},day,,,{days},qfq"
    data = json.loads(_http_get(url))
    node = data.get("data", {}).get(symbol, {})
    kline = node.get("qfqday") or node.get("day") or []
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
    index_df = fetch_kline(_INDEX, 700)
    index_df["date"] = pd.to_datetime(index_df["date"])
    index_df["index_return"] = index_df["close"].pct_change()

    data = {}
    for sym in POOL_40:
        df = fetch_kline(sym, 600)
        if df.empty or len(df) < 250:
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
        time.sleep(0.25)

    print(f"数据就绪: {len(data)} 只")

    configs = [
        (
            "旧参数(0.618/0.5)+时间止损",
            {"golden_level": 0.618, "low_vol_ratio": 0.5, "max_hold_days": 10},
        ),
        (
            "校准参数(0.7/0.7)+时间止损",
            {"golden_level": 0.7, "low_vol_ratio": 0.7, "max_hold_days": 10},
        ),
    ]
    rows = []
    for label, params in configs:
        rets, sharpes, trades, winrates, dds = [], [], [], [], []
        for sym, df in data.items():
            st = LinJuDaYeStrategy(params)
            engine = BacktestEngine(
                initial_capital=100000.0,
                commission_rate=0.00025,
                stamp_tax=0.0005,
                slippage=0.002,
            )
            try:
                r = engine.run(st, df.copy(), sym, index_df=index_df)
                m = r.metrics
                rets.append(m.get("total_return", 0))
                sharpes.append(m.get("sharpe", 0))
                trades.append(m.get("total_trades", 0))
                winrates.append(m.get("win_rate", 0))
                dds.append(m.get("max_drawdown", 0))
            except Exception as e:
                print(f"  {sym} 失败: {e}")
            time.sleep(0.05)
        rows.append(
            {
                "label": label,
                "avg_return": np.mean(rets),
                "avg_sharpe": np.mean(sharpes),
                "total_trades": int(np.sum(trades)),
                "avg_win_rate": np.mean(winrates),
                "avg_maxdd": np.mean(dds),
                "positive_stocks": int(np.sum(np.array(rets) > 0)),
                "n_stocks": len(rets),
            }
        )

    print("\n=== 引擎级 A/B（含时间止损，熔断已清）===")
    for r in rows:
        print(f"\n[{r['label']}]")
        print(f"  平均收益: {r['avg_return']*100:+.2f}%")
        print(f"  平均夏普: {r['avg_sharpe']:+.3f}")
        print(f"  总交易: {r['total_trades']}（{r['total_trades']/r['n_stocks']:.1f}笔/股）")
        print(f"  平均胜率: {r['avg_win_rate']*100:.1f}%")
        print(f"  平均最大回撤: {r['avg_maxdd']*100:.1f}%")
        print(f"  正收益股: {r['positive_stocks']}/{r['n_stocks']}")


if __name__ == "__main__":
    main()
