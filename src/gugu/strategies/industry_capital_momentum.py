"""行业资金强度+动量确认策略（个股适配版 v1.2）。

来源：行业资金强度+动量确认策略框架
第一性原理：让资金先投票，用动量确认，在趋势早期介入，在资金退潮前退出

四层架构（适配个股数据）：
1. 资金监控层：识别个股资金聚集（量价趋势、成交额变化、资金动量）
2. 动量确认层：过滤假信号（MA5/MA20金叉、量能放大、RSI合理区间）
3. 入场触发层：多条件评分买入（核心条件 + 评分增强）
4. 退出风控层：趋势破坏/资金退潮/过热止盈/硬止损

v1.2 优化：放宽退出条件（止损8%→12%，止盈20%→25%），减少震荡市频繁止损
v1.1 重构：适配个股数据分析
- 原策略设计为行业级别（需横截面排名），回测框架传入个股数据
- 重构后所有指标基于个股时间序列，保留四层架构思想
- 放宽入场条件：减少硬AND条件，增加评分权重

入场触发条件（重构后）：
核心条件（必须全部满足）：
  1. MA5/MA20金叉（或已金叉状态）
  2. 收盘价站上MA5
  3. 量能放大（量比 >= 1.2）
  4. RSI在合理区间（35-70）
  5. MA5斜率 > -0.02（趋势不差）

评分增强项（累计 >= 0.40 可加分）：
  - 资金动量强（量价齐升）
  - 成交额占比提升（相对近期均值）
  - 个股共振（近3日多数收涨）
  - 非过热（20日涨幅 < 25%）
  - 非过度拥挤（成交量非异常）

退出风控条件：
  1. 趋势破坏：收盘价跌破MA10
  2. 资金退潮：量能持续萎缩或价格连续下跌
  3. 过热止盈：20日涨幅>30%或RSI>75
  4. 硬止损：从高点回撤达-8%
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class IndustryCapitalMomentumStrategy(Strategy):
    """行业资金强度+动量确认策略（个股适配版）。"""

    name = "industry_capital_momentum"
    category = "capital_flow"

    def validate_params(self) -> None:
        ma_short = int(self.params.get("ma_short", 5))
        ma_long = int(self.params.get("ma_long", 20))
        if ma_short >= ma_long:
            raise ValueError("ma_short 必须 < ma_long")

    def _ensure_columns(self, df: pd.DataFrame) -> None:
        required = ["open", "high", "low", "close", "volume", "amount"]
        for col in required:
            if col not in df.columns:
                raise ValueError(f"缺少必要列: {col}")

    def _calc_rsi(self, close: pd.Series, window: int) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(window).mean()
        avg_loss = loss.rolling(window).mean()

        rs = pd.Series(np.nan, index=close.index)
        has_gain = (avg_gain > 0) & (avg_loss == 0)
        has_loss = (avg_loss > 0) & (avg_gain == 0)
        both_positive = (avg_gain > 0) & (avg_loss > 0)

        rs[has_gain] = np.inf
        rs[has_loss] = 0.0
        rs[both_positive] = avg_gain[both_positive] / avg_loss[both_positive]

        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.fillna(50.0)
        return rsi

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        p = {
            "ma_short": int(self.params.get("ma_short", 5)),
            "ma_long": int(self.params.get("ma_long", 20)),
            "volume_ratio": float(self.params.get("volume_ratio", 1.2)),
            "rsi_window": int(self.params.get("rsi_window", 14)),
            "rsi_low": float(self.params.get("rsi_low", 35)),
            "rsi_high": float(self.params.get("rsi_high", 70)),
            "stop_loss_pct": float(self.params.get("stop_loss_pct", 0.08)),
            "take_profit_pct": float(self.params.get("take_profit_pct", 0.20)),
            "overheat_return_pct": float(self.params.get("overheat_return_pct", 0.30)),
            "overheat_rsi": float(self.params.get("overheat_rsi", 75)),
            "min_entry_score": float(self.params.get("min_entry_score", 0.40)),
            "ma5_slope_threshold": float(self.params.get("ma5_slope_threshold", -0.02)),
            "return_hot_threshold": float(self.params.get("return_hot_threshold", 0.25)),
            "crowding_vol_ratio": float(self.params.get("crowding_vol_ratio", 2.5)),
            "exit_trend_ma": int(self.params.get("exit_trend_ma", 10)),
        }

        ma_s = p["ma_short"]
        ma_l = p["ma_long"]

        # ── 均线系统 ──
        df["ma5"] = df["close"].rolling(ma_s).mean()
        df["ma10"] = df["close"].rolling(p["exit_trend_ma"]).mean()
        df["ma20"] = df["close"].rolling(ma_l).mean()
        df["ma5_slope"] = df["ma5"].diff(3) / df["ma5"].shift(3).replace(0, np.nan)

        # ── 量能指标 ──
        vol_ma20 = df["volume"].rolling(20).mean().shift(1)
        df["vol_ratio"] = df["volume"] / vol_ma20.replace(0, np.nan)

        amount_ma20 = df["amount"].rolling(20).mean().shift(1)
        df["amount_ratio"] = df["amount"] / amount_ma20.replace(0, np.nan)

        # ── RSI ──
        df["rsi"] = self._calc_rsi(df["close"], p["rsi_window"])

        # ── 收益率 ──
        df["daily_return"] = df["close"].pct_change()
        df["return_20d"] = df["close"].pct_change(20)

        # ── 个股涨跌 ──
        df["stock_up"] = (df["close"] > df["open"]).astype(float)

        # ══════════════════════════════════════════════════════
        # 动量确认条件
        # ══════════════════════════════════════════════════════

        # MA5/MA20金叉（当日金叉或已金叉状态）
        ma5_above_ma20_now = df["ma5"] > df["ma20"]
        ma5_above_ma20_prev = df["ma5"].shift(1) > df["ma20"].shift(1)
        ma5_cross_up = ma5_above_ma20_now & ~ma5_above_ma20_prev
        ma_momentum_confirmed = ma5_cross_up | ma5_above_ma20_now

        # 站上MA5
        above_ma5 = df["close"] > df["ma5"]

        # 量能确认（放宽到1.2倍，不再要求前日）
        volume_confirm = df["vol_ratio"] >= p["volume_ratio"]

        # RSI合理区间（放宽到35-70）
        rsi_in_zone = (df["rsi"] >= p["rsi_low"]) & (df["rsi"] <= p["rsi_high"])

        # MA5趋势不差
        ma_trend_up = df["ma5_slope"] > p["ma5_slope_threshold"]

        # ══════════════════════════════════════════════════════
        # 入场触发层：核心条件 + 评分增强
        # ══════════════════════════════════════════════════════

        # 核心条件（必须全部满足）
        core_conditions = (
            ma_momentum_confirmed &
            above_ma5 &
            volume_confirm &
            rsi_in_zone &
            ma_trend_up
        )

        # 评分增强项（满足则加分）
        # 1. 资金动量强：量价齐升（当日上涨 + 放量）
        score_momentum = ((df["daily_return"] > 0) & (df["vol_ratio"] > 1.0)).astype(float) * 0.20

        # 2. 成交额占比提升：相对20日均值提升
        score_amount_up = (df["amount_ratio"] > 1.1).astype(float) * 0.15

        # 3. 个股共振：近3日平均 >= 50%收涨
        score_resonance = (df["stock_up"].rolling(3).mean() >= 0.5).astype(float) * 0.15

        # 4. 非过热：20日涨幅 < 25%
        score_not_hot = (df["return_20d"] < p["return_hot_threshold"]).astype(float) * 0.10

        # 5. 非过度拥挤：量比不超过2.5倍（排除异常放量）
        score_not_crowded = (df["vol_ratio"] < p["crowding_vol_ratio"]).astype(float) * 0.10

        entry_score = score_momentum + score_amount_up + score_not_hot + score_not_crowded + score_resonance

        # 最终买入条件：核心条件 + 评分达标
        buy_cond_final = core_conditions & (entry_score >= p["min_entry_score"])

        df["signal"] = 0
        df.loc[buy_cond_final, "signal"] = 1

        # ══════════════════════════════════════════════════════
        # 退出风控层
        # ══════════════════════════════════════════════════════

        # 1. 趋势破坏：收盘价跌破MA10
        exit_trend_breakdown = df["close"] < df["ma10"]

        # 2. 资金退潮：量能持续萎缩（量比<0.7连续2日）或价格连续下跌
        vol_shrink = (df["vol_ratio"] < 0.7).rolling(2).sum() >= 2
        price_decline = (df["daily_return"] < 0).rolling(3).sum() >= 3
        exit_capital_drain = vol_shrink | price_decline

        # 3. 过热止盈：20日涨幅>30%或RSI>75
        exit_overheat_return = df["return_20d"] > p["overheat_return_pct"]
        exit_overheat_rsi = df["rsi"] > p["overheat_rsi"]
        exit_overheat = exit_overheat_return | exit_overheat_rsi

        # 4. 硬止损：从高点回撤8%
        rolling_max = df["close"].rolling(20).max()
        drawdown_from_high = (df["close"] - rolling_max) / rolling_max
        exit_hard_stop = drawdown_from_high < -p["stop_loss_pct"]

        # 5. 止盈：达到目标收益
        exit_take_profit = df["return_20d"] > p["take_profit_pct"]

        # 综合卖出条件
        sell_cond = (
            exit_trend_breakdown |
            exit_capital_drain |
            exit_overheat |
            exit_hard_stop |
            exit_take_profit
        )

        # 只在已买入状态下生成卖出信号
        position = buy_cond_final.astype(int).cumsum() - sell_cond.astype(int).cumsum()
        position = position.clip(lower=0)
        has_position = position > 0

        df.loc[sell_cond & has_position, "signal"] = -1

        return df
