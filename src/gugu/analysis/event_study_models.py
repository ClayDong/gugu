"""统一事件研究的数据模型和配置。"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeAlias

import pandas as pd

DEFAULT_HORIZONS = (3, 5, 10)
DEFAULT_TRAIN_PERIOD = ("2024-02-01", "2025-01-31")
DEFAULT_VAL_PERIOD = ("2025-02-01", "2026-07-31")
DEFAULT_CONCENTRATION_THRESHOLD = 0.30
DEFAULT_MIN_SAMPLES = 3
DEFAULT_COST_RATE = 0.004
DEFAULT_INDEX_CODE = "000300"
DEFAULT_DATA_DAYS = 600
DEFAULT_MIN_HISTORY_ROWS = 100

EnrichFunc: TypeAlias = Callable[
    [pd.DataFrame, str], tuple[pd.DataFrame, dict[str, Any]] | pd.DataFrame
]


@dataclass
class StudyConfig:
    """事件研究配置。"""

    strategy_name: str
    horizons: list[int] = field(default_factory=lambda: list(DEFAULT_HORIZONS))
    index_code: str = DEFAULT_INDEX_CODE
    train_start: str = DEFAULT_TRAIN_PERIOD[0]
    train_end: str = DEFAULT_TRAIN_PERIOD[1]
    val_start: str = DEFAULT_VAL_PERIOD[0]
    val_end: str = DEFAULT_VAL_PERIOD[1]
    concentration_threshold: float = DEFAULT_CONCENTRATION_THRESHOLD
    min_samples: int = DEFAULT_MIN_SAMPLES
    cost_rate: float = DEFAULT_COST_RATE
    entry_mode: str = "next_open"
    output_path: str | None = None
    stock_pool: list[str] | None = None
    enrich_func: EnrichFunc | None = None
    data_days: int = DEFAULT_DATA_DAYS
    min_history_rows: int = DEFAULT_MIN_HISTORY_ROWS

    def __post_init__(self) -> None:
        """拒绝会导致研究口径不明确的配置。"""
        try:
            self.horizons = sorted({int(h) for h in self.horizons if int(h) > 0})
            self.cost_rate = float(self.cost_rate)
            self.concentration_threshold = float(self.concentration_threshold)
            self.min_samples = int(self.min_samples)
            self.data_days = int(self.data_days)
            self.min_history_rows = int(self.min_history_rows)
        except (TypeError, ValueError) as exc:
            raise ValueError("事件研究配置包含无法解析的数值") from exc
        if not self.horizons:
            raise ValueError("horizons 必须包含至少一个正整数")
        if self.entry_mode not in {"close", "next_open"}:
            raise ValueError("entry_mode 必须是 close 或 next_open")
        if not math.isfinite(self.cost_rate) or not 0 <= self.cost_rate < 1:
            raise ValueError("cost_rate 必须在 [0, 1) 范围内")
        if not 0 < self.concentration_threshold <= 1:
            raise ValueError("concentration_threshold 必须在 (0, 1] 范围内")
        if self.min_samples < 3:
            raise ValueError("min_samples 不能小于 3")
        if self.data_days <= 0:
            raise ValueError("data_days 必须为正整数")
        if self.min_history_rows < 0:
            raise ValueError("min_history_rows 不能为负数")
        try:
            train_start = pd.Timestamp(self.train_start)
            train_end = pd.Timestamp(self.train_end)
            val_start = pd.Timestamp(self.val_start)
            val_end = pd.Timestamp(self.val_end)
        except (TypeError, ValueError) as exc:
            raise ValueError("事件研究训练/验证日期无效") from exc
        if train_start > train_end:
            raise ValueError("训练期起止日期无效")
        if val_start > val_end:
            raise ValueError("验证期起止日期无效")
        if val_start <= train_end:
            raise ValueError("训练期与验证期不能重叠")


@dataclass
class StudyEvent:
    """一次事件研究结果。"""

    symbol: str
    date: str
    entry: float
    period: str
    year: int
    entry_date: str | None = None
    entry_source: str = "signal_close"
    returns: dict[str, float] = field(default_factory=dict)
    excess: dict[str, float] = field(default_factory=dict)


@dataclass
class HorizonResult:
    """单个时间维度的统计结果。"""

    horizon: int
    n_events: int
    avg_return: float
    avg_excess: float
    std_excess: float
    t_stat: float
    p_value: float
    win_rate: float
    sharpe: float
    excess_sharpe: float
    significant: bool
    excess_win_rate: float = 0.0
    std_return: float = 0.0
    n_returns: int = 0
    n_excess: int = 0


@dataclass
class StudyResult:
    """事件研究的完整结果。"""

    strategy_name: str
    config: StudyConfig
    n_events: int
    n_symbols: int
    concentration_issues: list[dict[str, Any]]
    per_horizon: list[HorizonResult]
    per_year: dict[int, list[HorizonResult]]
    per_period: dict[str, list[HorizonResult]]
    events: list[StudyEvent]
    benchmark_available: bool = False
