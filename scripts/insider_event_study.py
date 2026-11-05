#!/usr/bin/env python3
"""内部人增减持事件研究（2026-11-05）。

为什么开这条线：数据能力审计显示，七个候选方向里只有 ``insider_trading`` 同时满足
「池内命中 39/39」与「行情可回测窗口内 82 个独立事件」。K 线形态那条路已被证伪
——三个在用策略的选股alpha 全部为负，任一正超额都只是池子同期涨幅。

三条必须守住的约束，每条都有对应测试：

1. **没有独立公告日**。实测 ``announcement_date`` 与 ``change_date`` 逐行完全相同
   （145471 行滞后恒为 0 天），它是复制列而非独立来源。所以信号日只能取
   ``change_date``，建仓放在**其后第 lag 个交易日开盘**（默认 lag=1）；
   lag=2 作为披露延迟的稳健性检验。若 alpha 只在 lag=1 存在、lag=2 就消失，
   那它来自「提前知道变动」这个拿不到的东西，不是可交易的效应。
2. **样本必须去重并限定在行情窗口内**。24 份滚动全量快照会把同一事件计 24 次；
   一次公告因多个变动人拆成多行。按（标的, 日期, 方向）去重后 N 才是真的。
3. **同池对照**。所有超额都要再减去「同一天买入池子其余股票」的均值，
   否则会把池子自身的漂移当事件效应。这与 K 线策略用的是同一套选股alpha 口径。

用法：
    python scripts/insider_event_study.py
    python scripts/insider_event_study.py --json --lags 1 2 3
退出码：0 = 研究完成；1 = 数据缺失
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _extra in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "src"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from gugu.validation.multiple_testing import report_correction  # noqa: E402
from strategy_scoreboard import (  # noqa: E402
    DEFAULT_CACHE,
    DEFAULT_COST,
    DEFAULT_INDEX,
    load_frames,
    load_index,
    load_pool,
    net_excess_return,
    t_p_value,
    t_stat,
)

# 代码规范化必须与数据能力审计共用一份，否则池内命中口径会分叉
from data_capability_audit import normalize_code  # noqa: E402

DEFAULT_ALTALPHA = PROJECT_ROOT / "data" / "altalpha"
DEFAULT_POOL = PROJECT_ROOT / "data" / "linjudaye_event_study_40.json"
DEFAULT_HORIZONS = (3, 5, 10)
DEFAULT_LAGS = (1, 2)
DIRECTIONS = ("增持", "减持")
# 低于这个事件数不给结论：样本太少时任何均值都只是噪声的位置
MIN_EVENTS = 20
MIN_CLUSTERS = 3
PLACEBO_DRAWS = 200
PLACEBO_SEED = 20261105
# 样本外抽样：只在「至少发生过这么多次增持」的标的里抽，否则抽到的标的多半没有事件
OOS_SEED = 20261105
OOS_SINCE = "2023-09-01"
OOS_MIN_EVENTS_PER_SYMBOL = 2


def _normalized(frame: pd.DataFrame) -> pd.DataFrame:
    """统一成按日期升序、date 为 YYYY-MM-DD 字符串的行情表。"""
    out = frame.copy()
    if "date" not in out.columns:
        out = out.reset_index()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    return out.sort_values("date").reset_index(drop=True)


def merged_events(altdir: Path) -> pd.DataFrame:
    """跨快照合并全市场增减持事件，按（标的, 日期, 方向）去重。

    每个快照都是全量滚动快照，累加会把同一笔事件计 24 次；一次公告又会因多个
    变动人拆成多行。两者都必须去掉，否则 N 会被放大一个量级。

    返回未按标的池过滤的全市场表——样本外抽样需要先看到全市场，
    再决定抽哪些标的。
    """
    frames: list[pd.DataFrame] = []
    for path in sorted(altdir.glob("*insider_trading*.parquet")):
        try:
            frame = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 — 坏快照跳过，不中断整表
            continue
        if "code" not in frame.columns or "change_date" not in frame.columns:
            continue
        mapping = {v: normalize_code(v) for v in frame["code"].dropna().unique()}
        frames.append(
            pd.DataFrame(
                {
                    "symbol": frame["code"].map(mapping),
                    "event_date": pd.to_datetime(frame["change_date"], errors="coerce").dt.strftime(
                        "%Y-%m-%d"
                    ),
                    "direction": (
                        frame["direction"].astype(str) if "direction" in frame.columns else ""
                    ),
                    "ratio": (
                        pd.to_numeric(frame["change_ratio_total"], errors="coerce")
                        if "change_ratio_total" in frame.columns
                        else math.nan
                    ),
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=["symbol", "event_date", "direction", "ratio"])
    merged = pd.concat(frames, ignore_index=True).dropna(subset=["event_date", "symbol"])
    return merged.drop_duplicates(subset=["symbol", "event_date", "direction"])


def select_events(merged: pd.DataFrame, pool: set[str]) -> list[dict[str, Any]]:
    """把全市场事件表收窄到指定标的池，转成可遍历的记录。"""
    if merged.empty:
        return []
    subset = merged[merged["symbol"].isin(pool)]
    records: list[dict[str, Any]] = []
    for row in subset.itertuples(index=False):
        ratio = getattr(row, "ratio", math.nan)
        records.append(
            {
                "symbol": str(row.symbol),
                "event_date": str(row.event_date),
                "direction": str(row.direction),
                "ratio": None if pd.isna(ratio) else float(ratio),
            }
        )
    return sorted(records, key=lambda r: (r["event_date"], r["symbol"]))


def load_events(altdir: Path, pool: set[str]) -> list[dict[str, Any]]:
    """读目录并收窄到标的池。"""
    return select_events(merged_events(altdir), pool)


def sample_universe(
    merged: pd.DataFrame,
    size: int,
    *,
    seed: int = OOS_SEED,
    since: str = OOS_SINCE,
    exclude: set[str] | None = None,
) -> list[str]:
    """从「有增持事件」的标的中随机抽样，用于与既有池无关的样本外检验。

    为什么要抽样：既有 39 只池是当年用另一个事件（linjudaye）筛出来的，
    拿它检验内部人交易本身带有选择偏差，而且 N 只有 49，功效不足。

    为什么先按「有事件」筛：没有事件的标的对事件研究毫无贡献。事件**频率**与
    事件后**收益**无关，所以这个条件不会把 alpha 往某个方向偏。

    ``exclude`` 用来剔掉原池，保证是真样本外。种子固定，干净 clone 也能复现。
    """
    if merged.empty:
        return []
    recent = merged[merged["event_date"] >= since]
    counts = recent[recent["direction"] == "增持"].groupby("symbol").size()
    candidates = sorted(c for c, n in counts.items() if n >= OOS_MIN_EVENTS_PER_SYMBOL)
    if exclude:
        candidates = [c for c in candidates if c not in exclude]
    if not candidates:
        return []
    rng = random.Random(seed)
    return sorted(rng.sample(candidates, min(size, len(candidates))))


def excess_tables(
    frames: dict[str, pd.DataFrame],
    index: pd.DataFrame,
    *,
    horizon: int,
    lag: int,
    cost: float,
) -> tuple[dict[str, dict[str, float]], dict[str, list[str]]]:
    """为每只标的、每个锚定日算出「锚定日后第 lag 日开盘 → 之后第 horizon 日收盘」的净超额。

    锚定日 = 事件发生当天（或之前最近的一个交易日）。表按锚定日索引，这样
    事件行与同池基线行能精确对齐到同一个持有窗口。
    """
    index_frame = _normalized(index)
    index_dates = index_frame["date"].tolist()
    index_pos = {d: i for i, d in enumerate(index_dates)}
    i_open = index_frame["open"].astype(float).tolist()
    i_close = index_frame["close"].astype(float).tolist()

    tables: dict[str, dict[str, float]] = {}
    windows: dict[str, list[str]] = {}
    for symbol, raw in frames.items():
        if raw is None or raw.empty:
            continue
        frame = _normalized(raw)
        dates = frame["date"].tolist()
        opens = pd.to_numeric(frame["open"], errors="coerce").tolist()
        closes = pd.to_numeric(frame["close"], errors="coerce").tolist()
        table: dict[str, float] = {}
        for i in range(len(dates)):
            entry = i + lag
            if entry + horizon >= len(dates):
                break
            bench = index_pos.get(dates[entry])
            if bench is None or bench + horizon >= len(index_dates):
                continue
            excess = net_excess_return(
                opens[entry],
                closes[entry + horizon],
                i_open[bench],
                i_close[bench + horizon],
                cost,
            )
            if excess is None:
                continue
            table[dates[i]] = excess
        tables[symbol] = table
        windows[symbol] = dates
    return tables, windows


def pool_mean_by_date(tables: dict[str, dict[str, float]]) -> dict[str, float]:
    """每个锚定日「把池子里每只都买一遍」的均值，作为同日对照。"""
    grouped: dict[str, list[float]] = {}
    for table in tables.values():
        for day, value in table.items():
            grouped.setdefault(day, []).append(value)
    return {day: sum(v) / len(v) for day, v in grouped.items()}


def build_rows(
    events: list[dict[str, Any]],
    tables: dict[str, dict[str, float]],
    windows: dict[str, list[str]],
    pool_mean: dict[str, float],
) -> tuple[list[dict[str, Any]], int]:
    """把事件落到锚定日，取净超额并减去同日池子均值。返回 (有效行, 丢弃数)。"""
    rows: list[dict[str, Any]] = []
    dropped = 0
    for event in events:
        dates = windows.get(event["symbol"])
        if not dates:
            dropped += 1
            continue
        pos = bisect.bisect_right(dates, event["event_date"]) - 1
        if pos < 0:
            dropped += 1
            continue
        anchor = dates[pos]
        value = tables.get(event["symbol"], {}).get(anchor)
        if value is None:
            dropped += 1
            continue
        pool = pool_mean.get(anchor)
        rows.append(
            {
                "symbol": event["symbol"],
                "date": anchor,
                "event_date": event["event_date"],
                "direction": event["direction"],
                "ratio": event["ratio"],
                "excess": value,
                "pool_mean": pool,
                "gap": None if pool is None else value - pool,
            }
        )
    return rows, dropped


def clustered_stat(values: list[float], keys: list[str]) -> dict[str, Any]:
    """先按簇内求均值再检验，避免同一标的的多次事件被当成独立样本。

    与 ``strategy_scoreboard`` 的 t_symbol 同源思路：重复事件造成的伪独立被压到
    簇层面。这里不设最小簇内样本要求——事件本来稀疏，一只标的一次也算一簇，
    代价是检验更保守而不是更激进。
    """
    buckets: dict[str, list[float]] = {}
    for key, value in zip(keys, values, strict=False):
        buckets.setdefault(key, []).append(value)
    means = [sum(v) / len(v) for v in buckets.values()]
    value = t_stat(means) if len(means) >= MIN_CLUSTERS else math.nan
    if math.isnan(value):
        return {"clusters": len(means), "mean": None, "t": None, "p": None}
    return {
        "clusters": len(means),
        "mean": sum(means) / len(means),
        "t": value,
        "p": t_p_value(value, len(means) - 1),
    }


def placebo_p_value(
    rows: list[dict[str, Any]],
    tables: dict[str, dict[str, float]],
    pool_mean: dict[str, float],
    rng: random.Random,
    draws: int = PLACEBO_DRAWS,
) -> float | None:
    """随机挪动事件日期后的经验 p 值（双侧）。

    参数 t 检验假设独立同分布，而事件在时间上聚集、同一标的反复出现，这个假设
    不成立。做法是保持标的与事件条数不变，只把每笔事件的日期在该标的可用区间内
    随机重排，看随机情况下「平均选股差额」有多大比例能达到观测值。

    它回答的是：如果这些增减持事件毫无信息，我们看到这么大的差额有多容易。
    """
    gaps = [r["gap"] for r in rows if r["gap"] is not None]
    if len(gaps) < MIN_EVENTS:
        return None
    observed = abs(sum(gaps) / len(gaps))
    anchor_lists = {s: sorted(t.keys()) for s, t in tables.items()}
    usable = [r for r in rows if anchor_lists.get(r["symbol"])]
    if not usable:
        return None
    hits = 0
    counted = 0
    for _ in range(draws):
        samples: list[float] = []
        for row in usable:
            anchors = anchor_lists[row["symbol"]]
            day = anchors[rng.randrange(len(anchors))]
            pool = pool_mean.get(day)
            if pool is None:
                continue
            samples.append(tables[row["symbol"]][day] - pool)
        if not samples:
            continue
        counted += 1
        if abs(sum(samples) / len(samples)) >= observed:
            hits += 1
    return hits / counted if counted else None


def summarize(
    rows: list[dict[str, Any]],
    direction: str,
    lag: int,
    horizon: int,
    *,
    tables: dict[str, dict[str, float]],
    pool_mean: dict[str, float],
    rng: random.Random,
) -> dict[str, Any]:
    """一个 (方向, lag, 持有期) 组合的结论。样本不足明确拒答，不给半个数字。"""
    subset = [r for r in rows if r["direction"] == direction]
    base = {
        "direction": direction,
        "lag": lag,
        "horizon": horizon,
        "events": len(subset),
        "clusters": len({r["symbol"] for r in subset}),
    }
    if len(subset) < MIN_EVENTS:
        return {**base, "sufficient": False, "reason": f"事件数 {len(subset)} < {MIN_EVENTS}"}

    excess = [r["excess"] for r in subset]
    gaps = [r["gap"] for r in subset if r["gap"] is not None]
    if len(gaps) != len(subset):
        return {**base, "sufficient": False, "reason": "部分事件缺同日池子对照"}

    by_symbol = clustered_stat(gaps, [r["symbol"] for r in subset])
    by_date = clustered_stat(gaps, [r["date"] for r in subset])
    t_excess = t_stat(excess)
    return {
        **base,
        "sufficient": True,
        "mean_excess": sum(excess) / len(excess),
        "t_excess": None if math.isnan(t_excess) else t_excess,
        "mean_gap": sum(gaps) / len(gaps),
        # 事件加权均值回答"跟着所有信号做会怎样"；簇均值是 t 检验实际用的统计量，
        # 两者可以不同号——同一标的重复出现多寡不同时权重不同，必须都列出来
        "mean_gap_cluster": by_symbol["mean"],
        "t_symbol": by_symbol["t"],
        "p_symbol": by_symbol["p"],
        "clusters": by_symbol["clusters"],
        "t_date": by_date["t"],
        "p_date": by_date["p"],
        "win_rate": sum(1 for g in gaps if g > 0) / len(gaps),
        "placebo_p": placebo_p_value(subset, tables, pool_mean, rng),
    }


def analyze(
    events: list[dict[str, Any]],
    frames: dict[str, pd.DataFrame],
    index: pd.DataFrame,
    *,
    horizons: tuple[int, ...],
    lags: tuple[int, ...],
    cost: float,
    draws: int = PLACEBO_DRAWS,
    universe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    dropped_total = 0
    for lag in lags:
        for horizon in horizons:
            tables, windows = excess_tables(frames, index, horizon=horizon, lag=lag, cost=cost)
            pool_mean = pool_mean_by_date(tables)
            rows, dropped = build_rows(events, tables, windows, pool_mean)
            dropped_total = max(dropped_total, dropped)
            for direction in DIRECTIONS:
                rng = random.Random(PLACEBO_SEED + horizon * 100 + lag)
                results.append(
                    summarize(
                        rows,
                        direction,
                        lag,
                        horizon,
                        tables=tables,
                        pool_mean=pool_mean,
                        rng=rng,
                    )
                )

    tested = [r for r in results if r["sufficient"]]
    correction = {"n_tests": len(tested), "n_significant": 0}
    if tested:
        p_values = [r["p_symbol"] for r in tested]
        if all(p is not None for p in p_values):
            correction = report_correction(p_values)
            rejected = correction["benjamini_hochberg"]["rejected"]
            for row, flag in zip(tested, rejected, strict=False):
                row["fdr_significant"] = bool(flag)
    for row in results:
        row.setdefault("fdr_significant", False)

    return {
        "universe": universe,
        "events_total": len(events),
        "events_dropped": dropped_total,
        "cost": cost,
        "horizons": list(horizons),
        "lags": list(lags),
        "results": results,
        "correction": correction,
    }


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.2f}%"


def _fmt_t(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _fmt_p(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 内部人增减持事件研究",
        "",
    ]
    kind = report.get("universe") or {}
    if kind.get("kind") == "oos":
        lines += [
            f"- 标的池：**样本外随机抽样 {kind.get('size')} 只**"
            f"（种子 {kind.get('seed')}，剔除原 39 只池；原池是当年用另一个事件筛出来的，"
            "直接拿它做检验既有选择偏差又缺功效）",
        ]
    elif kind.get("size"):
        lines += [f"- 标的池：既有 {kind.get('size')} 只事件研究池"]
    lines += [
        f"- 池内独立事件：{report['events_total']} 笔（已按标的+日期+方向去重）",
        f"- 因行情窗口不足丢弃：{report['events_dropped']} 笔",
        f"- 往返成本：{report['cost'] * 100:.1f}%；超额基准：沪深300；"
        "选股差额 = 事件股超额 − 同日池子均值",
        "",
        "| 方向 | 建仓延迟 | 持有期 | 事件数 | 标的数 | 净超额 | t(事件) "
        "| 选股差额 | 簇均值差额 | t(按标的) | p(按标的) | p_FDR | placebo p |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["results"]:
        if not row.get("sufficient"):
            lines.append(
                f"| {row['direction']} | T+{row['lag']} | {row['horizon']}日 "
                f"| {row['events']} | {row['clusters']} | 样本不足 | — | — | — | — | — | — | — |"
            )
            continue
        flag = " **" if row.get("fdr_significant") else " "
        lines.append(
            f"|{flag}{row['direction']} | T+{row['lag']} | {row['horizon']}日 "
            f"| {row['events']} | {row['clusters']} "
            f"| {_fmt_pct(row['mean_excess'])} | {_fmt_t(row['t_excess'])} "
            f"| {_fmt_pct(row['mean_gap'])} | {_fmt_pct(row.get('mean_gap_cluster'))} "
            f"| {_fmt_t(row['t_symbol'])} "
            f"| {_fmt_p(row['p_symbol'])} | {'是' if row.get('fdr_significant') else '否'} "
            f"| {_fmt_p(row['placebo_p'])} |"
        )

    corr = report["correction"]
    lines += [
        "",
        "> 建仓延迟 T+n = 变动日之后第 n 个交易日开盘建仓。数据里 "
        "`announcement_date` 与 `change_date` 逐行相同，不能当独立公告日，"
        "所以只看 T+1 是不严谨的：**T+1 显著、T+2 消失，说明效应来自提前获知**。",
        "> 净超额 = 相对沪深300、扣往返成本后的收益；选股差额 = 再减去同一天"
        "「把池子其余股票都买一遍」的均值。后者才是这个事件有没有选股能力的判据。",
        "> 「选股差额」按事件加权（跟着每个信号做会拿到多少），「簇均值差额」按标的"
        "等权（t 检验实际用的统计量）。同一标的反复出现时两者权重不同，可以不同号，"
        "别只看其中一个就下结论。",
        f"> 多重检验：共 {corr.get('n_tests', 0)} 个可比组合，"
        f"按 BH-FDR 校正（加粗 = 校正后仍显著，{corr.get('n_significant', 0)} 个）。",
        f"> placebo p = 保持标的与事件条数不变、把日期在标的可用区间内随机重排 "
        f"{PLACEBO_DRAWS} 次后，随机情况下差额不低于观测值的比例。"
        "事件聚集且同一标的反复出现时，参数 t 检验偏乐观，这个数更接近实际。",
        f"> 事件数 < {MIN_EVENTS} 的组合一律拒答，不给半个数字。",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="内部人增减持事件研究")
    parser.add_argument("--altalpha-dir", default=str(DEFAULT_ALTALPHA))
    parser.add_argument("--pool-file", default=str(DEFAULT_POOL))
    parser.add_argument(
        "--oos-sample",
        type=int,
        default=0,
        help="改用样本外随机抽样：从全市场有增持事件的标的中抽 N 只（剔除 --pool-file 里的池）",
    )
    parser.add_argument("--oos-seed", type=int, default=OOS_SEED)
    parser.add_argument("--oos-since", default=OOS_SINCE)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--cost", type=float, default=DEFAULT_COST)
    parser.add_argument("--horizons", type=int, nargs="+", default=list(DEFAULT_HORIZONS))
    parser.add_argument("--lags", type=int, nargs="+", default=list(DEFAULT_LAGS))
    parser.add_argument("--days", type=int, default=700)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    altdir = Path(args.altalpha_dir)
    if not altdir.exists():
        print(f"另类数据目录不存在：{altdir}", file=sys.stderr)
        return 1

    pool_file = Path(args.pool_file)
    pool_symbols = load_pool(pool_file)
    base_pool = {c for c in (normalize_code(s) for s in pool_symbols) if c}
    if not base_pool:
        print(f"标的池为空或不存在：{pool_file}", file=sys.stderr)
        return 1

    merged = merged_events(altdir)
    if args.oos_sample:
        symbols = sample_universe(
            merged,
            args.oos_sample,
            seed=args.oos_seed,
            since=args.oos_since,
            exclude=base_pool,
        )
        if not symbols:
            print("样本外抽样为空：没有符合条件的标的", file=sys.stderr)
            return 1
        universe = {
            "kind": "oos",
            "size": len(symbols),
            "seed": args.oos_seed,
            "since": args.oos_since,
        }
    else:
        symbols = pool_symbols
        universe = {"kind": "pool", "size": len(base_pool)}

    pool = {c for c in (normalize_code(s) for s in symbols) if c}
    events = select_events(merged, pool)
    if not events:
        print("池内没有可用的增减持事件", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir)
    frames, failed = load_frames(symbols, cache_dir, args.days)
    # 池子里写着 sz002230，事件已规范化成 002230；不统一键名会让所有事件都对不上行情
    frames = {
        code: frame
        for raw_key, frame in frames.items()
        if (code := normalize_code(raw_key)) is not None
    }
    index = load_index(args.index, cache_dir, args.days)
    if not frames:
        print("行情缓存为空，无法计算收益", file=sys.stderr)
        return 1
    if index is None or index.empty:
        print("基准指数缺失，无法计算超额", file=sys.stderr)
        return 1
    if failed:
        print(f"行情缺失 {len(failed)} 只：{','.join(failed[:5])}", file=sys.stderr)

    report = analyze(
        events,
        frames,
        index,
        horizons=tuple(args.horizons),
        lags=tuple(args.lags),
        cost=args.cost,
        universe=universe,
    )
    print(
        json.dumps(report, ensure_ascii=False, indent=1) if args.json else render_markdown(report)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
