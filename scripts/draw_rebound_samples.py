#!/usr/bin/env python3
"""断板反包 K线图可视化 —— 图+数据联合判断的样本生成器
对每个历史事件: 加载K线 → 定位事件日 → 绘制事件日前后窗口
标注: 断板日(事件日)/首板/护盘区间/买入点(entry)/止损位/后续收益
输出: PNG 到 /Users/dong/workspace/gugu/data/rebound_charts/
"""

import json, os, glob, sys
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle

# macOS 中文字体
plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Hiragino Sans GB"]
plt.rcParams["axes.unicode_minus"] = False

CSV_DIR = "/Users/dong/workspace/gugu/data/rebound_samples"
OUT_DIR = "/Users/dong/workspace/gugu/data/rebound_charts"
os.makedirs(OUT_DIR, exist_ok=True)

# A股配色: 阳=红 阴=绿
UP, DOWN = "#e64545", "#1aad5a"


def load_kline(code):
    c = code[-6:]
    path = os.path.join(CSV_DIR, f"{c}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def draw_chart(df, event_date, entry, label, ret10, fname, window=18):
    """绘制事件日±window的K线图"""
    ev = pd.Timestamp(event_date)
    # 定位事件日在数据中的位置
    if ev not in df["date"].values:
        # 找最近的前一个交易日
        prev = df[df["date"] <= ev]
        if prev.empty:
            return False
        ev = prev["date"].iloc[-1]
    idx = df.index[df["date"] == ev][0]
    s, e = max(0, idx - window), min(len(df), idx + window + 1)
    d = df.iloc[s:e].copy()
    d = d.reset_index(drop=True)
    ev_local = d.index[d["date"] == ev][0]

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(13, 7.5), gridspec_kw={"height_ratios": [3.2, 1]}, sharex=True
    )
    fig.subplots_adjust(bottom=0.13, top=0.88)

    # ---- K线 ----
    for i, row in d.iterrows():
        color = UP if row["close"] >= row["open"] else DOWN
        ax.vlines(i, row["low"], row["high"], color=color, linewidth=0.8)
        body_b = min(row["open"], row["close"])
        body_h = max(abs(row["close"] - row["open"]), 0.01)
        ax.add_patch(
            Rectangle(
                (i - 0.3, body_b), 0.6, body_h, facecolor=color, edgecolor=color, linewidth=0.5
            )
        )

    # ---- 标注 ----
    # 事件日竖线
    ax.axvline(ev_local, color="#ff9500", linestyle="--", linewidth=1.2, alpha=0.8)
    ax.text(
        ev_local,
        d["high"].max() * 1.02,
        "断板日/信号日",
        ha="center",
        fontsize=9,
        color="#ff9500",
        fontweight="bold",
    )

    # 首板: 事件日前找最近的大阳线(涨幅>7%)
    ret = d["close"].pct_change()
    first_boards = d.index[(ret > 0.07)].tolist()
    first_boards = [i for i in first_boards if i < ev_local]
    if first_boards:
        fb = first_boards[-1]
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

    # entry 买入线
    ax.axhline(entry, color="#2e86de", linewidth=1.0, linestyle="-", alpha=0.7)
    ax.text(len(d) - 1, entry, f" 买入@{entry:.2f}", va="bottom", fontsize=8, color="#2e86de")

    # 止损参考: entry*0.95
    sl = entry * 0.95
    ax.axhline(sl, color="#8e44ad", linewidth=0.8, linestyle=":", alpha=0.6)

    # 后续走势标注(事件日后)
    after = d.iloc[ev_local:]
    if len(after) > 2:
        end_close = after["close"].iloc[-1]
        if end_close > entry:
            ax.annotate(
                "",
                xy=(len(d) - 1, end_close),
                xytext=(len(d) - 1, entry),
                arrowprops=dict(arrowstyle="->", color=UP, lw=1.5),
            )
        ax.text(
            len(d) - 0.5,
            after["close"].iloc[-1],
            f" {ret10*100:+.1f}%",
            ha="right",
            va="bottom",
            fontsize=11,
            color=UP if ret10 > 0 else DOWN,
            fontweight="bold",
        )

    # 护盘区间(事件日前5日至事件日之间的低点区域) —— 视觉参考线
    pre = d.iloc[max(0, ev_local - 5) : ev_local]
    if len(pre):
        support = pre["low"].min()
        ax.axhline(support, color="#f1c40f", linewidth=0.8, linestyle="--", alpha=0.5)
        ax.text(0, support, f" 护盘支撑{support:.2f}", va="bottom", fontsize=7, color="#b7950b")

    # ---- 成交量 ----
    vmax = d["volume"].max()
    for i, row in d.iterrows():
        color = UP if row["close"] >= row["open"] else DOWN
        axv.bar(i, row["volume"] / vmax, color=color, alpha=0.7, width=0.6)

    # ---- 样式 ----
    ax.set_title(
        f"{label}  断板反包·{event_date}  10日收益 {ret10*100:+.1f}%",
        fontsize=12,
        fontweight="bold",
        loc="left",
    )
    ax.set_ylabel("价格")
    axv.set_ylabel("量")
    ax.grid(alpha=0.2)
    axv.grid(alpha=0.2)
    ax.set_xticks(range(0, len(d), 3))
    ax.set_xticklabels([str(x)[:10] for x in d["date"].iloc[::3]], rotation=30, fontsize=7)
    axv.set_xticks(range(0, len(d), 3))
    axv.set_xticklabels([str(x)[:10] for x in d["date"].iloc[::3]], rotation=30, fontsize=7)

    plt.savefig(fname, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return True


def main():
    with open("/tmp/rebound_chart_selection.json") as f:
        sel = json.load(f)

    manifest = []
    for bucket, items in sel.items():
        for e in items:
            code, date = e["code"], e["date"]
            df = load_kline(code)
            if df is None:
                print(f"  ⚠️ 无K线: {code}")
                continue
            name = code.upper()
            fname = os.path.join(OUT_DIR, f"{bucket[:2]}_{code}_{date}.png")
            # 中文标签用于图内显示
            label = f"{code} {date}"
            ok = draw_chart(df, date, e["entry"], label, e["ret_10d"], fname)
            if ok:
                manifest.append(
                    {
                        "file": fname,
                        "bucket": bucket,
                        "code": code,
                        "date": date,
                        "ret_10d": e["ret_10d"],
                        "entry": e["entry"],
                    }
                )
                print(f"  ✅ {bucket[:2]} {code} {date} → {fname}")

    with open("/tmp/rebound_chart_manifest.json", "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"\n共生成 {len(manifest)} 张图")


if __name__ == "__main__":
    main()
