"""linjudaye 扩池事件研究（40只，2026-08-01）。

目的：
1. 扩池验证信号层有效性（15股→40股，排除小样本偏差）
2. 分年/分规则有效性
3. 为参数校准提供基线
"""

import argparse
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
_CTX = ssl._create_unverified_context()
_INDEX_CODE = "sh000300"

# 40 只跨板块活跃股（覆盖科技/消费/金融/新能源/周期）
POOL_40 = [
    # 科技/算力/半导体
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
    # AI/软件/传媒
    "sz300033",
    "sz002475",
    "sz300059",
    "sz002415",
    "sz300418",
    # 新能源/汽车
    "sz300750",
    "sz002594",
    "sz300274",
    "sz002460",
    "sz300014",
    # 消费/医药
    "sh600519",
    "sz000858",
    "sz300760",
    "sh600276",
    "sz000333",
    # 金融/券商
    "sz300059",
    "sh600030",
    "sz000001",
    "sh601318",
    "sz002142",
    # 周期/制造
    "sz002472",
    "sh600050",
    "sz000725",
    "sh601012",
    "sz002648",
    # 其他活跃
    "sh600900",
    "sz300124",
    "sh601899",
    "sz002601",
    "sh600036",
]
POOL_40 = list(dict.fromkeys(POOL_40))  # 去重

TRAIN_START, TRAIN_END = "2024-02-01", "2025-01-31"
VAL_START, VAL_END = "2025-02-01", "2026-07-31"


def welch_t(a):
    from math import erf, sqrt

    a = np.asarray(a, float)
    a = a[~np.isnan(a)]
    if len(a) < 3:
        return 0.0, 1.0
    ma, va, n = a.mean(), a.var(ddof=1), len(a)
    se = np.sqrt(va / n)
    if se == 0:
        return 0.0, 1.0
    t = ma / se
    return t, 1 - erf(abs(t) / sqrt(2))


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="linjudaye")
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    from gugu.strategies.registry import get_strategy

    strategy = get_strategy(args.strategy)
    index = fetch_kline(_INDEX_CODE, 700)
    index["date"] = pd.to_datetime(index["date"])
    index = index.set_index("date")["close"]
    print(f"策略: {args.strategy}  指数 {len(index)} 行  池 {len(POOL_40)} 只")

    all_rows = []
    ok = 0
    for symbol in POOL_40:
        df = fetch_kline(symbol, args.days)
        if df.empty or len(df) < 250:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df["idx_close"] = df["date"].map(index)
        df["idx_ret"] = df["idx_close"] / df["idx_close"].shift(1) - 1
        df["index_return"] = df["idx_ret"].fillna(0.0)

        sig_df = strategy.generate_signals(df.copy(), is_backtest=True)
        buy_days = sig_df.index[sig_df["signal"] == 1].tolist()
        if len(buy_days) < 5:
            continue  # 信号过少的股票跳过（可能是蓝筹不适配）
        ok += 1
        close = df["close"].values
        for i in buy_days:
            d = df["date"].iloc[i]
            entry = close[i]
            if entry <= 0:
                continue
            row = {"code": symbol, "date": d, "entry": entry}
            for h in (3, 5, 10):
                if i + h < len(close) and close[i + h] > 0:
                    row[f"ret_{h}d"] = close[i + h] / entry - 1
            for h in (3, 5, 10):
                if i + h < len(df):
                    idx_ret = df["idx_close"].iloc[i + h] / df["idx_close"].iloc[i] - 1
                    if pd.notna(idx_ret) and f"ret_{h}d" in row:
                        row[f"excess_{h}d"] = row[f"ret_{h}d"] - idx_ret
            all_rows.append(row)
        time.sleep(0.25)

    if not all_rows:
        print("❌ 无信号")
        return
    ev = pd.DataFrame(all_rows)
    ev["date"] = pd.to_datetime(ev["date"])
    ev["period"] = np.where(
        (ev["date"] >= pd.Timestamp(TRAIN_START)) & (ev["date"] <= pd.Timestamp(TRAIN_END)),
        "train",
        np.where(
            (ev["date"] >= pd.Timestamp(VAL_START)) & (ev["date"] <= pd.Timestamp(VAL_END)),
            "val",
            "outside",
        ),
    )
    ev["year"] = ev["date"].dt.year

    print(
        f"\n有效股票 {ok}/40  总信号 {len(ev)}（train={int((ev.period=='train').sum())} "
        f"val={int((ev.period=='val').sum())}）"
    )

    print("\n=== 触发股分布（Top10）===")
    conc = ev["code"].value_counts()
    for code, n in conc.head(10).items():
        pct = n / len(ev) * 100
        mark = "⚠️集中" if pct > 30 else ""
        print(f"  {code}: {n} ({pct:.0f}%) {mark}")

    print("\n=== 收益统计（超额 vs 沪深300）===")
    for period in ("train", "val", "all"):
        sub = ev[ev["period"] == period] if period != "all" else ev
        if len(sub) < 3:
            continue
        print(f"\n[{period}] n={len(sub)}")
        for h in (3, 5, 10):
            ecol = f"excess_{h}d"
            if ecol in sub and sub[ecol].notna().sum() >= 3:
                t, p = welch_t(sub[ecol].values)
                mark = (
                    "✅"
                    if (p < 0.05 and sub[ecol].mean() > 0)
                    else ("🔴" if (p < 0.05 and sub[ecol].mean() < 0) else "➖")
                )
                print(f"  {ecol}: {sub[ecol].mean():+7.2%}  p={p:.3f} {mark}")

    print("\n=== 按年份分桶（超额 5d/10d）===")
    for y, g in ev.groupby("year"):
        for h in (5, 10):
            ecol = f"excess_{h}d"
            if ecol in g and g[ecol].notna().sum() >= 3:
                t, p = welch_t(g[ecol].values)
                mark = (
                    "✅"
                    if (p < 0.05 and g[ecol].mean() > 0)
                    else ("🔴" if (p < 0.05 and g[ecol].mean() < 0) else "➖")
                )
                print(f"  {y}: n={len(g)} {ecol}: {g[ecol].mean():+7.2%} p={p:.3f} {mark}")

    # 剔除最大集中股重验
    if len(conc) > 1 and conc.iloc[0] / len(ev) > 0.3:
        top = conc.index[0]
        others = ev[ev["code"] != top]
        print(f"\n=== 剔除 {top}({conc.iloc[0]}) 后重验 ===")
        for period in ("val", "all"):
            psub = others[others["period"] == period] if period != "all" else others
            if len(psub) < 3:
                continue
            for h in (5, 10):
                ecol = f"excess_{h}d"
                if ecol in psub and psub[ecol].notna().sum() >= 3:
                    t, p = welch_t(psub[ecol].values)
                    mark = "✅" if (p < 0.05 and psub[ecol].mean() > 0) else "➖"
                    print(f"  [{period}] {ecol}: {psub[ecol].mean():+7.2%} p={p:.3f} {mark}")

    json_path = args.json or f"data/{args.strategy}_event_study_40.json"
    ev["date"] = ev["date"].dt.strftime("%Y-%m-%d")
    with open(json_path, "w") as f:
        json.dump(
            {"strategy": args.strategy, "total": len(ev), "events": ev.to_dict("records")},
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    print(f"\n已存 {json_path}")


if __name__ == "__main__":
    main()
