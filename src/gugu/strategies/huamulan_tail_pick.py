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
        take_profit_pct = float(self.params.get("take_profit_pct", 0.10))
        time_stop_window = int(self.params.get("time_stop_window", 3))
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

        # ── 计算换手率 ──────────────────────────────
        # v0.8.0 修复：优先使用数据层注入的真实换手率列（腾讯实时行情），
        # 回退到 amount 近似（amount 为真实成交额时 ≈ 换手额/流通市值）
        if "turnover_rate" in df.columns:
            tr_col = df["turnover_rate"]
            if isinstance(tr_col, pd.Series) and bool(tr_col.notna().any()):
                df["turnover_rate"] = tr_col.astype(float)
            else:
                df["turnover_rate"] = 0.0
        elif "amount" in df.columns:
            # 真实流通市值注入时：换手率 = amount / market_cap
            if "market_cap" in df.columns:
                mc_col = df["market_cap"]
                if isinstance(mc_col, pd.Series) and bool(mc_col.notna().any()):
                    df["turnover_rate"] = df["amount"] / mc_col.replace(0, np.nan)
                else:
                    df["turnover_rate"] = 0.0
            else:
                # 兜底近似（数据层未注入时）——不再用 close*1e8 假市值
                df["turnover_rate"] = 0.0
        else:
            df["turnover_rate"] = 0.0

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

        # 条件4：流通市值 50亿-200亿（无真实市值数据时自动放行）
        mc = df["market_cap"]
        cond_market_cap = mc.isna() | ((mc >= market_cap_min) & (mc <= market_cap_max))

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
        # 1. 止损：跌破入场价5%（用信号日低点为基准，v0.8.0 修复——
        #    旧版 `close × (1-5%)` 用当日 close 算自己永远不成立，形同虚设）
        signal_low = df["low"].where(buy_cond).ffill()
        df["stop_loss_price"] = signal_low * (1 - stop_loss_pct)
        cond_stop_loss = df["close"] < df["stop_loss_price"]

        # 2. 止盈：涨幅达10%（用信号日低点为基准）或滞涨（上影线>5%）
        df["take_profit_price"] = signal_low * (1 + take_profit_pct)
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(
            0, np.nan
        )
        cond_take_profit = (df["close"] >= df["take_profit_price"]) | (df["upper_shadow"] > 0.05)

        # 信号赋值（卖出优先于买入；信号日当天不卖出；无持仓时卖出信号不生效）
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        has_entry = buy_cond.cumsum() > 0
        sell_cond = (cond_stop_loss | cond_take_profit) & ~buy_cond & has_entry
        df.loc[sell_cond, "signal"] = -1

        # v0.8.0 修复：时间止损改为 max_hold 机制（持仓 N 日强制平仓），
        # 替代旧版全局 `return_3d < 2%`（该条件在任何回调日都触发，
        # 买入信号易被同日卖出覆盖）。
        df = self._apply_max_hold_exit(df, max_hold_days=time_stop_window)

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
