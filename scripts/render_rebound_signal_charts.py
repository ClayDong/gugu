#!/usr/bin/env python3
"""断板反包·当日命中标的 K 线图自动渲染（飞书附图用）。

设计目标：扫描命中 → 自动出图 → 飞书推送，实现"筛选标的 + 阶段 + K线图"三合一。

输入：data/rebound_scan_result.csv（scan_rebound_market.py --track 输出）
输出：data/rebound_charts_daily/YYYY-MM-DD/*.png
      每图带标注：首板开盘价(主力成本底线, 红虚)、支撑位(橙虚)、当前价标记、
      阶段/买点文字说明、V2视觉评分。

用法：
    .venv/bin/python scripts/render_rebound_signal_charts.py [--csv data/rebound_scan_result.csv]
    .venv/bin/python scripts/render_rebound_signal_charts.py --codes sz002293,sz002291  # 指定标的

依赖：matplotlib（已装 3.11.1，中文字体 Arial Unicode MS）
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Hiragino Sans GB"]
plt.rcParams["axes.unicode_minus"] = False

ssl._create_default_https_context = ssl._create_unverified_context

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Referer": "http://finance.qq.com/",
}
UP, DOWN = "#e64545", "#1aad5a"  # 红涨绿跌（A股/同花顺配色）


def fetch_kline(code: str, days: int = 260):
    """腾讯 K 线 qfq（与扫描器同一实现，保证形态判定一致）。"""
    endpoints = [
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq",
        f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            node = data.get("data", {}).get(code, {})
            rows = node.get("qfqday") or node.get("day") or []
            if not rows:
                continue
            df = pd.DataFrame(
                [r[:6] for r in rows], columns=["date", "open", "close", "high", "low", "volume"]
            )
            df["date"] = pd.to_datetime(df["date"])
            for c in ["open", "close", "high", "low", "volume"]:
                df[c] = df[c].astype(float)
            return df
        except Exception:
            continue
    return None


def draw_kline(ax, df, start=None, end=None):
    """绘制 K 线（红涨绿跌），df 需含 date/open/close/high/low。"""
    d = df.copy()
    if start is not None:
        d = d[d["date"] >= start]
    if end is not None:
        d = d[d["date"] <= end]
    d = d.reset_index(drop=True)
    xs = np.arange(len(d))
    for i, r in d.iterrows():
        color = UP if r["close"] >= r["open"] else DOWN
        # 影线
        ax.plot([i, i], [r["low"], r["high"]], color=color, linewidth=0.8, zorder=2)
        # 实体
        body_lo, body_hi = sorted([r["open"], r["close"]])
        ax.add_patch(
            plt.Rectangle(
                (i - 0.3, body_lo),
                0.6,
                max(body_hi - body_lo, 0.01),
                facecolor=color,
                edgecolor=color,
                zorder=3,
            )
        )
    ax.set_xticks(range(0, len(d), max(len(d) // 8, 1)))
    ax.set_xticklabels(
        [d["date"].iloc[i].strftime("%m-%d") for i in range(0, len(d), max(len(d) // 8, 1))],
        fontsize=7,
    )
    ax.grid(alpha=0.3)
    return d


def render_one(code: str, row: dict, out_dir: str) -> str | None:
    """渲染单只标的 K 线图，返回 PNG 路径。"""
    df = fetch_kline(code, days=200)
    if df is None or df.empty:
        print(f"  ⚠️ {code} K线拉取失败，跳过")
        return None

    name = row.get("name", code)
    stage = row.get("stage", "")
    first_open = row.get("first_open")
    support = row.get("support")
    price = row.get("price") or float(df["close"].iloc[-1])
    v2 = row.get("v2_score")
    last_date = row.get("last_date")

    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=110)
    # 窗口：首板前 15 日 → 当前（或最近 60 根）
    start = None
    if last_date:
        try:
            ld = pd.Timestamp(last_date)
            start = ld - pd.Timedelta(days=45)
        except Exception:
            start = None
    d = draw_kline(ax, df, start=start)

    # ── 标注：首板开盘价（主力成本底线）──
    if first_open and np.isfinite(first_open):
        ax.axhline(first_open, color="#d62728", linestyle="--", linewidth=1.3, alpha=0.85)
        ax.text(
            len(d) - 1,
            first_open,
            f" 首板开盘 {first_open:.2f}",
            color="#d62728",
            fontsize=9,
            va="bottom",
            ha="right",
            fontweight="bold",
        )
    # ── 标注：支撑位 ──
    if support and np.isfinite(support):
        ax.axhline(support, color="#ff7f0e", linestyle=":", linewidth=1.2, alpha=0.9)
        ax.text(
            len(d) - 1,
            support,
            f" 支撑 {support:.2f}",
            color="#ff7f0e",
            fontsize=9,
            va="top",
            ha="right",
        )
    # ── 标注：当前价 ──
    ax.scatter([len(d) - 1], [float(df["close"].iloc[-1])], color="#1f77b4", s=42, zorder=5)
    ax.annotate(
        f"现价 {float(df['close'].iloc[-1]):.2f}",
        (len(d) - 1, float(df["close"].iloc[-1])),
        textcoords="offset points",
        xytext=(-55, 8),
        fontsize=9,
        color="#1f77b4",
        fontweight="bold",
    )

    # ── 标题：标的 + 阶段 + V2评分 ──
    v2_txt = f" | V2视觉 {v2}" if v2 is not None else ""
    title = f"{name}（{code}）断板反包·{stage}{v2_txt}"
    ax.set_title(title, fontsize=12, fontweight="bold", color="#333333")
    ax.set_ylabel("价格")
    fig.tight_layout()

    os.makedirs(out_dir, exist_ok=True)
    safe = code.replace("sh", "").replace("sz", "").replace("bj", "")
    path = os.path.join(out_dir, f"{safe}_{name}.png")
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  ✅ {code} {name} → {path}")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="data/rebound_scan_result.csv")
    parser.add_argument("--codes", default="", help="逗号分隔指定标的代码（默认全量命中）")
    parser.add_argument("--max", type=int, default=20, help="最多渲染数量（保护腾讯K线限流）")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    csv_path = os.path.join(PROJECT_ROOT, args.csv)
    if not os.path.exists(csv_path):
        print(f"❌ CSV 不存在: {csv_path}，先运行 scan_rebound_market.py")
        sys.exit(1)

    df = pd.read_csv(csv_path, encoding="utf-8-sig")  # 带 BOM
    if df.empty:
        print("CSV 为空，无命中标的")
        return

    # 指定标的 or 全量（优先买点A > 伏击圈 > 反包）
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        sub = df[df["code"].isin(codes)]
    else:
        stage_str = df["stage"].fillna("").astype(str)
        stage_rank = stage_str.apply(lambda s: 0 if "买点A" in s else (1 if "伏击圈" in s else 2))
        sub = df.iloc[stage_rank.argsort(kind="stable")].head(args.max)

    if sub.empty:
        print("无匹配标的")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = args.out or os.path.join(PROJECT_ROOT, "data", "rebound_charts_daily", today)
    print(f"渲染 {len(sub)} 只标的 → {out_dir}")

    paths = []
    for _, r in sub.iterrows():
        code = str(r["code"])
        # 代码带 sh/sz 前缀直接用
        if not code.startswith(("sh", "sz", "bj")):
            code = f"sz{code}" if code.startswith(("0", "3")) else f"sh{code}"
        p = render_one(code, r, out_dir)
        if p:
            paths.append(p)

    print(f"\n完成: {len(paths)}/{len(sub)} 张图")
    for p in paths:
        print(p)


if __name__ == "__main__":
    main()
