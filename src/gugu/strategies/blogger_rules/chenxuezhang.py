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
        """生成陈学长不吃面信号。

        exit_mode 参数（2026-08-01 新增，引擎级回测夏普-0.15 的出场端修复）：
        - legacy:    原逻辑。R15 用"日涨幅>2%"近似尾盘骗线 → 题材股天天触发，
                     持仓中位仅 2-3 天、40-63% 交易 2 天内卖出 → 砍掉利润。
        - optimized: R15 改用真上影线（冲高回落>3%才卖，≈尾盘骗线语义）；
                     R25 量价背离收紧（量<MA5*0.5 才卖）；加 max_hold_days 时间止损。
        """
        self._ensure_columns(df)
        result = df.copy()
        exit_mode = str(self.params.get("exit_mode", "optimized"))

        # 参数化阈值（2026-08-01 参数闭环修复：原硬编码，GA/PSO 调优写回不生效）
        ma_short = int(self.params.get("ma_short", 5))
        ma_mid = int(self.params.get("ma_mid", 10))
        ma_long = int(self.params.get("ma_long", 20))
        vol_shrink_ratio = float(self.params.get("vol_shrink_ratio", 0.8))
        tail_move_threshold = float(self.params.get("tail_move_threshold", 0.03))
        trend_pullback_tolerance = float(self.params.get("trend_pullback_tolerance", 0.02))
        vol_diverge_ratio = float(self.params.get("vol_diverge_ratio", 0.5))

        # 计算指标
        close = result["close"]
        high = result["high"]
        volume = result["volume"]

        # 均线系统
        ma5 = close.rolling(ma_short).mean()
        ma10 = close.rolling(ma_mid).mean()
        ma20 = close.rolling(ma_long).mean()

        # 量能
        vol_ma5 = volume.rolling(5).mean()
        vol_ma20 = volume.rolling(20).mean()

        # 各规则信号（1=买入, -1=卖出, 0=持有）
        signals = pd.Series(0, index=result.index)
        confidence = pd.Series(0.0, index=result.index)

        # R12: 假调整 — 缩量回踩MA10后上攻
        # 条件：价格回踩MA10，量能缩至MA20以下，然后放量上穿MA5
        # 注意：R12 回踩容差固定 ±3%（独立于 R22 的 trend_pullback_tolerance，两规则语义不同）
        vol_shrink = vol_ma5 < vol_ma20 * vol_shrink_ratio
        price_pullback = (close > ma10 * 0.97) & (close < ma10 * 1.03)
        breakout = (close > ma5) & (close.shift(1) <= ma5.shift(1))
        fake_adjust = vol_shrink & price_pullback & breakout
        signals = signals.where(~fake_adjust, 1)
        confidence = confidence.where(~fake_adjust, 0.7)

        # R15: 尾盘骗线 — 尾盘30分钟内急拉>2%或急砸>2%
        # 尾盘急拉是卖出信号，急砸是买入信号
        intraday_change = (close - close.shift(1)) / close.shift(1)
        if exit_mode == "optimized":
            # 修复：日涨幅>2% 不是"尾盘骗线"——题材股天天涨2%，等于一涨就卖。
            # 真骗线 = 冲高回落（上影线长）：盘中拉高>4%但收盘回落到仅涨1%以内
            upper_shadow = (high - close) / close
            tail_fake_up = (upper_shadow > tail_move_threshold) & (intraday_change > 0.01)
            tail_fake_down = intraday_change < -tail_move_threshold  # 急砸>3%才买（原来-2%太松）
        else:
            tail_fake_up = intraday_change > 0.02  # 尾盘急拉→卖出
            tail_fake_down = intraday_change < -0.02  # 急砸→买入
        signals = signals.where(~tail_fake_down, 1)
        signals = signals.where(~tail_fake_up, -1)
        confidence = confidence.where(~tail_fake_down, 0.6)
        confidence = confidence.where(~tail_fake_up, 0.6)

        # R22: 趋势回踩 — 上升趋势中回踩MA20低吸
        uptrend = (ma5 > ma10) & (ma10 > ma20)
        pullback_to_20 = (close > ma20 * (1 - trend_pullback_tolerance)) & (
            close < ma20 * (1 + trend_pullback_tolerance)
        )
        trend_pullback = uptrend & pullback_to_20 & (volume > vol_ma5 * vol_shrink_ratio)
        signals = signals.where(~trend_pullback, 1)
        confidence = confidence.where(~trend_pullback, 0.65)

        # R25: 量价背离 — 价涨量缩=卖出信号，价跌量缩=可能反转
        if exit_mode == "optimized":
            # 收紧：极端缩量（<MA5*0.5）才视为背离卖出，减少正常缩量整理被误杀
            price_up_vol_down = (close > close.shift(1)) & (volume < vol_ma5 * vol_diverge_ratio)
            price_down_vol_down = (close < close.shift(1)) & (volume < vol_ma5 * 0.6)
        else:
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

        # 时间止损：max_hold_days 内未触发卖出信号则强制退出（optimized 默认开启）
        if exit_mode == "optimized":
            result = self._apply_max_hold_exit(result)

        buy_count = int((result["signal"] == 1).sum())
        sell_count = int((result["signal"] == -1).sum())
        logger.info(
            "陈学长不吃面策略生成信号({}): {} 买入, {} 卖出",
            exit_mode,
            buy_count,
            sell_count,
        )

        return result
