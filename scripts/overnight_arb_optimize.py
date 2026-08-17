#!/usr/bin/env python3
"""「隔夜套利法」参数调优（2026-08-01）。

方法论（gugu-strategy-backtest skill 铁律）：
- 参数校准 = 训练期选参 + 验证期冻结验证（防前视偏差）
- 训练期: <2025-01-01，验证期: >=2025-01-01
- 超额 = 事件隔夜收益 − 沪深300 同期隔夜
- 候选门槛（训练期）: n>=30 且 超额>0 且 p<0.05
- 扣双边成本 0.15%（佣金+印花税+滑点）展示净期望
- 卖出端 A/B：次日开盘 / 早盘均价 / 冲高 / 收盘
- 市场过滤：沪深300 站上 20 日线

用法: .venv/bin/python scripts/overnight_arb_optimize.py
"""

from __future__ import annotations

import json
import pickle
import ssl
import sys
import urllib.request
from collections import defaultdict
from itertools import product
from math import erf, sqrt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CTX = ssl._create_unverified_context()
CACHE_DIR = ROOT / "data" / "cache_overnight_arb"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 与主脚本同池
POOL = [
    "sh600702",
    "sz002507",
    "sh600779",
    "sh603848",
    "sh603387",
    "sz002901",
    "sh603666",
    "sz002368",
    "sh600728",
    "sz000555",
    "sz002268",
    "sh603236",
    "sz000997",
    "sh600131",
    "sh600271",
    "sh603508",
    "sz002519",
    "sh601890",
    "sh603026",
    "sz000969",
    "sz000949",
    "sh603556",
    "sh603690",
    "sh600125",
    "sz002941",
]
LARGE_CAP_CTRL = ["sh600519", "sh601318", "sh600036", "sz000858"]

TRAIN_CUTOFF = pd.Timestamp("2025-01-01")
COST = 0.0015  # 双边成本 0.15%


def fetch_kline(symbol: str, days: int = 600) -> pd.DataFrame:
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
    req = urllib.request.Request(url, headers={"Referer": "http://finance.qq.com/"})
    data = json.loads(urllib.request.urlopen(req, context=CTX, timeout=20).read())
    k = data["data"][symbol]
    rows = k.get("qfqday") or k.get("day")
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["date", "open", "close", "high", "low", "volume"]
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = df[c].astype(float)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_quote(symbol: str) -> dict:
    url = f"https://qt.gtimg.cn/q={symbol}"
    req = urllib.request.Request(url, headers={"Referer": "http://finance.qq.com/"})
    raw = urllib.request.urlopen(req, context=CTX, timeout=15).read().decode("gbk", errors="ignore")
    parts = raw.split("~")
    if len(parts) <= 46:
        raise RuntimeError(f"字段不足: {len(parts)}")
    return {
        "mcap_yi": float(parts[44]),
        "turnover": float(parts[38]) / 100.0,
        "price": float(parts[3]),
    }


def load_pool() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """拉取/缓存数据。"""
    cache_f = CACHE_DIR / "pool_data.pkl"
    if cache_f.exists():
        with open(cache_f, "rb") as f:
            return pickle.load(f)

    dfs = {}
    for sym in POOL + LARGE_CAP_CTRL:
        try:
            df = fetch_kline(sym)
            q = fetch_quote(sym)
            df["_float_shares"] = q["mcap_yi"] * 1e8 / q["price"]
            latest_vol = df["volume"].iloc[-1]
            raw_factor = float(df["_float_shares"].iloc[-1]) * q["turnover"] / latest_vol
            df["_vol_factor"] = 100 if abs(raw_factor - 100) < abs(raw_factor - 1) else 1
            df["market_cap"] = q["mcap_yi"] * 1e8
            dfs[sym] = df
            print(f"  ✅ {sym} ({len(df)}d, {q['mcap_yi']:.0f}亿)")
        except Exception as e:
            print(f"  ❌ {sym}: {e}")
    index_df = fetch_kline("sh000300")
    with open(cache_f, "wb") as f:
        pickle.dump((dfs, index_df), f)
    return dfs, index_df


def detect(
    df: pd.DataFrame,
    chg_lo: float,
    chg_hi: float,
    turn_lo: float,
    turn_hi: float,
    vr_min: float,
    limit_win: int,
    mcap_max: float,
) -> pd.Series:
    """6 条件检测，返回 bool Series。"""
    d = df
    prev_close = d["close"].shift(1)
    chg = d["close"] / prev_close - 1.0
    vol_ma5 = d["volume"].rolling(5).mean().shift(1)
    vol_ratio = d["volume"] / vol_ma5
    has_limit = chg.shift(1).rolling(limit_win).max() >= 0.098
    turnover = d["volume"] * d["_vol_factor"] / d["_float_shares"]
    typical = (d["high"] + d["low"] + d["close"]) / 3.0
    return (
        (chg >= chg_lo)
        & (chg <= chg_hi)
        & has_limit
        & (vol_ratio > vr_min)
        & (d["market_cap"] < mcap_max)
        & (turnover >= turn_lo)
        & (turnover <= turn_hi)
        & (d["close"] > typical)
    )


