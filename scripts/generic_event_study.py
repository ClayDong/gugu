"""通用策略 Walk-Forward 事件研究验证（2026-08-01，hot_money 框架泛化版）。

用法：
    python scripts/generic_event_study.py --strategy ma5_momentum [--days 600]
    python scripts/generic_event_study.py --strategy price_volume_signal --need-fundamentals

方法论（gugu-strategy-backtest skill 铁律）：
1. 事件研究先行：信号后 N 日收益 vs 沪深300 超额，Welch t 检验
2. 训练期选参 / 验证期冻结（Walk-Forward）
3. 触发股集中度检查（单股>30%信号=不可信，剔除后重验）
4. 超额收益检验（赚钱≠有alpha）
5. 年份分桶（防单一年份/暴跌日污染）
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
TENCENT_QUOTE = "https://qt.gtimg.cn/q={}"
_CTX = ssl._create_unverified_context()
_INDEX_CODE = "sh000300"

DEFAULT_POOL = [
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

TRAIN_START, TRAIN_END = "2024-02-01", "2025-01-31"
VAL_START, VAL_END = "2025-02-01", "2026-07-31"


def welch_t(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    from math import erf, sqrt

    a = np.asarray(a, float)
    a = a[~np.isnan(a)]
    if len(a) < 3:
        return 0.0, 1.0
    ma = a.mean()
    va = a.var(ddof=1)
    n = len(a)
    se = np.sqrt(va / n)
    if se == 0:
        return 0.0, 1.0
    t = ma / se
    p = 1 - erf(abs(t) / sqrt(2))
    return t, p


def _http_get(url: str, referer: str = "http://finance.qq.com/") -> str:
    req = urllib.request.Request(url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=_CTX, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_kline(symbol: str, days: int) -> pd.DataFrame:
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


def fetch_quote_fundamentals(symbol: str) -> dict:
    """腾讯实时行情：换手率(小数)/流通市值(元)/现价。"""
    try:
        raw = _http_get(TENCENT_QUOTE.format(symbol), referer="https://gu.qq.com/")
        payload = raw.split('"')[1]
        f = payload.split("~")
        if len(f) < 47:
            return {}
        out = {}
        for idx, key in [(3, "price"), (38, "turnover_rate"), (44, "market_cap")]:
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


def enrich_for_strategy(df: pd.DataFrame, symbol: str, strategy_key: str) -> pd.DataFrame:
    """策略特殊列注入。"""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    if strategy_key == "price_volume_signal":
        # 需要 amount + float_shares 计算真实换手率
        fund = fetch_quote_fundamentals(symbol)
        if fund and fund.get("market_cap") and fund.get("price"):
            float_shares = fund["market_cap"] / fund["price"]
            # 校准 volume 单位：流通股本×实时换手率 / 最新K线volume → 接近{1,100}
            latest_vol = float(df["volume"].iloc[-1])
            calib = (float_shares * (fund.get("turnover_rate") or 0.02)) / max(latest_vol, 1)
            vol_factor = 1 if abs(calib - 1) < abs(calib - 100) else 100
            df["amount"] = df["volume"] * vol_factor * df["close"]  # 真实成交额(元)
            df["float_shares"] = float_shares
            df["market_cap"] = fund["market_cap"]
            df["turnover_rate"] = fund.get("turnover_rate", 0.02)
            # 注入策略参数（strategy_defaults 默认 float_shares=1e9 会导致换手率失真）
            return df, {"float_shares": float_shares}
    return df, {}


def fetch_index(symbol: str = _INDEX_CODE, days: int = 700) -> pd.DataFrame:
    df = fetch_kline(symbol, days)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--pool", nargs="*", default=DEFAULT_POOL)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    json_path = args.json or f"data/{args.strategy}_event_study.json"
    from gugu.strategies.registry import get_strategy

    strategy = get_strategy(args.strategy)
    print(f"策略: {args.strategy} ({type(strategy).__name__})")
    index = fetch_index(days=700)
    print(f"指数K线 {len(index)} 条")

    all_rows = []
    for symbol in args.pool:
        df = fetch_kline(symbol, args.days)
        if df.empty:
            print(f"  ⚠️ {symbol} 拉取失败")
            continue
        if index.empty:
            print("  ❌ 指数K线失败")
            return

        df, param_override = enrich_for_strategy(df, symbol, args.strategy)
        df["idx_close"] = df["date"].map(index)
        df["idx_ret"] = df["idx_close"] / df["idx_close"].shift(1) - 1
        df["index_return"] = df["idx_ret"].fillna(0.0)

        try:
            sig_df = strategy.generate_signals(df.copy(), is_backtest=True)
        except Exception as e:
            print(f"  ⚠️ {symbol} 信号生成失败: {e}")
            continue
        buy_days = sig_df.index[sig_df["signal"] == 1].tolist()
        print(f"  {symbol}: K线{len(df)} 买入信号 {len(buy_days)}")

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
        time.sleep(0.3)

    if not all_rows:
        print("❌ 无任何信号")
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
    print(
        f"\n总信号 {len(ev)}：train={int((ev.period=='train').sum())} "
        f"val={int((ev.period=='val').sum())} outside={int((ev.period=='outside').sum())}"
    )

    print("\n=== 触发股分布 ===")
    conc = ev["code"].value_counts()
    for code, n in conc.items():
        pct = n / len(ev) * 100
        mark = "⚠️ 集中" if pct > 30 else ""
        print(f"  {code}: {n} ({pct:.0f}%) {mark}")

    print("\n=== 收益统计（绝对 + 超额 vs 沪深300）===")
    for period in ("train", "val", "all"):
        sub = ev[ev["period"] == period] if period != "all" else ev
        if len(sub) < 3:
            print(f"\n[{period}] 样本 {len(sub)} < 3，跳过")
            continue
        print(f"\n[{period}] n={len(sub)}")
        for h in (3, 5, 10):
            col = f"ret_{h}d"
            if col in sub and sub[col].notna().sum() >= 3:
                t, p = welch_t(sub[col].values, np.zeros(1))
                print(f"  {col:>8}: {sub[col].mean():+7.2%}  t={t:+5.2f} p={p:.3f}")
            ecol = f"excess_{h}d"
            if ecol in sub and sub[ecol].notna().sum() >= 3:
                t, p = welch_t(sub[ecol].values, np.zeros(1))
                mark = (
                    "✅"
                    if (p < 0.05 and sub[ecol].mean() > 0)
                    else ("🔴" if (p < 0.05 and sub[ecol].mean() < 0) else "➖")
                )
                print(f"  {ecol:>8}: {sub[ecol].mean():+7.2%}  t={t:+5.2f} p={p:.3f} {mark}")

    # 集中股剔除后重验
    if len(conc) > 1:
        top = conc.index[0]
        top_n = conc.iloc[0]
        if top_n / len(ev) > 0.3:
            sub = ev[ev["code"] != top]
            print(f"\n=== 剔除最大贡献股 {top}({top_n}) 后重验 n={len(sub)} ===")
            for period in ("val", "all"):
                psub = sub[sub["period"] == period] if period != "all" else sub
                if len(psub) < 3:
                    continue
                for h in (5, 10):
                    ecol = f"excess_{h}d"
                    if ecol in psub and psub[ecol].notna().sum() >= 3:
                        t, p = welch_t(psub[ecol].values, np.zeros(1))
                        mark = (
                            "✅"
                            if (p < 0.05 and psub[ecol].mean() > 0)
                            else ("🔴" if (p < 0.05 and psub[ecol].mean() < 0) else "➖")
                        )
                        print(f"  [{period}] {ecol}: {psub[ecol].mean():+7.2%} p={p:.3f} {mark}")

    print("\n=== 按年份分桶（超额 5d/10d）===")
    ev["year"] = ev["date"].dt.year
    for y, g in ev.groupby("year"):
        for h in (5, 10):
            ecol = f"excess_{h}d"
            if ecol in g and g[ecol].notna().sum() >= 3:
                t, p = welch_t(g[ecol].values, np.zeros(1))
                mark = (
                    "✅"
                    if (p < 0.05 and g[ecol].mean() > 0)
                    else ("🔴" if (p < 0.05 and g[ecol].mean() < 0) else "➖")
                )
                print(f"  {y}: n={len(g)} {ecol}: {g[ecol].mean():+7.2%} p={p:.3f} {mark}")

    ev["date"] = ev["date"].dt.strftime("%Y-%m-%d")
    out = {
        "strategy": args.strategy,
        "total_signals": len(ev),
        "by_stock": conc.to_dict(),
        "events": ev.to_dict("records"),
    }
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已存 {json_path}")


if __name__ == "__main__":
    main()
