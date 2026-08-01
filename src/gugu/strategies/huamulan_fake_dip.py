"""早盘假摔低吸战法：花木兰捕捉主力洗盘策略。

来源：战神花木兰系列文档整理（视频16，2026-07-04《记住这个信号，不亏！》）

适用场景：个股上升趋势中的正常回调。

原文核心逻辑（视频16 逐字提炼）：
1. 信号识别：开盘半小时快速下杀，跌幅超过 3 个点，但 10:30 前就把跌幅全部收回，
   当天最终收红甚至直接封板。
2. 必要条件：早盘砸盘的时候一定要缩量，成交额必须低于最近 5 天的平均水平。
   如果下跌过程中明显放量，说明真有资金在往外跑，属于真正下跌，这种票不能碰。
3. 买点：假摔结束之后股票当天收红或涨停，第二天一般不会直接暴涨，更多是
   小幅震荡整理为主。调整过程中绝对不能跌破之前那根大跌K线的最低点，
   等它回踩支撑位的时候才是相对舒服、稳妥的买点。不要盲目追高。
4. 纪律：如果第二天走出一根大阴线并且跌破前面的最低点，不管后面有没有反弹
   都必须无条件离场。

与现有策略差异：
- breakout_rebound：涨停后断板反包（短线主力行为）
- huamulan_fake_dip：开盘假摔+缩量收回（洗盘识别）

日线近似（本策略实现口径）：
- 长下影线 > 3%（对应"开盘半小时下杀超3%后收回"，盘中最低点远低于开盘/收盘）
- 收阳（close > open，对应"当天最终收红"）
- 缩量（成交量 < 前5日均量，对应"成交额低于最近5天平均水平"）
- 止损：跌破信号日最低点无条件离场（对应原文纪律）

诚实局限：
- 日线无法还原"开盘半小时下杀 + 10:30前收回"的分时节奏，用长下影线近似
- "缩量"原文指成交额，日线数据用成交量近似（两者高度相关）
- 实盘中需分钟级数据精确判断，本策略定位为全市场扫描的日线筛选版
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class HuamulanFakeDipStrategy(Strategy):
    """早盘假摔低吸战法策略。

    捕捉主力洗盘机会，适合上升趋势中的回调买入。
    核心逻辑：缩量跌是真洗盘，放量跌是真出货。
    """

    name = "huamulan_fake_dip"
    category = "mean_revert"

    def validate_params(self) -> None:
        """校验参数语义。"""
        shadow_min = float(self.params.get("lower_shadow_min_pct", 0.03))
        if shadow_min <= 0:
            raise ValueError("lower_shadow_min_pct 必须大于 0")
        vol_shrink = float(self.params.get("vol_shrink_ratio", 1.0))
        if vol_shrink <= 0:
            raise ValueError("vol_shrink_ratio 必须大于 0")
        stop_loss = float(self.params.get("stop_loss_pct", 0.05))
        if stop_loss <= 0:
            raise ValueError("stop_loss_pct 必须大于 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成假摔低吸信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        lower_shadow_min_pct = float(self.params.get("lower_shadow_min_pct", 0.03))
        vol_shrink_ratio = float(self.params.get("vol_shrink_ratio", 1.0))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.05))
        time_stop_window = int(self.params.get("time_stop_window", 5))
        vol_ma_window = int(self.params.get("vol_ma_window", 5))

        # ── 计算量能指标 ──────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(vol_ma_window).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # ── K线基础属性 ──────────────────────────────
        df["daily_return"] = df["close"].pct_change()
        df["is_yang"] = (df["close"] > df["open"]).astype(bool)

        # 下影线长度 = (min(open, close) - low) / open
        df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["open"].replace(
            0, np.nan
        )

        # ════════════════════════════════════════════════
        # 买入条件（假摔低吸）—— 忠实原文核心 3 条件
        # ════════════════════════════════════════════════
        # 条件1：长下影线（>3%，开盘半小时下杀超3%后收回）
        cond_long_lower = df["lower_shadow"] >= lower_shadow_min_pct

        # 条件2：当天收阳线（close > open，对应"当天最终收红"）
        cond_yang = df["is_yang"]

        # 条件3：缩量（成交量 < 前5日均量，对应"成交额低于最近5天平均水平"）
        cond_shrink = df["vol_ratio"] < vol_shrink_ratio

        # 综合买入信号（3条件全满足，不额外附加原文没有的趋势/前低条件）
        buy_cond = cond_long_lower & cond_yang & cond_shrink

        # ════════════════════════════════════════════════
        # 卖出条件
        # ════════════════════════════════════════════════
        # 信号日最低点（原文："跌破之前那根大跌K线的最低点，无条件离场"）
        signal_low = df["low"].where(buy_cond).ffill()
        # 信号日收盘价 = 买入基准（次日开盘附近执行）
        signal_close = df["close"].where(buy_cond).ffill()
        has_entry = buy_cond.cumsum() > 0  # 已出现过买入信号

        # v0.8.0 修复：破位止损（核心纪律），跌破信号日最低点无条件离场。
        # 仅当持仓中（出现买入信号后）且非信号日当天才触发。
        cond_break_signal_low = (df["low"] < signal_low) & has_entry

        # v0.8.0 修复：固定止损改为基于买入基准（信号日收盘价）× (1-5%)。
        # 旧版 `signal_low × 0.95` 距买入价可达 -13% 以上（追高买入+下影线最低点），
        # 止损形同虚设（实测 -15.7% 亏损仍未被截断）。
        # 花木兰通用纪律："百分之五射直损"（视频15）。
        df["stop_loss_price"] = signal_close * (1 - stop_loss_pct)
        cond_stop_loss = df["close"] < df["stop_loss_price"]

        # 信号赋值（卖出优先于买入；信号日当天不卖出）
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        sell_cond = (cond_break_signal_low | cond_stop_loss) & ~buy_cond & has_entry
        df.loc[sell_cond, "signal"] = -1

        # v0.8.0 修复：时间止损改为 max_hold 机制（持仓 N 日强制平仓），
        # 替代旧版全局 `return_5d < 2%`（该条件在任何回调日都触发，
        # 导致买入后立刻被踢出，是胜率低的元凶）。
        df = self._apply_max_hold_exit(df, max_hold_days=time_stop_window)

        # ── 置信度计算 ────────────────────────────────
        # 基于：下影线长度 + 缩量程度（去掉原文没有的趋势成分）
        shadow_score = (df["lower_shadow"].clip(upper=0.08) / 0.08).clip(0, 1)
        shrink_score = (1 - df["vol_ratio"].clip(upper=1.0)).clip(0, 1)

        df["confidence"] = (shadow_score * 0.6 + shrink_score * 0.4).fillna(0.0).clip(0, 1)

        # 无信号时置信度为0
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        # 清理临时列（保留 low/high/close/open 等原始列）
        df.drop(
            columns=[
                "vol_ma5",
                "vol_ratio",
                "daily_return",
                "is_yang",
                "lower_shadow",
                "signal_low",
                "has_entry",
                "stop_loss_price",
                "return_5d",
            ],
            errors="ignore",
            inplace=True,
        )

        return df
