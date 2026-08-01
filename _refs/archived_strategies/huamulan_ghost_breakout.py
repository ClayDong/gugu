"""烂板成妖打板战法：花木兰博取连板溢价策略。

来源：战神花木兰系列文档整理

适用场景：情绪高涨期，捕捉龙头主升浪。

核心逻辑：
1. 启动位置：长期横盘后突破箱体，或突破前高压力位
2. 必须放量：涨停当日必须放出阶段性巨量，消化套牢盘
3. 炸板洗盘：允许盘中炸板，这是主力制造恐慌、交换筹码的行为
4. 回封有力：炸板后回落幅度不超3%，围绕均线横盘震荡
5. 尾盘缩量：最终回封时封单不一定巨大，但筹码锁定良好（散户惜售）

与现有策略差异：
- hot_money：涨停后缩量入场（游资策略）
- breakout_rebound：涨停后断板反包（陈学长战法）
- huamulan_ghost_breakout：烂板成妖（炸板洗盘再回封）

回测说明：
- 烂板信号在日K线中表现为：前日涨停 + 当日高开放量 + 收阳/十字星
- 炸板洗盘：当日最高价 > 前日收盘价 + 3%（盘中冲高回落）
- 回封有力：当日收盘价 > 前日收盘价（未破前日涨停价）
- 缩量锁定：当日成交量 < 前日成交量（散户惜售）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class HuamulanGhostBreakoutStrategy(Strategy):
    """烂板成妖打板战法策略。

    博取连板溢价，适合情绪高涨期。
    核心逻辑：炸板是主力洗盘，回封是筹码锁定。

    回测说明：
    - 日K线近似：前日涨停 + 当日高开放量收阳 + 量能萎缩
    - 实盘中需分钟级数据精确判断"炸板+回封"
    """

    name = "huamulan_ghost_breakout"
    category = "main_force"

    def validate_params(self) -> None:
        """校验参数语义。"""
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up_pct <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11% 之间")
        vol_expand = float(self.params.get("vol_expand_ratio", 1.5))
        if vol_expand < 1.0:
            raise ValueError("vol_expand_ratio 必须 >= 1.0")
        stop_loss = float(self.params.get("stop_loss_pct", 0.05))
        if stop_loss <= 0:
            raise ValueError("stop_loss_pct 必须大于 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成烂板成妖信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        vol_expand_ratio = float(self.params.get("vol_expand_ratio", 1.5))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.05))
        take_profit_pct = float(self.params.get("take_profit_pct", 0.10))
        time_stop_window = int(self.params.get("time_stop_window", 5))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.02))
        consolidation_window = int(self.params.get("consolidation_window", 20))

        # ── 计算均线系统 ──────────────────────────────
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()

        # ── 计算量能指标 ──────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # ── K线基础属性 ──────────────────────────────
        df["daily_return"] = df["close"].pct_change()
        df["is_yang"] = (df["close"] > df["open"]).astype(bool)

        # 涨停判定（涨幅 >= 9.5%）
        df["is_limit_up"] = np.round(df["daily_return"], 4) >= limit_up_pct

        # 前日涨停
        df["prev_limit_up"] = df["is_limit_up"].shift(1)

        # ════════════════════════════════════════════════
        # 买入条件（烂板成妖）
        # ════════════════════════════════════════════════
        # 条件1：前日涨停（启动位置：突破箱体或前高）
        cond_prev_limit = df["prev_limit_up"]

        # 条件2：当日高开放量（炸板洗盘：高开放量冲高回落）
        df["gap_up"] = df["open"] / df["close"].shift(1) - 1
        cond_gap_up = df["gap_up"] > 0.02  # 高开2%以上

        # 条件3：当日成交量放大（炸板洗盘：放量交换筹码）
        df["prev_vol"] = df["volume"].shift(1)
        cond_vol_expand = df["volume"] > df["prev_vol"] * vol_expand_ratio

        # 条件4：当日收阳或十字星（回封有力：未破前日涨停价）
        cond_reseal = df["close"] > df["close"].shift(1)

        # 条件5：当日最高价 > 前日收盘价 + 3%（盘中冲高，炸板）
        df["prev_close"] = df["close"].shift(1)
        cond_break_high = df["high"] > df["prev_close"] * 1.03

        # 条件6：长期横盘后突破（启动位置）
        df["consolidation_high"] = df["high"].rolling(consolidation_window).max().shift(1)
        cond_breakout = df["close"] > df["consolidation_high"]

        # 综合买入信号（6条件全满足）
        buy_cond = (
            cond_prev_limit
            & cond_gap_up
            & cond_vol_expand
            & cond_reseal
            & cond_break_high
            & cond_breakout
        )

        # ════════════════════════════════════════════════
        # 卖出条件
        # ════════════════════════════════════════════════
        # 1. 止损：跌破入场价5%或跌破前日涨停价
        df["stop_loss_price"] = df["close"] * (1 - stop_loss_pct)
        cond_stop_loss = (df["close"] < df["stop_loss_price"]) | (
            df["close"] < df["prev_close"] * 0.97
        )

        # 2. 止盈：涨幅达10%或连续2日滞涨
        df["take_profit_price"] = df["close"] * (1 + take_profit_pct)
        cond_take_profit = df["close"] >= df["take_profit_price"]

        # 3. 时间止损：持仓5日涨幅 < 2%（无进展）
        df["return_5d"] = df["close"].pct_change(periods=time_stop_window)
        cond_time_stop = df["return_5d"] < time_stop_threshold

        # 信号赋值（卖出优先于买入）
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[cond_stop_loss | cond_take_profit | cond_time_stop, "signal"] = -1

        # ── 置信度计算 ────────────────────────────────
        # 基于：涨停强度 + 量能配合 + 突破力度
        limit_score = (df["daily_return"].clip(upper=0.10) / 0.10).clip(0, 1)
        vol_score = (df["vol_ratio"].clip(upper=3.0) / 3.0).clip(0, 1)
        breakout_score = (
            (
                (df["close"] - df["consolidation_high"])
                / df["consolidation_high"].replace(0, np.nan)
            ).clip(0, 0.1)
            / 0.1
        ).clip(0, 1)

        df["confidence"] = (
            (limit_score * 0.4 + vol_score * 0.3 + breakout_score * 0.3).fillna(0.0).clip(0, 1)
        )

        # 无信号时置信度为0
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        return df
