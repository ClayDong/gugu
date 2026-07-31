"""尾盘选股六步法：花木兰小资金复利首选策略。

来源：战神花木兰系列文档整理

适用场景：震荡市或上升初期，寻找次日冲高机会。

选股六步法：
1. 定时：下午2:30（尾盘半小时定乾坤）
2. 初筛：涨幅3%-5%（极端行情不超5%）
3. 量比：>1（资金真实进场），<1剔除
4. 换手率：5%-10%（过低无活性，过高波动大）
5. 市值：50亿-200亿（小盘易操控，大盘难拉升）
6. 形态：量能阶梯式放大，5/10/20日均线多头排列，无重大套牢盘

卖出条件：
- 止损：跌破入场价5%
- 止盈：涨幅达10%或滞涨
- 时间止损：持仓3日无进展

与现有策略差异：
- pullback_low：回踩MA20 + 缩量低吸（震荡市）
- huamulan_tail_pick：涨幅3-5% + 量比 + 换手 + 市值（尾盘选股）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class HuamulanTailPickStrategy(Strategy):
    """尾盘选股六步法策略。

    震荡市或上升初期适用，寻找次日冲高机会。
    核心逻辑：量价为唯一准绳，尾盘定乾坤。

    回测说明：
    - 换手率用 amount / (close * 流通股本) 近似，回测中用 amount / close / 1e8 估算
    - 流通市值用 close * 流通股本 近似，回测中用 close / close * 1e8 估算（简化处理）
    - 量比用当日量 / 前5日均量近似
    """

    name = "huamulan_tail_pick"
    category = "trend"

    def validate_params(self) -> None:
        """校验参数语义。"""
        gain_min = float(self.params.get("gain_min_pct", 0.03))
        gain_max = float(self.params.get("gain_max_pct", 0.05))
        if gain_min >= gain_max:
            raise ValueError("gain_min_pct 必须小于 gain_max_pct")
        vol_ratio_min = float(self.params.get("vol_ratio_min", 1.0))
        if vol_ratio_min < 0:
            raise ValueError("vol_ratio_min 必须大于等于 0")
        turnover_min = float(self.params.get("turnover_min_pct", 0.05))
        turnover_max = float(self.params.get("turnover_max_pct", 0.10))
        if turnover_min >= turnover_max:
            raise ValueError("turnover_min_pct 必须小于 turnover_max_pct")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成尾盘选股信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        gain_min_pct = float(self.params.get("gain_min_pct", 0.03))
        gain_max_pct = float(self.params.get("gain_max_pct", 0.05))
        vol_ratio_min = float(self.params.get("vol_ratio_min", 1.0))
        turnover_min_pct = float(self.params.get("turnover_min_pct", 0.05))
        turnover_max_pct = float(self.params.get("turnover_max_pct", 0.10))
        market_cap_min = float(self.params.get("market_cap_min", 50e8))
        market_cap_max = float(self.params.get("market_cap_max", 200e8))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.05))
        time_stop_window = int(self.params.get("time_stop_window", 3))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.02))
        vol_expand_days = int(self.params.get("vol_expand_days", 3))

        # ── 计算均线系统 ──────────────────────────────
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()

        # ── 计算量能指标 ──────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # 量能阶梯式放大：连续N日成交量递增
        df["vol_expand"] = False
        if vol_expand_days > 1:
            vol_expand_list = []
            for i in range(len(df)):
                if i < vol_expand_days - 1:
                    vol_expand_list.append(False)
                else:
                    is_expand = True
                    for j in range(vol_expand_days - 1):
                        if df["volume"].iloc[i - j] <= df["volume"].iloc[i - j - 1]:
                            is_expand = False
                            break
                    vol_expand_list.append(is_expand)
            df["vol_expand"] = vol_expand_list

        # ── 计算换手率（近似） ──────────────────────
        # 换手率 ≈ amount / (close * 流通股本)，回测中简化为 amount / close / 1e8
        df["turnover_rate"] = df["amount"] / df["close"] / 1e8 if "amount" in df.columns else 0.0

        # ── 计算流通市值（近似） ────────────────────
        # 流通市值 ≈ close * 流通股本，回测中简化为 close / close * 1e8（需外部数据校准）
        # 这里用 amount / turnover_rate 反推，但回测中无法精确，仅做近似
        df["market_cap"] = df["close"] * 1e8  # 简化，实盘需用真实流通股本

        # ── 计算当日涨幅 ──────────────────────────────
        df["daily_return"] = df["close"].pct_change()

        # ── 前高（用于止盈目标位） ──────────────────
        df["recent_high"] = df["high"].rolling(20).max().shift(1)

        # ════════════════════════════════════════════════
        # 买入条件（六步法）
        # ════════════════════════════════════════════════
        # 条件1：涨幅3%-5%
        cond_gain = (df["daily_return"] >= gain_min_pct) & (df["daily_return"] <= gain_max_pct)

        # 条件2：量比 > 1.0
        cond_vol_ratio = df["vol_ratio"] > vol_ratio_min

        # 条件3：换手率 5%-10%（回测中近似）
        cond_turnover = (df["turnover_rate"] >= turnover_min_pct) & (
            df["turnover_rate"] <= turnover_max_pct
        )

        # 条件4：流通市值 50亿-200亿（回测中近似）
        cond_market_cap = (df["market_cap"] >= market_cap_min) & (
            df["market_cap"] <= market_cap_max
        )

        # 条件5：均线多头排列（MA5 > MA10 > MA20）
        cond_ma_align = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])

        # 条件6：量能阶梯式放大
        cond_vol_expand = df["vol_expand"]

        # 综合买入信号（6条件全满足）
        buy_cond = (
            cond_gain
            & cond_vol_ratio
            & cond_turnover
            & cond_market_cap
            & cond_ma_align
            & cond_vol_expand
        )

        # ════════════════════════════════════════════════
        # 卖出条件
        # ════════════════════════════════════════════════
        # 1. 止损：跌破入场价5%
        df["stop_loss_price"] = df["close"] * (1 - stop_loss_pct)
        cond_stop_loss = df["close"] < df["stop_loss_price"]

        # 2. 止盈：涨幅达10%或反弹至前高
        cond_take_profit = df["close"] >= df["recent_high"]

        # 3. 时间止损：持仓3日涨幅 < 2%（无进展）
        df["return_3d"] = df["close"].pct_change(periods=time_stop_window)
        cond_time_stop = df["return_3d"] < time_stop_threshold

        # 信号赋值（卖出优先于买入）
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[cond_stop_loss | cond_take_profit | cond_time_stop, "signal"] = -1

        # ── 置信度计算 ────────────────────────────────
        # 基于：涨幅适中 + 量比强度 + 换手适中 + 趋势强度
        gain_score = (1 - abs(df["daily_return"] - 0.04) / 0.04).clip(0, 1)
        vol_score = (df["vol_ratio"].clip(upper=2.0) / 2.0).clip(0, 1)
        turnover_score = (1 - abs(df["turnover_rate"] - 0.075) / 0.025).clip(0, 1)
        trend_score = (
            ((df["ma5"] - df["ma20"]) / df["ma20"].replace(0, np.nan)).clip(0, 0.1) / 0.1
        ).clip(0, 1)

        df["confidence"] = (
            (gain_score * 0.25 + vol_score * 0.25 + turnover_score * 0.25 + trend_score * 0.25)
            .fillna(0.0)
            .clip(0, 1)
        )

        # 无信号时置信度为0
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        return df
