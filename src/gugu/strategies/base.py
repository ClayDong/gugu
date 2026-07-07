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
    """策略抽象基类。

    子类必须实现 `_generate_signals_impl` 方法。
    """

    name: str = "base"

    @abstractmethod
    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """子类实现：生成交易信号的核心逻辑。"""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        # 合并默认参数 + 传入参数
        defaults = strategy_defaults().get(self.name, {})
        self.params = {**defaults, **(params or {})}
        self.validate_params()

    def generate_signals(self, df: pd.DataFrame, is_backtest: bool = False) -> pd.DataFrame:
        """生成交易信号（模板方法）。

        子类应实现 _generate_signals_impl 而非重写 generate_signals。
        基类自动对生成的信号执行确认机制。

        Args:
            df: 行情数据，含 date, open, high, low, close, volume, amount
            is_backtest: 是否在回测模式（默认 False）。
                          回测模式下跳过确认（回测引擎已通过 shift(1) 防前瞻）。
                          见 _require_confirmation 的 is_backtest 参数说明。

        Returns:
            df 增加 signal 列（1=买, -1=卖, 0=持有）和 confidence 列（0-1）
        """
        df = self._generate_signals_impl(df)
        return self._require_confirmation(df, is_backtest=is_backtest)

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """子类实现具体的信号生成逻辑。

        如果子类覆盖了 generate_signals（不走模板方法），可以不实现此方法。
        """
        raise NotImplementedError(f"{self.__class__.__name__} 必须实现 _generate_signals_impl")

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

    def _require_confirmation(
        self, df: pd.DataFrame, signal_col: str = "signal", confirm_days: int | None = None,
        is_backtest: bool = False,
    ) -> pd.DataFrame:
        """信号确认机制：检查后续 K 线是否维持信号方向。

        对于 signal 列中的非零值，检查后续 confirm_days 根 K 线收盘价
        是否反向突破（买入信号被后续阴线吞没，或卖出信号被后续阳线吞没）。
        未确认的信号设为 0。

        confirm_days 默认从 self.params["confirm_days"] 读取，未配置时为 0（不确认）。
        可通过策略配置参数单独控制每个策略是否需要确认。

        回测模式（is_backtest=True）：跳过确认，因为回测引擎已通过 shift(1) 保证信号不超前。
        实盘模式下使用未来 K 线做自然确认。

        Args:
            df: 含 signal 列的 DataFrame
            signal_col: 信号列名，默认 "signal"
            confirm_days: 确认所需后续 K 线根数，None 时从 self.params 读取
            is_backtest: 是否在回测模式（默认 False）

        Returns:
            确认后的 DataFrame（直接修改原 df）
        """
        # 默认从策略参数读取 confirm_days，未配置时为 0（不确认）
        if confirm_days is None:
            confirm_days = int(self.params.get("confirm_days", 0))
        if confirm_days <= 0:
            return df

        if is_backtest:
            return df

        df = df.copy()
        close = df["close"]
        signals = df[signal_col]

        for i in range(len(df)):
            sig = signals.iloc[i]
            if sig == 0:
                continue

            end_idx = min(i + confirm_days + 1, len(df))
            # 不足 confirm_days 根后续 K 线，无法确认
            if end_idx - i - 1 < confirm_days:
                df.loc[df.index[i], signal_col] = 0
                continue

            subsequent_close = close.iloc[i + 1 : end_idx]

            if sig == 1:  # 买入信号：后续不能有阴线吞没
                # 买入参考价为信号当天的收盘价
                entry_price = close.iloc[i]
                # 如果后续任一根 K 线收盘价低于信号当天收盘价（阴线吞没），信号失效
                if (subsequent_close < entry_price).any():
                    df.loc[df.index[i], signal_col] = 0

            elif sig == -1:  # 卖出信号：后续不能有阳线吞没
                exit_price = close.iloc[i]
                # 如果后续任一根 K 线收盘价高于信号当天收盘价（阳线吞没），信号失效
                if (subsequent_close > exit_price).any():
                    df.loc[df.index[i], signal_col] = 0

        return df
