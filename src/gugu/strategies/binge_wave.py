"""斌哥笨战法三步吃波段。

来源：斌哥Bg视频（2026-09-07）转写文档。

三步：
1. 筛选涨停股：近10日有过涨停，排除三连板以上
2. 周线定大趋势：周线MACD金叉
3. 日线找精准买点：回调到EMA20附近 + 放量阳线

卖出：跌破EMA20直接离场
止盈：涨30%减仓1/3，涨50%再减一半（回测中用移动止损近似）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["BingeWaveStrategy"]


class BingeWaveStrategy(Strategy):
    """斌哥笨战法三步吃波段。"""

    name = "binge_wave"
    category = "trend"

    def validate_params(self) -> None:
        if int(self.params.get("ema_period", 20)) <= 0:
            raise ValueError("ema_period 必须 > 0")
        if int(self.params.get("lookback_days", 10)) <= 0:
            raise ValueError("lookback_days 必须 > 0")
        if float(self.params.get("vol_ratio_min", 1.5)) < 1.0:
            raise ValueError("vol_ratio_min 必须 >= 1.0")
        if float(self.params.get("trailing_stop_pct", 0.08)) <= 0:
            raise ValueError("trailing_stop_pct 必须 > 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        ema_period = int(self.params.get("ema_period", 20))
        lookback = int(self.params.get("lookback_days", 10))
        vol_ratio_min = float(self.params.get("vol_ratio_min", 1.5))
        pullback_tolerance = float(self.params.get("pullback_tolerance", 0.03))
        trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.08))
        max_consecutive_boards = int(self.params.get("max_consecutive_boards", 3))

        df["ema20"] = df["close"].ewm(span=ema_period, adjust=False).mean()
        df["daily_ret"] = df["close"].pct_change()
        df["vol_ma20"] = df["volume"].rolling(ema_period).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma20"].replace(0, np.nan)
        df["is_yang"] = df["close"] > df["open"]

        # 涨停近似
        is_limit_up = df["daily_ret"] > 0.095
        has_limit_up = is_limit_up.rolling(lookback, min_periods=1).max().astype(bool)
        consecutive_boards = is_limit_up.rolling(max_consecutive_boards).sum()
        too_many_boards = consecutive_boards >= max_consecutive_boards

        # MACD（日线近似周线趋势）
        macd_fast = int(self.params.get("macd_fast", 12))
        macd_slow = int(self.params.get("macd_slow", 26))
        macd_signal = int(self.params.get("macd_signal", 9))
        ema_fast = df["close"].ewm(span=macd_fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=macd_slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=macd_signal, adjust=False).mean()
        macd_golden = (dif > dea) & (dif.shift(1) <= dea.shift(1))

        # 买点：回调到EMA20附近 + 放量阳线
        near_ema = (df["close"] - df["ema20"]).abs() / df["ema20"].replace(
            0, np.nan
        ) <= pullback_tolerance
        above_ema = df["close"] > df["ema20"]
        vol_confirmed = df["vol_ratio"] >= vol_ratio_min

        buy_cond = (
            has_limit_up
            & ~too_many_boards
            & macd_golden
            & near_ema
            & above_ema
            & df["is_yang"]
            & vol_confirmed
        ).fillna(False)

        # 持仓感知卖出
        sell_raw = df["close"] < df["ema20"]
        buy_arr = buy_cond.to_numpy(dtype=bool)
        sell_arr = sell_raw.fillna(False).to_numpy(dtype=bool)
        in_position = False
        valid_sell = np.zeros(len(df), dtype=bool)
        for i in range(len(df)):
            if buy_arr[i]:
                in_position = True
            elif sell_arr[i] and in_position:
                valid_sell[i] = True
                in_position = False

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[pd.Series(valid_sell, index=df.index), "signal"] = -1

        df["confidence"] = 0.0
        df.loc[buy_cond, "confidence"] = 0.65

        if trailing_stop_pct > 0:
            df = self._apply_trailing_stop_exit(df, trailing_stop_pct)

        buy_count = int((df["signal"] == 1).sum())
        sell_count = int((df["signal"] == -1).sum())
        logger.info(
            "binge_wave 信号统计: {} 买入, {} 卖出",
            buy_count,
            sell_count,
        )
        return df
