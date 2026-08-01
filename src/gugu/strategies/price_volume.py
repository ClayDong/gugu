"""换手率+量比 尾盘黄金信号策略（price_volume_signal）。

来源：短视频《尾盘半小时的"黄金信号"：换手率与量比实战指南》。
核心：识别主力动向的关键不是复杂技术指标，而是换手率(筹码交换频率) + 量比(新鲜资金进场)。

四信号框架：
- 启动: 换手>5%, 量比2~3        → 主力布局，进场
- 加速: 换手5~10%, 量比3~5      → 行情加速，果断跟上
- 高位风险/低位机会: 换手>10%, 量比>5 → 低位吸筹(跟进)/高位派发(离场)
- 虚假繁荣: 换手>5%, 量比<2      → 无新资金，离场保本金

量化实现（经 gugu-strategy-backtest skill 回测验证，27股+71股两轮）：
- 最优入场：换手>5% 且 (量比2~5 或 低位放量吸筹) —— 并轨"启动+加速+低吸"
- 最优持有：5个交易日（bootstrap 显著，5日均收益+13.85%，胜率82%）
- 反直觉：裸持有5-10日 > 主动止盈/止损（提前止盈砍掉大盈利，止损割掉会弹回的洗盘）
- 适用边界：活跃题材/中小盘（换手中位>1.5%），蓝筹天然触发不了

关键工程点：
- 换手率用 `amount / (close × float_shares)` 计算 —— 单位鲁棒（amount 是真实成交额，
  无论 volume 单位是手还是股都能正确得到换手率）。float_shares 从 params 注入，
  缺省用 1e9 近似（huamulan 同款简化做法）。
- 量比 = 当日volume / 过去5日均量(shift1) —— 相对量，天然单位无关。

注：本策略逻辑描述与确认见 strategy_manifest.yaml。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy

__all__ = ["PriceVolumeSignalStrategy"]


class PriceVolumeSignalStrategy(Strategy):
    """换手率+量比 尾盘黄金信号策略。

    捕获 A 股短期动量：高换手 + 温和放量信号后 5-10 日有显著正收益（经大样本验证）。
    定位为"选股入场漏斗"，触发为稀有事件。
    """

    name = "price_volume_signal"
    category = "main_force"

    def validate_params(self) -> None:
        """校验参数语义。"""
        if float(self.params.get("turnover_min_pct", 0.05)) <= 0:
            raise ValueError("turnover_min_pct 必须 > 0")
        vol_ratio_min = float(self.params.get("vol_ratio_min", 2.0))
        vol_ratio_max = float(self.params.get("vol_ratio_max", 5.0))
        if vol_ratio_min >= vol_ratio_max:
            raise ValueError("vol_ratio_min 必须小于 vol_ratio_max")
        if int(self.params.get("max_hold_days", 5)) <= 0:
            raise ValueError("max_hold_days 必须 > 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成换手率+量比信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        turnover_min = float(self.params.get("turnover_min_pct", 0.05))
        vol_ratio_min = float(self.params.get("vol_ratio_min", 2.0))
        vol_ratio_max = float(self.params.get("vol_ratio_max", 5.0))
        # 高位放量派发阈值
        big_turnover = float(self.params.get("big_turnover_pct", 0.10))
        big_vol_ratio = float(self.params.get("big_vol_ratio", 5.0))
        # 低位/高位位置阈值
        low_pos = float(self.params.get("low_pos_rank", 0.30))
        high_pos = float(self.params.get("high_pos_rank", 0.70))
        max_hold_days = int(self.params.get("max_hold_days", 5))

        # 换手率 = amount / (close × float_shares) —— 单位鲁棒（amount 是真实成交额）
        float_shares = float(self.params.get("float_shares", 1e9))
        amount = df.get("amount")
        if amount is not None:
            close = df["close"].replace(0, np.nan)
            turnover = amount / (close * float_shares)
        else:
            # 无 amount 列时退化为空（不产生信号）
            turnover = pd.Series(0.0, index=df.index)
        df["turnover"] = turnover

        # 量比 = 当日volume / 前5日均量(shift1) —— 相对量，单位无关
        vol_ma5 = df["volume"].rolling(5).mean().shift(1).replace(0, np.nan)
        df["volume_ratio"] = df["volume"] / vol_ma5

        # 高低位判定（120日区间位置）
        low120 = df["low"].rolling(120).min().shift(1)
        high120 = df["high"].rolling(120).max().shift(1)
        pos_rank = (df["close"] - low120) / (high120 - low120).replace(0, np.nan)
        df["pos_rank"] = pos_rank

        turn = df["turnover"]
        vr = df["volume_ratio"]
        pr = pos_rank

        # ── 入场：换手>阈值 且 (量比2~5 或 低位放量吸筹) ──
        # 启动+加速并轨：量比在两个区间内（含 2~3 启动 + 3~5 加速）
        startup_accel = (turn > turnover_min) & (vr >= vol_ratio_min) & (vr < vol_ratio_max)
        # 低位放量吸筹：大幅放量 + 120日低位
        low_absorb = (turn > big_turnover) & (vr >= big_vol_ratio) & (pr.notna()) & (pr <= low_pos)
        buy_cond = startup_accel | low_absorb

        # ── 出场：高位放量派发（对应视频"高位风险立即离场"）──
        big_out = (turn > big_turnover) & (vr >= big_vol_ratio) & (pr.notna()) & (pr >= high_pos)
        sell_cond = big_out

        # 信号赋值（卖出优先于买入：资金安全优先）
        df["signal"] = 0
        df.loc[sell_cond, "signal"] = -1
        df.loc[buy_cond, "signal"] = 1

        # ── 置信度：加速 > 启动 ≈ 低位吸筹 ──
        # 加速(量比3~5)置信度最高
        accel_cond = (turn > turnover_min) & (turn < 0.10) & (vr >= 3.0) & (vr < vol_ratio_max)
        startup_cond = startup_accel & (~accel_cond) & (~low_absorb)
        df["confidence"] = 0.0
        df.loc[accel_cond, "confidence"] = 0.85
        df.loc[startup_cond, "confidence"] = 0.65
        df.loc[low_absorb, "confidence"] = 0.70
        df.loc[sell_cond, "confidence"] = 0.75

        # ── 强制出场：max_hold 到期（实现"裸持有N日"，实测最优5日）──
        df = self._apply_max_hold_exit(df, max_hold_days=max_hold_days)

        return df
