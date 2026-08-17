#!/usr/bin/env python3
"""断板反包 全量样本图生成 + 视觉特征自动标注
步骤:
  1. 读取 chenxuezhang_event_study.json 全部有K线样本的事件
  2. 逐事件生成K线图(事件日±18日窗口, 带断板/首板/护盘/买入标注)
  3. 数据端精确计算视觉特征向量(断板日量比/阴线幅度/企稳天数/护盘完整度/反包速度)
  4. 输出: 图片 + features.csv(视觉特征×收益标签) —— 训练数据集
"""

import json, os, glob, sys
import pandas as pd
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Hiragino Sans GB"]
plt.rcParams["axes.unicode_minus"] = False

CSV_DIR = "/Users/dong/workspace/gugu/data/rebound_samples"
OUT_DIR = "/Users/dong/workspace/gugu/data/rebound_charts"
os.makedirs(OUT_DIR, exist_ok=True)
UP, DOWN = "#e64545", "#1aad5a"


def load_kline(code):
    c = code[-6:]
    path = os.path.join(CSV_DIR, f"{c}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    return df


def compute_visual_features(d, ev_local, entry):
    """从K线数据精确计算『视觉特征』—— 图+数据联合判断的特征层
    返回 dict: 每个特征都能在图上对应到可见元素"""
    f = {}
    ev = d.iloc[ev_local]
    # 1. 断板日形态
    f["break_ret"] = ev["close"] / ev["open"] - 1  # 断板日涨跌幅(负=阴)
    f["break_body_ratio"] = abs(ev["close"] - ev["open"]) / max(
        ev["high"] - ev["low"], 1e-9
    )  # 实体占比(无影线=1)
    f["break_vol_ratio"] = ev["volume"] / d["volume"].iloc[max(0, ev_local - 1)]  # 断板日量/前日量
    f["break_vol_vs_20d"] = (
        ev["volume"] / d["volume"].iloc[max(0, ev_local - 20) : ev_local].mean()
    )  # 断板日量/20日均量
    # 2. 断板前吸筹特征(前5日)
    pre = d.iloc[max(0, ev_local - 5) : ev_local]
    f["pre_ret_5d"] = pre["close"].iloc[-1] / pre["close"].iloc[0] - 1
    f["pre_uptrend"] = int((pre["close"].diff() > 0).sum() >= 3)  # 5日内≥3阳
    # 3. 断板后企稳(后3日)
    after = d.iloc[ev_local + 1 : min(len(d), ev_local + 4)]
    if len(after) >= 2:
        f["stabilize_1d"] = int(after["close"].iloc[0] >= after["open"].iloc[0])  # 次日收阳?
        f["stabilize_2d"] = int(after["close"].iloc[1] >= after["open"].iloc[1])
        f["max_drawdown_3d"] = after["low"].min() / ev["close"] - 1  # 3日最大回撤
        f["recover_3d"] = int(after["close"].max() > ev["close"])  # 3日内收复断板日收盘?
    else:
        f["stabilize_1d"] = f["stabilize_2d"] = f["recover_3d"] = np.nan
        f["max_drawdown_3d"] = np.nan
    # 4. 护盘完整度: 断板后5日最低价 vs 买入价
    after5 = d.iloc[ev_local + 1 : min(len(d), ev_local + 6)]
    f["hold_support_above_entry"] = int(after5["low"].min() > entry) if len(after5) else np.nan
    f["support_depth"] = (
        after5["low"].min() / entry - 1 if len(after5) else np.nan
    )  # 支撑相对买入价深度
    # 5. 反包速度: 断板后几日内出现>3%阳线
    reb = None
    for i in range(ev_local + 1, min(len(d), ev_local + 11)):
        if d.iloc[i]["close"] / d.iloc[i]["open"] - 1 > 0.03:
            reb = i - ev_local
            break
    f["rebound_day"] = reb if reb else 99  # 99=10日内无反包
    f["rebound_vol_ratio"] = (
        d.iloc[ev_local + reb]["volume"] / ev["volume"]
        if reb and ev_local + reb < len(d)
        else np.nan
    )
    # 6. 窗口内是否有破坏形态
    win = d.iloc[max(0, ev_local - 5) : min(len(d), ev_local + 6)]
    f["has_gap"] = int((win["low"] > win["close"].shift(1) * 1.02).any())  # 2%跳空
    f["consec_3_red"] = int((win["close"] < win["open"]).rolling(3).sum().max() >= 3)  # 连续3阴
    f["long_lower_shadow"] = int(
        (win["low"] < win[["open", "close"]].min(axis=1) * 0.96).any()
    )  # 4%长下影
    return f


def draw_chart(df, event_date, entry, label, ret10, fname, window=18):
    ev = pd.Timestamp(event_date)
    if ev not in df["date"].values:
        prev = df[df["date"] <= ev]
        if prev.empty:
            return None
        ev = prev["date"].iloc[-1]
    idx = df.index[df["date"] == ev][0]
    s, e = max(0, idx - window), min(len(df), idx + window + 1)
    d = df.iloc[s:e].reset_index(drop=True)
    ev_local = d.index[d["date"] == ev][0]

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(13, 7.5), gridspec_kw={"height_ratios": [3.2, 1]}, sharex=True
    )
    fig.subplots_adjust(bottom=0.13, top=0.88)
    for i, row in d.iterrows():
        color = UP if row["close"] >= row["open"] else DOWN
        ax.vlines(i, row["low"], row["high"], color=color, linewidth=0.8)
        bb, bh = min(row["open"], row["close"]), max(abs(row["close"] - row["open"]), 0.01)
        ax.add_patch(
            Rectangle((i - 0.3, bb), 0.6, bh, facecolor=color, edgecolor=color, linewidth=0.5)
        )

    ax.axvline(ev_local, color="#ff9500", linestyle="--", linewidth=1.2, alpha=0.8)
    ax.text(
        ev_local,
        d["high"].max() * 1.02,
        "断板日",
        ha="center",
        fontsize=9,
        color="#ff9500",
        fontweight="bold",
    )
    ret = d["close"].pct_change()
    fbs = [i for i in d.index[(ret > 0.07)].tolist() if i < ev_local]
    if fbs:
        fb = fbs[-1]
        ax.annotate(
            "首板",
            xy=(fb, d.loc[fb, "high"]),
            xytext=(fb, d.loc[fb, "high"] * 1.04),
            ha="center",
            fontsize=9,
            color="#d64541",
            fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#d64541", lw=0.8),
        )
    ax.axhline(entry, color="#2e86de", linewidth=1.0, alpha=0.7)
    ax.text(len(d) - 1, entry, f" 买@{entry:.2f}", va="bottom", fontsize=8, color="#2e86de")
    ax.axhline(entry * 0.95, color="#8e44ad", linewidth=0.8, linestyle=":", alpha=0.6)
    after = d.iloc[ev_local:]
    if len(after) > 2:
        ec = after["close"].iloc[-1]
        ax.text(
            len(d) - 0.5,
            ec,
            f" {ret10*100:+.1f}%",
            ha="right",
            va="bottom",
            fontsize=11,
            color=UP if ret10 > 0 else DOWN,
            fontweight="bold",
        )
    pre = d.iloc[max(0, ev_local - 5) : ev_local]
    if len(pre):
        sup = pre["low"].min()
        ax.axhline(sup, color="#f1c40f", linewidth=0.8, linestyle="--", alpha=0.5)
    vmax = d["volume"].max()
    for i, row in d.iterrows():
        c = UP if row["close"] >= row["open"] else DOWN
        axv.bar(i, row["volume"] / vmax, color=c, alpha=0.7, width=0.6)
    ax.set_title(
        f"{label}  断板反包·{event_date}  10日 {ret10*100:+.1f}%",
        fontsize=12,
        fontweight="bold",
        loc="left",
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
    return compute_visual_features(d, ev_local, entry)


def main():
    with open("/Users/dong/workspace/gugu/data/chenxuezhang_event_study.json") as f:
        events = json.load(f)["events"]
    feats = []
    cnt = 0
    for e in events:
        code = e["code"]
        c = code[-6:]
        if not os.path.exists(os.path.join(CSV_DIR, f"{c}.csv")):
            continue
        df = load_kline(code)
        if df is None:
            continue
        fname = os.path.join(OUT_DIR, f'ALL_{c}_{e["date"]}.png')
        label = f'{code} {e["date"]}'
        try:
            vf = draw_chart(df, e["date"], e["entry"], label, e["ret_10d"], fname)
        except Exception as ex:
            print(f'  ⚠️ {code} {e["date"]}: {ex}', flush=True)
            continue
        if vf:
            row = {
                "code": code,
                "date": e["date"],
                "ret_10d": e["ret_10d"],
                "ret_5d": e.get("ret_5d"),
                "ret_3d": e.get("ret_3d"),
                "year": e.get("year"),
                "entry": e["entry"],
                "file": fname,
            }
            row.update(vf)
            feats.append(row)
            cnt += 1
            if cnt % 50 == 0:
                print(f"  ...{cnt} 完成", flush=True)
    out = pd.DataFrame(feats)
    out.to_csv("/Users/dong/workspace/gugu/data/rebound_visual_features.csv", index=False)
    print(f"\n✅ 完成: {len(out)} 个样本 → rebound_visual_features.csv")
    print("特征列:", list(out.columns))


if __name__ == "__main__":
    main()
