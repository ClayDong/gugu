#!/usr/bin/env python3
"""通用策略事件研究兼容入口。

研究逻辑已收敛到 ``gugu.analysis.event_study.EventStudy``。保留本文件及
``fetch_kline``/``fetch_index``/``welch_t`` 等旧函数，避免历史脚本导入断裂。

推荐用法::

    python scripts/event_study.py --strategy box_breakout --from-strategy

旧用法仍可用::

    python scripts/generic_event_study.py --strategy box_breakout --days 600
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.analysis.event_study import (  # noqa: E402
    DEFAULT_DATA_DAYS,
    EventStudy,
    StudyConfig,
    fetch_index_klines,
    fetch_stock_klines,
    welch_t as _welch_t,
)

DEFAULT_POOL = [
    "002472",
    "300033",
    "002230",
    "603501",
    "300308",
    "300502",
    "600519",
    "300750",
    "002594",
    "300059",
    "002475",
    "600050",
    "000977",
    "688041",
    "002371",
]
TRAIN_START, TRAIN_END = "2024-02-01", "2025-01-31"
VAL_START, VAL_END = "2025-02-01", "2026-07-31"


def welch_t(a: np.ndarray, b: np.ndarray | None = None) -> tuple[float, float]:
    """保留旧签名；统一框架执行单样本超额收益检验。"""
    del b
    return _welch_t(a)


def fetch_kline(symbol: str, days: int) -> pd.DataFrame:
    """旧函数兼容别名，返回标准 OHLCV DataFrame。"""
    return fetch_stock_klines(symbol, days)


def fetch_index(symbol: str = "sh000300", days: int = 700) -> pd.Series:
    """旧函数兼容别名，返回以日期为索引的收盘价序列。"""
    code = symbol.lower().removeprefix("sh").removeprefix("sz").removeprefix("bj")
    df = fetch_index_klines(code, days)
    if df.empty:
        return pd.Series(dtype=float, name="close")
    return df.set_index("date")["close"]


def fetch_quote_fundamentals(symbol: str) -> dict[str, float | None]:
    """旧基本面函数兼容占位。

    统一框架不在事件研究时混入实时基本面，特殊策略应通过
    ``StudyConfig.enrich_func`` 显式提供可复现的历史列。
    """
    del symbol
    return {}


def enrich_for_strategy(
    df: pd.DataFrame, symbol: str, strategy_key: str
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """兼容旧接口；仅使用行情内可复现字段构造特殊列。"""
    out = df.copy()
    code = str(symbol).strip().lower().removeprefix("sh").removeprefix("sz").removeprefix("bj")
    if strategy_key == "price_volume_signal" and "amount" not in out.columns:
        # amount 不存在时按成交量单位约定估算；不混入未来时点的实时市值/换手。
        factor = 1 if code.startswith(("688", "689")) else 100
        out["amount"] = out["volume"] * out["close"] * factor
    return out, {}


def main() -> None:
    parser = argparse.ArgumentParser(description="通用策略事件研究（统一框架兼容入口）")
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DATA_DAYS)
    parser.add_argument("--pool", nargs="*", default=DEFAULT_POOL)
    parser.add_argument("--json", default=None)
    parser.add_argument("--cost", type=float, default=0.004)
    parser.add_argument("--entry-mode", choices=["close", "next_open"], default="next_open")
    parser.add_argument("--need-fundamentals", action="store_true", help="兼容旧参数")
    parser.add_argument("--from-history", action="store_true")
    args = parser.parse_args()

    config = StudyConfig(
        strategy_name=args.strategy,
        stock_pool=args.pool,
        horizons=[3, 5, 10],
        train_start=TRAIN_START,
        train_end=TRAIN_END,
        val_start=VAL_START,
        val_end=VAL_END,
        cost_rate=args.cost,
        entry_mode=args.entry_mode,
        output_path=args.json or f"data/{args.strategy}_event_study.json",
        data_days=args.days,
        enrich_func=lambda frame, symbol: enrich_for_strategy(frame, symbol, args.strategy),
    )
    study = EventStudy(config)
    if args.from_history:
        result = study.run_from_history("data/signals_history.jsonl", max_days=args.days)
    else:
        from gugu.strategies.registry import get_strategy

        result = study.run_from_strategy(get_strategy(args.strategy), days=args.days)
    study.print_summary(result)


if __name__ == "__main__":
    main()
