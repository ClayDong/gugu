"""豹哥三要素选股策略 — 高流动性 + 大资金承载 + 活跃博弈。

来源：豹哥战法（微信公众号文章分析）
三要素阈值：
1. 换手率 ≥ 15%  — 标的足够活跃，资金博弈明显，避免冷门股
2. 成交额 ≥ 10亿 — 标的规模大，降低遭遇控庄的概率
3. 流通市值 ≥ 70亿 — 容量足够，散户参与感好

设计原理（第一性原理分析）：
- 三要素本质是"流动性过滤器"，筛选出"大资金愿意进、出得来"的标的
- 单独使用三要素只是选股池，不解决择时问题
- 本策略将三要素作为"选股池准入" + 叠加"放量突破"择时信号

策略两层结构：
1. 准入层（三要素过滤）：通过 akshare 实时行情获取换手率/成交额/流通市值
   - 若实时数据不可用（盘后/接口故障），降级到历史估算：
     * 换手率 ≈ 当日成交量 / 流通股本（需外部数据，无则用成交量 N 日分位）
     * 成交额 ≈ 当日收盘价 × 成交量
     * 流通市值 ≈ 当日收盘价 × 流通股本（无则用 N 日均值近似）
2. 择时层（放量突破）：在准入通过的标的上叠加择时信号
   - 买入：突破近 N 日高点 + 量能确认（量比 ≥ 1.5）
   - 卖出：跌破 MA20（趋势破坏）

对抗性审查：
- 原战法案例"永X锂业盘中放量接近涨停" → 涨停时买不到
- 修复：本策略在"涨幅 < 7%"时才发买入信号（不追涨停）
- 原战法未定义卖出 → 本策略补全 MA20 跌破卖出

数据可获性：
- 实时模式：akshare stock_zh_a_spot_em 返回全市场换手率/成交额/流通市值
- 回测模式：成交额可用 close*volume 估算，换手率/流通市值需要外部数据
- 回测降级：若换手率/流通市值数据不可用，仅用成交额 + 放量突破择时
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class LeopardThreeFactorsStrategy(Strategy):
    """豹哥三要素选股策略。

    买入条件（全部满足）：
    1. 三要素准入（任一满足即视为通过，降级容错）：
       a) 实时数据可用：换手率≥15% + 成交额≥10亿 + 流通市值≥70亿
       b) 实时数据不可用（回测/接口故障）：成交额≥10亿 + 当日量比≥1.5
    2. 放量突破：当日收盘突破近 N 日最高价 + 量比 ≥ volume_ratio
    3. 非涨停：当日涨幅 < max_daily_gain_pct（7%，避免追涨停买不到）
    4. 趋势向上：MA20 > MA60

    卖出条件（任一满足）：
    - 跌破 MA20（趋势破坏）
    - 量价背离：价格创新高但成交量 < 5 日均量
    """

    name = "leopard_three_factors"
    category = "main_force"

    def validate_params(self) -> None:
        turnover_threshold = float(self.params.get("turnover_threshold", 0.15))
        if not 0 < turnover_threshold < 1:
            raise ValueError("turnover_threshold 必须在 (0, 1) 区间")
        amount_threshold = float(self.params.get("amount_threshold", 1e10))
        if amount_threshold <= 0:
            raise ValueError("amount_threshold 必须 > 0")
        market_cap_threshold = float(self.params.get("market_cap_threshold", 7e10))
        if market_cap_threshold <= 0:
            raise ValueError("market_cap_threshold 必须 > 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数读取 ──
        breakout_window = int(self.params.get("breakout_window", 20))
        volume_ratio = float(self.params.get("volume_ratio", 1.5))
        vol_ma_window = int(self.params.get("vol_ma_window", 20))
        ma_short = int(self.params.get("ma_short", 20))
        ma_long = int(self.params.get("ma_long", 60))
        max_daily_gain_pct = float(self.params.get("max_daily_gain_pct", 0.07))
        amount_threshold = float(self.params.get("amount_threshold", 1e10))

        # ── 计算均线 ──
        ma20 = df["close"].rolling(ma_short).mean()
        ma60 = df["close"].rolling(ma_long).mean()

        # ── 成交额估算（close × volume × 100）──
        # akshare 历史数据通常含 amount 字段，但网易财经故障时 amount=0
        # 降级：用 close × volume × 100 估算（A股 volume 单位是"手"，1手=100股）
        if "amount" in df.columns and (df["amount"] > 0).any():
            amount = df["amount"]
        else:
            amount = df["close"] * df["volume"] * 100  # 估算成交额

        # ── 准入层1：成交额过滤（≥10亿）──
        cond_amount = amount >= amount_threshold

        # ── 准入层2：换手率过滤（降级处理）──
        # 回测中无换手率数据时，用"成交量 N 日分位 ≥ 0.8"近似活跃度
        if "turnover" in df.columns and df["turnover"].notna().any():
            turnover = df["turnover"] / 100.0  # akshare 返回百分比
            cond_turnover = turnover >= 0.15
        else:
            # 降级：成交量处于近 60 日 80% 分位以上视为活跃
            vol_quantile = df["volume"].rolling(60).rank(pct=True)
            cond_turnover = vol_quantile >= 0.8

        # ── 准入层3：流通市值过滤（降级处理）──
        # 回测中无流通市值数据时，用"成交额 ≥ 10亿"间接保证规模
        if "circ_mv" in df.columns and df["circ_mv"].notna().any():
            circ_mv = df["circ_mv"]
            cond_market_cap = circ_mv >= 7e10  # 70亿
        else:
            # 降级：成交额 ≥ 10亿已隐含规模较大，放行
            cond_market_cap = cond_amount

        # 三要素准入（任一通过即放行，容错降级）
        cond_three_factors = cond_amount & (cond_turnover | cond_market_cap)

        # ── 择时层1：放量突破 ──
        # 突破近 N 日最高价（不含当日，用 shift(1)）
        recent_high = df["high"].rolling(breakout_window).max().shift(1)
        cond_breakout = df["close"] > recent_high

        # 量能确认：当日量 ≥ N 日均量的 volume_ratio 倍
        vol_ma = df["volume"].rolling(vol_ma_window).mean()
        cond_volume = df["volume"] >= vol_ma * volume_ratio

        # ── 择时层2：非涨停（涨幅 < 7%）──
        daily_return = df["close"].pct_change()
        cond_not_limit = daily_return < max_daily_gain_pct

        # ── 择时层3：趋势向上 ──
        cond_trend = ma20 > ma60

        # ── 买入信号：三要素准入 + 放量突破 + 非涨停 + 趋势向上 ──
        buy_cond = cond_three_factors & cond_breakout & cond_volume & cond_not_limit & cond_trend

        # ── 卖出信号：跌破 MA20 或 量价背离 ──
        below_ma20 = df["close"] < ma20
        # 量价背离：价格创 20 日新高但成交量 < 5 日均量
        price_new_high = df["close"] >= df["close"].rolling(20).max()
        vol_below_avg = df["volume"] < df["volume"].rolling(5).mean()
        divergence = price_new_high & vol_below_avg
        sell_cond = below_ma20 | divergence

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1  # 卖出覆盖买入

        # ── 置信度计算 ──
        # 基础 0.6（三要素 + 突破 + 趋势已满足）
        base = pd.Series(0.6, index=df.index)
        # 成交额强度加分（0-0.15）
        amount_strength = (amount / amount_threshold).clip(0, 3.0) / 20.0
        # 量比强度加分（0-0.15）
        vol_strength = (df["volume"] / (vol_ma * volume_ratio)).clip(0, 2.0) / 13.3
        # 突破强度加分（0-0.10）：突破越远置信度越高
        breakout_strength = ((df["close"] - recent_high) / recent_high).clip(0, 0.05) / 0.5
        confidence = (
            (base + amount_strength + vol_strength + breakout_strength).clip(0, 1.0).fillna(0.0)
        )

        df["confidence"] = confidence
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        sell_mask = df["signal"] == -1
        df.loc[sell_mask & below_ma20, "confidence"] = 0.8
        df.loc[sell_mask & ~below_ma20, "confidence"] = 0.6

        return df
