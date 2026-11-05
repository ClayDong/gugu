#!/usr/bin/env python3
"""密度优先假设：信号越"孤立"，每笔 alpha 越高的说法能不能被数据支持？

背景（2026-11-05）：扣掉同池无条件漂移后，四个在用策略里只有 `box_breakout`
三档同号为正（+0.24%~+0.38%，密度 4.7%），`linjudaye` 密度 31.2% 只有
+0.11%。于是产生一个顺理成章的猜想：

    「少而准」是可提取的结构——把信号压到 5% 密度以下，每笔 alpha 会上升。

这个猜想必须用**信号当时可知**的条件去检验，否则就是拿未来收益挑样本。
本脚本用横截面广度作为切分条件：

    广度(信号日 d) = 当日全池有多少比例的标的同时发出买入信号

- 广度低 → 形态在池子里孤立出现，更可能是个股自身信息；
- 广度高 → 整个池子一起触发，更像是 beta / 板块共振。

广度只用当日及以前的信号计数，收盘即可知，不存在前视。

与 `strategy_scoreboard.py` 共用同一套事件采集、成本、基准与净alpha 口径，
两表可以直接对照。不复制任何统计逻辑。

用法：
    python scripts/density_hypothesis.py
    python scripts/density_hypothesis.py --strategies box_breakout linjudaye --buckets 3
    python scripts/density_hypothesis.py --json
退出码：0 = 正常；1 = 无标的 / 基准不可用 / 全部策略无事件
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from scipy import stats as sps

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from gugu.validation.multiple_testing import benjamini_hochberg  # noqa: E402
from strategy_scoreboard import (  # noqa: E402
    DEFAULT_COST,
    DEFAULT_HORIZON,
    DEFAULT_INDEX,
    DEFAULT_START,
    AlwaysOnStrategy,
    Events,
    baseline_events,
    build_stat,
    collect_events,
    load_frames,
    load_index,
    load_pool,
    net_alpha,
    t_stat,
)

DEFAULT_STRATEGIES = ("box_breakout", "linjudaye", "double_yin")
DEFAULT_CACHE = PROJECT_ROOT / "data" / "parquet_cache"
DEFAULT_POOL_FILE = PROJECT_ROOT / "data" / "linjudaye_event_study_40.json"

# 一个桶少于这个事件数就不下结论——分桶之后更容易出现"看着显著的巧合"
MIN_BUCKET_EVENTS = 100

BUCKET_LABELS = {
    2: ("低广度", "高广度"),
    3: ("低广度（信号孤立）", "中广度", "高广度（全池共振）"),
    4: ("低广度", "中低广度", "中高广度", "高广度"),
}


def breadth_by_date(events: Events, pool_size: int) -> dict[str, float]:
    """每个信号日的横截面广度（当日发信号的标的数 / 池子标的数）。

    只用当日信号计数，收盘即可知道，不含任何未来信息。
    """
    size = max(int(pool_size), 1)
    counters: dict[str, set[str]] = {}
    for symbol, dates in events.signal_dates.items():
        for date in dates:
            counters.setdefault(date, set()).add(symbol)
    return {date: len(symbols) / size for date, symbols in counters.items()}


def attach_breadth(events: Events, pool_size: int) -> list[dict[str, Any]]:
    """把广度贴到每一笔事件上；信号日不在广度表里的事件直接丢弃（不猜）。"""
    breadth = breadth_by_date(events, pool_size)
    rows: list[dict[str, Any]] = []
    for row in events.rows:
        value = breadth.get(str(row["date"]))
        if value is None:
            continue
        rows.append({**row, "breadth": value})
    return rows


def split_buckets(rows: list[dict[str, Any]], buckets: int = 3) -> list[tuple[str, list[dict]]]:
    """按广度排序后等事件数切桶。

    等事件数切法保证每桶样本量一致，避免"低广度信号天然稀少"导致桶间样本悬殊；
    排序键加上日期是为了在广度相同的密集区间里有确定的切分结果。
    """
    if buckets < 2:
        raise ValueError("buckets 至少为 2，否则不存在对照")
    labels = BUCKET_LABELS.get(buckets)
    if labels is None:
        labels = tuple(f"第{i + 1}桶" for i in range(buckets))
    ordered = sorted(rows, key=lambda r: (r["breadth"], str(r["date"]), str(r["symbol"])))
    total = len(ordered)
    if total == 0:
        return []
    groups: list[tuple[str, list[dict]]] = []
    for index, label in enumerate(labels):
        start = index * total // buckets
        stop = (index + 1) * total // buckets
        groups.append((label, ordered[start:stop]))
    return groups


def events_from_rows(rows: list[dict[str, Any]]) -> Events:
    events = Events()
    for row in rows:
        events.per_symbol.setdefault(str(row["symbol"]), []).append(float(row["excess"]))
    return events


def t_p_value(value: float | None, df: int) -> float | None:
    """双侧 p 值（精确 t 分布）。

    不用正态近似：df 只有 38 时，t=2.0 在 t 分布下 p=0.052、正态下 p=0.046，
    恰好横跨 0.05 这条线，而这条线正是要判断的地方。
    """
    if value is None or df < 1:
        return None
    return float(2 * sps.t.sf(abs(float(value)), df))


def bh_adjusted_p(p_values: list[float]) -> list[float]:
    """BH 校正后的 p 值（逐项）。

    项目里的 ``benjamini_hochberg`` 只回"哪些通过"，不给逐项校正 p 值；
    看表时需要知道"这一桶校正后到底多少"，所以这里补上标准算法：
    从大到小累取 min，再取 (n/rank)·p。
    """
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: p_values[i])
    adjusted = [1.0] * n
    running = 1.0
    for rank in range(n, 0, -1):
        index = order[rank - 1]
        running = min(running, p_values[index] * n / rank)
        adjusted[index] = min(1.0, running)
    return adjusted


def apply_fdr(rows: list[dict[str, Any]]) -> None:
    """对本次运行的分桶结果做 BH-FDR 校正，结果写回每一行。

    只对「桶」做校正，不把全集算进去（全集是参照，不是一条待检验的主张）。
    一次跑三个桶就是三次比较：最高那个 t 天然偏大，不校正等于自己骗自己。
    """
    buckets = [r for r in rows if r.get("bucket") != "全集（不分桶）" and r.get("p_date")]
    if not buckets:
        return
    p_values = [float(r["p_date"]) for r in buckets]
    result = benjamini_hochberg(p_values, alpha=0.05)
    for row, rejected, adjusted in zip(
        buckets, result["rejected"], bh_adjusted_p(p_values), strict=False
    ):
        row["p_date_fdr"] = adjusted
        row["sig_fdr"] = bool(rejected)
    for row in rows:
        row.setdefault("p_date_fdr", None)
        row.setdefault("sig_fdr", None)


def date_cluster_stat(rows: list[dict[str, Any]]) -> tuple[int, float | None]:
    """按信号日聚类的 t 值：先把同一天的所有标的压成日均值，再对日期做检验。

    为什么必须这样：广度是**日期级**变量。高广度那天全池 39 只一起发信号，
    它们不是 39 个独立观测，而是同一件事被数了 39 遍。按标的配对会把同一批
    交易日重复计入，t 值虚高——这正是 2026-11-05 实测里必须查清的那一步。
    """
    per_date: dict[str, list[float]] = {}
    for row in rows:
        per_date.setdefault(str(row["date"]), []).append(float(row["excess"]))
    means = [sum(v) / len(v) for v in per_date.values()]
    if len(means) < 3:
        return len(means), None
    value = t_stat(means)
    return len(means), (None if math.isnan(value) else value)


def summarize(
    label: str,
    rows: list[dict[str, Any]],
    base_events: Events,
) -> dict[str, Any]:
    """一个广度桶的结论行；样本不足时只报数量、不给统计量。"""
    breadth = [float(r["breadth"]) for r in rows]
    stat: dict[str, Any] = {
        "bucket": label,
        "events": len(rows),
        "breadth_min": min(breadth) if breadth else None,
        "breadth_max": max(breadth) if breadth else None,
        "breadth_mean": sum(breadth) / len(breadth) if breadth else None,
        "samples": 0,
        "mean_excess": None,
        "win_rate": None,
        "t_event": None,
        "t_symbol": None,
        "t_date": None,
        "dates": 0,
        "pairs": 0,
        "mean_alpha": None,
        "t_alpha": None,
        "positive_ratio": None,
        "insufficient": len(rows) < MIN_BUCKET_EVENTS,
    }
    if stat["insufficient"]:
        return stat
    events = events_from_rows(rows)
    stat.update(build_stat(label, events))
    stat["bucket"] = label
    stat.update(net_alpha(events, base_events))
    stat["dates"], stat["t_date"] = date_cluster_stat(rows)
    stat["p_date"] = t_p_value(stat["t_date"], stat["dates"] - 1)
    stat["p_alpha"] = t_p_value(stat["t_alpha"], stat["pairs"] - 1)
    return stat


def analyze(
    strategy: Any,
    frames: dict[str, pd.DataFrame],
    index_df: pd.DataFrame,
    base_events: Events,
    *,
    horizon: int,
    cost_rate: float,
    start: str,
    buckets: int = 3,
) -> list[dict[str, Any]]:
    """返回 [全集] + 各广度桶 的行。全集行用于确认分桶前后总量一致。"""
    events = collect_events(
        strategy, frames, index_df, horizon=horizon, cost_rate=cost_rate, start=start
    )
    rows = attach_breadth(events, len(frames))
    overall = summarize("全集（不分桶）", rows, base_events)
    overall["breadth_min"] = None
    overall["breadth_max"] = None
    overall["breadth_mean"] = None
    overall["insufficient"] = len(rows) < MIN_BUCKET_EVENTS
    result = [overall]
    for label, group in split_buckets(rows, buckets):
        result.append(summarize(label, group, base_events))
    apply_fdr(result)
    return result


def _fmt_t(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _fmt_p(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2%}"


def render_markdown(rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        "# 密度优先假设检验（横截面广度分桶）",
        "",
        f"- 策略：{meta['strategy']}",
        f"- 窗口起点：{meta['start']}",
        f"- 往返成本：{meta['cost']:.1%}",
        f"- 持有周期：{meta['horizon']} 个交易日",
        f"- 基准：{meta['index']}",
        f"- 标的数：{meta['symbols']}",
        f"- 分桶数：{meta['buckets']}（按广度排序后等事件数切分）",
        "",
        "| 分组 | 广度区间 | 事件数 | 信号日数 | 净超额 | 净alpha | 胜率 "
        "| t(按标的) | t(按日) | p(按日, FDR) | t(净alpha) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        span = "—"
        if row.get("breadth_min") is not None:
            span = f"{row['breadth_min']:.0%}~{row['breadth_max']:.0%}"
        if row.get("insufficient"):
            lines.append(
                f"| {row['bucket']} | {span} | {row['events']} | 样本不足 | — | — | — | — | — |"
            )
            continue
        lines.append(
            f"| {row['bucket']} | {span} | {row['events']} | {row['dates']} "
            f"| {_fmt_pct(row['mean_excess'])} | {_fmt_pct(row['mean_alpha'])} "
            f"| {row['win_rate']:.0%} | {_fmt_t(row['t_symbol'])} "
            f"| {_fmt_t(row['t_date'])} | {_fmt_p(row.get('p_date_fdr'))} "
            f"| {_fmt_t(row['t_alpha'])} |"
        )
    baseline = meta.get("baseline") or {}
    if baseline.get("samples"):
        lines.append(
            f"| （对照）{baseline['strategy']} | 100% | {baseline['samples']} | — "
            f"| {_fmt_pct(baseline['mean_excess'])} | — | {baseline['win_rate']:.0%} "
            f"| {_fmt_t(baseline['t_symbol'])} | — | — | — |"
        )
    lines += [
        "",
        "> 广度 = 当日全池发买入信号的标的占比，只用当日及以前的信号计数，收盘即可知，不存在前视。",
        "> 假设内容是：低广度（信号孤立）的每笔净alpha 应高于高广度（全池共振）。"
        "若各桶净alpha 随广度上升而不下降，假设被证伪。",
        f"> 单桶事件数 < {MIN_BUCKET_EVENTS} 时只报数量、不下结论。",
        "> t(按日) 才是这一节该看的显著性：广度是日期级变量，高广度那天全池一起发"
        "信号，按标的配对会把同一批交易日重复计数。用事件数读 t 会系统性虚高。",
        "> p(按日, FDR) 已对本次三个桶做 BH 校正——三个桶就是三次比较，"
        "不校正时最大的那个 t 天然偏大。",
        "> 同一信号日全池属同一个广度值，因此分桶等价于按日期分组；"
        "t(净alpha) 的按标的配对会重复计数，仅作参考。",
        "> 净alpha 与净超额口径不同（逐标的等权 vs 逐事件加权），不可相加。",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="密度优先假设检验（横截面广度分桶）")
    parser.add_argument("--strategies", nargs="*", default=list(DEFAULT_STRATEGIES))
    parser.add_argument("--pool-file", default=str(DEFAULT_POOL_FILE))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--days", type=int, default=800)
    parser.add_argument("--cost", type=float, default=DEFAULT_COST)
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--buckets", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    from gugu.strategies.registry import get_strategy

    if args.buckets < 2:
        print("--buckets 至少为 2", file=sys.stderr)
        return 1

    symbols = load_pool(Path(args.pool_file))
    if not symbols:
        print(f"标的池为空或不存在：{args.pool_file}", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir)
    index_df = load_index(args.index, cache_dir, args.days)
    if index_df is None or index_df.empty:
        print(f"基准 {args.index} 行情不可用，无法计算超额", file=sys.stderr)
        return 1

    frames, failed = load_frames(symbols, cache_dir, args.days)
    if not frames:
        print(f"全部 {len(symbols)} 只标的行情不可用，未产生结论", file=sys.stderr)
        return 1

    base_events = baseline_events(
        frames, index_df, horizon=args.horizon, cost_rate=args.cost, start=args.start
    )
    payload: list[dict[str, Any]] = []
    analysed = 0
    for name in args.strategies:
        try:
            strategy = get_strategy(name)
        except Exception as exc:  # noqa: BLE001
            print(f"[跳过] {name} 实例化失败：{exc}", file=sys.stderr)
            continue
        rows = analyze(
            strategy,
            frames,
            index_df,
            base_events,
            horizon=args.horizon,
            cost_rate=args.cost,
            start=args.start,
            buckets=args.buckets,
        )
        if not rows[0]["events"]:
            print(f"[跳过] {name} 在该窗口无事件", file=sys.stderr)
            continue
        analysed += 1
        meta = {
            "strategy": name,
            "start": args.start,
            "cost": args.cost,
            "horizon": args.horizon,
            "index": args.index,
            "symbols": len(frames),
            "failed": failed,
            "buckets": args.buckets,
            "baseline": build_stat(AlwaysOnStrategy.name, base_events),
        }
        if args.json:
            payload.append({"meta": meta, "buckets": rows})
        else:
            print(render_markdown(rows, meta))
            print()

    if analysed == 0:
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
