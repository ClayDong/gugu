#!/usr/bin/env python3
"""规则级归因：把「多规则 OR」策略拆开，看每条规则各自有没有 edge。

为什么需要它（2026-11-05 实证）：
`linjudaye` 整体密度 27.6%、5 日净超额 -0.10%，看上去就是"这策略不行"。
拆开之后才看见结构问题：`R02_黄金分割` 单独就有 22.9% 密度，
**整个策略基本等于 R02 加四条噪声**；而 `R03_缩量回踩` 是唯一正号。
不拆开，就没法把"整体不行"变成"哪一条值得研究"。

设计原则（关键）：
条件来自策略自己的 `rule_masks()`，本脚本不复制任何规则逻辑。
复制一份规则必然产生漂移——工具算的和线上跑的不是同一套条件，
那这种归因就只是另一种自欺。

口径与 `scripts/strategy_scoreboard.py` 完全共用（次日开盘入场 / 持有 N 日 /
扣往返成本 / 相对基准超额 / 同一标的池），两表可直接对比。

用法：
    python scripts/rule_attribution.py                       # linjudaye 默认
    python scripts/rule_attribution.py --strategies linjudaye --horizon 10
    python scripts/rule_attribution.py --json
退出码：0 = 正常；1 = 无标的/基准不可用或无策略支持分解
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from strategy_scoreboard import (  # noqa: E402
    DEFAULT_COST,
    DEFAULT_HORIZON,
    DEFAULT_INDEX,
    DEFAULT_START,
    AlwaysOnStrategy,
    baseline_events,
    build_stat,
    collect_events,
    load_frames,
    load_index,
    load_pool,
    net_alpha,
)

DEFAULT_STRATEGIES = ("linjudaye",)
DEFAULT_CACHE = PROJECT_ROOT / "data" / "parquet_cache"
DEFAULT_POOL_FILE = PROJECT_ROOT / "data" / "linjudaye_event_study_40.json"


class RuleSubset:
    """把策略的某几条规则当成一个独立策略喂给统计层。

    通过调用 ``strategy.rule_masks()`` 取条件，保证与线上信号同源。
    ``rules=None`` 表示全部规则 OR（即策略现状）。
    """

    def __init__(self, strategy: Any, rules: tuple[str, ...] | None = None) -> None:
        self._strategy = strategy
        self._rules = rules

    def generate_signals(self, df: pd.DataFrame, is_backtest: bool = False) -> pd.DataFrame:
        out = df.copy()
        masks = self._strategy.rule_masks(out)
        names = list(masks) if self._rules is None else list(self._rules)
        combined = np.zeros(len(out), dtype=bool)
        for name in names:
            combined |= masks[name].fillna(False).to_numpy()
        out["signal"] = np.where(combined, 1, 0)
        return out


def rule_names(strategy: Any, sample: pd.DataFrame) -> list[str]:
    """取规则的规范顺序（与策略里的声明顺序一致）。"""
    return list(strategy.rule_masks(sample))


def attribute(
    strategy: Any,
    frames: dict[str, pd.DataFrame],
    index_df: pd.DataFrame,
    *,
    horizon: int,
    cost_rate: float,
    start: str,
    base_events: Any = None,
) -> list[dict[str, Any]]:
    """返回 [全集] + 每条规则 的统计行。

    传了 ``base_events``（同池无条件事件）时，每行额外带 ``mean_alpha`` /
    ``t_alpha`` / ``pairs``——即扣掉池子漂移、按标的配对检验之后还剩多少。
    """
    sample = next(iter(frames.values()))
    subsets = [("全集 OR（现状）", None)]
    subsets += [(name, (name,)) for name in rule_names(strategy, sample)]

    rows: list[dict[str, Any]] = []
    for label, rules in subsets:
        events = collect_events(
            RuleSubset(strategy, rules),
            frames,
            index_df,
            horizon=horizon,
            cost_rate=cost_rate,
            start=start,
        )
        row = build_stat(label, events)
        if base_events is not None:
            row.update(net_alpha(events, base_events))
        rows.append(row)
    return rows


def render_markdown(rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        "# 规则级归因",
        "",
        f"- 策略：{meta['strategy']}",
        f"- 窗口起点：{meta['start']}",
        f"- 往返成本：{meta['cost']:.1%}",
        f"- 持有周期：{meta['horizon']} 个交易日",
        f"- 基准：{meta['index']}",
        f"- 标的数：{meta['symbols']}",
        "",
        "| 规则 | 密度 | 事件数 | 净超额 | 净alpha | 胜率 | t(按标的) | t(净alpha) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        if not row["samples"]:
            lines.append(f"| {row['strategy']} | {row['density']:.1%} | 0 | — | — | — | — | — |")
            continue
        t_sym = "—" if row["t_symbol"] is None else f"{row['t_symbol']:+.2f}"
        alpha = row.get("mean_alpha")
        alpha_txt = "—" if alpha is None else f"{alpha:+.2%}"
        t_alpha = "—" if row.get("t_alpha") is None else f"{row['t_alpha']:+.2f}"
        lines.append(
            f"| {row['strategy']} | {row['density']:.1%} | {row['samples']} "
            f"| {row['mean_excess']:+.2%} | {alpha_txt} | {row['win_rate']:.0%} "
            f"| {t_sym} | {t_alpha} |"
        )
    baseline = meta.get("baseline")
    if isinstance(baseline, dict) and baseline.get("samples"):
        t_sym = "—" if baseline["t_symbol"] is None else f"{baseline['t_symbol']:+.2f}"
        lines.append(
            f"| （对照）{baseline['strategy']} | {baseline['density']:.0%} "
            f"| {baseline['samples']} | {baseline['mean_excess']:+.2%} | — "
            f"| {baseline['win_rate']:.0%} | {t_sym} | — |"
        )
        lines += [
            "",
            "> 对照行是「同池每天都买」的无条件超额，代表池子自身的漂移。"
            "规则行减去它才是规则贡献。",
            "> 净alpha = 逐标的（规则事件均值 − 同标的无条件均值）后做配对检验。"
            "这一步必须做：池子漂移在 3 日口径为负、10 日口径为正，会变号。",
            "> 净alpha 逐标的等权、净超额逐事件加权，两者口径不同，不可相加。",
        ]
    lines += [
        "",
        "> 密度 = 该规则命中且可评估的交易日 / 全部交易日。",
        "> 单条规则密度接近全集密度时，说明其余规则几乎不贡献信号。",
        "> t(按标的) 为保守口径；|t| < 2 只能当线索，不能当结论。",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多规则策略的规则级归因")
    parser.add_argument("--strategies", nargs="*", default=list(DEFAULT_STRATEGIES))
    parser.add_argument("--pool-file", default=str(DEFAULT_POOL_FILE))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--days", type=int, default=800, help="缓存缺失时联网回看天数")
    parser.add_argument("--cost", type=float, default=DEFAULT_COST)
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--json", action="store_true")
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
        print(f"全部 {len(symbols)} 只标的行情不可用，未产生结论", file=sys.stderr)
        return 1

    payload: list[dict[str, Any]] = []
    supported = 0
    base_events = baseline_events(
        frames, index_df, horizon=args.horizon, cost_rate=args.cost, start=args.start
    )
    for name in args.strategies:
        try:
            strategy = get_strategy(name)
        except Exception as exc:  # noqa: BLE001
            print(f"[跳过] {name} 实例化失败：{exc}", file=sys.stderr)
            continue
        if not hasattr(strategy, "rule_masks"):
            # 不猜、不复制规则：没实现分解就如实说没有
            print(f"[跳过] {name} 未实现 rule_masks()，无法做规则级归因", file=sys.stderr)
            continue
        supported += 1
        rows = attribute(
            strategy,
            frames,
            index_df,
            horizon=args.horizon,
            cost_rate=args.cost,
            start=args.start,
            base_events=base_events,
        )
        meta = {
            "strategy": name,
            "start": args.start,
            "cost": args.cost,
            "horizon": args.horizon,
            "index": args.index,
            "symbols": len(frames),
            "failed": failed,
            "baseline": build_stat(AlwaysOnStrategy.name, base_events),
        }
        if args.json:
            payload.append({"meta": meta, "rules": rows})
        else:
            print(render_markdown(rows, meta))
            if failed:
                print(f"\n未取到行情（已跳过）：{', '.join(failed)}")
            print()

    if supported == 0:
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
