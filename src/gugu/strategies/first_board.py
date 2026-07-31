"""首板战法（北京炒家）：首板套利 + 极致风控 + 机械执行。

来源：北京炒家首板战法（公开信息整理）

第一性原理：
- 只做个股首次涨停（首板），次日冲高即卖，不参与连板接力
- 靠高频交易（年化200-300笔）滚复利，单月目标收益10%
- 核心模式：首板套利 + 极致风控 + 机械执行

═══════════════════════════════════════════════════════════
选股标准（买入前的筛选条件）
═══════════════════════════════════════════════════════════
1. 时间要求：
   - 只做上午10:30之前封死涨停的个股
   - 优先选择9:30-10:00快速封板的个股
   - 放弃尾盘封板（尤其14:30后的偷袭板）

2. 市值与价格：
   - 流通市值30-100亿
   - 股价<20元
   - 非机构重仓股

3. 题材与板块效应：
   - 当日同概念至少3只涨停，形成板块效应
   - 优先选择新题材（政策/突发新闻驱动）
   - 优先选择板块内首只涨停的先锋股

4. 封单质量：
   - 封单金额 > 当日成交额的20%
   - 或封单金额 > 流通市值的1%
   - 封单稳定，不频繁撤单

5. 换手率：
   - 换手率5%-15%，代表充分换手、浮筹清洗干净
   - 排除缩量秒板与爆量烂板

6. 技术形态：
   - 股价处于历史低位或中期调整末端
   - 突破关键压力位（年线/周线平台、成交密集区等）
   - 首板成交量放大至前5日均量的2-3倍，但不超过历史天量

7. 股性筛选：
   - 过去1年内至少3次涨停且次日平均溢价>2%
   - 或半年内至少3次涨停

8. 排除项：
   - ST股、次新股、一字板（无换手）、公告利好板
   - 高位股（近期涨幅过大）、下降趋势的板
   - 近1个月有减持/解禁、业绩暴雷、被监管问询的个股

═══════════════════════════════════════════════════════════
买入规则（何时及如何买入）
═══════════════════════════════════════════════════════════
1. 买入时机：
   - 涨停封单瞬间挂单（排板为主，扫板为辅）
   - 确认封单稳定、无明显开板风险再成交
   - 回封板优先：股票涨停后打开、再次回封时买入

2. 单票仓位：
   - 单票仓位≤10%（极端行情≤5%）
   - 每日持仓3-5只，分散风险

═══════════════════════════════════════════════════════════
卖出规则（何时卖出）
═══════════════════════════════════════════════════════════
1. 核心原则：
   - 隔日卖出：绝大多数情况下第二天卖出，无论盈亏
   - 不格局、不恋战，10多分钟卖完
   - 二连板可继续持有，开板必走

2. 高开处理：
   - 高开≥3%：集合竞价卖一半，剩余观察10分钟，不冲板则清仓
   - 高开≥5%但板块效应低：卖出50%，剩余10:30前不封板则出货

3. 低开处理：
   - 低开>2%：开盘3分钟内无条件止损
   - 低开≤2%：观察15分钟，无法翻红则清仓
   - 低开低走反抽、跌停必走

4. 盘中处理：
   - 高开低走或盘中杀跌破分时均线，挂跌3%止损单
   - 红了以后翻绿开始分批出
   - 绝不补仓、不硬抗

═══════════════════════════════════════════════════════════
仓位管理与风控（生命线）
═══════════════════════════════════════════════════════════
1. 分仓规则：
   - 70万以下：1-2仓，单票≤50%
   - 70万-500万：2-4仓，单票≤15%
   - 500万-2000万：4-8仓，单票≤10%
   - 2000万以上：8仓+，单票≤5%

2. 硬性停盘规则：
   - 单日总亏损≥2% → 立即停手，停止交易3天
   - 月回撤>10% → 当月空仓休息

3. 其他风控：
   - 永不满仓，保留现金应对极端行情
   - 坚决不用杠杆（融资、配资）
   - 封板率低于80%时降低仓位
   - 弱市/退潮期直接空仓

═══════════════════════════════════════════════════════════
关键量化参数
═══════════════════════════════════════════════════════════
- 流通市值范围 = 30-100亿
- 股价上限 = 20元
- 涨停判定阈值 = 9.5%（A股10%涨停板留0.5%容差）
- 首板换手率范围 = 5%-15%
- 量比范围 = 2-3倍（首板成交量/前5日均量）
- 封单比下限 = 20%（封单金额/当日成交额）
- 历史涨停次数 = 半年内至少3次
- 次日止损阈值 = 低开2%（开盘3分钟止损）
- 次日止盈阈值 = 高开3%（竞价卖一半）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class FirstBoardStrategy(Strategy):
    """首板战法（北京炒家）：首板套利 + 极致风控 + 机械执行。

    只做个股首次涨停（首板），次日冲高即卖，不参与连板接力。
    靠高频交易（年化200-300笔）滚复利，单月目标收益10%。
    """

    name = "first_board"
    category = "breakout"

    def validate_params(self) -> None:
        """校验参数语义。"""
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up_pct <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11% 之间")
        market_cap_min = float(self.params.get("market_cap_min", 30e8))
        market_cap_max = float(self.params.get("market_cap_max", 100e8))
        if market_cap_min >= market_cap_max:
            raise ValueError("market_cap_min 必须小于 market_cap_max")
        price_max = float(self.params.get("price_max", 20.0))
        if price_max <= 0:
            raise ValueError("price_max 必须大于 0")
        turnover_min = float(self.params.get("turnover_min", 0.05))
        turnover_max = float(self.params.get("turnover_max", 0.15))
        if turnover_min >= turnover_max:
            raise ValueError("turnover_min 必须小于 turnover_max")
        vol_ratio_min = float(self.params.get("vol_ratio_min", 2.0))
        vol_ratio_max = float(self.params.get("vol_ratio_max", 3.0))
        if vol_ratio_min >= vol_ratio_max:
            raise ValueError("vol_ratio_min 必须小于 vol_ratio_max")
        lookback = int(self.params.get("limit_up_lookback", 120))
        if lookback < 30:
            raise ValueError("limit_up_lookback 必须大于等于 30")

    def _ensure_columns(self, df: pd.DataFrame) -> None:
        """确保必要列存在。"""
        required = ["open", "high", "low", "close", "volume", "amount"]
        for col in required:
            if col not in df.columns:
                raise ValueError(f"缺少必要列: {col}")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成首板战法信号（北京炒家完整版）。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        turnover_min = float(self.params.get("turnover_min", 0.05))
        turnover_max = float(self.params.get("turnover_max", 0.15))
        vol_ratio_min = float(self.params.get("vol_ratio_min", 2.0))
        vol_ratio_max = float(self.params.get("vol_ratio_max", 3.0))
        limit_up_lookback = int(self.params.get("limit_up_lookback", 120))
        history_limit_up_min = int(self.params.get("history_limit_up_min", 3))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.02))
        take_profit_pct = float(self.params.get("take_profit_pct", 0.03))
        time_stop_window = int(self.params.get("time_stop_window", 1))

        # ── 计算基础指标 ──────────────────────────────
        df["daily_return"] = df["close"].pct_change()
        df["prev_close"] = df["close"].shift(1)
        df["prev_open"] = df["open"].shift(1)
        df["prev_high"] = df["high"].shift(1)
        df["prev_low"] = df["low"].shift(1)
        df["prev_volume"] = df["volume"].shift(1)
        df["prev_amount"] = df["amount"].shift(1)

        # ── 均线系统 ──────────────────────────────────
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()

        # ── 量能指标 ──────────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # ════════════════════════════════════════════════
        # 条件1: 涨停板识别（首次涨停判定）
        # ════════════════════════════════════════════════
        df["is_limit_up"] = np.round(df["daily_return"], 4) >= limit_up_pct

        # 首板判定：当日涨停且前一日未涨停（排除连板）
        df["prev_is_limit_up"] = df["is_limit_up"].shift(1).fillna(False)
        df["is_first_board"] = df["is_limit_up"] & (~df["prev_is_limit_up"])

        # ════════════════════════════════════════════════
        # 条件2: 换手率判断（5%-15%）
        # ════════════════════════════════════════════════
        # 换手率 = 成交量 / 流通股本（这里用 amount / close 近似）
        # 由于没有流通股本数据，用 vol_ratio 作为替代指标
        df["turnover_ok"] = (df["vol_ratio"] >= turnover_min * 10) & (
            df["vol_ratio"] <= turnover_max * 10
        )

        # ════════════════════════════════════════════════
        # 条件3: 量能放大（2-3倍前5日均量）
        # ════════════════════════════════════════════════
        df["vol_expand_ok"] = (df["vol_ratio"] >= vol_ratio_min) & (
            df["vol_ratio"] <= vol_ratio_max
        )

        # ════════════════════════════════════════════════
        # 条件4: 技术形态（突破关键压力位）
        # ════════════════════════════════════════════════
        # 突破年线（MA250 ≈ MA20这里用MA60近似）
        df["break_ma60"] = df["close"] > df["ma60"]
        # 突破20日高点
        df["high_20d"] = df["high"].rolling(20).max().shift(1)
        df["break_20d_high"] = df["close"] > df["high_20d"]
        # 股价在MA20上方（趋势向上）
        df["above_ma20"] = df["close"] > df["ma20"]

        # ════════════════════════════════════════════════
        # 条件5: 股性筛选（历史涨停次数）
        # ════════════════════════════════════════════════
        df["history_limit_up_count"] = (
            df["is_limit_up"].rolling(window=limit_up_lookback).sum().shift(1)
        )
        df["is_active_stock"] = df["history_limit_up_count"] >= history_limit_up_min

        # ════════════════════════════════════════════════
        # 条件6: 非连板排除（首板前无涨停）
        # ════════════════════════════════════════════════
        df["recent_limit_up"] = df["is_limit_up"].rolling(window=5).sum().shift(1)
        df["no_recent_limit_up"] = df["recent_limit_up"] == 0

        # ════════════════════════════════════════════════
        # 买入信号：首板 + 换手 + 量能 + 技术 + 股性
        # ════════════════════════════════════════════════
        buy_first_board = (
            df["is_first_board"]
            & df["turnover_ok"]
            & df["vol_expand_ok"]
            & df["above_ma20"]
            & (df["break_ma60"] | df["break_20d_high"])
            & df["is_active_stock"]
            & df["no_recent_limit_up"]
        )

        # ════════════════════════════════════════════════
        # 评分项（置信度，0-1）
        # ════════════════════════════════════════════════
        # 1. 封板强度（量比适中，不过大不过小）
        score_vol_fit = (1.0 - (df["vol_ratio"] - 2.5).abs() / 2.5).clip(0, 1) * 0.20

        # 2. 突破力度（收盘价相对MA60偏离度）
        score_break_strength = ((df["close"] / df["ma60"] - 1).clip(0, 0.10) / 0.10) * 0.15

        # 3. 股性活跃度（历史涨停次数）
        score_activity = (df["history_limit_up_count"].clip(0, 10) / 10) * 0.15

        # 4. 趋势强度（MA20斜率）
        df["ma20_slope"] = df["ma20"].diff(5) / df["ma20"].shift(5)
        score_trend = df["ma20_slope"].clip(0, 0.05) / 0.05 * 0.20

        # 5. 换手充分度（换手率在10%左右最佳）
        score_turnover = (1.0 - (df["vol_ratio"] - 1.0).abs() / 1.0).clip(0, 1) * 0.15

        # 6. 位置优势（相对60日高低点位置）
        df["high_60d"] = df["high"].rolling(60).max().shift(1)
        df["low_60d"] = df["low"].rolling(60).min().shift(1)
        df["position_pct"] = (df["close"] - df["low_60d"]) / (
            df["high_60d"] - df["low_60d"]
        ).replace(0, np.nan)
        # 位置在30%-70%最佳（低位突破或中期调整末端）
        score_position = (1.0 - (df["position_pct"] - 0.5).abs() / 0.5).clip(0, 1) * 0.15

        df["confidence_score"] = (
            score_vol_fit
            + score_break_strength
            + score_activity
            + score_trend
            + score_turnover
            + score_position
        ).clip(0, 1)

        # ════════════════════════════════════════════════
        # 卖出信号（隔日卖出逻辑）
        # ════════════════════════════════════════════════
        # 买入后次日卖出信号
        df["in_position"] = buy_first_board.rolling(window=time_stop_window + 1).sum() > 0

        # 追踪入场点信息
        df["entry_close"] = np.nan
        df.loc[buy_first_board, "entry_close"] = df.loc[buy_first_board, "close"]
        df["entry_close"] = df["entry_close"].ffill()

        # ── 卖点1: 高开止盈（高开≥3%）──
        df["gap_up"] = (df["open"] - df["prev_close"]) / df["prev_close"]
        sell_high_open = df["in_position"] & (df["gap_up"] >= take_profit_pct)

        # ── 卖点2: 低开止损（低开>2%）──
        sell_low_open = df["in_position"] & (df["gap_up"] < -stop_loss_pct)

        # ── 卖点3: 时间止损（持仓超期）──
        df["days_since_entry"] = 0
        buy_idx_list = df.index[buy_first_board].tolist()
        for i in range(len(df)):
            prev_buys = [idx for idx in buy_idx_list if idx <= i]
            if prev_buys:
                df.loc[df.index[i], "days_since_entry"] = i - prev_buys[-1]
        sell_time_stop = df["in_position"] & (df["days_since_entry"] >= time_stop_window)

        # ── 卖点4: 破位止损（跌破入场日收盘价）──
        sell_break_entry = df["in_position"] & (df["close"] < df["entry_close"])

        # ════════════════════════════════════════════════
        # 信号赋值（卖出优先于买入）
        # ════════════════════════════════════════════════
        min_confidence = float(self.params.get("min_confidence", 0.0))
        buy_cond_final = buy_first_board & (df["confidence_score"] >= min_confidence)

        df["signal"] = 0
        df.loc[buy_cond_final, "signal"] = 1

        # 卖出信号：时间止损优先（隔日必卖），然后是高低开
        df.loc[
            sell_time_stop | sell_high_open | sell_low_open | sell_break_entry,
            "signal",
        ] = -1

        # ════════════════════════════════════════════════
        # 置信度赋值
        # ════════════════════════════════════════════════
        df["confidence"] = 0.0
        df.loc[buy_cond_final, "confidence"] = df.loc[buy_cond_final, "confidence_score"].clip(
            0.5, 0.90
        )

        return df
