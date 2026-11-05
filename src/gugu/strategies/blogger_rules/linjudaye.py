"""
邻居大爷：8个独特规则策略。

来源：邻居大爷系列视频转写

独特规则：
1. 地量抄底（R01）- 地量后温和放量上涨
2. 黄金分割买入（R02）- 回撤到0.618/0.382支撑位
3. 缩量回踩（R03）- 上升趋势中缩量回踩关键均线
4. 量能背离（R04）- 价格与成交量出现背离
5. 箱体震荡突破（R05）- 箱体整理后突破上沿
6. 均线粘合突破（R06）- 多条均线粘合后发散
7. 龙抬头（R07）- 长期均线走平后首次上翘
8. 量价齐升（R08）- 量能温和放大+价格稳步上涨
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class LinJuDaYeStrategy(Strategy):
    """邻居大爷：8个独特规则策略（当前实现 5 条买入规则，OR 连接）。"""

    name = "linjudaye"
    category = "blogger_rules"

    # 规则顺序敏感：同一根 bar 命中多条规则时，靠后的覆盖靠前的置信度。
    # 这个顺序与 2026-08-01 版本完全一致，改动会改变 confidence 数值。
    RULE_CONFIDENCE: tuple[tuple[str, float], ...] = (
        ("R01_地量抄底", 0.7),
        ("R02_黄金分割", 0.65),
        ("R03_缩量回踩", 0.7),
        ("R05_箱体突破", 0.6),
        ("R08_量价齐升", 0.6),
    )

    def rule_masks(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        """按规则拆出买入条件，返回 {规则名: 布尔序列}。

        单一真源：``_generate_signals_impl`` 与 ``scripts/rule_attribution.py``
        都从这里取条件，避免「工具里另抄一份规则」导致的静默漂移。

        2026-11-05 归因结论：``R02_黄金分割`` 单独就有约 23% 的信号密度，
        接近全策略的 28%——整条策略基本等于 R02 加四条噪声。拆开才看得见。
        """
        self._ensure_columns(df)
        golden_level = float(self.params.get("golden_level", 0.618))
        vol_shrink_ratio = float(self.params.get("vol_shrink_ratio", 0.7))
        box_range_threshold = float(self.params.get("box_range_threshold", 0.05))
        low_vol_ratio = float(self.params.get("low_vol_ratio", 0.5))
        gentle_vol_min = float(self.params.get("gentle_vol_min", 1.2))
        golden_tolerance = float(self.params.get("golden_tolerance", 0.02))
        pullback_tolerance = float(self.params.get("pullback_tolerance", 0.03))
        r08_vol_min = float(self.params.get("r08_vol_min", 1.2))
        r08_vol_max = float(self.params.get("r08_vol_max", 2.0))
        r08_gain_max = float(self.params.get("r08_gain_max", 0.03))

        close = df["close"]
        volume = df["volume"]
        high = df["high"]
        low = df["low"]

        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        vol_ma5 = volume.rolling(5).mean()
        vol_ma20 = volume.rolling(20).mean()

        # R01: 地量抄底 — 地量后温和放量上涨
        extremely_low_vol = volume < vol_ma20 * low_vol_ratio
        gentle_increase = (volume > vol_ma5 * gentle_vol_min) & (close > close.shift(1))
        r01 = extremely_low_vol.shift(1) & gentle_increase

        # R02: 黄金分割 — 回撤到 golden_level 买入
        recent_high = high.rolling(20).max()
        recent_low = low.rolling(20).min()
        golden_618 = recent_low + (recent_high - recent_low) * golden_level
        at_support = (close > golden_618 * (1 - golden_tolerance)) & (
            close < golden_618 * (1 + golden_tolerance)
        )
        r02 = at_support & (close > ma20)

        # R03: 缩量回踩 — 上升趋势中缩量回踩 MA20
        vol_shrink = volume < vol_ma20 * vol_shrink_ratio
        pullback_to_20 = (close > ma20 * (1 - pullback_tolerance)) & (
            close < ma20 * (1 + pullback_tolerance * 0.33)
        )
        r03 = (close > ma20) & vol_shrink & pullback_to_20

        # R05: 箱体震荡突破 — 近 20 日振幅收窄后突破上轨
        box_high = high.rolling(20).max()
        box_low = low.rolling(20).min()
        in_box = ((box_high - box_low) / box_low) < box_range_threshold
        r05 = in_box.shift(1) & (close > box_high.shift(1))

        # R08: 量价齐升 — 量能温和放大 + 价格稳步上涨
        vol_growth = (volume > vol_ma5 * r08_vol_min) & (volume < vol_ma5 * r08_vol_max)
        price_growth = (close > close.shift(1)) & (
            (close - close.shift(1)) / close.shift(1) < r08_gain_max
        )
        r08 = vol_growth & price_growth & (close > ma5)

        return {
            "R01_地量抄底": r01,
            "R02_黄金分割": r02,
            "R03_缩量回踩": r03,
            "R05_箱体突破": r05,
            "R08_量价齐升": r08,
        }

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成邻居大爷信号。

        2026-08-01 参数化改造：原实现全部硬编码（golden_level/vol_shrink_ratio/
        box_range_threshold 配置从未被读取），现接入 self.params 支持校准。
        注意：策略只有买入规则（R01/R02/R03/R05/R08），无卖出信号——
        引擎级回测 58 笔/15股失真即因资金被买入锁死无法再交易。

        2026-11-05：条件抽取到 :meth:`rule_masks`，此处只负责按顺序 OR 与置信度。
        """
        result = df.copy()
        masks = self.rule_masks(result)

        signals = pd.Series(0, index=result.index)
        confidence = pd.Series(0.0, index=result.index)
        for rule_name, rule_confidence in self.RULE_CONFIDENCE:
            hit = masks[rule_name]
            signals = signals.where(~hit, 1)
            confidence = confidence.where(~hit, rule_confidence)

        result["signal"] = signals
        result["confidence"] = confidence

        # 2026-08-01 补时间止损：策略无卖出信号，资金会被买入锁死
        # （引擎级回测 157笔/39股 = 每只~4笔后资金耗尽，胜率0%期末结算失真）。
        # max_hold_days 时间止损保证持仓周转，让引擎回测反映真实交易能力。
        max_hold_days = int(self.params.get("max_hold_days", 0))
        if max_hold_days > 0:
            result = self._apply_max_hold_exit(result)

        buy_count = int((result["signal"] == 1).sum())
        sell_count = int((result["signal"] == -1).sum())
        logger.info(
            "邻居大爷策略生成信号: {} 买入, {} 卖出 (max_hold={})",
            buy_count,
            sell_count,
            max_hold_days,
        )

        return result
