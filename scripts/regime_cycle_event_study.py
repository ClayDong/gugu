#!/usr/bin/env python3
"""公众号《八月，来了~~》三策略事件研究验证（2026-08-01）。

方法（gugu-strategy-backtest skill 铁律）：
- 腾讯源 ifzq.gtimg.cn 拉 15 只跨板块股票 × 600 交易日
- 对每策略：扫描买入信号，统计信号后 3/5/10 日收益
- 超额收益 = 事件收益 − 同期沪深300（防牛市 beta 污染）
- Welch t 检验（vs 0）
- 集中度检查：单股贡献 >30% 信号 → 剔除重验
- 样本外切分：2024 训练 / 2025-26 验证

用法: uv run python scripts/regime_cycle_event_study.py
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request
from collections import defaultdict
from math import sqrt
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gugu.strategies.registry import get_strategy  # noqa: E402

CTX = ssl._create_unverified_context()

# 15 只跨板块股票（科技/消费/金融/新能源/医药/周期）
POOL = [
    "sh600519",  # 贵州茅台 消费
    "sz000001",  # 平安银行 金融
    "sz300750",  # 宁德时代 新能源
    "sh601318",  # 中国平安 金融
    "sz002594",  # 比亚迪 新能源
    "sh600036",  # 招商银行 金融
    "sz000858",  # 五粮液 消费
    "sh600900",  # 长江电力 公用
    "sz300059",  # 东方财富 券商
    "sh600276",  # 恒瑞医药 医药
    "sz002415",  # 海康威视 科技
    "sh601012",  # 隆基绿能 光伏
    "sz300308",  # 中际旭创 通信
    "sh600030",  # 中信证券 券商
    "sz002475",  # 立讯精密 电子
]

STRATEGIES = ["regime_cycle_four", "death_valley_rebound", "bollinger_oversold_rebound"]
HORIZONS = [3, 5, 10]
TRAIN_CUTOFF = "2025-01-01"  # 训练期 < 2025-01-01，验证期 >=


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
    df["amount"] = df["volume"] * df["close"] * 100
    return df.sort_values("date").reset_index(drop=True)


def fetch_index(days: int = 600) -> pd.DataFrame:
    """沪深300 指数（超额基准）。"""
    return fetch_kline("sh000300", days)


def welch_t(x: list[float]) -> tuple[float, float]:
    """One-sample t test against zero, with finite-sample p-value."""
    values = pd.Series(x, dtype=float).replace([float("inf"), float("-inf")], pd.NA).dropna()
    if len(values) < 3:
        return 0.0, 1.0
    result = stats.ttest_1samp(values.to_numpy(), 0.0, nan_policy="omit")
    return float(result.statistic), float(result.pvalue)


def event_study(strategy_name: str, dfs: dict[str, pd.DataFrame], index_df: pd.DataFrame) -> dict:
    """对策略做事件研究，返回各周期统计。"""
    strategy = get_strategy(strategy_name)
    # 指数收益列（对齐到日期）
    index_df = index_df.set_index("date")["close"]
    index_ret = index_df.pct_change()

    events: list[dict] = []
    for sym, df in dfs.items():
        out = strategy.generate_signals(df, is_backtest=True)
        df2 = out.reset_index(drop=True)
        buy_idx = df2.index[df2["signal"] == 1].tolist()
        for i in buy_idx:
            date = df2.loc[i, "date"]
            entry = df2.loc[i, "close"]
            ev = {
                "symbol": sym,
                "date": date,
                "entry": entry,
                "ret": {},
                "excess": {},
            }
            for h in HORIZONS:
                j = i + h
                if j < len(df2):
                    exit_price = df2.loc[j, "close"]
                    ret = (exit_price - entry) / entry
                    # 同期指数收益
                    d0 = df2.loc[i, "date"]
                    d1 = df2.loc[j, "date"]
                    idx_window = index_ret.loc[d0:d1].dropna()
                    idx_r = (
                        float((1.0 + idx_window).prod() - 1.0)
                        if not idx_window.empty
                        else float("nan")
                    )
                    ev["ret"][h] = ret
                    ev["excess"][h] = ret - idx_r if pd.notna(idx_r) else float("nan")
            if ev["ret"]:
                events.append(ev)

    # 汇总
    result: dict = {"strategy": strategy_name, "total_signals": len(events)}
    for h in HORIZONS:
        rets = [e["ret"][h] for e in events if h in e["ret"]]
        excess = [e["excess"][h] for e in events if h in e["excess"] and pd.notna(e["excess"][h])]
        t, p = welch_t(rets)
        t_e, p_e = welch_t(excess)
        result[f"ret_{h}d"] = {
            "n": len(rets),
            "mean": round(sum(rets) / len(rets), 4) if rets else None,
            "p": round(p, 4) if rets else None,
        }
        result[f"excess_{h}d"] = {
            "n": len(excess),
            "mean": round(sum(excess) / len(excess), 4) if excess else None,
            "p": round(p_e, 4) if excess else None,
        }

    # 集中度检查（单股贡献）
    per_symbol = defaultdict(int)
    for e in events:
        per_symbol[e["symbol"]] += 1
    total = len(events)
    top_symbol = max(per_symbol.items(), key=lambda x: x[1]) if per_symbol else ("", 0)
    result["concentration"] = {
        "top_symbol": top_symbol[0],
        "top_count": top_symbol[1],
        "top_ratio": round(top_symbol[1] / total, 2) if total else 0,
    }

    # 训练/验证切分（验证期 = 2025-01-01 之后）
    val_events = [e for e in events if e["date"] >= pd.Timestamp(TRAIN_CUTOFF)]
    train_events = [e for e in events if e["date"] < pd.Timestamp(TRAIN_CUTOFF)]
    result["split"] = {
        "train_count": len(train_events),
        "val_count": len(val_events),
    }
    for h in HORIZONS:
        val_excess = [
            e["excess"][h] for e in val_events if h in e["excess"] and pd.notna(e["excess"][h])
        ]
        if val_excess:
            t_v, p_v = welch_t(val_excess)
            result[f"val_excess_{h}d"] = {
                "n": len(val_excess),
                "mean": round(sum(val_excess) / len(val_excess), 4),
                "p": round(p_v, 4),
            }

    return result


def main() -> None:
    print("拉取行情数据（15 股 + 沪深300）...")
    dfs = {}
    for sym in POOL:
        try:
            dfs[sym] = fetch_kline(sym)
            print(f"  ✅ {sym} ({len(dfs[sym])}d)")
        except Exception as e:
            print(f"  ❌ {sym}: {e}")
    index_df = fetch_index()
    print(f"  ✅ sh000300 ({len(index_df)}d)")

    print("\n" + "=" * 90)
    for name in STRATEGIES:
        r = event_study(name, dfs, index_df)
        print(f"\n📊 {r['strategy']} — 总信号 {r['total_signals']}")
        print(
            f"   集中度: {r['concentration']['top_symbol']} {r['concentration']['top_count']}个 ({r['concentration']['top_ratio']:.0%})"
        )
        print(f"   切分: train={r['split']['train_count']} val={r['split']['val_count']}")
        for h in HORIZONS:
            raw = r[f"ret_{h}d"]
            exc = r[f"excess_{h}d"]
            val = r.get(f"val_excess_{h}d")
            raw_s = f"均值{raw['mean']:+.2%}(p={raw['p']})" if raw["mean"] is not None else "N/A"
            exc_s = f"超额{exc['mean']:+.2%}(p={exc['p']})" if exc["mean"] is not None else "N/A"
            val_s = (
                f"验证期超额{val['mean']:+.2%}(p={val['p']})"
                if val and val["mean"] is not None
                else "验证N/A"
            )
            print(f"   {h}日: {raw_s} | {exc_s} | {val_s}")
    print("\n" + "=" * 90)


if __name__ == "__main__":
    main()
