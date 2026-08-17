#!/usr/bin/env python3
"""实时信号附图 + 视觉评分 —— 对 scan_rebound_market.py 命中信号即时渲染K线图并打分
用法: python3 scripts/score_rebound_signals.py [--limit 20]
输出: data/rebound_charts/live_{code}_{date}.png + 评分表(飞书卡片用)
"""

import json, os, ssl, sys, time
import urllib.request
import pandas as pd
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Hiragino Sans GB"]
plt.rcParams["axes.unicode_minus"] = False
ssl._create_default_https_context = ssl._create_unverified_context

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "http://finance.qq.com/",
}
OUT_DIR = "/Users/dong/workspace/gugu/data/rebound_charts"
SIG_FILE = "/Users/dong/workspace/gugu/data/rebound_signals.jsonl"
UP, DOWN = "#e64545", "#1aad5a"


def fetch_kline(code, days=120, retries=3):
    endpoints = [
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq",
        f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq",
    ]
    for attempt in range(retries):
        for url in endpoints:
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
                node = data.get("data", {}).get(code, {})
                rows = node.get("qfqday") or node.get("day") or []
                if not rows:
                    continue
                df = pd.DataFrame(
                    [r[:6] for r in rows],
                    columns=["date", "open", "close", "high", "low", "volume"],
                )
                df["date"] = pd.to_datetime(df["date"])
                for c in ["open", "close", "high", "low", "volume"]:
                    df[c] = df[c].astype(float)
                return df
            except Exception:
                continue
        time.sleep(1 + attempt)
    return None


def visual_score(d, ev_local, entry):
    """与 gen_rebound_charts.py 相同的视觉评分卡(8特征)"""
    s = 0.0
    ev = d.iloc[ev_local]
    after5 = d.iloc[ev_local + 1 : min(len(d), ev_local + 6)]
    if len(after5):
        sd = after5["low"].min() / entry - 1
        if sd > -0.03:
            s += 1.5
        elif sd > -0.06:
            s += 0.8
        elif sd > -0.10:
            s += 0.3
    after3 = d.iloc[ev_local + 1 : min(len(d), ev_local + 4)]
    if len(after3) >= 2:
        mdd = after3["low"].min() / ev["close"] - 1
        if mdd > -0.03:
            s += 1.5
        elif mdd > -0.06:
            s += 0.8
    # 反包(断板后10日内>3%阳)
    reb_day, reb_vol = 99, np.nan
    for i in range(ev_local + 1, min(len(d), ev_local + 11)):
        if d.iloc[i]["close"] / d.iloc[i]["open"] - 1 > 0.03:
            reb_day = i - ev_local
            reb_vol = d.iloc[i]["volume"] / ev["volume"]
            break
    if reb_vol and reb_vol >= 1.5:
        s += 1.5
    elif reb_vol and reb_vol >= 1.0:
        s += 0.8
    if reb_day <= 3:
        s += 1.5
    elif reb_day <= 6:
        s += 0.8
    after3r = d.iloc[ev_local + 1 : min(len(d), ev_local + 4)]
    if len(after3r) and after3r["close"].max() > ev["close"]:
        s += 1.2
    win = d.iloc[max(0, ev_local - 5) : min(len(d), ev_local + 6)]
    if not (win["close"] < win["open"]).rolling(3).sum().max() >= 3:
        s += 1.2
    if len(after3r) >= 2 and after3r["close"].iloc[1] >= after3r["open"].iloc[1]:
        s += 0.8
    if ev["volume"] < d["volume"].iloc[max(0, ev_local - 1)]:
        s += 0.8
    return round(min(s, 10), 1)


