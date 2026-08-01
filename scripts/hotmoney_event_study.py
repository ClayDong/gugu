"""hot_money 游资三数交易法 — Walk-Forward 事件研究验证（2026-08-01）。

背景：
- manifest: enabled=true 但 validation_gate.status=pending（0 样本外交易）
- optimization_report_hot_money_20260726.json 标 passed:true（夏普0.69），但参数稳定性门槛是旧的50%
- diagnose_hot_money 2026-07-23 显示 5股回测平均 -6.99%/夏普-0.66
- 标签矛盾 → 按防过拟合铁律重新验证

方法论（gugu-strategy-backtest skill 铁律）：
1. 事件研究先行：信号后 N 日收益 vs 沪深300 超额，Welch t 检验
2. 训练期选参 / 验证期冻结（Walk-Forward）
3. 触发股集中度检查（单股>30%信号=不可信）
4. 超额收益检验（赚钱≠有alpha）
5. 小样本(<30)正收益不可信

用法：python scripts/hotmoney_event_study.py [--days 600] [--pool 活跃股池]
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

# 活跃题材股池（涨停频繁，hot_money 适配对象；含不同板块避免单板块偏差）
DEFAULT_POOL = [
    "sz002472",  # 双环传动 机器人活跃
    "sz300033",  # 同花顺 金融科技
    "sz002230",  # 科大讯飞 AI
    "sh603501",  # 韦尔股份 半导体
    "sz300308",  # 中际旭创 CPO
    "sz300502",  # 新易盛 CPO
    "sh600519",  # 贵州茅台 蓝筹负对照
    "sz300750",  # 宁德时代 白马
    "sz002594",  # 比亚迪 新能源
    "sz300059",  # 东方财富 券商
    "sz002475",  # 立讯精密 消费电子
    "sh600050",  # 中国联通 题材
    "sz000977",  # 浪潮信息 算力
    "sh688041",  # 海光信息 芯片
    "sz002371",  # 北方华创 半导体设备
]

# 训练/验证切分（与 fakedip/rebound walkforward 一致）
TRAIN_START, TRAIN_END = "2024-02-01", "2025-01-31"
VAL_START, VAL_END = "2025-02-01", "2026-07-31"


def welch_t(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """单样本 Welch t 检验 vs 0（b 全零），返回 (t, p)。"""
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
        return (ma / 1e-9), 0.0 if ma != 0 else (0.0, 1.0)
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
            [
                row[0],
                float(row[1]),
                float(row[3]),
                float(row[4]),
                float(row[2]),
                float(row[5]),
            ]
        )
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    return df.dropna(subset=["open", "high", "low", "close"])


def fetch_index(symbol: str = _INDEX_CODE, days: int = 700) -> pd.DataFrame:
    df = fetch_kline(symbol, days)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")["close"]
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=600)
    ap.add_argument("--pool", nargs="*", default=DEFAULT_POOL)
    ap.add_argument("--json", default="data/hotmoney_event_study.json")
    args = ap.parse_args()

    from gugu.strategies.registry import get_strategy

    strategy = get_strategy("hot_money")
    index = fetch_index(days=700)
    print(f"指数K线 {len(index)} 条")

    all_rows = []
    fetch_fail = 0
    for symbol in args.pool:
        df = fetch_kline(symbol, args.days)
        if df.empty:
            print(f"  ⚠️ {symbol} 拉取失败")
            fetch_fail += 1
            continue
        if index.empty:
            print("  ❌ 指数K线失败，无法计算超额")
            return
        df["date"] = pd.to_datetime(df["date"])
        df["idx_close"] = df["date"].map(index)
        df["idx_ret"] = df["idx_close"] / df["idx_close"].shift(1) - 1
        df["index_return"] = df["idx_ret"].fillna(0.0)

        # 生成信号（回测模式，跳过确认机制）
        sig_df = strategy.generate_signals(df.copy(), is_backtest=True)
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
            # 超额：用指数同窗口收益
            if not index.empty:
                for h in (3, 5, 10):
                    if i + h < len(df):
                        idx_ret = df["idx_close"].iloc[i + h] / df["idx_close"].iloc[i] - 1
                        if pd.notna(idx_ret) and f"ret_{h}d" in row:
                            row[f"excess_{h}d"] = row[f"ret_{h}d"] - idx_ret
            all_rows.append(row)
        time.sleep(0.3)  # 限流保护

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

    # 触发股集中度
    print("\n=== 触发股分布 ===")
    conc = ev["code"].value_counts()
    for code, n in conc.items():
        pct = n / len(ev) * 100
        mark = "⚠️ 集中" if pct > 30 else ""
        print(f"  {code}: {n} ({pct:.0f}%) {mark}")

    out = {"total_signals": len(ev), "by_stock": conc.to_dict(), "events": []}
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

    # 去重集中股后重验（最大贡献者剔除）
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
                        print(f"  [{period}] {ecol}: {psub[ecol].mean():+7.2%} p={p:.3f}")

    # 按年份分桶（防单一年份/暴跌日污染）
    print("\n=== 按年份分桶（超额 5d/10d）===")
    ev["year"] = ev["date"].dt.year
    for y, g in ev.groupby("year"):
        for h in (5, 10):
            ecol = f"excess_{h}d"
            if ecol in g and g[ecol].notna().sum() >= 3:
                t, p = welch_t(g[ecol].values, np.zeros(1))
                print(f"  {y}: n={len(g)} {ecol}: {g[ecol].mean():+7.2%} p={p:.3f}")

    # 保存
    ev["date"] = ev["date"].dt.strftime("%Y-%m-%d")
    out["events"] = ev.to_dict("records")
    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    with open(args.json, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已存 {args.json}")


if __name__ == "__main__":
    main()
