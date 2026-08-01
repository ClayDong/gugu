"""四周期市场模型策略（公众号《八月，来了~~》战法）。

核心认知（原文提炼）：
- 股价站上20日均线形成「银山谷」（MA20上穿MA60）→ 潜伏期，控制仓位耐心等待
- 放量站上60日均线 → 醒悟期，分批加仓滚动操作
- >45度拉升 → 狂热期，大涨大卖获利了结
- 顶背离跌破20日均线形成「死亡谷」（MA20下穿MA60）→ 幻灭期，降低仓位获利了结
- MACD 死叉状态不会有主升浪，主升浪必须建立在 MACD 金叉状态

三个策略：
1. regime_cycle_four —— 四周期识别（潜伏期买入 / 幻灭期卖出）
2. death_valley_rebound —— 幻灭期修复反弹（3日不创新低+KDJ金叉+站上5日线）
3. bollinger_oversold_rebound —— 布林下轨超跌反弹（跌出下轨抢反弹）
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


def _macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9):
    """计算 MACD，返回 (dif, dea, hist)。"""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


def _kdj(df: pd.DataFrame, window: int = 9):
    """计算 KDJ，返回 (k, d)。"""
    low_n = df["low"].rolling(window).min()
    high_n = df["high"].rolling(window).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, 1e-10) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return k, d


class RegimeCycleFourStrategy(Strategy):
    """四周期模型：潜伏期买入（银山谷），幻灭期卖出（死亡谷）。

    原文判据：
    - 买入（潜伏期确认）：站上20日均线 + 银山谷（MA20上穿MA60）+ MACD金叉
    - 卖出（幻灭期确认）：跌破20日均线 + 死亡谷（MA20下穿MA60）+ MACD死叉
    - 狂热期（>45度拉升）：视为持仓信号增强，不单独触发卖出（卖出以幻灭期为准）
    """

    name = "regime_cycle_four"
    category = "trend"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        ma_short = int(self.params["ma_short"])
        ma_long = int(self.params["ma_long"])
        macd_fast = int(self.params["macd_fast"])
        macd_slow = int(self.params["macd_slow"])
        macd_signal = int(self.params["macd_signal"])

        df["ma_short"] = df["close"].rolling(ma_short).mean()
        df["ma_long"] = df["close"].rolling(ma_long).mean()
        dif, dea, _ = _macd(df, macd_fast, macd_slow, macd_signal)
        df["macd_dif"] = dif
        df["macd_dea"] = dea

        # 银山谷：MA20 上穿 MA60（潜伏期开始）
        silver_valley = (df["ma_short"] > df["ma_long"]) & (
            df["ma_short"].shift(1) <= df["ma_long"].shift(1)
        )
        # 死亡谷：MA20 下穿 MA60（幻灭期开始）
        death_valley = (df["ma_short"] < df["ma_long"]) & (
            df["ma_short"].shift(1) >= df["ma_long"].shift(1)
        )

        # MACD 状态（原文语义：主升浪必须建立在 MACD 金叉状态，死叉状态无主升浪）
        macd_golden_state = dif > dea
        macd_dead_state = dif < dea

        # 买入：银山谷（多头排列确立）+ 站上20日线 + MACD 处于金叉状态
        buy_cond = silver_valley & (df["close"] > df["ma_short"]) & macd_golden_state
        # 卖出：死亡谷（空头排列确立）+ 跌破20日线 + MACD 处于死叉状态
        sell_cond = death_valley & (df["close"] < df["ma_short"]) & macd_dead_state

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1

        # 置信度：金叉/死叉的 DIF-DEA 距离归一化
        macd_spread = (dif - dea).abs()
        df["confidence"] = (macd_spread / df["close"] * 100).clip(0, 1)
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df


class DeathValleyReboundStrategy(Strategy):
    """幻灭期修复反弹（短线超跌反弹）。

    原文判据（幻灭期个股）：
    - 只有跌到三天不创新低 + KDJ金叉 + 站上5日均线 → 才会有修复性反弹
    - 卖出：反弹后跌破5日均线 / 持有超过 max_hold_days
    """

    name = "death_valley_rebound"
    category = "mean_revert"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        no_new_low_days = int(self.params["no_new_low_days"])
        kdj_window = int(self.params["kdj_window"])
        ma_fast = int(self.params["ma_fast"])
        ma_trend_window = int(self.params.get("ma_trend_window", 60))
        oversold_drop_pct = float(self.params.get("oversold_drop_pct", 0.10))
        oversold_window = int(self.params.get("oversold_window", 20))

        df["ma_fast"] = df["close"].rolling(ma_fast).mean()
        df["ma_trend"] = df["close"].rolling(ma_trend_window).mean()
        k, d = _kdj(df, kdj_window)
        df["kdj_k"] = k
        df["kdj_d"] = d

        # 三天不创新低：最近 N 日最低点高于前 N 日最低点
        recent_low = df["low"].rolling(no_new_low_days).min()
        prior_low = df["low"].rolling(no_new_low_days).min().shift(no_new_low_days)
        no_new_low = recent_low > prior_low

        # KDJ 金叉状态（原文"KDJ金叉"：超跌后 K>D 即修复动能恢复，用状态而非交叉事件
        # —— 交叉事件在暴跌末段已发生但当时不满足企稳条件，状态判断更符合原文语义）
        kdj_golden = k > d

        # 站上5日均线
        above_ma = df["close"] > df["ma_fast"]

        # 幻灭期前提（原文："处于幻灭期的个股"才有修复反弹）：
        # 幻灭期 = 跌破20日均线 + 死亡谷（MA20<MA60 空头排列）
        # 用 MA5 < MA60 近似空头排列（策略已用 MA5 作快线，避免重复计算 MA20）
        death_valley_state = df["ma_fast"] < df["ma_trend"]

        # 超跌前提：前 oversold_window 日累计跌幅超过阈值（真正跌透才反弹，防半山腰接刀）
        drop = df["close"] / df["close"].shift(oversold_window) - 1
        oversold = drop < -oversold_drop_pct

        buy_cond = no_new_low & kdj_golden & above_ma & death_valley_state & oversold
        # 卖出：跌破5日均线（反弹结束）
        sell_cond = df["close"] < df["ma_fast"]

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1

        # 时间止损（防资金锁死）
        self._apply_max_hold_exit(df)

        # 置信度：KDJ 金叉强度 + 反弹动能
        kdj_strength = ((k - d) / 100).clip(0, 1)
        df["confidence"] = kdj_strength.fillna(0.0)
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df


class BollingerOversoldReboundStrategy(Strategy):
    """布林下轨超跌反弹（短线抢反弹）。

    原文判据：
    - 一路砸盘的话，也是要等到跌出布林下轨线了，才会有报复性反弹
    - 参考创业板指数历史：跌出下轨才有超跌反弹，但反弹很弱，站不上中轨没拐点
    - 因此卖出：反弹到布林中轨 / 持有超过 max_hold_days
    """

    name = "bollinger_oversold_rebound"
    category = "mean_revert"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        window = int(self.params["window"])
        num_std = float(self.params["num_std"])
        exit_mode = str(self.params.get("exit_mode", "mid"))  # mid=中轨卖出 / hold=持有N日
        # 超跌前提（2026-08-01 优化：验证期 p=0.116 未显著 → 加累计跌幅过滤，参考 death_valley 成功经验）
        oversold_drop_pct = float(self.params.get("oversold_drop_pct", 0.0))  # 0=不过滤
        oversold_window = int(self.params.get("oversold_window", 20))

        df["ma"] = df["close"].rolling(window).mean()
        df["std"] = df["close"].rolling(window).std()
        df["upper"] = df["ma"] + num_std * df["std"]
        df["lower"] = df["ma"] - num_std * df["std"]
        df["mid"] = df["ma"]

        # 跌出布林下轨（收盘价低于下轨）
        buy_cond = df["close"] < df["lower"]
        # 超跌前提：前 oversold_window 日累计跌幅超阈值（防普通波动跌破下轨误触发）
        if oversold_drop_pct > 0:
            drop = df["close"] / df["close"].shift(oversold_window) - 1
            buy_cond = buy_cond & (drop < -oversold_drop_pct)

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1

        if exit_mode == "mid":
            # 反弹到中轨卖出（原文：站不上布林中轨线，没有走出拐点——中轨是目标位）
            sell_cond = df["close"] > df["mid"]
            df.loc[sell_cond, "signal"] = -1
            # 时间止损
            self._apply_max_hold_exit(df)
        else:
            # 持有N日固定卖出（裸持有，防过早止盈砍掉大反弹）
            self._apply_max_hold_exit(df, max_hold_days=int(self.params["max_hold_days"]))

        # 置信度：跌破下轨的深度
        band_width = (df["upper"] - df["lower"]).clip(lower=df["close"].abs() * 0.01).replace(0, 1)
        oversold_depth = ((df["lower"] - df["close"]).clip(lower=0) / band_width).clip(0, 1)
        df["confidence"] = (0.5 + oversold_depth * 0.5).clip(0, 1)
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df