def render(df, entry, trigger_price, code, date, score, fname, window=18):
    ev = df[df["date"] <= pd.Timestamp(date)]
    if ev.empty:
        return False
    ev = ev["date"].iloc[-1]
    idx = df.index[df["date"] == ev][0]
    s, e = max(0, idx - window), min(len(df), idx + window + 1)
    d = df.iloc[s:e].reset_index(drop=True)
    ev_local = d.index[d["date"] == ev][0]
    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(13, 7.5), gridspec_kw={"height_ratios": [3.2, 1]}, sharex=True
    )
    fig.subplots_adjust(bottom=0.13, top=0.88)
    for i, row in d.iterrows():
        c = UP if row["close"] >= row["open"] else DOWN
        ax.vlines(i, row["low"], row["high"], color=c, linewidth=0.8)
        bb, bh = min(row["open"], row["close"]), max(abs(row["close"] - row["open"]), 0.01)
        ax.add_patch(Rectangle((i - 0.3, bb), 0.6, bh, facecolor=c, edgecolor=c, linewidth=0.5))
    ax.axvline(ev_local, color="#ff9500", linestyle="--", linewidth=1.2, alpha=0.8)
    ax.text(
        ev_local,
        d["high"].max() * 1.02,
        "信号日",
        ha="center",
        fontsize=9,
        color="#ff9500",
        fontweight="bold",
    )
    ax.axhline(entry, color="#2e86de", linewidth=1.0, alpha=0.7)
    ax.text(len(d) - 1, entry, f" 买@{entry:.2f}", va="bottom", fontsize=8, color="#2e86de")
    ax.axhline(entry * 0.95, color="#8e44ad", linewidth=0.8, linestyle=":", alpha=0.6)
    vmax = d["volume"].max()
    for i, row in d.iterrows():
        c = UP if row["close"] >= row["open"] else DOWN
        axv.bar(i, row["volume"] / vmax, color=c, alpha=0.7, width=0.6)
    ax.set_title(
        f"{code} 信号日{date}  视觉分{score}/10", fontsize=12, fontweight="bold", loc="left"
    )
    ax.set_ylabel("价格")
    axv.set_ylabel("量")
    ax.grid(alpha=0.2)
    axv.grid(alpha=0.2)
    xt = range(0, len(d), 3)
    ax.set_xticks(xt)
    ax.set_xticklabels([str(x)[:10] for x in d["date"].iloc[::3]], rotation=30, fontsize=7)
    axv.set_xticks(xt)
    axv.set_xticklabels([str(x)[:10] for x in d["date"].iloc[::3]], rotation=30, fontsize=7)
    plt.savefig(fname, dpi=90, bbox_inches="tight")
    plt.close(fig)
    return True


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    with open(SIG_FILE) as f:
        sigs = [json.loads(l) for l in f]
    # 只处理可交易信号(非W观察)
    tradable = [s for s in sigs if not s["signal_type"].startswith("W")]
    print(f"信号总数 {len(sigs)}, 可交易 {len(tradable)}, 处理前{limit}")
    results = []
    for sig in tradable[:limit]:
        code, date = sig["code"], sig["signal_date"]
        df = fetch_kline(code, days=120)
        if df is None:
            print(f"  ⚠️ K线失败 {code}")
            continue
        entry = sig.get("trigger_price") or df["close"].iloc[-1]
        fname = os.path.join(OUT_DIR, f"live_{code}_{date}.png")
        # 用信号日当天K线(非最新)定位
        if not render(df, entry, sig["trigger_price"], code, date, 0, fname):
            print(f"  ⚠️ 定位失败 {code} {date}")
            continue
        # 评分: 定位信号日在df中的位置
        ev = pd.Timestamp(date)
        prev = df[df["date"] <= ev]
        if not prev.empty:
            ev_idx = df.index[df["date"] == prev["date"].iloc[-1]][0]
            sc = visual_score(
                df.iloc[max(0, ev_idx - 18) : ev_idx + 19].reset_index(drop=True),
                df.index[df["date"] == prev["date"].iloc[-1]][0] - max(0, ev_idx - 18),
                entry,
            )
            results.append(
                {
                    "code": code,
                    "name": sig["name"],
                    "date": date,
                    "type": sig["signal_type"],
                    "score": sc,
                    "trigger": entry,
                    "file": fname,
                }
            )
            print(f'  ✅ {code} {sig["name"]} {sig["signal_type"]} 视觉分={sc}')
        time.sleep(0.5)
    print(f"\n完成 {len(results)} 只")
    with open("/Users/dong/workspace/gugu/data/rebound_live_scores.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    # 简单汇总
    if results:
        dfr = pd.DataFrame(results)
        print(
            dfr[["code", "name", "type", "score"]]
            .sort_values("score", ascending=False)
            .to_string(index=False)
        )


if __name__ == "__main__":
    main()
