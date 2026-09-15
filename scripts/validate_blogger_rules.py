"""批量验证外部博主规则的配对事件研究。

规则函数来自可选的 ``mulan-quant`` 工作区。事件收益与同一股票、同一
信号日的基准收益成对比较，避免把两个不同样本的均值硬拼成 alpha。

用法::

    python scripts/validate_blogger_rules.py --mulan-root /path/to/mulan-quant
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_rules(mulan_root: Path) -> dict[str, Callable[..., Any]]:
    """按显式路径加载外部规则，避免导入阶段依赖固定机器路径。"""
    if not mulan_root.exists():
        raise FileNotFoundError(f"mulan-quant 路径不存在: {mulan_root}")
    sys.path.insert(0, str(mulan_root))
    try:
        from rules_library.multi_blogger_engine import (
            chen_bottom_divergence,
            chen_failed_limitup_reversal,
            neighbor_2b_buy,
            neighbor_golden_section,
            neighbor_pullback_buy,
        )
    except ImportError as exc:
        raise ImportError(f"{mulan_root} 中缺少 rules_library.multi_blogger_engine") from exc
    return {
        "陈学长·底背离买入": chen_bottom_divergence,
        "陈学长·断板反包": chen_failed_limitup_reversal,
        "邻居大爷·2B反转": neighbor_2b_buy,
        "邻居大爷·黄金分割": neighbor_golden_section,
        "邻居大爷·缩量回踩": neighbor_pullback_buy,
    }


def _rule_signal(rule_fn: Callable[..., Any], df: pd.DataFrame) -> bool:
    """调用规则函数，兼容 mulan Signal 对象或 dict。"""
    try:
        signal = rule_fn(df)
    except Exception:
        return False
    if signal is None:
        return False
    if hasattr(signal, "signal_type"):
        text = str(getattr(signal, "signal_type", "")).upper()
        return "BUY" in text or "买入" in text
    if isinstance(signal, dict):
        text = str(signal.get("signal_type", "")).upper()
        return "BUY" in text or "买入" in text
    return True


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """补充外部规则需要的历史技术列。"""
    out = df.sort_values("date").reset_index(drop=True).copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")
    out = out.dropna(subset=["date", "close", "volume"])
    if "prev_close" not in out.columns:
        out["prev_close"] = out["close"].shift(1)
    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    out["dif"] = ema12 - ema26
    if "ma_20" not in out.columns:
        out["ma_20"] = out["close"].rolling(20).mean()
    return out


def _paired_event_rows(
    df: pd.DataFrame,
    rule_fn: Callable[..., Any],
    symbol: str,
    horizons: tuple[int, ...],
    benchmark_by_date: dict[int, dict[pd.Timestamp, dict[str, float]]],
) -> list[dict[str, Any]]:
    """生成事件和同日横截面基准收益，排除事件股票自身。"""
    closes = df["close"].to_numpy(dtype=float)
    dates = df["date"]
    rows: list[dict[str, Any]] = []
    max_horizon = max(horizons)
    for index in range(30, len(df) - max_horizon):
        if closes[index] <= 0 or not _rule_signal(rule_fn, df.iloc[: index + 1].copy()):
            continue
        event_date = dates.iloc[index]
        row: dict[str, Any] = {"symbol": symbol, "date": event_date}
        for horizon in horizons:
            event_return = closes[index + horizon] / closes[index] - 1
            peers = [
                value
                for peer_symbol, value in benchmark_by_date.get(horizon, {})
                .get(event_date, {})
                .items()
                if peer_symbol != symbol and np.isfinite(value)
            ]
            row[f"event_{horizon}d"] = event_return
            row[f"benchmark_{horizon}d"] = float(np.mean(peers)) if peers else np.nan
        rows.append(row)
        break
    return rows


def _summarize_paired(rows: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    """对事件和配对基准做超额统计。"""
    from scipy import stats

    event_key = f"event_{horizon}d"
    benchmark_key = f"benchmark_{horizon}d"
    if not rows:
        return {
            "horizon": horizon,
            "samples": 0,
            "verdict": "样本不足",
            "detail": "无事件",
        }
    event_values = np.asarray([float(row[event_key]) for row in rows], dtype=float)
    benchmark_values = np.asarray([float(row[benchmark_key]) for row in rows], dtype=float)
    excess_values = event_values - benchmark_values
    finite = np.isfinite(event_values) & np.isfinite(benchmark_values)
    event_values = event_values[finite]
    benchmark_values = benchmark_values[finite]
    excess_values = excess_values[finite]
    if len(excess_values) < 5:
        return {
            "horizon": horizon,
            "samples": int(len(excess_values)),
            "verdict": "样本不足",
            "detail": "配对事件 < 5，无法统计",
        }
    t_stat, p_value = stats.ttest_1samp(excess_values, 0.0, nan_policy="omit")
    t_stat = float(t_stat) if np.isfinite(t_stat) else 0.0
    p_value = float(p_value) if np.isfinite(p_value) else 1.0
    mean_excess = float(np.mean(excess_values))
    verdict = (
        "有edge"
        if p_value < 0.05 and mean_excess > 0
        else ("证伪" if p_value < 0.05 and mean_excess < 0 else "样本不足")
    )
    return {
        "horizon": horizon,
        "samples": int(len(excess_values)),
        "event_avg_return": round(float(np.mean(event_values)), 4),
        "benchmark_avg_return": round(float(np.mean(benchmark_values)), 4),
        "excess_return": round(mean_excess, 4),
        "t": round(t_stat, 3),
        "p": round(p_value, 4),
        "win_rate": round(float(np.mean(event_values > 0)), 4),
        "excess_win_rate": round(float(np.mean(excess_values > 0)), 4),
        "verdict": verdict,
    }


async def run(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from gugu.data.manager import DataManager

    try:
        rules = _load_rules(args.mulan_root)
    except (FileNotFoundError, ImportError) as exc:
        raise SystemExit(str(exc)) from exc

    dm = DataManager()
    cache_dir = PROJECT_ROOT / "data" / "parquet_cache"
    symbols = sorted(
        {
            path.name.split("_")[0]
            for path in cache_dir.glob("*_120d_none.parquet")
            if path.name.split("_")[0].isdigit()
        }
    )[: args.max_symbols]
    print(f"扫描 {len(symbols)} 只缓存股票\n")

    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            frame = await dm.fetch_stock_history(symbol, days=args.days)
        except Exception:
            continue
        if frame is None or len(frame) < 30:
            continue
        frames[symbol] = _enrich(frame)

    # 先构造同日横截面基准，再扫描规则事件。
    benchmark_by_date: dict[int, dict[pd.Timestamp, dict[str, float]]] = {
        horizon: {} for horizon in (5, 10)
    }
    for symbol, frame in frames.items():
        closes = frame["close"].to_numpy(dtype=float)
        dates = frame["date"]
        for index in range(len(frame) - 10):
            if closes[index] <= 0:
                continue
            for horizon in (5, 10):
                if index + horizon >= len(frame) or closes[index + horizon] <= 0:
                    continue
                benchmark_by_date[horizon].setdefault(dates.iloc[index], {})[symbol] = (
                    closes[index + horizon] / closes[index] - 1
                )

    rule_rows: dict[str, list[dict[str, Any]]] = {name: [] for name in rules}
    for symbol, frame in frames.items():
        for name, rule_fn in rules.items():
            rule_rows[name].extend(
                _paired_event_rows(frame, rule_fn, symbol, (5, 10), benchmark_by_date)
            )

    report: dict[str, dict[str, Any]] = {}
    for name, rows in rule_rows.items():
        by_horizon = {str(horizon): _summarize_paired(rows, horizon) for horizon in (5, 10)}
        report[name] = {
            "rule": name,
            "event_count": len(rows),
            "by_horizon": by_horizon,
        }
        result_5d = by_horizon["5"]

        def _pct(value: object) -> str:
            try:
                return f"{float(value):+.2%}"
            except (TypeError, ValueError):
                return "N/A"

        print(
            f"{name:<14} n={result_5d['samples']:<4} "
            f"事件均{_pct(result_5d.get('event_avg_return'))} "
            f"基准{_pct(result_5d.get('benchmark_avg_return'))} "
            f"超额{_pct(result_5d.get('excess_return'))} "
            f"p={result_5d.get('p', 1):.3f} → {result_5d['verdict']}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存: {args.output}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量验证外部规则的配对事件研究")
    parser.add_argument(
        "--mulan-root",
        type=Path,
        default=Path(os.environ.get("MULAN_QUANT_ROOT", str(PROJECT_ROOT.parent / "mulan-quant"))),
        help="mulan-quant 工作区路径",
    )
    parser.add_argument("--max-symbols", type=int, default=182)
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "rule_event_study_report.json",
    )
    return parser


def main() -> int:
    return asyncio.run(run(_build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
