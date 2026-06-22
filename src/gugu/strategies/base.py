"""策略抽象基类。

所有策略继承此类，实现 generate_signals 方法。
统一输入：DataFrame[date, open, high, low, close, volume, amount]
统一输出：在 df 上加 signal 列（1=买入, -1=卖出, 0=持有）和 confidence 列（0-1）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from gugu.config import strategy_defaults
from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class StrategyConfig:
    """策略配置。"""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


class Strategy(ABC):
    """策略抽象基类。"""

    name: str = "base"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        # 合并默认参数 + 传入参数
        defaults = strategy_defaults().get(self.name, {})
        self.params = {**defaults, **(params or {})}
        self.validate_params()

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成交易信号。

        Args:
            df: 行情数据，含 date, open, high, low, close, volume, amount

        Returns:
            df 增加 signal 列（1=买, -1=卖, 0=持有）和 confidence 列（0-1）
        """
        ...

    def validate_params(self) -> None:
        """校验参数（子类可覆盖）。默认不做校验。"""
        return

    def _ensure_columns(self, df: pd.DataFrame) -> None:
        """确保 DataFrame 含必需列。"""
        required = ["date", "close", "high", "low", "volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"{self.name} 缺失列: {missing}")

    @staticmethod
    def _atr(df: pd.DataFrame, window: int) -> pd.Series:
        """计算 ATR（平均真实波幅）。"""
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr = pd.concat(
            [
                high - low,
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.rolling(window=window, min_periods=1).mean()
