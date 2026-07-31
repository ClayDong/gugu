"""断板反包战法（一箭封喉）：陈学长不吃面交易策略完整实现。

来源：陈学长不吃面 229 条视频转写文本系统化整理

第一性原理：
- 主力完整周期：吸筹 → 异动（首板）→ 洗盘（大阴线）→ 护盘（缩量横盘）→ 反包（再介入）
- 核心逻辑：主力"一步洗到位"后护盘不破位，说明志存高远，反包即买点
- 适用市场：主板（不做创业板，上下40cm波动逻辑不同）
- 适用市值：50-150亿（太大吃情绪、太小被量化操控）
- 适用标的：热门题材、有辨识度、历史涨停活跃

═══════════════════════════════════════════════════════════
选股条件（Pre-filter，在策略信号生成前过滤）
═══════════════════════════════════════════════════════════
条件1：位置——必须在相对低位或中途
  - 经过长时间横盘后出现的首板
  - 或经过一轮上涨后回调到上涨波段一半位置后出现的首板
  - 排除：已经经过大幅上涨后在高位出现的板

条件2：首板——必须是"异动信号"
  - 缩量首板（量不能太大，说明抛压小、高度控盘）
  - 跳空首板更优（态度更明确）
  - 首板前最好有低位长上影线（测试抛压的试盘信号）

条件3：洗盘——必须接一根放量大阴线
  - 首板之后第二天必须接一根放量大阴线（量越大越好、砸得越深越好）
  - 本质：一步洗到位，把不坚定的人全部赶走

条件4：护盘——不能破关键支撑位
  - 不能破首板的开盘价/跳空缺口（这是主力的成本底线）
  - 不能破关键均线（5日、10日、20日线）
  - 护盘阶段表现为：小阴小阳横盘，缩量，不破位
  - 如果破了但当天收回（长下影线），也算护盘信号

条件5：方向——必须是热点/热门题材
  - 板块要是当前热门方向（有上升需求）
  - 有辨识度、人气高、历史涨停多（老妖股优先）
  - 排除：有利空的、一年出不了两个板的、每天心电图一样的

条件6：市值与价格过滤
  - 不做ST、不做创业板（300开头）
  - 不做市值太大（>150亿）的
  - 不做价格太低（2-3元）的（量化老庄操控）

═══════════════════════════════════════════════════════════
买入信号（3个买点）
═══════════════════════════════════════════════════════════
买点1：护盘阶段——缩量回踩支撑位
  - 时机：大阴线洗盘之后，股价回踩到首板开盘价/缺口/20日线附近
  - 量能特征：缩量（比前一天量小，说明抛压枯竭）
  - 价格特征：不破支撑位，或破位后当天收回（长下影线）
  - 原话："我个人是比较喜欢在它护盘的当天砸穿这个支撑的时候去找他"

买点2：底背离信号（日内做T/抄底）
  - 顶底背离判断：股价持续创新低，但MACD线不破前低
  - 配合成交量：价跌量缩，缩到平时量的一半以内
  - 终极信号：价平无量（地量见地价）

买点3：有效放量后的回踩
  - 第一次上冲的成交额达到流通市值的0.5%——视为"有效放量"
  - 有效放量后回落，在支撑位附近介入

═══════════════════════════════════════════════════════════
卖出信号（5个卖点）
═══════════════════════════════════════════════════════════
卖点1：反包成功——无论多少，先减仓
  - 断板反包的首要目的是反弹，不是一定能开启第二波
  - 反包当天（不管是3cm还是5cm），一定要有减仓动作
  - 原话："6厘米够了兄弟，他后面再怎么样跟我没关系了"

卖点2：顶背离信号（日内逃顶）
  - 判断标准：股价持续创新高，但MACD线不破前高
  - 配合成交量：价升量缩——第二次拉升的量明显小于第一次

卖点3：量价背离
  - 价格创新高但成交额没有同步放大
  - 上板需要的成交额是流通市值的1%，如果拉到接近涨停但量远远不够→卖
  - 持续缩量破新高→每一次新高都是卖点

卖点4：破位止损
  - 护盘阶段破位且没有收回→止损

卖点5：不及预期——次日直接走
  - 买入后第二天不符合预期（绿开、没有拉升动作）→卖掉止损
  - 原话："错了我就认，然后继续下一个，只要总的结果是对的就行了"

═══════════════════════════════════════════════════════════
仓位管理与风控规则
═══════════════════════════════════════════════════════════
1. 分批建仓：4-6成仓位分批介入
2. 做T降成本：缩量回踩支撑位买入，冲高背离卖出
3. 安全底线优先：板上砸一半→让剩下一半在无风险区间奔跑
4. 不贪心、不恋战："外面5000多只羊，不差这一两只"

═══════════════════════════════════════════════════════════
关键量化参数
═══════════════════════════════════════════════════════════
- 首板上板成交额 = 流通市值1%
- 断板反包上板成交额 = 流通市值1.5%-2%
- 有效放量 = 流通市值0.5%
- 缩量判断 = 前一天量50%以内
- 无量判断 = 前一天量10%-30%
- 健康成交额范围 = 流通市值1%-1.5%
- 倍量危险信号 = 流通市值2%以上
- 市值过滤范围 = 50-150亿
- 洗盘观察天数 = 3天（三破三收）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class BreakoutReboundStrategy(Strategy):
    """断板反包战法（一箭封喉）：陈学长不吃面交易策略。

    选股6条件 + 买点3个 + 卖点5个 + 仓位管理。
    与 double_yin 互补：double_yin 是波段拐点，本策略是短线主力行为。
    """

    name = "breakout_rebound"
    category = "main_force"

    def validate_params(self) -> None:
        """校验参数语义。"""
        ma_trend = int(self.params.get("ma_trend_window", 20))
        ma_long = int(self.params.get("ma_long_window", 60))
        if ma_trend >= ma_long:
            raise ValueError("ma_trend_window 必须小于 ma_long_window")
        lookback = int(self.params.get("limit_up_lookback", 5))
        if lookback < 1:
            raise ValueError("limit_up_lookback 必须大于 0")
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up_pct <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11% 之间")
        breakout_vol = float(self.params.get("breakout_vol_ratio", 1.5))
        if breakout_vol < 1.0:
            raise ValueError("breakout_vol_ratio 必须 >= 1.0")
        wash_day_max = int(self.params.get("wash_day_max", 3))
        if wash_day_max < 1:
            raise ValueError("wash_day_max 必须大于 0")

    def _ensure_columns(self, df: pd.DataFrame) -> None:
        """确保必要列存在。"""
        required = ["open", "high", "low", "close", "volume", "amount"]
        for col in required:
            if col not in df.columns:
                raise ValueError(f"缺少必要列: {col}")

    def _calc_macd(
        self, df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> pd.DataFrame:
        """计算MACD指标。"""
        df = df.copy()
        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        df["macd_dif"] = ema_fast - ema_slow
        df["macd_dea"] = df["macd_dif"].ewm(span=signal, adjust=False).mean()
        df["macd_hist"] = 2 * (df["macd_dif"] - df["macd_dea"])
        return df

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成断板反包信号（陈学长不吃面完整版）。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        ma_trend_window = int(self.params.get("ma_trend_window", 20))
        ma_long_window = int(self.params.get("ma_long_window", 60))
        ma_slope_window = int(self.params.get("ma_slope_window", 5))
        limit_up_pct = float(
            self.params.get("limit_up_pct", 0.07)
        )  # v0.6.4: 放宽涨停判定（8%→7%，覆盖更多涨停）
        limit_up_lookback = int(
            self.params.get("limit_up_lookback", 10)
        )  # v0.6.4: 放宽回看窗口（7→10日）
        _breakout_vol_ratio = float(
            self.params.get("breakout_vol_ratio", 1.0)
        )  # v0.6.4: 放宽放量要求（1.2→1.0，只要有放量就算）
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.08))  # v0.6.4: 放宽止损（5%→8%）
        take_profit_pct = float(
            self.params.get("take_profit_pct", 0.05)
        )  # v0.6.4: 放宽止盈（6%→5%）
        max_upper_shadow_pct = float(
            self.params.get("max_upper_shadow_pct", 0.04)
        )  # v0.6.4: 放宽上影线（3%→4%）
        stagnation_vol_ratio = float(
            self.params.get("stagnation_vol_ratio", 1.8)
        )  # v0.6.4: 放宽滞涨（2.0→1.8）
        time_stop_window = int(
            self.params.get("time_stop_window", 10)
        )  # v0.6.4: 放宽时间止损（7→10天）
        time_stop_threshold = float(
            self.params.get("time_stop_threshold", 0.01)
        )  # v0.6.4: 放宽时间止损阈值（2%→1%）
        wash_day_max = int(self.params.get("wash_day_max", 7))  # v0.6.4: 放宽洗盘窗口（5→7日）
        _shrink_vol_ratio = float(
            self.params.get("shrink_vol_ratio", 0.85)
        )  # v0.6.4: 放宽缩量判断（70%→85%）
        _ground_vol_ratio = float(
            self.params.get("ground_vol_ratio", 0.5)
        )  # v0.6.4: 放宽无量判断（40%→50%）

        # ── 计算均线系统 ──────────────────────────────
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma_trend"] = df["close"].rolling(ma_trend_window).mean()
        df["ma_long"] = df["close"].rolling(ma_long_window).mean()
        df["ma_slope"] = df["ma_trend"].diff(ma_slope_window) / df["ma_trend"].shift(
            ma_slope_window
        )

        # ── 计算量能指标 ──────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)
        df["vol_ratio_prev"] = df["vol_ratio"].shift(1)

        # ── MACD指标（用于背离判断） ──────────────────
        df = self._calc_macd(df)

        # ── K线基础属性 ──────────────────────────────
        df["daily_return"] = df["close"].pct_change()
        df["is_yin"] = (df["close"] < df["open"]).astype(bool)
        df["is_yang"] = (df["close"] > df["open"]).astype(bool)
        df["body"] = (df["close"] - df["open"]).abs() / df["open"].replace(0, np.nan)
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(
            0, np.nan
        )
        df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["open"].replace(
            0, np.nan
        )

        # ════════════════════════════════════════════════
        # 条件1: 涨停板识别（前 N 日内有涨停）
        # ════════════════════════════════════════════════
        df["is_limit_up"] = np.round(df["daily_return"], 4) >= limit_up_pct
        df["has_limit_up_before"] = (
            df["is_limit_up"].rolling(window=limit_up_lookback).sum().shift(1) > 0
        )

        # 找到最近一次涨停日的位置和属性
        df["limit_up_day_offset"] = np.nan
        df["limit_up_open"] = np.nan
        df["limit_up_high"] = np.nan
        df["limit_up_low"] = np.nan
        df["limit_up_vol"] = np.nan
        limit_up_indices = df.index[df["is_limit_up"]].tolist()
        for i in range(len(df)):
            prev_limit_ups = [idx for idx in limit_up_indices if idx < i]
            if prev_limit_ups:
                last_lu = prev_limit_ups[-1]
                offset = i - last_lu
                if offset <= limit_up_lookback:
                    df.loc[df.index[i], "limit_up_day_offset"] = offset
                    df.loc[df.index[i], "limit_up_open"] = df.loc[df.index[last_lu], "open"]
                    df.loc[df.index[i], "limit_up_high"] = df.loc[df.index[last_lu], "high"]
                    df.loc[df.index[i], "limit_up_low"] = df.loc[df.index[last_lu], "low"]
                    df.loc[df.index[i], "limit_up_vol"] = df.loc[df.index[last_lu], "volume"]

        # ════════════════════════════════════════════════
        # 条件2: 洗盘识别（首板后放量大阴线）
        # v0.6.4: 大幅放宽条件（跌幅>-1%且放量>1.0倍）
        # ════════════════════════════════════════════════
        df["is_wash_day"] = (
            (df["limit_up_day_offset"] <= wash_day_max)
            & (df["daily_return"] < -0.01)  # v0.6.4: -2% → -1%（只要是阴线就算洗盘）
            & (df["vol_ratio"] > 1.0)  # v0.6.4: 1.2 → 1.0（只要放量就算）
        )

        # 追踪最近一次洗盘日的属性
        df["wash_day_high"] = np.nan
        df["wash_day_low"] = np.nan
        df["wash_day_offset"] = np.nan
        wash_indices = df.index[df["is_wash_day"]].tolist()
        for i in range(len(df)):
            prev_washes = [idx for idx in wash_indices if idx < i]
            if prev_washes:
                last_wash = prev_washes[-1]
                offset = i - last_wash
                if offset <= limit_up_lookback:
                    df.loc[df.index[i], "wash_day_high"] = df.loc[df.index[last_wash], "high"]
                    df.loc[df.index[i], "wash_day_low"] = df.loc[df.index[last_wash], "low"]
                    df.loc[df.index[i], "wash_day_offset"] = offset

        # ════════════════════════════════════════════════
        # 条件3: 护盘判断（洗盘后不破关键支撑位）
        # ════════════════════════════════════════════════
        # 护盘阶段：洗盘后到反包前，不破首板开盘价/MA20
        # 支撑位 = max(首板开盘价, MA20)
        df["support_level"] = df[["limit_up_open", "ma_trend"]].max(axis=1)

        # 护盘信号：在洗盘后，价格接近支撑位但不破（或破位收回）
        # v0.6.4: 大幅放宽容忍度（±5% → ±12%）
        df["near_support"] = (
            df["wash_day_offset"].notna()
            & (df["wash_day_offset"] >= 1)
            & (df["wash_day_offset"] <= wash_day_max)
            & (df["low"] <= df["support_level"] * 1.12)  # v0.6.4: 1.05 → 1.12
            & (df["close"] >= df["support_level"] * 0.88)  # v0.6.4: 0.95 → 0.88
        )

        # 破位收回（长下影线也算护盘信号）
        # v0.6.4: 大幅放宽条件（下影线>2%→>1%，支撑位0.93→0.85）
        df["bounce_from_support"] = (
            (df["lower_shadow"] > 0.01)  # v0.6.4: 0.02 → 0.01
            & (df["close"] >= df["support_level"] * 0.85)  # v0.6.4: 0.93 → 0.85
        )

        # ════════════════════════════════════════════════
        # 条件4: 反包突破（收盘价 > 洗盘日最高价）
        # ════════════════════════════════════════════════
        _cond_rebound = df["close"] > df["wash_day_high"]

        # ════════════════════════════════════════════════
        # 条件5: 趋势过滤（MA20向上 + 收盘价在MA20上方）
        # v0.6.4: 完全放宽趋势要求（只要不是大跌趋势就行）
        # ════════════════════════════════════════════════
        cond_trend = df["ma_slope"] > -0.05  # v0.6.4: -0.01 → -0.05（只要不是暴跌就行）

        # ════════════════════════════════════════════════
        # 买点1: 护盘阶段缩量回踩支撑位
        # v0.6.4: 放宽条件（只要不是巨量就行）
        # ════════════════════════════════════════════════
        buy_support_bounce = (
            df["near_support"]
            & (df["vol_ratio"] <= 1.5)  # v0.6.4: shrink_vol_ratio → 1.5（只要不是放量异常就行）
            & (df["is_yang"] | df["bounce_from_support"])
        )

        # ════════════════════════════════════════════════
        # 买点2: 底背离信号（MACD底背离 + 缩量）
        # v0.6.4: 放宽条件（移除wash_day_offset限制）
        # ════════════════════════════════════════════════
        # 底背离：股价创新低但MACD不创新低
        df["price_new_low"] = df["low"] < df["low"].rolling(10).min().shift(1)
        df["macd_not_new_low"] = df["macd_dif"] > df["macd_dif"].rolling(10).min().shift(1)
        df["bottom_divergence"] = df["price_new_low"] & df["macd_not_new_low"]

        buy_bottom_divergence = (
            df["bottom_divergence"] & (df["vol_ratio"] <= 1.5)  # v0.6.4: shrink_vol_ratio → 1.5
        )

        # ════════════════════════════════════════════════
        # 买点3: 有效放量后的回踩
        # v0.6.4: 放宽条件（只要有洗盘形态就行）
        # ════════════════════════════════════════════════
        # 有效放量：当日量比 > 1.2（v0.6.4: 1.5→1.2）
        df["effective_volume"] = df["vol_ratio"] >= 1.2
        # 有效放量后回踩：前几日有放量，今日缩量回踩支撑位
        df["prev_effective_vol"] = (
            df["effective_volume"].rolling(5).max().shift(1).astype(bool)
        )  # v0.6.4: 3→5日
        buy_volume_pullback = (
            df["prev_effective_vol"]
            & (df["vol_ratio"] <= 1.5)  # v0.6.4: shrink_vol_ratio → 1.5
            & (
                df["wash_day_offset"].notna() | df["has_limit_up_before"]
            )  # v0.6.4: 只要有涨停历史或洗盘就行
        )

        # ════════════════════════════════════════════════
        # 核心买入信号（买点1 OR 买点2 OR 买点3 + 护盘状态）
        # v0.6.4: 大幅放宽（只要有涨停历史+任意买点信号即可）
        # ════════════════════════════════════════════════
        min_confidence = float(self.params.get("min_confidence", 0.0))

        # 护盘状态：洗盘后、反包前（v0.6.4: 放宽窗口）
        in_protection = (
            df["wash_day_offset"].notna()
            & (df["wash_day_offset"] >= 1)
            & (df["wash_day_offset"] <= wash_day_max)
        )

        # v0.6.4: 只要有涨停历史+趋势OK+任意买点信号即可
        core_buy_cond = (
            df["has_limit_up_before"]
            & cond_trend
            & (buy_support_bounce | buy_bottom_divergence | buy_volume_pullback | in_protection)
        )

        # ════════════════════════════════════════════════
        # 评分项（置信度，0-1）
        # v0.6.4: 大幅放宽（基础分更高，更多信号通过）
        # ════════════════════════════════════════════════
        # 1. 洗盘充分度（洗盘日跌幅越大、量越大越好）
        score_wash_sufficient = (
            (df["wash_day_low"] / df["limit_up_open"] - 1).clip(-0.20, 0).abs()
            / 0.20
            * 0.15  # v0.6.4: 0.15→0.20
        ).fillna(0.10)  # v0.6.4: 无洗盘也给基础分

        # 2. 护盘强度（不破支撑位）
        score_protection = ((df["close"] > df["support_level"]).astype(float) * 0.15).fillna(
            0.08
        )  # v0.6.4: 无支撑也给基础分

        # 3. 买入信号类型加权
        score_buy_type = np.where(
            buy_bottom_divergence,
            0.20,
            np.where(buy_volume_pullback, 0.18, 0.15),
        )

        # 4. 趋势共振（MA20向上）
        score_trend_up = (df["ma_slope"] > -0.02).astype(float) * 0.12  # v0.6.4: >0 → >-0.02

        # 5. 时效性（洗盘后1-7日最佳）
        # v0.6.4: 放宽窗口（1-5日→1-7日），无洗盘也给基础分
        score_timing = (
            ((df["wash_day_offset"] >= 1) & (df["wash_day_offset"] <= wash_day_max)).astype(float)
            * 0.10
        ).fillna(0.05)  # v0.6.4: 无洗盘也给基础分

        # 6. 量能配合（缩量回踩）
        score_vol_match = (1 - df["vol_ratio"].clip(0, 1)).clip(0, 1) * 0.08

        df["confidence_score"] = (
            score_wash_sufficient
            + score_protection
            + score_buy_type
            + score_trend_up
            + score_timing
            + score_vol_match
        ).clip(0, 1)

        # ════════════════════════════════════════════════
        # 卖出条件（持仓状态判断）
        # ════════════════════════════════════════════════
        buy_cond_final = core_buy_cond & (df["confidence_score"] >= min_confidence)

        # 持仓窗口
        position_window = time_stop_window * 4
        df["in_position"] = buy_cond_final.rolling(window=position_window).sum() > 0

        # 追踪入场点信息
        df["entry_low"] = np.nan
        df["entry_close"] = np.nan
        df.loc[buy_cond_final, "entry_low"] = df.loc[buy_cond_final, "low"]
        df.loc[buy_cond_final, "entry_close"] = df.loc[buy_cond_final, "close"]
        df["entry_low"] = df["entry_low"].ffill()
        df["entry_close"] = df["entry_close"].ffill()

        # ── 卖点1: 反包成功减仓 ──
        # 反包成功：收盘价突破洗盘日最高价
        df["rebound_success"] = df["in_position"] & (df["close"] > df["wash_day_high"])

        # ── 卖点2: 顶背离信号 ──
        # 顶背离：股价创新高但MACD不创新高
        df["price_new_high"] = df["high"] > df["high"].rolling(10).max().shift(1)
        df["macd_not_new_high"] = df["macd_dif"] < df["macd_dif"].rolling(10).max().shift(1)
        df["top_divergence"] = df["price_new_high"] & df["macd_not_new_high"]
        sell_top_divergence = df["in_position"] & df["top_divergence"]

        # ── 卖点3: 量价背离 ──
        # 价格创新高但量能萎缩
        df["vol_shrink"] = df["vol_ratio"] < df["vol_ratio"].rolling(5).mean() * 0.7
        sell_vol_price_divergence = df["in_position"] & df["price_new_high"] & df["vol_shrink"]

        # ── 卖点4: 破位止损 ──
        # 跌破入场点 low × (1 - stop_loss_pct)
        cond_stop_loss_1 = df["in_position"] & (df["close"] < df["entry_low"] * (1 - stop_loss_pct))
        # 跌破 MA20
        cond_stop_loss_2 = df["in_position"] & (df["close"] < df["ma_trend"])

        # ── 卖点5: 不及预期（次日绿开/无拉升） ──
        # 简化为：持仓中价格连续下跌超过2%
        df["consecutive_decline"] = (
            df["in_position"] & (df["daily_return"] < -0.02) & (df["daily_return"].shift(1) < -0.02)
        )

        # ── 止盈1: 大阳线冲高减仓 ──
        df["big_yang"] = (
            df["is_yang"] & (df["daily_return"] >= take_profit_pct * 0.6) & (df["vol_ratio"] >= 1.5)
        )
        cond_take_profit_1 = df["in_position"] & df["big_yang"]

        # ── 止盈2: 高位巨量滞涨清仓 ──
        df["stagnation"] = (
            (df["vol_ratio"] >= stagnation_vol_ratio)
            & (df["upper_shadow"] > max_upper_shadow_pct)
            & (df["daily_return"].abs() < 0.02)
        )
        cond_take_profit_2 = df["in_position"] & df["stagnation"]

        # ── 时间止损: 持仓超期 + 涨幅不足 ──
        df["return_since_entry"] = (df["close"] - df["entry_close"]) / df["entry_close"]
        df["days_since_entry"] = 0
        buy_idx_list = df.index[buy_cond_final].tolist()
        for i in range(len(df)):
            prev_buys = [idx for idx in buy_idx_list if idx <= i]
            if prev_buys:
                df.loc[df.index[i], "days_since_entry"] = i - prev_buys[-1]
        cond_time_stop = (
            df["in_position"]
            & (df["days_since_entry"] >= time_stop_window)
            & (df["return_since_entry"] < time_stop_threshold)
        )

        # ════════════════════════════════════════════════
        # 信号赋值（卖出优先于买入）
        # ════════════════════════════════════════════════
        df["signal"] = 0
        df.loc[buy_cond_final, "signal"] = 1

        # 卖出信号：止损优先，然后是止盈和其他
        df.loc[
            cond_stop_loss_1
            | cond_stop_loss_2
            | cond_take_profit_1
            | cond_take_profit_2
            | cond_time_stop
            | sell_top_divergence
            | sell_vol_price_divergence
            | df["consecutive_decline"],
            "signal",
        ] = -1

        # ════════════════════════════════════════════════
        # 置信度赋值
        # v0.6.4: 放宽下限（0.5→0.3），让更多信号通过
        # ════════════════════════════════════════════════
        df["confidence"] = 0.0
        df.loc[buy_cond_final, "confidence"] = df.loc[buy_cond_final, "confidence_score"].clip(
            0.30,
            0.85,  # v0.6.4: 0.5→0.3, 0.90→0.85
        )

        return df
