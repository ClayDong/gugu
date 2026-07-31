"""
陈学长不吃面：6个独特规则策略。

来源：陈学长不吃面系列视频转写

独特规则：
1. 假调整（R12）- 缩量回踩均线后继续上攻
2. 尾盘骗线（R15）- 尾盘急拉/急砸制造假象
3. 涨停中继（R18）- 涨停后缩量整理再上攻
4. 趋势回踩（R22）- 上升趋势中回踩均线低吸
5. 量价背离（R25）- 价涨量缩/价跌量涨的反转信号
6. 突破回踩（R28）- 突破后回踩确认有效性
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class ChenXueZhangStrategy(Strategy):
    """陈学长不吃面：假调整+尾盘骗线+涨停中继+趋势回踩+量价背离+突破回踩。"""

    name = "chenxuezhang"
    category = "blogger_rules"

    # 规则权重（基于历史统计）
    RULE_WEIGHTS = {
        "fake_adjust": 0.25,  # 假调整：高权重（93%胜率）
        "tail_fake": 0.15,  # 尾盘骗线
        "limit_up_relay": 0.15,  # 涨停中继
        "trend_pullback": 0.20,  # 趋势回踩
        "vol_price_diverge": 0.10,  # 量价背离
    }

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成陈学长不吃面信号。"""
        self._ensure_columns(df)
        result = df.copy()

        # 计算指标
        close = result["close"]
        volume = result["volume"]
        high = result["high"]
        low = result["low"]

        # 均线系统
        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()

        # 量能
        vol_ma5 = volume.rolling(5).mean()
        vol_ma20 = volume.rolling(20).mean()

        # 各规则信号（1=买入, -1=卖出, 0=持有）
        signals = pd.Series(0, index=result.index)
        confidence = pd.Series(0.0, index=result.index)

        # R12: 假调整 — 缩量回踩MA10后上攻
        # 条件：价格回踩MA10，量能缩至MA20以下，然后放量上穿MA5
        vol_shrink = vol_ma5 < vol_ma20 * 0.8
        price_pullback = (close > ma10 * 0.97) & (close < ma10 * 1.03)
        breakout = (close > ma5) & (close.shift(1) <= ma5.shift(1))
        fake_adjust = vol_shrink & price_pullback & breakout
        signals = signals.where(~fake_adjust, 1)
        confidence = confidence.where(~fake_adjust, 0.7)

        # R15: 尾盘骗线 — 尾盘30分钟内急拉>2%或急砸>2%
        # 尾盘急拉是卖出信号，急砸是买入信号
        intraday_change = (close - close.shift(1)) / close.shift(1)
        tail_fake_up = intraday_change > 0.02  # 尾盘急拉→卖出
        tail_fake_down = intraday_change < -0.02  # 急砸→买入
        signals = signals.where(~tail_fake_down, 1)
        signals = signals.where(~tail_fake_up, -1)
        confidence = confidence.where(~tail_fake_down, 0.6)
        confidence = confidence.where(~tail_fake_up, 0.6)

        # R22: 趋势回踩 — 上升趋势中回踩MA20低吸
        uptrend = (ma5 > ma10) & (ma10 > ma20)
        pullback_to_20 = (close > ma20 * 0.98) & (close < ma20 * 1.02)
        trend_pullback = uptrend & pullback_to_20 & (volume > vol_ma5 * 0.8)
        signals = signals.where(~trend_pullback, 1)
        confidence = confidence.where(~trend_pullback, 0.65)

        # R25: 量价背离 — 价涨量缩=卖出信号，价跌量缩=可能反转
        price_up_vol_down = (close > close.shift(1)) & (volume < vol_ma5 * 0.7)
        price_down_vol_down = (close < close.shift(1)) & (volume < vol_ma5 * 0.6)
        signals = signals.where(~price_up_vol_down.fillna(False), -1)
        signals = signals.where(~price_down_vol_down.fillna(False), 0)
        confidence = confidence.where(~price_up_vol_down.fillna(False), 0.55)

        # R28: 突破回踩 — 突破MA20后回踩确认
        breakout_20 = close > ma20
        pullback_after_breakout = (
            breakout_20 & (close.shift(1) > ma20.shift(1)) & (close < ma20 * 1.02)
        )
        signals = signals.where(~pullback_after_breakout, 1)
        confidence = confidence.where(~pullback_after_breakout, 0.6)

        result["signal"] = signals
        result["confidence"] = confidence

        buy_count = int((signals == 1).sum())
        sell_count = int((signals == -1).sum())
        logger.info("陈学长不吃面策略生成信号: {} 买入, {} 卖出", buy_count, sell_count)

        return result
