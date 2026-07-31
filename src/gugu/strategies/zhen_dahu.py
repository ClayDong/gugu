"""珍大户投资策略系统 —— AI 量化决策引擎。

核心逻辑：
1. 行业筛选：景气度向上 + 有进入壁垒 + 非夕阳行业
2. 护城河识别：物理垄断 / 网络效应 / 品牌知识产权 / 规模经济
3. 估值判断：PE < 15 极度低估 / PB < 1.0 抄底 / 股息率 > 6% 极佳
4. 筹码结构：集中度 < 15% 高度控盘 / 价格区间位置
5. 技术面确认：均线多头 + MACD金叉 + 量能配合
6. 综合评分：5维加权（行业0.15 + 护城河0.25 + 估值0.30 + 筹码0.15 + 技术0.15）
7. 卖出规则：估值泡沫 / 筹码松动 / 基本面恶化 / 技术破位 / 换股机会
8. 仓位管理：单股≤20% / 单行业≤40% / 分档建仓 / 轮动退出

数据约定：
- 基础列：date, open, high, low, close, volume, amount
- 扩展列（可选，由上游填充）：pe, pb, dividend_yield, chip_concentration,
  moat_type, moat_score, industry_outlook, industry_barrier
- 缺失扩展列时，策略退化为纯技术+估值信号（置信度降低）
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class ZhenDahuStrategy(Strategy):
    """珍大户价值投资策略。

    核心理念：
    - 价值回归：价格围绕价值波动，发现低估资产等待回归
    - 行业优先：先选景气行业，再选个股
    - 护城河：投资壁垒决定长期回报
    - 安全边际：留余地，不加杠杆，闲钱投资
    """

    name = "zhen_dahu"
    category = "value"

    # 护城河类型枚举
    MOAT_PHYSICAL = "physical"
    MOAT_NETWORK = "network"
    MOAT_BRAND = "brand"
    MOAT_SCALE = "scale"

    def validate_params(self) -> None:
        """校验参数语义。"""
        pe_undervalued = float(self.params.get("pe_undervalued", 15))
        if pe_undervalued < 5 or pe_undervalued > 30:
            raise ValueError("pe_undervalued 应在 5-30 之间")
        pe_overvalued = float(self.params.get("pe_overvalued", 25))
        if pe_overvalued < 15 or pe_overvalued > 60:
            raise ValueError("pe_overvalued 应在 15-60 之间")
        pb_bargain = float(self.params.get("pb_bargain", 1.0))
        if pb_bargain < 0.3 or pb_bargain > 3.0:
            raise ValueError("pb_bargain 应在 0.3-3.0 之间")
        dividend_excellent = float(self.params.get("dividend_excellent", 0.06))
        if not 0.02 <= dividend_excellent <= 0.15:
            raise ValueError("dividend_excellent 应在 0.02-0.15 之间")
        chip_high_concentration = float(self.params.get("chip_high_concentration", 0.15))
        if not 0.05 <= chip_high_concentration <= 0.40:
            raise ValueError("chip_high_concentration 应在 0.05-0.40 之间")
        max_single_position = float(self.params.get("max_single_position", 0.20))
        if not 0.10 <= max_single_position <= 0.40:
            raise ValueError("max_single_position 应在 0.10-0.40 之间")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成珍大户价值投资信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        pe_undervalued = float(self.params.get("pe_undervalued", 15))
        pe_overvalued = float(self.params.get("pe_overvalued", 25))
        pb_bargain = float(self.params.get("pb_bargain", 1.0))
        pb_high = float(self.params.get("pb_high", 2.0))
        dividend_excellent = float(self.params.get("dividend_excellent", 0.06))
        dividend_poor = float(self.params.get("dividend_poor", 0.02))
        chip_high_concentration = float(self.params.get("chip_high_concentration", 0.15))
        chip_low_concentration = float(self.params.get("chip_low_concentration", 0.30))
        max_single_position = float(self.params.get("max_single_position", 0.20))
        first_lot_pct = float(self.params.get("first_lot_pct", 0.30))
        ma_short = int(self.params.get("ma_short", 5))
        ma_mid = int(self.params.get("ma_mid", 10))
        ma_long = int(self.params.get("ma_long", 20))
        ma_very_long = int(self.params.get("ma_very_long", 60))
        macd_fast = int(self.params.get("macd_fast", 12))
        macd_slow = int(self.params.get("macd_slow", 26))
        macd_signal = int(self.params.get("macd_signal", 9))
        vol_ma_window = int(self.params.get("vol_ma_window", 5))
        pe_bubble_buffer = float(self.params.get("pe_bubble_buffer", 0.20))

        # ── 基础技术指标 ──────────────────────────────────
        df["ma5"] = df["close"].rolling(ma_short, min_periods=1).mean()
        df["ma10"] = df["close"].rolling(ma_mid, min_periods=1).mean()
        df["ma20"] = df["close"].rolling(ma_long, min_periods=1).mean()
        df["ma60"] = df["close"].rolling(ma_very_long, min_periods=1).mean()
        df["vol_ma"] = df["volume"].rolling(vol_ma_window, min_periods=1).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, 1)

        # MACD
        ema_fast = df["close"].ewm(span=macd_fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=macd_slow, adjust=False).mean()
        df["dif"] = ema_fast - ema_slow
        df["dea"] = df["dif"].ewm(span=macd_signal, adjust=False).mean()
        df["macd_hist"] = 2 * (df["dif"] - df["dea"])

        # ── 扩展数据可用性检查 ─────────────────────────────
        has_fundamental = "pe" in df.columns and "pb" in df.columns
        has_dividend = "dividend_yield" in df.columns
        has_chip = "chip_concentration" in df.columns
        has_moat = "moat_type" in df.columns or "moat_score" in df.columns
        has_industry = "industry_outlook" in df.columns

        # ── 第一步：行业筛选评分（0-10分）──────────────────
        df["industry_score"] = 5.0  # 默认中性
        if has_industry:
            # industry_outlook: 1=景气向上, 0=中性, -1=下行
            outlook = df["industry_outlook"].fillna(0)
            barrier = df.get("industry_barrier", pd.Series(0, index=df.index)).fillna(0)
            # 景气向上 +2, 有壁垒 +1, 下行 -2
            df["industry_score"] = 5.0 + outlook * 2.0 + barrier * 1.0
            df["industry_score"] = df["industry_score"].clip(0, 10)

        # 行业筛选通过：评分 >= 6
        df["industry_pass"] = df["industry_score"] >= 6.0

        # ── 第二步：护城河评分（0-10分）──────────────────
        df["moat_score_calc"] = 5.0  # 默认中性
        if has_moat:
            if "moat_score" in df.columns:
                # 直接使用外部评分（0-10）
                df["moat_score_calc"] = df["moat_score"].fillna(5.0).clip(0, 10)
            elif "moat_type" in df.columns:
                # 根据护城河类型评分
                moat_map = {
                    self.MOAT_PHYSICAL: 9.0,  # 物理垄断最硬
                    self.MOAT_NETWORK: 8.0,  # 网络效应
                    self.MOAT_BRAND: 7.5,  # 品牌知识产权
                    self.MOAT_SCALE: 7.0,  # 规模经济
                }
                df["moat_score_calc"] = df["moat_type"].map(moat_map).fillna(5.0)
        else:
            # 无护城河数据时，用ROE近似（如有）
            if "roe" in df.columns:
                roe = df["roe"].fillna(0)
                df["moat_score_calc"] = (roe * 30).clip(0, 10)  # ROE 20% → 6分

        # 护城河通过：评分 >= 6
        df["moat_pass"] = df["moat_score_calc"] >= 6.0

        # ── 第三步：估值评分（0-10分）──────────────────
        df["valuation_score"] = 5.0  # 默认中性
        df["pe_signal"] = 0  # -1=高估, 0=合理, 1=低估
        df["pb_signal"] = 0
        df["dividend_signal"] = 0

        if has_fundamental:
            pe = df["pe"].fillna(0)
            pb = df["pb"].fillna(0)

            # PE 评分（非周期股）
            pe_score = pd.Series(5.0, index=df.index)
            pe_score = pe_score.where(pe <= 0, 10.0 - (pe - pe_undervalued) * 0.5)
            pe_score = pe_score.clip(0, 10)
            # PE < 15 → 极度低估信号
            df.loc[pe > 0, "pe_signal"] = (pe[pe > 0] < pe_undervalued).astype(int) - (
                pe[pe > 0] > pe_overvalued
            ).astype(int)

            # PB 评分
            pb_score = pd.Series(5.0, index=df.index)
            pb_score = pb_score.where(pb <= 0, 10.0 - (pb - pb_bargain) * 3.0)
            pb_score = pb_score.clip(0, 10)
            df.loc[pb > 0, "pb_signal"] = (pb[pb > 0] < pb_bargain).astype(int) - (
                pb[pb > 0] > pb_high
            ).astype(int)

            # 综合估值评分（PE 权重 0.5 + PB 权重 0.3 + 股息率 0.2）
            if has_dividend:
                div = df["dividend_yield"].fillna(0)
                div_score = pd.Series(5.0, index=df.index)
                div_score = div_score.where(div <= 0, 5.0 + (div - dividend_poor) * 50)
                div_score = div_score.clip(0, 10)
                df.loc[div > 0, "dividend_signal"] = (div[div > 0] > dividend_excellent).astype(int)
                df["valuation_score"] = pe_score * 0.5 + pb_score * 0.3 + div_score * 0.2
            else:
                df["valuation_score"] = pe_score * 0.6 + pb_score * 0.4
        else:
            # 无基本面数据时，用价格位置近似估值
            price_vs_ma60 = df["close"] / df["ma60"].replace(0, 1) - 1
            # 价格相对MA60低估 → 估值分高
            df["valuation_score"] = (5.0 - price_vs_ma60 * 20).clip(0, 10)

        # 估值通过：评分 >= 6
        df["valuation_pass"] = df["valuation_score"] >= 6.0

        # ── 第四步：筹码结构评分（0-10分）──────────────────
        df["chip_score"] = 5.0  # 默认中性
        df["chip_position"] = 0.5  # 默认中段

        if has_chip:
            chip = df["chip_concentration"].fillna(0.25)
            # 集中度越低越好（主力控盘）
            chip_score = pd.Series(5.0, index=df.index)
            chip_score = 10.0 - (chip - chip_high_concentration) * 20
            chip_score = chip_score.clip(0, 10)
            df["chip_score"] = chip_score

            # 价格区间位置（如有90%筹码区间数据）
            if "chip_price_low" in df.columns and "chip_price_high" in df.columns:
                low = df["chip_price_low"]
                high = df["chip_price_high"]
                span = high - low
                span_safe = span.replace(0, 1)
                df["chip_position"] = ((df["close"] - low) / span_safe).clip(0, 1)

        # 筹码通过：评分 >= 6
        df["chip_pass"] = df["chip_score"] >= 6.0

        # ── 第五步：技术面评分（0-10分）──────────────────
        df["technical_score"] = 5.0  # 默认中性

        # 均线多头排列：MA5 > MA10 > MA20 > MA60
        bull_align = (
            (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"]) & (df["ma20"] > df["ma60"])
        )
        # 均线空头排列
        bear_align = (
            (df["ma5"] < df["ma10"]) & (df["ma10"] < df["ma20"]) & (df["ma20"] < df["ma60"])
        )

        # MACD 金叉 / 死叉
        macd_golden = (df["dif"] > df["dea"]) & (df["dif"].shift(1) <= df["dea"].shift(1))
        macd_death = (df["dif"] < df["dea"]) & (df["dif"].shift(1) >= df["dea"].shift(1))
        # DIF 在零轴上方
        dif_above_zero = df["dif"] > 0

        # 量能配合：温和放大或缩量不破支撑
        vol_expand = df["vol_ratio"] > 1.2
        vol_shrink_healthy = (df["vol_ratio"] < 0.8) & (df["close"] > df["ma20"])

        # 股价站上MA5/MA10/MA20
        above_mas = (
            (df["close"] > df["ma5"]) & (df["close"] > df["ma10"]) & (df["close"] > df["ma20"])
        )

        # 上升趋势：低点逐步抬高（用MA20斜率近似）
        ma20_slope = df["ma20"] - df["ma20"].shift(5)
        uptrend = ma20_slope > 0

        # 技术面综合评分
        tech_score = pd.Series(5.0, index=df.index)
        tech_score = tech_score + bull_align * 2.0  # 多头排列 +2
        tech_score = tech_score - bear_align * 2.0  # 空头排列 -2
        tech_score = tech_score + macd_golden * 1.5  # MACD金叉 +1.5
        tech_score = tech_score - macd_death * 1.5  # MACD死叉 -1.5
        tech_score = tech_score + dif_above_zero * 1.0  # DIF零轴上方 +1
        tech_score = tech_score + vol_expand * 0.5  # 放量 +0.5
        tech_score = tech_score + vol_shrink_healthy * 0.5  # 健康缩量 +0.5
        tech_score = tech_score + above_mas * 1.0  # 站上均线 +1
        tech_score = tech_score + uptrend * 1.0  # 上升趋势 +1
        df["technical_score"] = tech_score.clip(0, 10)

        # 技术面通过：评分 >= 6
        df["technical_pass"] = df["technical_score"] >= 6.0

        # ── 综合评分（加权）──────────────────────────────────
        # 权重：行业0.15 + 护城河0.25 + 估值0.30 + 筹码0.15 + 技术0.15
        df["composite_score"] = (
            df["industry_score"] * 0.15
            + df["moat_score_calc"] * 0.25
            + df["valuation_score"] * 0.30
            + df["chip_score"] * 0.15
            + df["technical_score"] * 0.15
        )

        # ── 买入信号判定 ──────────────────────────────────
        # 核心规则：行业通过 AND 护城河满足 AND 估值合理 AND 技术面≥2项
        # 信号强度：5项全满足=强烈买入 / 前4项=分批建仓 / 前3项不满足=不买入

        # 技术面至少满足2项
        tech_conditions = (
            bull_align.astype(int)
            + dif_above_zero.astype(int)
            + (vol_expand | vol_shrink_healthy).astype(int)
            + uptrend.astype(int)
        )
        tech_2plus = tech_conditions >= 2

        # 买入信号
        df["signal"] = 0
        buy_condition = df["industry_pass"] & df["moat_pass"] & df["valuation_pass"] & tech_2plus
        df.loc[buy_condition, "signal"] = 1

        # ── 卖出信号判定 ──────────────────────────────────
        # 信号1：估值泡沫（PE > 行业均值+20%）
        # 信号2：筹码松动（集中度从<15%升至>20%）
        # 信号3：基本面恶化（行业景气度下行）
        # 信号4：技术面走坏（跌破MA60且MA60拐头向下）
        # 信号5：更好的机会（由外部逻辑处理）

        # 技术面走坏：跌破MA60 + MA60拐头向下
        ma60_slope = df["ma60"] - df["ma60"].shift(5)
        below_ma60 = df["close"] < df["ma60"]
        ma60_turning_down = ma60_slope < 0
        tech_breakdown = below_ma60 & ma60_turning_down

        # 估值泡沫（PE > overvalued）
        if has_fundamental:
            pe = df["pe"].fillna(0)
            valuation_bubble = pe > pe_overvalued * (1 + pe_bubble_buffer)
        else:
            valuation_bubble = pd.Series(False, index=df.index)

        # 基本面恶化（行业景气度下行）
        fundamental_deterioration = df["industry_score"] < 3.0

        # 筹码松动（集中度扩大）
        if has_chip:
            chip = df["chip_concentration"].fillna(0.25)
            chip_loosening = chip > chip_low_concentration
        else:
            chip_loosening = pd.Series(False, index=df.index)

        # 综合卖出信号
        sell_condition = (
            tech_breakdown | valuation_bubble | fundamental_deterioration | chip_loosening
        )
        df.loc[sell_condition & (df["signal"] != 1), "signal"] = -1

        # ── 置信度计算 ──────────────────────────────────
        df["confidence"] = 0.0

        # 买入置信度：基于综合评分
        buy_confidence = (
            (df["composite_score"] / 10.0) * 0.6  # 综合评分贡献60%
            + (df["moat_score_calc"] / 10.0) * 0.2  # 护城河贡献20%
            + (df["valuation_score"] / 10.0) * 0.2  # 估值贡献20%
        )
        df.loc[df["signal"] == 1, "confidence"] = buy_confidence[df["signal"] == 1].clip(0.55, 0.95)

        # 卖出置信度
        sell_confidence = pd.Series(0.5, index=df.index)
        sell_confidence = sell_confidence + tech_breakdown * 0.3  # 技术破位 +0.3
        sell_confidence = sell_confidence + valuation_bubble * 0.2  # 估值泡沫 +0.2
        sell_confidence = sell_confidence + fundamental_deterioration * 0.2  # 基本面恶化 +0.2
        sell_confidence = sell_confidence + chip_loosening * 0.1  # 筹码松动 +0.1
        df.loc[df["signal"] == -1, "confidence"] = sell_confidence[df["signal"] == -1].clip(
            0.55, 0.95
        )

        # ── 仓位建议 ──────────────────────────────────
        df["position_ratio"] = 0.0
        # 强烈买入（5项全满足 + 综合评分>=7.5）：重仓 20%
        strong_buy = buy_condition & (df["composite_score"] >= 7.5) & df["chip_pass"]
        df.loc[strong_buy, "position_ratio"] = max_single_position

        # 分批建仓（前4项满足）：10%-15%
        normal_buy = buy_condition & ~strong_buy
        df.loc[normal_buy, "position_ratio"] = max_single_position * first_lot_pct / 0.3 * 0.5

        # ── 止损止盈参考价 ──────────────────────────────────
        # 止损：MA60 × 0.97 或 入场价 × 0.95（取较高者）
        df["stop_loss_price"] = df["ma60"] * 0.97
        # 止盈：基于PE估值（如有）
        if has_fundamental:
            pe = df["pe"].fillna(0)
            # 极度低估(PE<15) → 目标PE=20
            df["take_profit_price"] = df["close"] * (20 / pe.replace(0, 20))
            df.loc[pe <= 0, "take_profit_price"] = df["close"] * 1.15
        else:
            df["take_profit_price"] = df["close"] * 1.15

        # ── 信号强度标签（1-5星）────────────────────────────
        df["signal_strength"] = 0
        df.loc[df["signal"] == 1, "signal_strength"] = (
            (df["composite_score"] / 2).clip(1, 5).astype(int)
        )
        df.loc[df["signal"] == -1, "signal_strength"] = -1

        return df