def study(
    dfs: dict[str, pd.DataFrame],
    index_df: pd.DataFrame,
    params: dict,
    sell_mode: str = "open",
    mkt_filter: bool = False,
) -> dict:
    """事件研究：返回 train/val 超额统计。"""
    idx = index_df.set_index("date")[["open", "close"]]
    idx["idx_ov"] = idx["open"].shift(-1) / idx["close"] - 1.0
    # 市场过滤：沪深300 站上 20 日线
    idx["ma20"] = idx["close"].rolling(20).mean()
    idx["mkt_ok"] = idx["close"] > idx["ma20"]

    events = []
    for sym, d in dfs.items():
        ev = detect(d, **params)
        ev_idx = d.index[ev]
        for i in ev_idx:
            if i + 1 >= len(d):
                continue
            nxt = d.iloc[i + 1]
            d0 = d.loc[i, "date"]
            if mkt_filter:
                if d0 not in idx.index or not bool(idx.loc[d0, "mkt_ok"]):
                    continue
            entry = d.loc[i, "close"]
            if sell_mode == "open":
                sell = nxt["open"]
            elif sell_mode == "am_avg":  # 早盘均价近似 (open+high)/2
                sell = (nxt["open"] + nxt["high"]) / 2
            elif sell_mode == "high":  # 次日冲高（乐观上限）
                sell = nxt["high"]
            else:  # close
                sell = nxt["close"]
            ret = sell / entry - 1.0
            idx_ov = idx.loc[d0]["idx_ov"] if d0 in idx.index else float("nan")
            events.append(
                {
                    "date": d0,
                    "ret": ret,
                    "excess": ret - idx_ov if pd.notna(idx_ov) else float("nan"),
                }
            )

    def stats(grp: list[dict]) -> dict | None:
        if len(grp) < 5:
            return None
        rets = [e["ret"] for e in grp]
        exc = [e["excess"] for e in grp if pd.notna(e["excess"])]
        n = len(rets)
        mean = sum(rets) / n
        net = mean - COST
        t, p = welch_t(exc) if exc else (0.0, 1.0)
        win = sum(1 for r in rets if r > 0) / n
        return {
            "n": n,
            "mean": mean,
            "net": net,
            "p": p,
            "win": win,
            "exc": sum(exc) / len(exc) if exc else float("nan"),
        }

    train = [e for e in events if e["date"] < TRAIN_CUTOFF]
    val = [e for e in events if e["date"] >= TRAIN_CUTOFF]
    return {"train": stats(train), "val": stats(val), "total": len(events)}


def welch_t(x: list[float]) -> tuple[float, float]:
    n = len(x)
    if n < 2:
        return 0.0, 1.0
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / (n - 1)
    if var == 0:
        return (mean / 1e-9, 0.0) if mean != 0 else (0.0, 1.0)
    se = sqrt(var / n)
    t = mean / se
    p = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))
    return t, p


