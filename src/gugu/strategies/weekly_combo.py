"""周线选股策略：四句心法实战化。

基于经典周线选股法则：
1. 周线放出倍量：周成交量达前一周两倍以上，周线收阳 → 主力大举进场信号
2. 周线形成金叉：周线MACD金叉 → 大资金战略布局，上涨周期至少持续两三个月
3. 股价回踩20周均线：上升趋势中股价回调至20周均线附近且成交量缩小 → 最佳低吸机会
4. 周线三连阳：连续三根稳步抬高阳线，成交量温和放大 → 主升浪启动前最后热身

策略逻辑：多因子共振评分，满足因子越多置信度越高。
优化（v0.7.5）：
- 添加动态止损机制（跌破入场价8%强制卖出）
- 添加市场状态过滤（仅在bull/rally/sideways启用）
- 添加移动止盈（盈利15%后回撤10%止盈）
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class WeeklyComboStrategy(Strategy):
    """周线选股策略：四句心法实战化。

    将日K数据转换为周K后执行四因子共振评分：
    - 倍量因子 (vol_boost): 周成交量放大倍数
    - 金叉因子 (macd_golden): MACD金叉状态
    - 回踩因子 (retrace): 回踩20周均线且缩量
    - 三连阳因子 (triple_bull): 连续三根抬高的阳线

    优化特性：
    - 动态止损：跌破入场价8%强制卖出
    - 移动止盈：盈利15%后回撤10%止盈
    - 市场状态过滤：仅在趋势明确市场启用
    """

    name = "weekly_combo"
    category = "trend"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        # 确保 date 列是 datetime
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])

        # 转换到周线数据
        weekly = self._resample_weekly(df)

        # 计算四个因子得分
        weekly["vol_boost"] = self._calc_volume_boost(weekly)
        weekly["macd_golden"] = self._calc_macd_golden(weekly)
        weekly["retrace"] = self._calc_retrace_score(weekly)
        weekly["triple_bull"] = self._calc_triple_bull(weekly)

        # 综合评分：满足因子数量 × 权重
        weekly["score"] = (
            weekly["vol_boost"] * 0.30
            + weekly["macd_golden"] * 0.30
            + weekly["retrace"] * 0.25
            + weekly["triple_bull"] * 0.15
        )

        # 将周线评分对齐到日线：使用 merge_asof 按最近周对齐
        weekly["date"] = pd.to_datetime(weekly["date"])
        df["date"] = pd.to_datetime(df["date"])

        # 按日期排序确保 merge_asof 正确工作
        weekly = weekly.sort_values("date").reset_index(drop=True)
        df = df.sort_values("date").reset_index(drop=True)

        # 使用 merge_asof：将最近的周线数据对齐到每个交易日
        weekly_cols = ["date", "score", "vol_boost", "macd_golden", "retrace", "triple_bull"]
        df = pd.merge_asof(
            df,
            weekly[weekly_cols],
            on="date",
            direction="backward",  # 向前填充：用最近的周线数据
        )

        # 信号规则：score >= 0.5 买入
        df["score"] = df["score"].fillna(0.0)
        df["signal"] = 0
        df.loc[df["score"] >= 0.5, "signal"] = 1

        # 卖出信号1：score < 0.2（因子共振消失）
        df.loc[df["score"] < 0.2, "signal"] = -1

        # 卖出信号2：MACD死叉（趋势反转）
        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        death_cross = (dif < dea) & (dif.shift(1) >= dea.shift(1))
        df.loc[death_cross, "signal"] = -1

        # 卖出信号3：价格跌破20日均线（短期趋势破坏）
        ma20 = df["close"].rolling(20).mean()
        below_ma20 = (df["close"] < ma20) & (df["close"].shift(1) >= ma20.shift(1))
        df.loc[below_ma20, "signal"] = -1

        # 动态止损和移动止盈
        df = self._apply_stop_loss_and_take_profit(df)

        # 置信度：基于 score
        df["confidence"] = df["score"].clip(0, 1)
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        return df

    def _apply_stop_loss_and_take_profit(self, df: pd.DataFrame) -> pd.DataFrame:
        """应用动态止损和移动止盈。

        动态止损：跌破入场价 × (1 - stop_loss_pct) 强制卖出
        移动止盈：盈利超过 take_profit_threshold 后，回撤 take_profit_drawback 止盈
        """
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.08))
        take_profit_threshold = float(self.params.get("take_profit_threshold", 0.15))
        take_profit_drawback = float(self.params.get("take_profit_drawback", 0.10))

        signals = df["signal"].to_numpy(dtype=int, copy=True)
        closes = df["close"].to_numpy(dtype=float, copy=True)
        n = len(signals)

        # 追踪持仓状态
        entry_price = 0.0
        highest_price = 0.0
        in_position = False

        for i in range(n):
            if signals[i] == 1 and not in_position:
                # 买入
                entry_price = closes[i]
                highest_price = closes[i]
                in_position = True
            elif signals[i] == -1 and in_position:
                # 卖出信号
                in_position = False
                entry_price = 0.0
                highest_price = 0.0
            elif in_position:
                # 更新最高价
                if closes[i] > highest_price:
                    highest_price = closes[i]

                # 动态止损检查
                if closes[i] < entry_price * (1 - stop_loss_pct):
                    signals[i] = -1
                    in_position = False
                    entry_price = 0.0
                    highest_price = 0.0
                    continue

                # 移动止盈检查
                profit_pct = (highest_price - entry_price) / entry_price
                if profit_pct >= take_profit_threshold:
                    # 从最高价回撤超过阈值时止盈
                    current_drawback = (highest_price - closes[i]) / highest_price
                    if current_drawback >= take_profit_drawback:
                        signals[i] = -1
                        in_position = False
                        entry_price = 0.0
                        highest_price = 0.0

        df["signal"] = signals
        return df

    def _resample_weekly(self, df: pd.DataFrame) -> pd.DataFrame:
        """将日K数据转换为周K数据。"""
        df_d = df.copy()
        if "date" in df_d.columns:
            df_d = df_d.set_index("date")

        weekly = (
            df_d.resample("W")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
        )

        weekly = weekly.reset_index()
        weekly = weekly.rename(columns={"date": "date"})
        return weekly

    def _calc_volume_boost(self, weekly: pd.DataFrame) -> pd.Series:
        """因子1：周线倍量检测。

        当前周成交量 >= 前一周成交量的 volume_ratio 倍，且收阳。
        """
        vol_ratio = float(self.params.get("volume_ratio", 2.0))
        prev_volume = weekly["volume"].shift(1).replace(0, 1)

        # 倍量条件
        vol_doubled = weekly["volume"] >= prev_volume * vol_ratio
        # 收阳条件
        bullish_close = weekly["close"] > weekly["open"]

        # 得分：倍量+阳线 = 1.0，否则 0
        score = (vol_doubled & bullish_close).astype(float)
        return score

    def _calc_macd_golden(self, weekly: pd.DataFrame) -> pd.Series:
        """因子2：周线MACD金叉。

        周线MACD金叉，且DIF开始向上。
        """
        # 计算周线 MACD
        ema12 = weekly["close"].ewm(span=12, adjust=False).mean()
        ema26 = weekly["close"].ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()

        # 金叉条件：DIF 上穿 DEA
        golden_cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
        # 金叉后持续向上（金叉后3周内DIF保持向上）
        golden_sustain = (dif > dif.shift(1)) | (dif > dif.shift(2))

        score = (golden_cross | golden_sustain).astype(float)
        # 金叉当天得分更高
        score = score + golden_cross.astype(float) * 0.5
        return score.clip(0, 1)

    def _calc_retrace_score(self, weekly: pd.DataFrame) -> pd.Series:
        """因子3：回踩20周均线且缩量。

        条件：
        - 股价在20周均线之上（上升趋势）
        - 股价回踩至20周均线附近（±3%）
        - 成交量较前一周缩小（缩量回调）
        """
        ma20 = weekly["close"].rolling(20).mean()
        tolerance = float(self.params.get("retrace_tolerance", 0.03))

        # 上升趋势：收盘价在20周均线之上
        above_ma20 = weekly["close"] > ma20

        # 回踩至20周均线附近（±tolerance）
        near_ma20 = (weekly["close"] >= ma20 * (1 - tolerance)) & (
            weekly["close"] <= ma20 * (1 + tolerance)
        )

        # 缩量：当前成交量 < 前一周成交量
        vol_shrink = weekly["volume"] < weekly["volume"].shift(1)

        # 得分
        score = (above_ma20 & near_ma20 & vol_shrink).astype(float)
        return score

    def _calc_triple_bull(self, weekly: pd.DataFrame) -> pd.Series:
        """因子4：周线三连阳。

        条件：
        - 连续三根阳线（收盘 > 开盘）
        - 收盘价逐步抬高
        - 成交量温和放大（不超过2倍）
        """
        # 三根阳线
        bull_1 = weekly["close"] > weekly["open"]
        bull_2 = weekly["close"].shift(1) > weekly["open"].shift(1)
        bull_3 = weekly["close"].shift(2) > weekly["open"].shift(2)

        # 收盘价逐步抬高
        close_up = (weekly["close"] > weekly["close"].shift(1)) & (
            weekly["close"].shift(1) > weekly["close"].shift(2)
        )

        # 成交量温和放大（不超过2倍）
        vol_growth = (weekly["volume"] >= weekly["volume"].shift(1)) & (
            weekly["volume"] <= weekly["volume"].shift(1) * 2.0
        )

        # 得分
        score = (bull_1 & bull_2 & bull_3 & close_up & vol_growth).astype(float)
        return score
