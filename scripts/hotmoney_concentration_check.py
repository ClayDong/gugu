"""hot_money 集中股补充分析：sh688041 单独 vs 剔除后表现。"""

import json

import numpy as np
import pandas as pd

with open("data/hotmoney_event_study.json") as f:
    data = json.load(f)

ev = pd.DataFrame(data["events"])
ev["date"] = pd.to_datetime(ev["date"])


def welch_t(a, b):
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
    p = 1 - erf(abs(t) / sqrt(2))
    return t, p


# 1. sh688041 单独
top = ev[ev["code"] == "sh688041"]
print(f"=== sh688041 单独: n={len(top)} ===")
for h in (3, 5, 10):
    for col in (f"ret_{h}d", f"excess_{h}d"):
        s = top[col].dropna()
        if len(s) >= 3:
            t, p = welch_t(s.values, np.zeros(1))
            print(f"  {col}: {s.mean():+7.2%}  p={p:.3f}")

# 2. 剔除 sh688041 后 val 期分年
others = ev[ev["code"] != "sh688041"]
others["year"] = others["date"].dt.year
print("\n=== 剔除 sh688041 后分年超额 ===")
for y, g in others.groupby("year"):
    for h in (5, 10):
        col = f"excess_{h}d"
        s = g[col].dropna()
        if len(s) >= 3:
            t, p = welch_t(s.values, np.zeros(1))
            print(f"  {y}: n={len(g)} {col}: {s.mean():+7.2%} p={p:.3f}")

# 3. val 期剔除后完整统计
val_others = others[(others["date"] >= "2025-02-01") & (others["date"] <= "2026-07-31")]
print(f"\n=== val 期剔除集中股 n={len(val_others)} ===")
for h in (3, 5, 10):
    for col in (f"ret_{h}d", f"excess_{h}d"):
        s = val_others[col].dropna()
        if len(s) >= 3:
            t, p = welch_t(s.values, np.zeros(1))
            print(f"  {col}: {s.mean():+7.2%}  p={p:.3f}")
