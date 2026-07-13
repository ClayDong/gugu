"""Alpha因子工厂：参考 qlib 的 Alpha158/Alpha360 因子体系。

实现A股最常用的技术因子，分为5大类：
1. 趋势类因子：MA偏离度、MACD、DMI
2. 动量类因子：RSI、KDJ、CCI、MFI
3. 波动类因子：ATR、布林带宽度、历史波动率
4. 成交量因子：量比、OBV、VWAP偏离
5. 形态类因子：K线形态、缺口、十字星

每个因子返回标准化后的值（0-1或-1到1），可叠加到策略信号中。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class AlphaFactor:
    """单个Alpha因子定义"""

    name: str  # 因子名称
    category: str  # 分类：trend/momentum/volatility/volume/pattern
    direction: str  # 方向：positive(越大越好)/negative(越小越好)/neutral
    value: float = 0.0  # 当前值
    normalized: float = 0.0  # 标准化值（0-1）
    signal: float = 0.0  # 信号值（-1到1，正=买入，负=卖出）
    description: str = ""  # 因子描述


class AlphaFactory:
    """Alpha因子工厂：批量计算技术因子。

    使用方式：
    factory = AlphaFactory()
    factors = factory.compute_all(df)  # 计算所有因子
    score = factory.composite_score(factors)  # 综合评分
    """

    # 因子定义：(名称, 分类, 方向, 计算函数)
    FACTOR_DEFS: list[tuple[str, str, str, str]] = [
        # === 趋势类 ===
        ("ma_deviation_20", "trend", "positive", "价格相对20日均线的偏离度"),
        ("ma_deviation_60", "trend", "positive", "价格相对60日均线的偏离度"),
        ("ma_alignment", "trend", "positive", "5/10/20/60日均线多头排列程度"),
        ("macd_hist", "trend", "positive", "MACD柱（DIF-DEA）"),
        ("macd_cross", "trend", "positive", "MACD金叉/死叉信号"),
        ("adx", "trend", "positive", "平均趋向指数（趋势强度）"),
        ("di_cross", "trend", "positive", "+DI/-DI交叉信号"),
        # === 动量类 ===
        ("rsi", "momentum", "neutral", "RSI(14)超买超卖"),
        ("kdj_k", "momentum", "neutral", "KDJ的K值位置"),
        ("kdj_cross", "momentum", "positive", "KDJ金叉/死叉"),
        ("cci", "momentum", "neutral", "商品通道指数"),
        ("mfi", "momentum", "positive", "资金流量指标"),
        ("momentum_5d", "momentum", "positive", "5日动量"),
        ("momentum_20d", "momentum", "positive", "20日动量"),
        # === 波动类 ===
        ("atr_ratio", "volatility", "neutral", "ATR相对价格比率"),
        ("bollinger_width", "volatility", "neutral", "布林带宽度"),
        ("bollinger_position", "volatility", "neutral", "价格在布林带中的位置"),
        ("volatility_20d", "volatility", "neutral", "20日历史波动率"),
        # === 成交量类 ===
        ("volume_ratio", "volume", "positive", "5日均量比"),
        ("volume_trend", "volume", "positive", "量价配合度"),
        ("obv_divergence", "volume", "positive", "OBV背离"),
        ("vwap_deviation", "volume", "positive", "价格相对VWAP偏离"),
        # === 形态类 ===
        ("gap", "pattern", "positive", "跳空缺口"),
        ("doji", "pattern", "neutral", "十字星"),
        ("hammer", "pattern", "positive", "锤子线/倒锤子"),
        ("three_consecutive", "pattern", "positive", "三连阳/三连阴"),
    ]

    def compute_all(self, df: pd.DataFrame) -> dict[str, AlphaFactor]:
        """计算所有因子。

        Args:
            df: OHLCV数据，DataFrame

        Returns:
            dict[str, AlphaFactor]: 因子名到因子值的映射
        """
        factors = {}
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        volume = df["volume"].values if "volume" in df.columns else np.ones(len(close))

        # === 趋势类 ===
        # MA偏离度
        for name, window, display in [
            ("ma_deviation_20", 20, "MA20偏离"),
            ("ma_deviation_60", 60, "MA60偏离"),
        ]:
            if len(close) >= window:
                ma = pd.Series(close).rolling(window).mean().iloc[-1]
                if ma > 0:
                    dev = (close[-1] - ma) / ma
                    factors[name] = AlphaFactor(
                        name=name,
                        category="trend",
                        direction="positive",
                        value=round(dev, 4),
                        normalized=round(self._sigmoid(dev * 20), 4),
                        signal=round(np.clip(dev * 20, -1, 1), 4),
                        description=display,
                    )

        # 均线排列
        if len(close) >= 60:
            mas = {}
            for w in [5, 10, 20, 60]:
                mas[w] = pd.Series(close).rolling(w).mean().iloc[-1]
            alignment_score: float = 0.0
            if mas[5] > mas[10] > mas[20] > mas[60]:
                alignment_score = 1.0  # 完美多头排列
            elif mas[5] < mas[10] < mas[20] < mas[60]:
                alignment_score = -1.0  # 完美空头排列
            else:
                # 部分排列
                ups = sum(1 for a, b in [(5, 10), (10, 20), (20, 60)] if mas[a] > mas[b])
                alignment_score = (ups - 1.5) / 1.5  # 归一化到-1~1
            factors["ma_alignment"] = AlphaFactor(
                name="ma_alignment",
                category="trend",
                direction="positive",
                value=round(alignment_score, 4),
                normalized=round((alignment_score + 1) / 2, 4),
                signal=round(alignment_score, 4),
                description="均线多头排列程度",
            )

        # MACD
        if len(close) >= 26:
            ema12 = pd.Series(close).ewm(span=12).mean()
            ema26 = pd.Series(close).ewm(span=26).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9).mean()
            macd_hist = (dif - dea).iloc[-1] * 2
            # 标准化
            atr = self._atr(high, low, close, 14)
            if atr > 0:
                norm_hist = np.clip(macd_hist / atr, -3, 3) / 3
            else:
                norm_hist = 0
            factors["macd_hist"] = AlphaFactor(
                name="macd_hist",
                category="trend",
                direction="positive",
                value=round(float(macd_hist), 4),
                normalized=round((norm_hist + 1) / 2, 4),
                signal=round(float(norm_hist), 4),
                description="MACD柱强度",
            )

            # MACD金叉/死叉
            dif_prev = dif.iloc[-2]
            dea_prev = dea.iloc[-2]
            dif_curr = dif.iloc[-1]
            dea_curr = dea.iloc[-1]
            if dif_prev <= dea_prev and dif_curr > dea_curr:
                cross_signal = 1.0  # 金叉
            elif dif_prev >= dea_prev and dif_curr < dea_curr:
                cross_signal = -1.0  # 死叉
            else:
                cross_signal = 0.0
            factors["macd_cross"] = AlphaFactor(
                name="macd_cross",
                category="trend",
                direction="positive",
                value=round(cross_signal, 4),
                normalized=round((cross_signal + 1) / 2, 4),
                signal=round(cross_signal, 4),
                description="MACD金叉/死叉",
            )

        # === 动量类 ===
        # RSI
        if len(close) >= 14:
            rsi = self._rsi(close, 14)
            # RSI 30-70 映射到 -1 到 1
            rsi_norm = (rsi - 50) / 20
            rsi_signal = np.clip(rsi_norm, -1, 1)
            factors["rsi"] = AlphaFactor(
                name="rsi",
                category="momentum",
                direction="neutral",
                value=round(rsi, 2),
                normalized=round(rsi / 100, 4),
                signal=round(float(rsi_signal), 4),
                description="RSI(14)超买超卖",
            )

        # KDJ
        if len(close) >= 9:
            k, d, _j = self._kdj(high, low, close, 9, 3, 3)
            k_norm = (k - 50) / 25
            factors["kdj_k"] = AlphaFactor(
                name="kdj_k",
                category="momentum",
                direction="neutral",
                value=round(k, 2),
                normalized=round(k / 100, 4),
                signal=round(float(np.clip(k_norm, -1, 1)), 4),
                description="KDJ的K值",
            )

            # KDJ金叉/死叉
            k_prev = self._kdj_at(high, low, close, 9, 3, 3, -2)
            d_prev = self._kdj_at(high, low, close, 9, 3, 3, -2, is_d=True)
            if k_prev <= d_prev and k > d:
                kdj_cross = 1.0
            elif k_prev >= d_prev and k < d:
                kdj_cross = -1.0
            else:
                kdj_cross = 0.0
            factors["kdj_cross"] = AlphaFactor(
                name="kdj_cross",
                category="momentum",
                direction="positive",
                value=round(kdj_cross, 4),
                normalized=round((kdj_cross + 1) / 2, 4),
                signal=round(kdj_cross, 4),
                description="KDJ金叉/死叉",
            )

        # 动量
        for name, window, display in [
            ("momentum_5d", 5, "5日动量"),
            ("momentum_20d", 20, "20日动量"),
        ]:
            if len(close) > window:
                mom = (close[-1] - close[-window - 1]) / close[-window - 1]
                factors[name] = AlphaFactor(
                    name=name,
                    category="momentum",
                    direction="positive",
                    value=round(mom, 4),
                    normalized=round(self._sigmoid(mom * 10), 4),
                    signal=round(np.clip(mom * 10, -1, 1), 4),
                    description=display,
                )

        # === 波动类 ===
        # ATR比率
        if len(close) >= 14:
            atr14 = self._atr(high, low, close, 14)
            if close[-1] > 0:
                atr_ratio = atr14 / close[-1]
                factors["atr_ratio"] = AlphaFactor(
                    name="atr_ratio",
                    category="volatility",
                    direction="neutral",
                    value=round(atr_ratio, 4),
                    normalized=round(min(atr_ratio * 20, 1.0), 4),
                    signal=0.0,  # 波动率本身不产生方向信号
                    description="ATR相对价格比率",
                )

        # 布林带位置
        if len(close) >= 20:
            bb_ma = pd.Series(close).rolling(20).mean().iloc[-1]
            bb_std = pd.Series(close).rolling(20).std().iloc[-1]
            if bb_std > 0:
                bb_pos = (close[-1] - bb_ma) / (2 * bb_std)
                # 下轨=-1, 中轨=0, 上轨=1
                bb_signal = 0.0
                if bb_pos < -0.8:
                    bb_signal = 1.0  # 触及下轨，超卖
                elif bb_pos > 0.8:
                    bb_signal = -1.0  # 触及上轨，超买
                factors["bollinger_position"] = AlphaFactor(
                    name="bollinger_position",
                    category="volatility",
                    direction="neutral",
                    value=round(bb_pos, 4),
                    normalized=round((bb_pos + 1) / 2, 4),
                    signal=round(bb_signal, 4),
                    description="价格在布林带中的位置",
                )

        # === 成交量类 ===
        # 量比
        if len(volume) >= 5:
            vol_ma5 = pd.Series(volume).rolling(5).mean().iloc[-1]
            if vol_ma5 > 0:
                vol_ratio = volume[-1] / vol_ma5
                factors["volume_ratio"] = AlphaFactor(
                    name="volume_ratio",
                    category="volume",
                    direction="positive",
                    value=round(vol_ratio, 2),
                    normalized=round(min(vol_ratio / 3, 1.0), 4),
                    signal=round(np.clip((vol_ratio - 1) * 0.5, -1, 1), 4),
                    description="5日均量比",
                )

        # 量价配合
        if len(close) >= 5 and len(volume) >= 5:
            price_change = close[-1] - close[-5]
            vol_change = volume[-1] - volume[-5:].mean()
            if abs(vol_change) > 0:
                vol_price_corr = (
                    1.0
                    if (price_change > 0 and vol_change > 0)
                    or (price_change < 0 and vol_change < 0)
                    else -1.0
                )
            else:
                vol_price_corr = 0.0
            factors["volume_trend"] = AlphaFactor(
                name="volume_trend",
                category="volume",
                direction="positive",
                value=round(vol_price_corr, 4),
                normalized=round((vol_price_corr + 1) / 2, 4),
                signal=round(vol_price_corr * 0.5, 4),
                description="量价配合度",
            )

        # === 补全：CCI（商品通道指数）===
        if len(close) >= 20:
            tp = (high + low + close) / 3.0
            tp_series = pd.Series(tp)
            tp_ma = tp_series.rolling(20).mean().iloc[-1]
            mean_dev = tp_series.rolling(20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True).iloc[-1]
            if mean_dev > 0:
                cci = (tp[-1] - tp_ma) / (0.015 * mean_dev)
                cci_signal = float(np.clip(cci / 200, -1, 1))
                factors["cci"] = AlphaFactor(
                    name="cci",
                    category="momentum",
                    direction="neutral",
                    value=round(float(cci), 2),
                    normalized=round(float((cci + 200) / 400), 4),
                    signal=round(cci_signal, 4),
                    description="商品通道指数",
                )

        # === 补全：MFI（资金流量指标）===
        if len(close) >= 14 and "volume" in df.columns:
            tp = (high + low + close) / 3.0
            mf = tp * volume
            pos_mf = np.where(tp[1:] > tp[:-1], mf[1:], 0)
            neg_mf = np.where(tp[1:] < tp[:-1], mf[1:], 0)
            if len(pos_mf) >= 14:
                pos_sum = float(np.sum(pos_mf[-14:]))
                neg_sum = float(np.sum(neg_mf[-14:]))
                if neg_sum > 0:
                    mfi = 100 - 100 / (1 + pos_sum / neg_sum)
                else:
                    mfi = 100.0
                mfi_signal = float(np.clip((mfi - 50) / 25, -1, 1))
                factors["mfi"] = AlphaFactor(
                    name="mfi",
                    category="momentum",
                    direction="positive",
                    value=round(mfi, 2),
                    normalized=round(mfi / 100, 4),
                    signal=round(mfi_signal, 4),
                    description="资金流量指标",
                )

        # === 补全：OBV 背离 ===
        if len(close) >= 10 and "volume" in df.columns:
            obv = np.zeros(len(close))
            for i in range(1, len(close)):
                if close[i] > close[i - 1]:
                    obv[i] = obv[i - 1] + volume[i]
                elif close[i] < close[i - 1]:
                    obv[i] = obv[i - 1] - volume[i]
                else:
                    obv[i] = obv[i - 1]
            # 检测背离：价格5日趋势 vs OBV 5日趋势
            price_slope = close[-1] - close[-5]
            obv_slope = obv[-1] - obv[-5]
            if price_slope > 0 and obv_slope > 0:
                obv_signal = 1.0  # 正向背离
            elif price_slope > 0 and obv_slope < 0:
                obv_signal = -0.8  # 顶背离
            elif price_slope < 0 and obv_slope < 0:
                obv_signal = -1.0  # 同向下跌
            elif price_slope < 0 and obv_slope > 0:
                obv_signal = 0.8  # 底背离
            else:
                obv_signal = 0.0
            factors["obv_divergence"] = AlphaFactor(
                name="obv_divergence",
                category="volume",
                direction="positive",
                value=round(float(obv[-1]), 2),
                normalized=round((obv_signal + 1) / 2, 4),
                signal=round(obv_signal, 4),
                description="OBV背离",
            )

        # === 补全：VWAP 偏离 ===
        if len(close) >= 5 and "amount" in df.columns:
            amount = df["amount"].values
            cum_amount = np.sum(amount[-5:])
            cum_vol = np.sum(volume[-5:])
            if cum_vol > 0:
                vwap = cum_amount / cum_vol
                vwap_dev = (close[-1] - vwap) / vwap if vwap > 0 else 0.0
                vwap_signal = float(np.clip(vwap_dev * 20, -1, 1))
                factors["vwap_deviation"] = AlphaFactor(
                    name="vwap_deviation",
                    category="volume",
                    direction="positive",
                    value=round(float(vwap_dev), 4),
                    normalized=round(float(self._sigmoid(vwap_dev * 20)), 4),
                    signal=round(vwap_signal, 4),
                    description="价格相对VWAP偏离",
                )
            elif len(close) >= 5:
                # 无 amount 列时用 close*volume 近似
                pseudo_amount = close * volume
                cum_amount = np.sum(pseudo_amount[-5:])
                cum_vol = np.sum(volume[-5:])
                if cum_vol > 0:
                    vwap = cum_amount / cum_vol
                    vwap_dev = (close[-1] - vwap) / vwap if vwap > 0 else 0.0
                    vwap_signal = float(np.clip(vwap_dev * 20, -1, 1))
                    factors["vwap_deviation"] = AlphaFactor(
                        name="vwap_deviation",
                        category="volume",
                        direction="positive",
                        value=round(float(vwap_dev), 4),
                        normalized=round(float(self._sigmoid(vwap_dev * 20)), 4),
                        signal=round(vwap_signal, 4),
                        description="价格相对VWAP偏离(近似)",
                    )

        # === 补全：布林带宽度 ===
        if len(close) >= 20:
            bb_ma = pd.Series(close).rolling(20).mean().iloc[-1]
            bb_std = pd.Series(close).rolling(20).std().iloc[-1]
            if bb_ma > 0 and bb_std is not None:
                bb_width = (2 * bb_std) / bb_ma
                factors["bollinger_width"] = AlphaFactor(
                    name="bollinger_width",
                    category="volatility",
                    direction="neutral",
                    value=round(float(bb_width), 4),
                    normalized=round(min(float(bb_width * 10), 1.0), 4),
                    signal=0.0,
                    description="布林带宽度",
                )

        # === 补全：20日历史波动率 ===
        if len(close) >= 21:
            rets = np.diff(np.log(close[-21:]))
            hv = float(np.std(rets) * np.sqrt(250))
            factors["volatility_20d"] = AlphaFactor(
                name="volatility_20d",
                category="volatility",
                direction="neutral",
                value=round(hv, 4),
                normalized=round(min(hv * 5, 1.0), 4),
                signal=0.0,
                description="20日历史波动率",
            )

        # === 补全：ADX 和 DI 交叉 ===
        if len(close) >= 28:
            adx_val, di_plus, di_minus = self._adx_di(high, low, close, 14)
            factors["adx"] = AlphaFactor(
                name="adx",
                category="trend",
                direction="positive",
                value=round(float(adx_val), 2),
                normalized=round(min(float(adx_val) / 50, 1.0), 4),
                signal=round(float(np.clip((adx_val - 25) / 25, -1, 1)), 4),
                description="平均趋向指数",
            )
            if di_plus > di_minus:
                di_signal = float(np.clip((di_plus - di_minus) / 20, 0, 1))
            else:
                di_signal = float(np.clip(-(di_minus - di_plus) / 20, -1, 0))
            factors["di_cross"] = AlphaFactor(
                name="di_cross",
                category="trend",
                direction="positive",
                value=round(float(di_plus - di_minus), 2),
                normalized=round((di_signal + 1) / 2, 4),
                signal=round(di_signal, 4),
                description="+DI/-DI交叉信号",
            )

        # === 补全：形态类因子 ===
        if len(close) >= 3 and "open" in df.columns:
            open_arr = df["open"].values

            # 跳空缺口
            gap = open_arr[-1] - close[-2] if len(close) >= 2 else 0.0
            if close[-1] > 0:
                gap_pct = gap / close[-1]
                gap_signal = float(np.clip(gap_pct * 50, -1, 1))
                factors["gap"] = AlphaFactor(
                    name="gap",
                    category="pattern",
                    direction="positive",
                    value=round(float(gap_pct), 4),
                    normalized=round(float(self._sigmoid(gap_pct * 50)), 4),
                    signal=round(gap_signal, 4),
                    description="跳空缺口",
                )

            # 十字星
            body = abs(close[-1] - open_arr[-1])
            hl_range = high[-1] - low[-1]
            if hl_range > 0:
                doji_ratio = body / hl_range
                is_doji = 1.0 if doji_ratio < 0.1 else 0.0
                factors["doji"] = AlphaFactor(
                    name="doji",
                    category="pattern",
                    direction="neutral",
                    value=round(float(doji_ratio), 4),
                    normalized=round(float(1 - doji_ratio), 4),
                    signal=round(float(is_doji * 0.5 if close[-1] > close[-2] else -is_doji * 0.5), 4),
                    description="十字星",
                )

            # 锤子线
            if hl_range > 0:
                lower_shadow = min(open_arr[-1], close[-1]) - low[-1]
                upper_shadow = high[-1] - max(open_arr[-1], close[-1])
                body2 = abs(close[-1] - open_arr[-1])
                if body2 > 0:
                    is_hammer = 1.0 if (lower_shadow > 2 * body2 and upper_shadow < body2 * 0.5) else 0.0
                    hammer_signal = float(is_hammer * 0.8 if close[-1] > close[-2] else -is_hammer * 0.8)
                    factors["hammer"] = AlphaFactor(
                        name="hammer",
                        category="pattern",
                        direction="positive",
                        value=round(float(lower_shadow / body2 if body2 > 0 else 0), 4),
                        normalized=round(float(is_hammer), 4),
                        signal=round(hammer_signal, 4),
                        description="锤子线/倒锤子",
                    )

            # 三连阳/三连阴
            if len(close) >= 4:
                up3 = all(close[i] > close[i - 1] for i in range(-3, 0))
                down3 = all(close[i] < close[i - 1] for i in range(-3, 0))
                if up3:
                    tc_signal = 1.0
                elif down3:
                    tc_signal = -1.0
                else:
                    tc_signal = 0.0
                factors["three_consecutive"] = AlphaFactor(
                    name="three_consecutive",
                    category="pattern",
                    direction="positive",
                    value=round(float(tc_signal), 4),
                    normalized=round((tc_signal + 1) / 2, 4),
                    signal=round(float(tc_signal), 4),
                    description="三连阳/三连阴",
                )

        return factors

    def composite_score(
        self, factors: dict[str, AlphaFactor], weights: dict[str, float] | None = None
    ) -> dict[str, Any]:
        """计算综合因子评分。

        Args:
            factors: compute_all() 的输出
            weights: 自定义权重，不传则使用默认权重

        Returns:
            dict with: score, category_scores, buy_signal, sell_signal, top_factors
        """
        # 默认权重
        if weights is None:
            weights = {
                "trend": 0.30,
                "momentum": 0.25,
                "volatility": 0.10,
                "volume": 0.20,
                "pattern": 0.15,
            }

        category_signals: dict[str, list[float]] = {}
        for f in factors.values():
            if f.category not in category_signals:
                category_signals[f.category] = []
            category_signals[f.category].append(f.signal)

        # 每个分类的评分
        category_scores = {}
        for cat, signals in category_signals.items():
            if signals:
                category_scores[cat] = round(float(np.mean(signals)), 4)
            else:
                category_scores[cat] = 0.0

        # 加权综合评分
        total_score = 0.0
        total_weight = 0.0
        for cat, score in category_scores.items():
            w = weights.get(cat, 0.1)
            total_score += score * w
            total_weight += w
        if total_weight > 0:
            total_score /= total_weight

        # 选出最强因子
        sorted_factors = sorted(factors.values(), key=lambda f: abs(f.signal), reverse=True)
        top_factors = [(f.name, f.signal, f.description) for f in sorted_factors[:5]]

        return {
            "score": round(float(total_score), 4),
            "category_scores": category_scores,
            "buy_signal": total_score > 0.15,
            "sell_signal": total_score < -0.15,
            "signal_strength": round(abs(total_score), 4),
            "top_factors": top_factors,
        }

    def get_factor_names(self) -> list[str]:
        """获取所有因子名称列表"""
        return [f[0] for f in self.FACTOR_DEFS]

    # === 辅助计算函数 ===

    def _rsi(self, close: np.ndarray, period: int = 14) -> float:
        """计算RSI"""
        deltas = np.diff(close)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100 - 100 / (1 + rs))

    def _kdj(
        self,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        n: int = 9,
        m1: int = 3,
        m2: int = 3,
    ) -> tuple[float, float, float]:
        """计算KDJ指标"""
        if len(close) < n:
            return 50.0, 50.0, 50.0
        low_n = np.min(low[-n:])
        high_n = np.max(high[-n:])
        if high_n == low_n:
            rsv = 50.0
        else:
            rsv = (close[-1] - low_n) / (high_n - low_n) * 100
        # 用EMA近似
        k = float(rsv * 2 / 3 + 50 * 1 / 3)  # 简化
        d = float(k * 2 / 3 + 50 * 1 / 3)
        j = float(3 * k - 2 * d)
        return k, d, j

    def _kdj_at(
        self,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        n: int,
        m1: int,
        m2: int,
        offset: int,
        is_d: bool = False,
    ) -> float:
        """计算offset位置的KDJ值"""
        idx = len(close) + offset
        if idx < n:
            return 50.0
        slice_high = high[:idx] if idx > 0 else high[:idx]
        slice_low = low[:idx] if idx > 0 else low[:idx]
        slice_close = close[:idx] if idx > 0 else close[:idx]
        if len(slice_close) < n:
            return 50.0
        k, d, _j = self._kdj(slice_high, slice_low, slice_close, n, m1, m2)
        return d if is_d else k

    def _atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
        """计算ATR"""
        prev_close = np.roll(close, 1)
        prev_close[0] = close[0]
        tr = np.maximum(high - low, np.maximum(abs(high - prev_close), abs(low - prev_close)))
        return float(np.mean(tr[-period:]))

    def _adx_di(
        self,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        period: int = 14,
    ) -> tuple[float, float, float]:
        """计算 ADX 和 +DI/-DI。

        Returns:
            (adx, +DI, -DI)
        """
        if len(close) < period * 2:
            return 25.0, 25.0, 25.0

        prev_high = np.roll(high, 1)
        prev_low = np.roll(low, 1)
        prev_high[0] = high[0]
        prev_low[0] = low[0]

        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        prev_close = np.roll(close, 1)
        prev_close[0] = close[0]
        tr = np.maximum(
            high - low,
            np.maximum(abs(high - prev_close), abs(low - prev_close)),
        )

        # Wilder smoothing
        atr = np.zeros(len(tr))
        atr[period - 1] = np.sum(tr[:period])
        plus_dm_s = np.zeros(len(tr))
        minus_dm_s = np.zeros(len(tr))
        plus_dm_s[period - 1] = np.sum(plus_dm[:period])
        minus_dm_s[period - 1] = np.sum(minus_dm[:period])

        for i in range(period, len(tr)):
            atr[i] = atr[i - 1] - atr[i - 1] / period + tr[i]
            plus_dm_s[i] = plus_dm_s[i - 1] - plus_dm_s[i - 1] / period + plus_dm[i]
            minus_dm_s[i] = minus_dm_s[i - 1] - minus_dm_s[i - 1] / period + minus_dm[i]

        plus_di = np.where(atr > 0, 100 * plus_dm_s / atr, 0)
        minus_di = np.where(atr > 0, 100 * minus_dm_s / atr, 0)

        dx = np.where((plus_di + minus_di) > 0, 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di), 0)

        if len(dx) >= period * 2:
            adx = float(np.mean(dx[-period:]))
        else:
            adx = 25.0

        return adx, float(plus_di[-1]), float(minus_di[-1])

    def _sigmoid(self, x: float) -> float:
        """Sigmoid归一化"""
        return float(1 / (1 + np.exp(-x)))
