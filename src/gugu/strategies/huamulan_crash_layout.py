"""空仓等暴跌布局法：花木兰大资金稳健复利策略。

来源：战神花木兰系列文档整理

适用场景：市场恐慌性大跌（如指数单日暴跌3%以上）。

核心逻辑：
1. 等待：不预测底部，耐心等市场情绪宣泄完毕，割肉盘涌出
2. 选股：只选被错杀的优质白马股、行业龙头（业绩稳定、逻辑未变）
3. 分批：资金分3-5份，企稳后分批买入3-5只优质股
4. 兑现：目标收益15%-20%即走，不贪最后一块铜板

与现有策略差异：
- rsi_reversal：RSI超卖反弹（指标驱动）
- huamulan_crash_layout：指数暴跌3%+白马错杀（事件驱动）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class HuamulanCrashLayoutStrategy(Strategy):
    """空仓等暴跌布局法策略。

    大资金稳健复利策略，适合保守型用户。
    核心逻辑：放弃预测，跟随趋势；空仓等暴跌是顶级策略。

    注意：本策略需要大盘指数数据判断暴跌，回测中需传入指数跌幅数据。
    实盘中通过 SignalPipeline 的 MarketFilter 获取大盘状态。

    回测说明：
    - 指数跌幅通过 df["index_return"] 列传入（需外部数据）
    - 优质白马股通过市值前20% + 业绩稳定（近4季净利润>0）近似
    - 估值合理通过 PE < 行业均值近似（回测中用 PE < 30 简化）
    """

    name = "huamulan_crash_layout"
    category = "trend"

    def validate_params(self) -> None:
        """校验参数语义。"""
        crash_threshold = float(self.params.get("crash_threshold", -0.03))
        if crash_threshold > 0:
            raise ValueError("crash_threshold 必须为负数（跌幅）")
        target_gain = float(self.params.get("target_gain", 0.15))
        if target_gain <= 0:
            raise ValueError("target_gain 必须大于 0")
        stop_loss = float(self.params.get("stop_loss_pct", 0.08))
        if stop_loss <= 0:
            raise ValueError("stop_loss_pct 必须大于 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成空仓等暴跌布局信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        crash_threshold = float(self.params.get("crash_threshold", -0.03))
        target_gain = float(self.params.get("target_gain", 0.15))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.08))
        market_cap_min = float(self.params.get("market_cap_min", 200e8))
        pe_max = float(self.params.get("pe_max", 30))
        ma_long_window = int(self.params.get("ma_long_window", 60))
        recovery_days = int(self.params.get("recovery_days", 3))

        # ── 计算均线系统 ──────────────────────────────
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma_long"] = df["close"].rolling(ma_long_window).mean()

        # ── 计算当日涨幅 ──────────────────────────────
        df["daily_return"] = df["close"].pct_change()

        # ── 计算量能指标 ──────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # ── 计算流通市值 ────────────────────────────
        # v0.8.0 修复：优先使用数据层注入的真实流通市值列（腾讯实时行情）
        if "market_cap" in df.columns:
            mc_col = df["market_cap"]
            if isinstance(mc_col, pd.Series) and bool(mc_col.notna().any()):
                df["market_cap"] = mc_col.astype(float)
            else:
                df["market_cap"] = float("nan")  # 无真实数据时市值条件自动放行
        else:
            df["market_cap"] = float("nan")

        # ── 计算PE ──────────────────────────────
        # v0.8.0 修复：优先使用数据层注入的真实 PE-TTM 列（腾讯实时行情）
        if "pe" in df.columns:
            pe_col = df["pe"]
            if isinstance(pe_col, pd.Series) and bool(pe_col.notna().any()):
                df["pe"] = pe_col.astype(float)
            else:
                df["pe"] = float("nan")  # 无真实数据时估值条件自动放行
        else:
            df["pe"] = float("nan")

        # ════════════════════════════════════════════════
        # 买入条件（暴跌布局法）
        # ════════════════════════════════════════════════
        # 条件1：大盘指数单日跌幅 >= 3%（需外部数据，回测中通过 index_return 列传入）
        if "index_return" in df.columns:
            cond_crash = df["index_return"] <= crash_threshold
        else:
            # 无指数数据时，用个股跌幅 >= 5% 近似（个股暴跌）
            cond_crash = df["daily_return"] <= -0.05

        # 条件2：个股为行业龙头（市值前20%，这里简化为市值 > 200亿；无真实数据时放行）
        mc = df["market_cap"]
        cond_large_cap = mc.isna() | (mc >= market_cap_min)

        # 条件3：个股跌幅 > 指数跌幅（被错杀）—— v0.8.0 修复：
        # 错杀判断应在暴跌日（而非买入日）。买入日是企稳反弹日，当日涨幅为正，
        # 不可能满足"个股跌得比指数多"。改为：最近 recovery_days 内存在被错杀的暴跌日。
        if "index_return" in df.columns:
            killed_crash_day = (df["index_return"] <= crash_threshold) & (
                df["daily_return"] < df["index_return"]
            )
        else:
            killed_crash_day = df["daily_return"] < crash_threshold
        cond_killed_more = (
            killed_crash_day.rolling(recovery_days + 1, min_periods=1)
            .max()
            .shift(1)
            .fillna(False)
            .astype(bool)
        )

        # 条件4：估值合理（PE < 30；无真实数据时放行）
        pe_col = df["pe"]
        cond_valuation = pe_col.isna() | (pe_col < pe_max)

        # 条件5：企稳信号：连续N日不再创新低
        df["low_3d"] = df["low"].rolling(recovery_days).min()
        df["is_stable"] = df["low"] >= df["low_3d"].shift(1)

        # 综合买入信号（5条件全满足）
        buy_cond = cond_crash & cond_large_cap & cond_killed_more & cond_valuation & df["is_stable"]

        # ════════════════════════════════════════════════
        # 卖出条件
        # ════════════════════════════════════════════════
        # 1. 止损：跌破入场价8%（用信号日低点为基准，v0.8.0 修复——
        #    旧版 `close × (1-8%)` 用当日 close 算自己永远不成立，形同虚设）
        signal_low = df["low"].where(buy_cond).ffill()
        df["stop_loss_price"] = signal_low * (1 - stop_loss_pct)
        cond_stop_loss = df["close"] < df["stop_loss_price"]

        # 2. 止盈：目标收益15%-20%即走（用信号日低点为基准）
        df["take_profit_price"] = signal_low * (1 + target_gain)
        cond_take_profit = df["close"] >= df["take_profit_price"]

        # 信号赋值（卖出优先于买入；信号日当天不卖出；无持仓时卖出信号不生效）
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        has_entry = buy_cond.cumsum() > 0
        sell_cond = (cond_stop_loss | cond_take_profit) & ~buy_cond & has_entry
        df.loc[sell_cond, "signal"] = -1

        # v0.8.0 修复：时间止损改为 max_hold 机制（持仓 N 日强制平仓），
        # 替代旧版全局 `return_10d < 3%`——该条件在暴跌日必然触发，
        # 导致买入信号被同日卖出覆盖（策略永远买不进去）。
        time_stop_window = int(self.params.get("time_stop_window", 10))
        df = self._apply_max_hold_exit(df, max_hold_days=time_stop_window)

        # ── 置信度计算 ────────────────────────────────
        # 基于：错杀程度 + 市值规模 + 估值安全边际
        kill_score = (abs(df["daily_return"]) / 0.10).clip(0, 1)
        cap_score = ((df["market_cap"] / 1e9).clip(upper=5.0) / 5.0).clip(0, 1)
        pe_score = (1 - (df["pe"] / pe_max).clip(0, 1)).clip(0, 1)

        df["confidence"] = (
            (kill_score * 0.4 + cap_score * 0.3 + pe_score * 0.3).fillna(0.0).clip(0, 1)
        )

        # 无信号时置信度为0
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        return df
