"""ml_gradient_boost 集中度 + 板块偏差检查。"""

import json
from math import erf, sqrt

import numpy as np
import pandas as pd

data = json.load(open("data/ml_gradient_boost_event_study.json"))
ev = pd.DataFrame(data["events"])
ev["date"] = pd.to_datetime(ev["date"])


def welch_t(a):
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


print("=== 触发股分布 ===")
conc = ev["code"].value_counts()
for code, n in conc.items():
    pct = n / len(ev) * 100
    mark = "⚠️集中" if pct > 30 else ""
    print(f"  {code}: {n} ({pct:.0f}%) {mark}")

top = conc.index[0]
others = ev[ev["code"] != top]
val_others = others[(others["date"] >= "2025-02-01") & (others["date"] <= "2026-07-31")]
print(f"\n剔除 {top}({conc.iloc[0]}) 后 val 期 n={len(val_others)}")

for h in (5, 10):
    col = f"excess_{h}d"
    s = val_others[col].dropna()
    if len(s) >= 3:
        t, p = welch_t(s.values)
        mark = "✅" if p < 0.05 and s.mean() > 0 else "➖"
        print(f"  {col}: {s.mean():+.2%} p={p:.3f} {mark}")

kcb = ev[ev["code"].str.startswith("sh688")]
print(f"\n科创板占比: {len(kcb)}/{len(ev)} = {len(kcb)/len(ev)*100:.0f}%")

# 2026年单独看剔除后
y2026 = val_others[val_others["date"] >= "2026-01-01"]
print(f"2026 剔除集中股后 n={len(y2026)}")
for h in (5, 10):
    col = f"excess_{h}d"
    s = y2026[col].dropna()
    if len(s) >= 3:
        t, p = welch_t(s.values)
        mark = "✅" if p < 0.05 and s.mean() > 0 else ("🔴" if p < 0.05 else "➖")
        print(f"  {col}: {s.mean():+.2%} p={p:.3f} {mark}")
