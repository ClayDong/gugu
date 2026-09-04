"""批量事件研究：验证网络博主战法是否真有 edge（P0）。

来源：mulan-quant rules_library/multi_blogger_engine.py 的 8 位博主规则函数。
方法：对每条可量化买入规则，扫描全缓存股票，计算信号后 5/10 日收益，
与全市场同期平均收益（基准）对比，用 t 检验判断超额是否显著。

输出：data/rule_event_study_report.json（每条规则的样本数、超额、t、p、判定）。
判定：p<0.05 且超额>0 → "有edge"；p<0.05 且超额<0 → "证伪"；否则 "样本不足"。

用法：
    uv run python scripts/validate_blogger_rules.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, "/Users/dong/workspace/mulan-quant")

# 可量化买入规则（从 multi_blogger_engine.py 导入）
from rules_library.multi_blogger_engine import (  # noqa: E402
    chen_bottom_divergence,
    chen_failed_limitup_reversal,
    neighbor_2b_buy,
    neighbor_golden_section,
    neighbor_pullback_buy,
)

RULES: dict[str, object] = {
    "陈学长·底背离买入": chen_bottom_divergence,
    "陈学长·断板反包": chen_failed_limitup_reversal,
    "邻居大爷·2B反转": neighbor_2b_buy,
    "邻居大爷·黄金分割": neighbor_golden_section,
    "邻居大爷·缩量回踩": neighbor_pullback_buy,
}


def _rule_signal(rule_fn, df: pd.DataFrame) -> bool:
    """调用规则函数，返回是否产生买入信号（兼容 mulan Signal 对象或 None）。"""
    try:
        sig = rule_fn(df)
        if sig is None:
            return False
        # mulan Signal.signal_type 是 SignalType 枚举，转字符串是 "SignalType.BUY"
        if hasattr(sig, "signal_type"):
            st = str(getattr(sig, "signal_type", "")).upper()
            return "BUY" in st or "买入" in st
        if isinstance(sig, dict):
            return "BUY" in str(sig.get("signal_type", "")).upper()
        return True
    except Exception:
        return False


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """补充规则需要的列（dif/dea/均线等）。"""
    out = df.sort_values("date").reset_index(drop=True).copy()
    if "prev_close" not in out.columns:
        out["prev_close"] = out["close"].shift(1)
    # MACD（规则用 dif）
    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    out["dif"] = ema12 - ema26
    # 均线（邻居大爷回踩买点需要 ma_20）
    if "ma_20" not in out.columns:
        out["ma_20"] = out["close"].rolling(20).mean()
    return out


async def main() -> None:
    from gugu.data.manager import DataManager

    dm = DataManager()
    # 从 parquet 缓存拿所有可用股票
    cache_dir = PROJECT_ROOT / "data" / "parquet_cache"
    symbols = sorted(
        {
            p.name.split("_")[0]
            for p in cache_dir.glob("*_120d_none.parquet")
            if p.name.split("_")[0].isdigit()
        }
    )[:182]
    print(f"扫描 {len(symbols)} 只缓存股票\n")

    # 1. 扫描所有股票，收集每条规则的信号事件 + 全体收益基准
    rule_events: dict[str, list[float]] = {k: [] for k in RULES}
    market_returns: dict[int, list[float]] = {5: [], 10: []}

    for sym in symbols:
        try:
            df = await dm.fetch_stock_history(sym, days=300)
        except Exception:
            continue
        if df is None or len(df) < 30:
            continue
        df = _enrich(df)
        closes = df["close"].to_numpy(dtype=float)
        n = len(closes)
        # 全体收益基准（所有股票所有日的平均未来收益）
        for h in (5, 10):
            for i in range(n - h):
                if closes[i] > 0:
                    market_returns[h].append(closes[i + h] / closes[i] - 1)
        # 每条规则的事件
        for name, fn in RULES.items():
            ev = rule_events[name]
            for i in range(30, n - 10):
                window = df.iloc[: i + 1].copy()
                if _rule_signal(fn, window):
                    # 信号日收盘买入，持有 5/10 日
                    for h in (5, 10):
                        if closes[i] > 0:
                            ev.append(closes[i + h] / closes[i] - 1)
                    break  # 每只股票每规则只记一次（避免重复计数）

    # 2. 汇总判定
    report: dict[str, dict] = {}
    for name, ev in rule_events.items():
        ev5 = ev  # 用 5 日事件近似（简化：记录时以 5 日为准）
        n_ev = len(ev)
        if n_ev < 5:
            report[name] = {
                "samples": n_ev,
                "verdict": "样本不足",
                "detail": "事件 < 5，无法统计",
            }
            continue
        ev_arr = np.array(ev)
        bench_arr = np.array(market_returns[5])[: len(ev_arr)]
        if len(bench_arr) == 0:
            continue
        excess = ev_arr.mean() - bench_arr.mean()
        # 单样本 t 检验：excess vs 0
        if len(ev_arr) > 1 and ev_arr.std(ddof=1) > 0:
            t = excess / (ev_arr.std(ddof=1) / np.sqrt(len(ev_arr)))
            from scipy import stats

            p = 2 * (1 - stats.t.cdf(abs(t), df=len(ev_arr) - 1))
        else:
            t, p = 0.0, 1.0
        if p < 0.05 and excess > 0:
            verdict = "有edge"
        elif p < 0.05 and excess < 0:
            verdict = "证伪"
        else:
            verdict = "样本不足"
        report[name] = {
            "samples": n_ev,
            "event_avg_5d": round(float(ev_arr.mean()), 4),
            "benchmark_avg_5d": round(float(bench_arr.mean()), 4),
            "excess_5d": round(float(excess), 4),
            "t": round(float(t), 3),
            "p": round(float(p), 4),
            "verdict": verdict,
        }
        print(
            f"{name:<14} n={n_ev:<4} 事件均{ev_arr.mean():+.2%} "
            f"基准{bench_arr.mean():+.2%} 超额{excess:+.2%} "
            f"t={t:.2f} p={p:.3f} → {verdict}"
        )

    # 3. 落盘
    out_path = PROJECT_ROOT / "data" / "rule_event_study_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 报告已保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
