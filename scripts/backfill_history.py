#!/usr/bin/env python3
"""补长历史行情缓存：让参数稳定性这类指标有数据可算。

为什么需要它（2026-11-05 实测）：
`data/parquet_cache/` 共 350 个文件，行数中位数 90、最大 200，
**没有一个是 ≥250 行**；而接口同一天可以稳定拉到 801 行。
后果是硬性的：
- `scripts/walk_forward_offline.py` 要求每只股票 ≥200 行才能切 3 窗口，
  实际只能勉强凑到下限（注释写明「2026-09-10 从 500 降为 200，适配现有缓存深度」）；
- 参数稳定性（gate 阈值 ≥80%）需要多窗口一致才估得出来，样本不够就只能记 0.0，
  而 0.0 会让策略永远卡在 gate 外——不是策略差，是数据不够。

本脚本只做一件事：把拉到的长历史写成 ``{symbol}_{行数}d_none.parquet``，
用真实行数命名，配合 `data_gate.validate_stock_history(min_rows=天数)` 的
「短样本冒充长历史」检查。

三条自我约束：
1. **不缩短**：已存在更长的同标的数据时跳过，绝不覆盖成更短；
2. **不伪造**：拉取失败、校验不过就如实记为失败，不写空文件不写占位；
3. **不留当日半根**：默认剔除运行日当天的 bar，避免把盘中未完成数据带进回测。

用法：
    python scripts/backfill_history.py --dry-run          # 只看计划
    python scripts/backfill_history.py                   # 补 39 只默认池
    python scripts/backfill_history.py --symbols 600519 000858 --days 800
退出码：0 = 至少写入一只；1 = 全部失败（如实报告，不伪造成功）
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

DEFAULT_CACHE = PROJECT_ROOT / "data" / "parquet_cache"
DEFAULT_POOL_FILE = PROJECT_ROOT / "data" / "linjudaye_event_study_40.json"
DEFAULT_DAYS = 800
DEFAULT_INDEX = "sh000300"
REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")


@dataclass
class Plan:
    symbol: str
    action: str  # write / skip_existing / no_data / invalid
    rows: int = 0
    target: Path | None = None
    reason: str = ""


def cache_filename(symbol: str, rows: int) -> str:
    """用真实行数命名，保证 ``min_rows=天数`` 的检查不会因四舍五入误杀。"""
    return f"{str(symbol).strip().zfill(6)}_{int(rows)}d_none.parquet"


def existing_max_rows(symbol: str, cache_dir: Path) -> int:
    """该标的在缓存目录里已有的最大行数；没有则 0。"""
    best = 0
    for path in Path(cache_dir).glob("*.parquet"):
        name = path.name
        if not name.startswith(str(symbol).strip().zfill(6) + "_"):
            continue
        try:
            best = max(best, len(pd.read_parquet(path)))
        except Exception:  # noqa: BLE001 — 坏文件不参与比较
            continue
    return best


def sanitize_history(df: pd.DataFrame, *, drop_date: str | None = None) -> pd.DataFrame | None:
    """清洗成可回测的历史：去重、排序、剔除非法价、可剔除指定日。

    返回 None 表示数据不可用——不用空表冒充。
    """
    if df is None or df.empty:
        return None
    if any(col not in df.columns for col in REQUIRED_COLUMNS):
        return None
    clean = df.loc[:, list(REQUIRED_COLUMNS)].copy()
    clean["date"] = pd.to_datetime(clean["date"], errors="coerce")
    clean = clean.dropna(subset=["date"])
    for col in ("open", "high", "low", "close", "volume"):
        clean[col] = pd.to_numeric(clean[col], errors="coerce")
    clean = clean.dropna(subset=["open", "high", "low", "close"])
    # 价格必须为正，成交量不能为负；非法行直接丢弃而不是填 0
    price_ok = (clean[["open", "high", "low", "close"]] > 0).all(axis=1)
    clean = clean[price_ok & (clean["volume"] >= 0)]
    if drop_date:
        clean = clean[clean["date"] != pd.Timestamp(drop_date)]
    clean = clean.drop_duplicates(subset=["date"], keep="last")
    clean = clean.sort_values("date").reset_index(drop=True)
    if clean.empty:
        return None
    return clean


def plan_symbol(
    symbol: str,
    fetched: pd.DataFrame | None,
    cache_dir: Path,
    *,
    drop_date: str | None,
) -> Plan:
    """决定这只标的写不写、写到哪。纯逻辑，便于测试。"""
    clean = sanitize_history(fetched, drop_date=drop_date)
    if clean is None:
        return Plan(symbol=symbol, action="no_data", reason="拉取为空或缺少必需列")
    rows = len(clean)
    existing = existing_max_rows(symbol, cache_dir)
    if existing >= rows:
        return Plan(
            symbol=symbol,
            action="skip_existing",
            rows=rows,
            reason=f"已有 {existing} 行 >= 本次 {rows} 行，不缩短",
        )
    return Plan(
        symbol=symbol,
        action="write",
        rows=rows,
        target=Path(cache_dir) / cache_filename(symbol, rows),
    )


def load_pool(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    codes = {str(e.get("code", "")) for e in data.get("events", [])}
    # 既有产物里的代码带 sh/sz 前缀，写入缓存要用 6 位纯数字
    return sorted(c[-6:] for c in codes if len(c) >= 6)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="补长历史行情缓存")
    parser.add_argument("--symbols", nargs="*", default=None, help="6 位代码，默认取内置池")
    parser.add_argument("--pool-file", default=str(DEFAULT_POOL_FILE))
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument(
        "--index",
        default=DEFAULT_INDEX,
        help="同时补基准指数（超额收益必需）；传空字符串跳过",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    parser.add_argument(
        "--keep-today",
        action="store_true",
        help="保留运行日当天的 bar（默认剔除，防止把盘中半根带进回测）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    from gugu.analysis.event_study_data import fetch_index_klines, fetch_stock_klines

    symbols = [s.strip() for s in (args.symbols or load_pool(Path(args.pool_file))) if s.strip()]
    tasks: list[tuple[str, Any]] = [
        (s, lambda s=s: fetch_stock_klines(s, args.days)) for s in symbols
    ]
    # 基准指数必须一起补：超额收益全靠它。指数缓存若只有 200 行，
    # 800 行窗口根本算不出基准，结论会静默变成"n/a"。
    if args.index:
        tasks.append(
            (str(args.index).strip()[-6:], lambda: fetch_index_klines(args.index, args.days))
        )
    if not tasks:
        print(f"没有可用标的：--symbols 为空且 {args.pool_file} 取不到池", file=sys.stderr)
        return 1

    cache_dir = Path(args.cache_dir)
    if not args.dry_run:
        cache_dir.mkdir(parents=True, exist_ok=True)
    drop_date = None if args.keep_today else str(date.today())

    plans: list[Plan] = []
    for symbol, fetch in tasks:
        try:
            fetched = fetch()
        except Exception as exc:  # noqa: BLE001 — 单只失败不中断整批
            plans.append(
                Plan(symbol=symbol, action="no_data", reason=f"拉取异常 {type(exc).__name__}")
            )
            continue
        plan = plan_symbol(symbol, fetched, cache_dir, drop_date=drop_date)
        plans.append(plan)
        if plan.action == "write" and not args.dry_run:
            cleaned = sanitize_history(fetched, drop_date=drop_date)
            assert cleaned is not None  # plan 已保证
            plan.target.parent.mkdir(parents=True, exist_ok=True)
            cleaned.to_parquet(plan.target)

    written = [p for p in plans if p.action == "write"]
    skipped = [p for p in plans if p.action == "skip_existing"]
    failed = [p for p in plans if p.action == "no_data"]

    print(f"标的 {len(tasks)} 只 | 写入 {len(written)} | 跳过 {len(skipped)} | 失败 {len(failed)}")
    if written:
        rows = [p.rows for p in written]
        print(
            f"  写入行数 min/median/max = {min(rows)} / {sorted(rows)[len(rows)//2]} / {max(rows)}"
        )
        for p in written[:5]:
            print(f"  + {p.target.name}")
        if len(written) > 5:
            print(f"  ... 其余 {len(written) - 5} 个")
    for p in skipped:
        print(f"  = {p.symbol}: {p.reason}")
    for p in failed:
        print(f"  ! {p.symbol}: {p.reason}")

    if args.dry_run:
        print("\n--dry-run：未写入任何文件")
        return 0
    if not written and not skipped:
        print("全部标的都拿不到数据（可能被数据源限流）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
