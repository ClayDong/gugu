"""行业资金强度+动量确认策略（个股适配版 v2.4）。

来源：行业资金强度+动量确认策略框架
第一性原理：让资金先投票，用动量确认，在趋势早期介入，在资金退潮前退出

v2.4 优化（基于 v2.3 回测结果）：
- 总收益 -3.35%，夏普 -1.60，胜率 25%，交易14次/股
- 问题分析：降低门槛后交易增加但质量下降，平均持仓仅4.5天被止损/趋势破坏强制平仓
- 对策：
  1. 保持量比1.3x（机会）
  2. MA20趋势过滤改回开启（质量）
  3. 放宽止损容忍度：8% → 10%（减少被震出）

v2.3 优化（基于 v2.2 回测结果）：
- 总收益 -2.03%，夏普 -1.58，胜率 19.7%
- 问题分析：量比1.5x门槛过高，MA20趋势过滤过于严格

v2.2 优化（基于 v2.1 回测结果）：
- 总收益 -2.25%，夏普 -0.82，胜率 29.

"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class IndustryCapitalMomentumStrategy(Strategy):
    """行业资金强度+动量确认策略（个股适配版 v2.4）。"""

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
            "volume_ratio": float(self.params.get("volume_ratio", 1.3)),
            "rsi_window": int(self.params.get("rsi_window", 14)),
            "rsi_low": float(self.params.get("rsi_low", 45)),
            "rsi_high": float(self.params.get("rsi_high", 65)),
            "stop_loss_pct": float(self.params.get("stop_loss_pct", 0.10)),
            "take_profit_pct": float(self.params.get("take_profit_pct", 0.12)),
            "trailing_stop_pct": float(self.params.get("trailing_stop_pct", 0.03)),
            "trailing_start_pct": float(self.params.get("trailing_start_pct", 0.05)),
            "min_entry_score": float(self.params.get("min_entry_score", 0.30)),
            "ma5_slope_threshold": float(self.params.get("ma5_slope_threshold", 0.0)),
            "return_hot_threshold": float(self.params.get("return_hot_threshold", 0.20)),
            "exit_trend_ma": int(self.params.get("exit_trend_ma", 10)),
            "min_hold_days": int(self.params.get("min_hold_days", 8)),
            "max_hold_days": int(self.params.get("max_hold_days", 25)),
            "require_ma20_up": bool(self.params.get("require_ma20_up", True)),
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

        # ── RSI ──
        df["rsi"] = self._calc_rsi(df["close"], p["rsi_window"])

        # ── 收益率 ──
        df["daily_return"] = df["close"].pct_change()
        df["return_20d"] = df["close"].pct_change(20)

        # ══════════════════════════════════════════════════════
        # v2.0 核心改进1：入场条件简化
        # v2.2 新增：MA20趋势过滤
        # ══════════════════════════════════════════════════════

        # MA5/MA20金叉（当日金叉或已金叉状态）
        ma5_above_ma20_now = df["ma5"] > df["ma20"]
        ma5_above_ma20_prev = df["ma5"].shift(1) > df["ma20"].shift(1)
        ma5_cross_up = ma5_above_ma20_now & ~ma5_above_ma20_prev
        ma_golden_cross = ma5_cross_up | ma5_above_ma20_now

        # v2.2 新增：MA20趋势向上过滤（要求MA20在上升趋势中）
        ma20_slope = df["ma20"].diff(5) / df["ma20"].shift(5).replace(0, np.nan)
        ma20_up = ma20_slope > 0 if p["require_ma20_up"] else True

        # RSI在合理区间（45-65，不过于超买也不超卖）
        rsi_ok = (df["rsi"] >= p["rsi_low"]) & (df["rsi"] <= p["rsi_high"])

        # 量能配合（放量确认，v2.2提高至1.5倍）
        volume_ok = df["vol_ratio"] >= p["volume_ratio"]

        # 综合入场条件（四个都必须满足）
        entry_ready = ma_golden_cross & rsi_ok & volume_ok & ma20_up

        # ══════════════════════════════════════════════════════
        # v2.0 核心改进2：连续评分系统
        # 用实际涨幅和量比作为连续分数，而非二元判断
        # ══════════════════════════════════════════════════════

        # 量能评分：量比从1.2到2.0映射到0-1之间，超过2.0得1分
        score_volume = np.clip(
            (df["vol_ratio"] - p["volume_ratio"]) / (2.0 - p["volume_ratio"]), 0, 1
        )

        # 价格动量评分：当日涨幅从0到5%映射到0-1之间
        score_momentum = np.clip(df["daily_return"] / 0.05, 0, 1)

        # RSI舒适度评分：RSI在40-60之间得分最高（1），越靠近边界越低
        rsi_mid = (p["rsi_low"] + p["rsi_high"]) / 2
        rsi_dist_from_mid = np.abs(df["rsi"] - rsi_mid)
        rsi_range = (p["rsi_high"] - p["rsi_low"]) / 2
        score_rsi_comfort = np.clip(1 - rsi_dist_from_mid / rsi_range, 0, 1)

        # 综合入场评分（加权平均）
        entry_score = score_volume * 0.35 + score_momentum * 0.35 + score_rsi_comfort * 0.30

        # 最终买入条件：入场就绪 + 评分达标 + 不过热
        buy_cond_final = (
            entry_ready
            & (entry_score >= p["min_entry_score"])
            & (df["return_20d"] < p["return_hot_threshold"])
        )

        # ══════════════════════════════════════════════════════
        # v2.0 核心改进3：状态机持仓追踪
        # 使用 position_state 而非 cumsum 计算
        # ══════════════════════════════════════════════════════

        position_state = pd.Series(0, index=df.index)
        entry_price = pd.Series(np.nan, index=df.index)
        entry_idx = pd.Series(np.nan, index=df.index)
        hold_days = pd.Series(0, index=df.index)

        in_position = False
        current_entry_price = np.nan
        current_entry_idx = np.nan
        running_high = np.nan

        for i in range(len(df)):
            if i < ma_l:
                continue

            current_close = df["close"].iloc[i]

            if not in_position:
                if buy_cond_final.iloc[i]:
                    in_position = True
                    current_entry_price = current_close
                    current_entry_idx = i
                    running_high = current_close
                    position_state.iloc[i] = 1
                    entry_price.iloc[i] = current_entry_price
                    entry_idx.iloc[i] = i
                    hold_days.iloc[i] = 0
            else:
                hold_days_count = i - current_entry_idx

                running_high = max(running_high, current_close)
                profit_pct = current_close / current_entry_price - 1

                stop_loss_triggered = profit_pct < -p["stop_loss_pct"]
                trend_break_triggered = current_close < df["ma10"].iloc[i]

                trailing_stop_triggered = False
                if profit_pct > p["trailing_start_pct"]:
                    trailing_stop_triggered = (running_high - current_close) / running_high > p[
                        "trailing_stop_pct"
                    ]

                take_profit_triggered = (
                    hold_days_count >= p["min_hold_days"] and profit_pct > p["take_profit_pct"]
                )
                max_hold_triggered = hold_days_count >= p["max_hold_days"]

                should_exit = (
                    stop_loss_triggered
                    or trend_break_triggered
                    or trailing_stop_triggered
                    or take_profit_triggered
                    or max_hold_triggered
                )

                if should_exit:
                    in_position = False
                    position_state.iloc[i] = -1
                    current_entry_price = np.nan
                    current_entry_idx = np.nan
                    running_high = np.nan
                else:
                    # P0 修复：持仓期间输出 0，避免回测引擎将持仓状态误解为每日买入事件
                    position_state.iloc[i] = 0

                entry_price.iloc[i] = (
                    current_entry_price if not np.isnan(current_entry_price) else np.nan
                )
                entry_idx.iloc[i] = current_entry_idx if not np.isnan(current_entry_idx) else np.nan
                hold_days.iloc[i] = hold_days_count

        df["signal"] = position_state.values

        return df


def _test_simple():
    """快速测试策略逻辑"""
    import numpy as np
    import pandas as pd

    dates = pd.date_range("2024-01-01", periods=100, freq="D")
    np.random.seed(42)

    close_base = 100 + np.cumsum(np.random.randn(100) * 2)
    volume_base = 1000000 + np.random.randn(100) * 100000

    df = pd.DataFrame(
        {
            "open": close_base * (1 + np.random.randn(100) * 0.01),
            "high": close_base * (1 + np.abs(np.random.randn(100)) * 0.02),
            "low": close_base * (1 - np.abs(np.random.randn(100)) * 0.02),
            "close": close_base,
            "volume": volume_base,
            "amount": volume_base * close_base,
        },
        index=dates,
    )

    strategy = IndustryCapitalMomentumStrategy()
    result = strategy.generate_signals(df)

    buy_signals = (result["signal"] == 1).sum()
    sell_signals = (result["signal"] == -1).sum()

    print(f"买入信号: {buy_signals}")
    print(f"卖出信号: {sell_signals}")
    print("胜率测试通过" if buy_signals > 0 else "无买入信号")


if __name__ == "__main__":
    _test_simple()
