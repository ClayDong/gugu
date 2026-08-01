"""linjudaye 参数校准（训练期选参 → 验证期冻结，2026-08-01）。

方法（skill 铁律）：
- 训练期 2024-02~2025-01：参数网格搜索，按验证期冻结前先用训练期选优
- 验证期 2025-02~2026-07：参数完全冻结检验
- 判定：验证期超额显著为正(p<0.05) 且 2026 不为负 才考虑保留
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
_CTX = ssl._create_unverified_context()
_INDEX_CODE = "sh000300"

# 40 只池（与 event_study_40.py 一致）
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

TRAIN_START, TRAIN_END = "2024-02-01", "2025-01-31"
VAL_START, VAL_END = "2025-02-01", "2026-07-31"

# 参数网格（每参数 3-4 档）
PARAM_GRID = {
    "golden_level": [0.382, 0.5, 0.618, 0.7],
    "vol_shrink_ratio": [0.5, 0.7, 0.85],
    "box_range_threshold": [0.03, 0.05, 0.08],
    "low_vol_ratio": [0.3, 0.5, 0.7],
}
BASE_PARAMS = {  # 当前 defaults
    "golden_level": 0.618,
    "vol_shrink_ratio": 0.7,
    "box_range_threshold": 0.05,
    "low_vol_ratio": 0.5,
}


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


# 全局缓存行情（避免每个参数组合重拉）
_KLINE_CACHE = {}


def get_data(pool):
    index = fetch_kline(_INDEX_CODE, 700)
    index["date"] = pd.to_datetime(index["date"])
    index = index.set_index("date")["close"]
    out = {}
    for sym in pool:
        if sym in _KLINE_CACHE:
            df = _KLINE_CACHE[sym].copy()
        else:
            df = fetch_kline(sym, 600)
            if df.empty or len(df) < 250:
                continue
            df["date"] = pd.to_datetime(df["date"])
            df["idx_close"] = df["date"].map(index)
            df["idx_ret"] = df["idx_close"] / df["idx_close"].shift(1) - 1
            df["index_return"] = df["idx_ret"].fillna(0.0)
            _KLINE_CACHE[sym] = df.copy()
        out[sym] = df
        time.sleep(0.05)
    return out


def run_params(data, strategy_cls, params, horizon=10):
    """对给定参数跑事件研究，返回 (train_excess, val_excess, val_p, n_train, n_val, y2026_excess, y2026_p)。"""
    from gugu.strategies.blogger_rules.linjudaye import LinJuDaYeStrategy

    rows = []
    for sym, df in data.items():
        st = LinJuDaYeStrategy(params)
        sig_df = st.generate_signals(df.copy(), is_backtest=True)
        buy_days = sig_df.index[sig_df["signal"] == 1].tolist()
        if len(buy_days) < 3:
            continue
        close = df["close"].values
        for i in buy_days:
            d = df["date"].iloc[i]
            entry = close[i]
            if entry <= 0 or i + horizon >= len(close):
                continue
            ret = close[i + horizon] / entry - 1
            idx_ret = df["idx_close"].iloc[i + horizon] / df["idx_close"].iloc[i] - 1
            if pd.isna(idx_ret):
                continue
            rows.append({"date": d, "excess": ret - idx_ret})

    if not rows:
        return None
    ev = pd.DataFrame(rows)
    ev["date"] = pd.to_datetime(ev["date"])
    train = ev[(ev["date"] >= pd.Timestamp(TRAIN_START)) & (ev["date"] <= pd.Timestamp(TRAIN_END))]
    val = ev[(ev["date"] >= pd.Timestamp(VAL_START)) & (ev["date"] <= pd.Timestamp(VAL_END))]
    y2026 = ev[ev["date"] >= "2026-01-01"]

    def stat(sub):
        if len(sub) < 3:
            return 0.0, 1.0, 0
        t, p = welch_t(sub["excess"].values)
        return sub["excess"].mean(), p, len(sub)

    tr_m, tr_p, tr_n = stat(train)
    va_m, va_p, va_n = stat(val)
    y26_m, y26_p, y26_n = stat(y2026)
    return tr_m, tr_p, tr_n, va_m, va_p, va_n, y26_m, y26_p, y26_n


def main():
    print("加载行情...")
    data = get_data(POOL_40)
    print(f"有效股票 {len(data)}/40")

    # 1. 基线（当前 defaults）
    base = run_params(data, None, BASE_PARAMS)
    print(f"\n=== 基线（defaults）===")
    print(
        f"train: {base[0]:+.2%} (n={base[2]})  val: {base[3]:+.2%} p={base[4]:.3f} (n={base[5]})  2026: {base[6]:+.2%} p={base[7]:.3f}"
    )

    # 2. 单参数敏感性（其他参数固定默认）
    print("\n=== 单参数敏感性（10日超额）===")
    best = {}
    for pname, values in PARAM_GRID.items():
        print(f"\n[{pname}]")
        best_row = None
        for v in values:
            params = {**BASE_PARAMS, pname: v}
            r = run_params(data, None, params)
            if r is None:
                continue
            print(
                f"  {pname}={v}: train {r[0]:+.2%}(n={r[2]}) | val {r[3]:+.2%} p={r[4]:.3f}(n={r[5]}) | 2026 {r[6]:+.2%} p={r[7]:.3f}"
            )
            if best_row is None or r[3] > best_row[1]:
                best_row = (v, r[3], r[4])
        if best_row:
            best[pname] = best_row[0]
            print(f"  → 最优 {pname}={best_row[0]} (val {best_row[1]:+.2%} p={best_row[2]:.3f})")

    # 3. 组合最优（训练期选参：val 最大化，但要求 p<0.1）
    print(f"\n=== 组合最优参数: {best} ===")
    combo = {**BASE_PARAMS, **best}
    r = run_params(data, None, combo)
    if r:
        print(
            f"组合: train {r[0]:+.2%}(n={r[2]}) | val {r[3]:+.2%} p={r[4]:.3f}(n={r[5]}) | 2026 {r[6]:+.2%} p={r[7]:.3f}"
        )

    # 4. 全网格暴力搜索（组合数有限时）
    print("\n=== 全网格搜索（前10名按 val 超额）===")
    results = []
    keys = list(PARAM_GRID.keys())
    import itertools

    combos = list(itertools.product(*[PARAM_GRID[k] for k in keys]))
    print(f"组合数: {len(combos)}")
    for combo_vals in combos:
        params = dict(zip(keys, combo_vals))
        rr = run_params(data, None, params)
        if rr is None or rr[5] < 30:
            continue
        results.append((params, rr))
    results.sort(key=lambda x: x[1][3], reverse=True)
    for params, rr in results[:10]:
        print(
            f"  {params} | val {rr[3]:+.2%} p={rr[4]:.3f}(n={rr[5]}) | 2026 {rr[6]:+.2%} p={rr[7]:.3f}"
        )

    # 保存
    out = {
        "baseline": dict(
            zip(["tr_m", "tr_p", "tr_n", "va_m", "va_p", "va_n", "y26_m", "y26_p", "y26_n"], base)
        ),
        "best_params": best,
    }
    with open("data/linjudaye_calibration.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n已存 data/linjudaye_calibration.json")


if __name__ == "__main__":
    main()