def main() -> None:
    print("加载数据（缓存优先）...")
    dfs, index_df = load_pool()

    # ── 1. 参数网格扫描（训练期选参）──
    grid = {
        "chg_lo": [0.01, 0.02, 0.03, 0.04, 0.05],
        "chg_hi": [0.03, 0.04, 0.05, 0.06, 0.08],
        "turn_lo": [0.03, 0.05, 0.08],
        "turn_hi": [0.08, 0.10, 0.15],
        "vr_min": [1.0, 1.5, 2.0],
        "limit_win": [10, 20, 30],
        "mcap_max": [100e8, 200e8, 300e8],
    }
    # 简化：先固定部分维度扫核心参数，避免 3645 组合爆炸
    base = {
        "chg_lo": 0.03,
        "chg_hi": 0.05,
        "turn_lo": 0.05,
        "turn_hi": 0.10,
        "vr_min": 1.0,
        "limit_win": 20,
        "mcap_max": 200e8,
    }

    print("\n" + "=" * 100)
    print("【阶段1】核心参数网格扫描（训练期 <2025-01 选参，n>=30 & 超额>0 & p<0.05 为候选）")

    candidates = []
    # 维度1: 涨幅 × 换手（4×3=12）
    for cl, ch in [(0.01, 0.03), (0.02, 0.04), (0.03, 0.05), (0.04, 0.06), (0.05, 0.08)]:
        for tl, th in [(0.03, 0.08), (0.05, 0.10), (0.08, 0.15)]:
            p = {**base, "chg_lo": cl, "chg_hi": ch, "turn_lo": tl, "turn_hi": th}
            r = study(dfs, index_df, p)
            tr = r["train"]
            tag = ""
            if tr and tr["n"] >= 30 and tr["exc"] > 0 and tr["p"] < 0.05:
                tag = " ★候选"
                candidates.append((p, r))
            print(
                f"  涨幅{cl:.0%}-{ch:.0%} 换手{tl:.0%}-{th:.0%} | "
                f"train n={tr['n'] if tr else 0:4d} 超额{tr['exc'] if tr else 0:+.3%} p={tr['p'] if tr else 1:.3f} "
                f"| val n={r['val']['n'] if r['val'] else 0:4d} 超额{r['val']['exc'] if r['val'] else 0:+.3%}{tag}"
            )

    # 维度2: 量比 × 涨停窗口 × 市值（基准涨幅换手）
    print("\n【阶段2】量比 × 涨停窗口 × 市值（基准涨幅3-5%/换手5-10%）")
    for vr in [1.0, 1.5, 2.0]:
        for lw in [10, 20, 30]:
            for mc in [100e8, 200e8, 300e8]:
                p = {**base, "vr_min": vr, "limit_win": lw, "mcap_max": mc}
                r = study(dfs, index_df, p)
                tr = r["train"]
                tag = ""
                if tr and tr["n"] >= 30 and tr["exc"] > 0 and tr["p"] < 0.05:
                    tag = " ★候选"
                    candidates.append((p, r))
                print(
                    f"  量比>{vr:.1f} 涨停窗{lw}日 市值<{mc/1e8:.0f}亿 | "
                    f"train n={tr['n'] if tr else 0:4d} 超额{tr['exc'] if tr else 0:+.3%} p={tr['p'] if tr else 1:.3f} "
                    f"| val n={r['val']['n'] if r['val'] else 0:4d} 超额{r['val']['exc'] if r['val'] else 0:+.3%}{tag}"
                )

    # ── 2. 候选参数验证期冻结验证 ──
    print("\n" + "=" * 100)
    print("【阶段3】候选参数 → 验证期（>=2025-01 参数冻结）验证")
    if not candidates:
        print("  ❌ 训练期无候选：没有任何参数组合在训练期显著为正")
    else:
        seen = set()
        for p, r in candidates:
            key = tuple(sorted(p.items()))
            if key in seen:
                continue
            seen.add(key)
            v = r["val"]
            verdict = (
                "✅ 验证期显著正"
                if v and v["n"] >= 30 and v["exc"] > 0 and v["p"] < 0.05
                else ("⚠️ 方向正但不显著" if v and v["exc"] > 0 else "❌ 验证期失效")
            )
            print(
                f"  {p} | train n={r['train']['n']} 超额{r['train']['exc']:+.3%} | "
                f"val n={v['n'] if v else 0} 净期望{(v['net'] if v else 0):+.3%} 超额{(v['exc'] if v else 0):+.3%} p={(v['p'] if v else 1):.3f} {verdict}"
            )

    # ── 3. 卖出端 A/B（基准参数）──
    print("\n" + "=" * 100)
    print("【阶段4】卖出端 A/B（基准参数，验证期）：open / 早盘均价 / 冲高 / 收盘")
    for mode, label in [
        ("open", "次日开盘(铁律)"),
        ("am_avg", "次日早盘均价"),
        ("high", "次日冲高(上限)"),
        ("close", "次日收盘"),
    ]:
        r = study(dfs, index_df, base, sell_mode=mode)
        v = r["val"]
        if v:
            print(
                f"  {label:14s} val n={v['n']:4d} 均值{v['mean']:+.3%} 净{v['net']:+.3%} 超额{v['exc']:+.3%}(p={v['p']:.3f}) 胜率{v['win']:.0%}"
            )

    # ── 4. 市场过滤 A/B（基准参数）──
    print("\n" + "=" * 100)
    print("【阶段5】市场环境过滤（沪深300>MA20，基准参数）")
    for mf, label in [(False, "不过滤"), (True, "沪深300>MA20")]:
        r = study(dfs, index_df, base, mkt_filter=mf)
        tr, v = r["train"], r["val"]
        print(
            f"  {label:14s} train n={tr['n'] if tr else 0:4d} 超额{tr['exc'] if tr else 0:+.3%} | "
            f"val n={v['n'] if v else 0:4d} 超额{v['exc'] if v else 0:+.3%}(p={v['p'] if v else 1:.3f})"
        )


if __name__ == "__main__":
    main()
