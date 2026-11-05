#!/usr/bin/env python3
"""策略同口径体检表：扣掉成本后，paper 里启用的策略到底还有没有 edge。

为什么需要它（2026-11-05）：
P0-2 的原计划是"改策略让信号能过 5% 涨跌幅闸门"。跑了一圈数据才发现方向反了——
`two_b_reversal` 在 39 只标的、2024-02 至今的口径下，扣 0.4% 往返成本后
5 日净超额是 -0.09%（t=-1.19）；把进场往 2B 低点收紧反而更差
（ext<=5% 时 -0.18%，t=-2.07），"真 2B"加贴近前低上限后单调恶化到 -0.34%。
也就是说：不是闸门太严，是策略本身没有 edge，收紧只会让它更负。

这个判断不应该只活在一次对话里。本脚本把它变成可复现的一条命令：
同一池、同一窗口、同一口径（次日开盘入场 / 持有 N 日收盘出场 / 扣往返成本 /
相对基准超额），把所有策略摆在一张表上比。

关键指标是「密度」——策略在多大比例的交易日里发买入信号。
密度 20%+ 说明条件几乎不筛选，那不是形态识别，是噪声；密度低但超额为负，
说明条件筛掉的是对的部分。两者都不是靠调参能救的。

只做统计，不改任何策略、不改任何判定逻辑。

用法：
    python scripts/strategy_scoreboard.py
    python scripts/strategy_scoreboard.py --strategies two_b_reversal linjudaye
    python scripts/strategy_scoreboard.py --days 700 --cost 0.004 --json
退出码：0 = 正常；1 = 完全取不到行情（如实报告，不伪造数字）
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

DEFAULT_STRATEGIES = ("box_breakout", "double_yin", "linjudaye", "two_b_reversal")
DEFAULT_INDEX = "sh000300"
DEFAULT_COST = 0.004
DEFAULT_HORIZON = 5
DEFAULT_DAYS = 700
DEFAULT_START = "2024-02-01"
DEFAULT_CACHE = PROJECT_ROOT / "data" / "parquet_cache"

MIN_HISTORY_ROWS = 60
MIN_CLUSTER_SAMPLES = 5
T_MIN_SAMPLES = 3


@dataclass
class Events:
    """一次采集的原始结果：逐标的的净超额序列 + 信号密度计数。"""

    per_symbol: dict[str, list[float]] = field(default_factory=dict)
    signal_days: int = 0
    total_days: int = 0

    @property
    def density(self) -> float:
        return self.signal_days / self.total_days if self.total_days > 0 else 0.0

    @property
    def returns(self) -> list[float]:
        return [v for values in self.per_symbol.values() for v in values]


def t_stat(values: list[float]) -> float:
    """单样本 t 值；样本不足返回 nan（不假装有结论）。"""
    if len(values) < T_MIN_SAMPLES:
        return math.nan
    mean = sum(values) / len(values)
    sd = statistics.stdev(values)
    if sd <= 0:
        return math.nan
    return mean / (sd / math.sqrt(len(values)))


def net_excess_return(
    entry_price: float,
    exit_price: float,
    benchmark_entry: float,
    benchmark_exit: float,
    cost_rate: float,
) -> float | None:
    """扣往返成本后的超额收益。任一价格非法返回 None，不用 0 冒充。"""
    prices = (entry_price, exit_price, benchmark_entry, benchmark_exit)
    if any(not _is_positive(p) for p in prices):
        return None
    net = (exit_price / entry_price) * (1 - cost_rate) - 1
    benchmark = benchmark_exit / benchmark_entry - 1
    return net - benchmark


def _is_positive(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """统一成按日期升序、date 为字符串的行情表。"""
    frame = df.copy()
    if "date" not in frame.columns:
        frame = frame.reset_index()
    frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    return frame.sort_values("date").reset_index(drop=True)


def collect_events(
    strategy: Any,
    frames: dict[str, pd.DataFrame],
    index_df: pd.DataFrame,
    *,
    horizon: int = DEFAULT_HORIZON,
    cost_rate: float = DEFAULT_COST,
    start: str = DEFAULT_START,
    min_rows: int = MIN_HISTORY_ROWS,
) -> Events:
    """按统一口径采集策略事件。

    入场 = 信号日的次日开盘（信号当日收盘后才可知，次日开盘才可执行）；
    出场 = 入场后第 horizon 个交易日收盘；基准同步用开盘进、收盘出。
    """
    if index_df is None or index_df.empty:
        return Events()
    index = _normalize_frame(index_df)
    index_pos = {d: i for i, d in enumerate(index["date"])}
    i_open = index["open"].astype(float).tolist()
    i_close = index["close"].astype(float).tolist()
    index_dates = list(index["date"])

    events = Events()
    for symbol, raw in frames.items():
        if raw is None or len(raw) < min_rows:
            continue
        df = _normalize_frame(raw)
        try:
            signals = strategy.generate_signals(df.copy(), is_backtest=True)
        except Exception:  # noqa: BLE001 — 单标的失败不应中断整表
            continue
        if signals is None or "signal" not in getattr(signals, "columns", []):
            continue
        values = pd.to_numeric(signals["signal"], errors="coerce").fillna(0).to_numpy()
        buy = values == 1
        events.total_days += len(df)
        events.signal_days += int(buy.sum())
        for i in range(len(df)):
            if not buy[i]:
                continue
            if str(df.loc[i, "date"]) < start:
                continue
            entry_idx = i + 1
            if entry_idx + horizon >= len(df):
                continue
            bench_pos = index_pos.get(str(df.loc[entry_idx, "date"]))
            if bench_pos is None or bench_pos + horizon >= len(index_dates):
                continue
            excess = net_excess_return(
                float(df.loc[entry_idx, "open"]),
                float(df.loc[entry_idx + horizon, "close"]),
                i_open[bench_pos],
                i_close[bench_pos + horizon],
                cost_rate,
            )
            if excess is None:
                continue
            events.per_symbol.setdefault(symbol, []).append(excess)
    return events


def build_stat(name: str, events: Events) -> dict[str, Any]:
    """把一个策略的事件集合压成一行结论。

    同时给两个 t 值：
    - t_event：逐事件检验，信号在时间上重叠时会虚高，只作参考；
    - t_symbol：先按标的求均值再检验（39 个簇），信号重叠造成的伪独立被压掉，
      是这个表里应当优先看的那个。
    """
    returns = events.returns
    if not returns:
        return {
            "strategy": name,
            "density": events.density,
            "samples": 0,
            "mean_excess": None,
            "win_rate": None,
            "t_event": None,
            "t_symbol": None,
        }
    clusters = [
        sum(v) / len(v) for v in events.per_symbol.values() if len(v) >= MIN_CLUSTER_SAMPLES
    ]
    return {
        "strategy": name,
        "density": events.density,
        "samples": len(returns),
        "mean_excess": sum(returns) / len(returns),
        "win_rate": sum(1 for r in returns if r > 0) / len(returns),
        "t_event": _nan_to_none(t_stat(returns)),
        "t_symbol": _nan_to_none(t_stat(clusters)),
    }


def _nan_to_none(value: float) -> float | None:
    return None if math.isnan(value) else value


class AlwaysOnStrategy:
    """对照基准：每个交易日都发买入信号。

    用途是量化「池子本身」相对指数的漂移。2026-11-05 实测：这套 39 只池
    在 10 日持有期上的无条件超额是 +0.25%（t=+4.89）——也就是说任何策略
    在 10 日口径下只要跟着池子走就会显示为正。不做这一步对照，
    就会把池子的 beta 当成策略的 alpha。
    """

    name = "同池无条件基准"

    def generate_signals(self, df: pd.DataFrame, is_backtest: bool = False) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = 1
        return out


def baseline_events(
    frames: dict[str, pd.DataFrame],
    index_df: pd.DataFrame,
    *,
    horizon: int = DEFAULT_HORIZON,
    cost_rate: float = DEFAULT_COST,
    start: str = DEFAULT_START,
) -> Events:
    """同池「每天都买」的无条件事件集合，供所有策略/规则做配对对照。"""
    return collect_events(
        AlwaysOnStrategy(),
        frames,
        index_df,
        horizon=horizon,
        cost_rate=cost_rate,
        start=start,
    )


def baseline_stat(
    frames: dict[str, pd.DataFrame],
    index_df: pd.DataFrame,
    *,
    horizon: int = DEFAULT_HORIZON,
    cost_rate: float = DEFAULT_COST,
    start: str = DEFAULT_START,
) -> dict[str, Any]:
    """同池「每天都买」的无条件超额，作为所有策略数字的对照组。"""
    events = baseline_events(frames, index_df, horizon=horizon, cost_rate=cost_rate, start=start)
    return build_stat(AlwaysOnStrategy.name, events)


def net_alpha(
    rule_events: Events,
    baseline_events_: Events,
    *,
    min_symbol_samples: int = MIN_CLUSTER_SAMPLES,
) -> dict[str, Any]:
    """规则相对「同池每天都买」的净 alpha，按标的配对检验。

    为什么必须配对而不是拿两个均值相减：池子自身的漂移在 3 日口径是 -0.16%、
    10 日口径是 +0.25%（会变号），直接减点估计既没有误差，也无法判断"这点
    差距是不是噪声"。

    做法：对每只标的，先算该标的规则事件的均值，再减去同标的无条件均值，
    得到 39 个配对差值，最后对这些差值做 t 检验。配对单位是标的，
    信号重叠造成的伪独立被压到标的层面——和 `t_symbol` 同一套保守逻辑。

    返回的 ``pairs`` 是真正参与检验的标的数；规则过稀（单标的不足
    ``min_symbol_samples`` 笔）时该标的被剔除，``pairs`` 会明显小于标的池，
    这时 alpha 只能当线索。
    """
    diffs: list[float] = []
    for symbol, values in rule_events.per_symbol.items():
        base = baseline_events_.per_symbol.get(symbol)
        if not values or not base:
            continue
        if len(values) < min_symbol_samples or len(base) < min_symbol_samples:
            continue
        diffs.append(sum(values) / len(values) - sum(base) / len(base))
    if len(diffs) < T_MIN_SAMPLES:
        return {"pairs": len(diffs), "mean_alpha": None, "t_alpha": None, "positive_ratio": None}
    return {
        "pairs": len(diffs),
        "mean_alpha": sum(diffs) / len(diffs),
        "t_alpha": _nan_to_none(t_stat(diffs)),
        "positive_ratio": sum(1 for d in diffs if d > 0) / len(diffs),
    }


def render_markdown(stats: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        "# 策略同口径体检表",
        "",
        f"- 窗口起点：{meta.get('start', DEFAULT_START)}",
        f"- 往返成本：{meta.get('cost', DEFAULT_COST):.1%}",
        f"- 持有周期：{meta.get('horizon', DEFAULT_HORIZON)} 个交易日",
        f"- 基准：{meta.get('index', DEFAULT_INDEX)}",
        f"- 标的数：{meta.get('symbols', 0)}",
        "",
        "| 策略 | 密度 | 事件数 | 5日净超额 | 净alpha | 胜率 | t(按标的) | t(净alpha) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in stats:
        if not row["samples"]:
            lines.append(f"| {row['strategy']} | {row['density']:.1%} | 0 | — | — | — | — | — |")
            continue
        lines.append(
            f"| {row['strategy']} | {row['density']:.1%} | {row['samples']} "
            f"| {row['mean_excess']:+.2%} | {_fmt_alpha(row)} | {row['win_rate']:.0%} "
            f"| {_fmt_t(row['t_symbol'])} | {_fmt_t(row.get('t_alpha'))} |"
        )
    baseline = meta.get("baseline")
    if isinstance(baseline, dict) and baseline.get("samples"):
        lines.append(
            f"| （对照）{baseline['strategy']} | {baseline['density']:.0%} "
            f"| {baseline['samples']} | {baseline['mean_excess']:+.2%} | — "
            f"| {baseline['win_rate']:.0%} | {_fmt_t(baseline['t_symbol'])} | — |"
        )
        lines += [
            "",
            "> 对照行是「同池每天都买」的无条件超额，代表池子自身的漂移。"
            "策略行减去它才是策略贡献；不减去就把 beta 当成了 alpha。",
            "> 净alpha = 逐标的（策略事件均值 − 同标的无条件均值）后做配对检验，"
            "|t| < 2 表示扣掉池子漂移后与零无法区分。",
            "> 净alpha 逐标的等权、净超额逐事件加权，两者口径不同，不可相加。",
        ]
    lines += [
        "",
        "> 密度 = 发买入信号的交易日 / 全部交易日。密度越高说明条件越不筛选。",
        "> t(按标的) 是保守口径，判断 edge 是否真实优先看它。",
        "> 逐事件的 t 值（信号重叠时会虚高）只在 --json 里给，不进度量表。",
        "> 本表只做统计，不含任何调参建议——负超额的策略不是靠调参能救的。",
    ]
    return "\n".join(lines)


def _fmt_t(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _fmt_alpha(row: dict[str, Any]) -> str:
    value = row.get("mean_alpha")
    return "—" if value is None else f"{value:+.2%}"


def load_pool(pool_file: Path) -> list[str]:
    """从既有事件研究产物里取标的池，保证与历史结论同池可比。"""
    if not pool_file.exists():
        return []
    data = json.loads(pool_file.read_text(encoding="utf-8"))
    codes = {str(event.get("code", "")) for event in data.get("events", [])}
    return sorted(c for c in codes if c)


def load_frames(
    symbols: list[str], cache_dir: Path, days: int
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """取行情；优先用缓存，缓存缺失才联网。返回 (frames, 失败列表)。"""
    from gugu.analysis.event_study_data import fetch_stock_klines

    cache_dir.mkdir(parents=True, exist_ok=True)
    frames: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    for symbol in symbols:
        cached = resolve_cache_file(symbol, cache_dir)
        if cached is not None:
            try:
                frames[symbol] = pd.read_parquet(cached)
                continue
            except Exception:  # noqa: BLE001 — 缓存损坏则回退联网
                pass
        try:
            df = fetch_stock_klines(symbol, days)
        except Exception:  # noqa: BLE001
            failed.append(symbol)
            continue
        if df is None or df.empty:
            failed.append(symbol)
            continue
        frames[symbol] = df
        try:
            df.to_parquet(cache_dir / f"{symbol}.parquet")
        except Exception:  # noqa: BLE001 — 写缓存失败不影响本次统计
            pass
    return frames, failed


def resolve_cache_file(symbol: str, cache_dir: Path) -> Path | None:
    """在缓存目录里找这只标的最长的行情文件。

    兼容两种布局：本工具自己的 ``{symbol}.parquet``，以及仓库
    ``data/parquet_cache/`` 的 ``{symbol}_{天数}d[_复权].parquet``。
    同标的存在多档时取行数最多的——短档撑不起 5 日窗口加基准对齐。

    标的池里常写成 ``sh600519`` 这种带交易所前缀的形式，而缓存文件名是
    6 位纯数字，所以两种写法都要试，否则会静默回退到联网。
    """
    directory = Path(cache_dir)
    keys = [str(symbol).strip()]
    digits = str(symbol).strip()[-6:]
    if digits and digits not in keys:
        keys.append(digits)
    for key in keys:
        exact = directory / f"{key}.parquet"
        if exact.is_file():
            return exact
    best: Path | None = None
    best_rows = -1
    for key in keys:
        for path in sorted(directory.glob(f"{key}_*d*.parquet")):
            try:
                rows = len(pd.read_parquet(path))
            except Exception:  # noqa: BLE001 — 坏文件跳过，不影响其他候选
                continue
            if rows > best_rows:
                best, best_rows = path, rows
    return best


def resolve_index_file(index_code: str, cache_dir: Path) -> Path | None:
    """找基准缓存；``sh000300`` 先试 ``_index.parquet``，再试 ``000300_*d*``。"""
    directory = Path(cache_dir)
    cached = directory / "_index.parquet"
    if cached.is_file():
        return cached
    return resolve_cache_file(str(index_code).strip()[-6:], directory)


def load_index(index_code: str, cache_dir: Path, days: int) -> pd.DataFrame:
    from gugu.analysis.event_study_data import fetch_index_klines

    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = resolve_index_file(index_code, cache_dir)
    if cached is not None:
        try:
            return pd.read_parquet(cached)
        except Exception:  # noqa: BLE001
            pass
    df = fetch_index_klines(index_code, days)
    if df is not None and not df.empty:
        try:
            df.to_parquet(cache_dir / "_index.parquet")
        except Exception:  # noqa: BLE001
            pass
        return df
    return pd.DataFrame()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="策略同口径体检表")
    parser.add_argument("--strategies", nargs="*", default=list(DEFAULT_STRATEGIES))
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--cost", type=float, default=DEFAULT_COST, help="往返交易成本，默认 0.4%%")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument(
        "--pool-file", default=str(PROJECT_ROOT / "data" / "linjudaye_event_study_40.json")
    )
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    from gugu.strategies.registry import get_strategy

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
        print(
            f"全部 {len(symbols)} 只标的行情不可用（可能被数据源限流），未产生结论", file=sys.stderr
        )
        return 1

    base_events = baseline_events(
        frames, index_df, horizon=args.horizon, cost_rate=args.cost, start=args.start
    )

    stats: list[dict[str, Any]] = []
    for name in args.strategies:
        try:
            strategy = get_strategy(name)
        except Exception as exc:  # noqa: BLE001
            print(f"[跳过] {name} 实例化失败：{exc}", file=sys.stderr)
            continue
        events = collect_events(
            strategy,
            frames,
            index_df,
            horizon=args.horizon,
            cost_rate=args.cost,
            start=args.start,
        )
        stat = build_stat(name, events)
        stat.update(net_alpha(events, base_events))
        stats.append(stat)

    meta = {
        "start": args.start,
        "cost": args.cost,
        "horizon": args.horizon,
        "index": args.index,
        "symbols": len(frames),
        "failed": failed,
        "baseline": build_stat(AlwaysOnStrategy.name, base_events),
    }
    if args.json:
        print(json.dumps({"meta": meta, "stats": stats}, ensure_ascii=False, indent=1))
    else:
        print(render_markdown(stats, meta))
        if failed:
            print(f"\n未取到行情（已跳过）：{', '.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
