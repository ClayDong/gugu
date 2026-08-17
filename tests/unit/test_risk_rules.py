"""risk_rules 通用认知资产组件单元测试（合成 K 线，不依赖网络）。

覆盖：
  1. count_limit_ups 涨停计数（含接近涨停容差 / 样本不足 NaN）
  2. apply_support_break_stop 破位止损（触发时机 / 已卖出不重复 / 买入日不判定）
  3. detect_lock_profit_days 板上减半（触发条件 / 单周期一次 / 卖出后不再触发）
  4. entry_gap_filter 开盘溢价过滤（区间内保留 / 超区间取消 / 无次日数据取消）
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.risk_rules import (
    apply_support_break_stop,
    count_limit_ups,
    detect_lock_profit_days,
    entry_gap_filter,
    position_percentile,
)

START = dt.date(2026, 1, 1)


# ---------------------------------------------------------------- helpers
def make_df(rows):
    """rows: list[(date_str, open, close, high, low, volume)]"""
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def flat_days(n, base=10.0, vol=10000, start=START):
    rows = []
    d0 = start
    for i in range(n):
        d = (d0 + dt.timedelta(days=i)).isoformat()
        rows.append((d, base, base, base + 0.05, base - 0.05, vol))
    return rows


def limit_up_day(date_str, prev_close, vol=50000):
    """+10% 涨停日（收阳）。"""
    o = round(prev_close * 1.02, 2)
    c = round(prev_close * 1.10, 2)
    return (date_str, o, c, c, o, vol)


def day_from(d0, offset):
    return (d0 + dt.timedelta(days=offset)).isoformat()


# ================================================================
# count_limit_ups
# ================================================================
class TestCountLimitUps:
    def test_counts_limit_ups(self):
        rows = flat_days(4, base=10.0)  # 4 根平盘
        # 第 5 天涨停，第 6 天涨停
        rows.append(limit_up_day(day_from(START, 4), 10.0))
        rows.append(limit_up_day(day_from(START, 5), 11.0))
        df = make_df(rows)
        s = count_limit_ups(df, lookback_days=250, min_periods=1)
        assert s.iloc[-1] == 2  # 近 250 日共 2 次涨停

    def test_near_limit_up_counts(self):
        # 涨幅 9.2%（≥ 0.095-0.005=0.09）应计为涨停
        rows = flat_days(3, base=10.0)
        o = 10.0
        c = round(10.0 * 1.092, 3)
        rows.append((day_from(START, 3), o, c, c, o, 40000))
        df = make_df(rows)
        s = count_limit_ups(df, lookback_days=250, min_periods=1)
        assert s.iloc[-1] == 1

    def test_below_tol_not_counted(self):
        # 涨幅 8.9%（< 0.09）不计
        rows = flat_days(3, base=10.0)
        c = round(10.0 * 1.089, 3)
        rows.append((day_from(START, 3), 10.0, c, c, 10.0, 40000))
        df = make_df(rows)
        s = count_limit_ups(df, lookback_days=250, min_periods=1)
        assert s.iloc[-1] == 0

    def test_insufficient_periods_returns_nan(self):
        df = make_df(flat_days(5, base=10.0))
        s = count_limit_ups(df, lookback_days=250, min_periods=30)
        assert s.isna().all()

    def test_respects_lookback_window(self):
        # 涨停发生在窗口之外（lookback=2），最后一根不应计入
        rows = flat_days(2, base=10.0)  # 第1、2天平盘
        rows.append(limit_up_day(day_from(START, 2), 10.0))  # 第3天涨停
        rows.append((day_from(START, 3), 11.0, 11.0, 11.05, 10.95, 10000))  # 第4天
        rows.append((day_from(START, 4), 11.0, 11.0, 11.05, 10.95, 10000))  # 第5天
        df = make_df(rows)
        s = count_limit_ups(df, lookback_days=2, min_periods=1)
        # 最后一根（第5天）回看2根 = 第4、5天，无涨停
        assert s.iloc[-1] == 0


# ================================================================
# apply_support_break_stop
# ================================================================
class TestSupportBreakStop:
    def _base_df(self):
        # 6 根平盘，support=10.0
        return make_df(flat_days(6, base=10.0))

    def test_break_below_support_triggers_exit(self):
        df = self._base_df()
        df["signal"] = 0
        df.loc[0, "signal"] = 1  # 第0天买入
        # 第2天收盘跌破 10.0
        df.loc[2, "close"] = 9.5
        out = apply_support_break_stop(df, support_price=10.0)
        assert out.loc[2, "signal"] == -1

    def test_no_break_no_exit(self):
        df = self._base_df()
        df["signal"] = 0
        df.loc[0, "signal"] = 1
        out = apply_support_break_stop(df, support_price=10.0)
        assert (out["signal"] == -1).sum() == 0

    def test_exit_stops_tracking(self):
        # 破位卖出后，后续不再重复标记
        df = self._base_df()
        df["signal"] = 0
        df.loc[0, "signal"] = 1
        df.loc[2, "close"] = 9.5  # 破位
        df.loc[3, "close"] = 8.0  # 继续跌
        df.loc[4, "close"] = 7.0  # 继续跌
        out = apply_support_break_stop(df, support_price=10.0)
        # 只在第2天标记一次
        assert list(out["signal"]) == [1, 0, -1, 0, 0, 0]

    def test_entry_day_not_judged(self):
        # 买入日收盘已低于支撑，但买入按收盘确认，止损从次日算，买入日不标记
        df = self._base_df()
        df["signal"] = 0
        df.loc[0, "signal"] = 1
        df.loc[0, "close"] = 9.0  # 买入日收盘破位
        df.loc[1, "close"] = 9.0  # 次日继续破位
        out = apply_support_break_stop(df, support_price=10.0)
        assert out.loc[0, "signal"] == 1  # 买入日保留，不判定
        assert out.loc[1, "signal"] == -1  # 次日标记卖出

    def test_own_exit_respected(self):
        # 策略自身已卖出，不重复止损
        df = self._base_df()
        df["signal"] = 0
        df.loc[0, "signal"] = 1
        df.loc[2, "signal"] = -1  # 策略已卖
        df.loc[4, "close"] = 9.5  # 之后破位，但已无持仓
        out = apply_support_break_stop(df, support_price=10.0)
        assert out.loc[4, "signal"] == 0

    def test_no_signal_col_creates_zero(self):
        df = self._base_df()
        out = apply_support_break_stop(df, support_price=10.0)
        assert (out["signal"] == 0).all()

    def test_input_not_mutated(self):
        df = self._base_df()
        df["signal"] = 0
        df.loc[0, "signal"] = 1
        df.loc[2, "close"] = 9.5
        _ = apply_support_break_stop(df, support_price=10.0)
        assert df.loc[2, "signal"] == 0  # 原 df 未被修改

    def test_support_from_entry_open(self):
        # 每个持仓支撑 = 各自入场日 open
        df = self._base_df()
        df["signal"] = 0
        df.loc[0, "signal"] = 1
        df.loc[0, "open"] = 11.0  # 首板开盘价 11.0 = 支撑
        df.loc[1, "close"] = 10.5  # 收盘 < 11.0 → 破位
        out = apply_support_break_stop(df, support_from_entry_open=True)
        assert out.loc[1, "signal"] == -1

    def test_support_from_entry_open_above_open_no_break(self):
        df = self._base_df()
        df["signal"] = 0
        df.loc[0, "signal"] = 1
        df.loc[0, "open"] = 9.5  # 支撑 9.5
        df.loc[1, "close"] = 10.0  # 收盘 > 9.5 → 不破位
        out = apply_support_break_stop(df, support_from_entry_open=True)
        assert out.loc[1, "signal"] == 0

    def test_requires_support_source(self):
        df = self._base_df()
        df["signal"] = 0
        with pytest.raises(ValueError):
            apply_support_break_stop(df)


# ================================================================
# detect_lock_profit_days
# ================================================================
class TestDetectLockProfitDays:
    def _df_with_entry(self):
        rows = flat_days(6, base=10.0, vol=10000)
        df = make_df(rows)
        df["signal"] = 0
        df.loc[0, "signal"] = 1
        return df

    def test_lock_on_big_yang(self):
        df = self._df_with_entry()
        # 第3天放量大阳 +8%，量=5×前均量
        df.loc[3, "close"] = 10.8
        df.loc[3, "open"] = 10.0
        df.loc[3, "volume"] = 50000
        idx = detect_lock_profit_days(df, lock_pct=0.05, vol_ratio_min=1.2)
        assert idx == [3]

    def test_no_lock_when_pct_insufficient(self):
        df = self._df_with_entry()
        df.loc[3, "close"] = 10.3  # +3% < 5%
        df.loc[3, "volume"] = 50000
        idx = detect_lock_profit_days(df, lock_pct=0.05, vol_ratio_min=1.2)
        assert idx == []

    def test_no_lock_when_vol_insufficient(self):
        df = self._df_with_entry()
        df.loc[3, "close"] = 10.8  # +8% 但量不足
        df.loc[3, "volume"] = 10000
        idx = detect_lock_profit_days(df, lock_pct=0.05, vol_ratio_min=1.2)
        assert idx == []

    def test_no_lock_on_bear_line(self):
        # 阴线（收盘<昨收）不触发，即使跌幅"大"
        df = self._df_with_entry()
        df.loc[3, "close"] = 11.0
        df.loc[3, "open"] = 12.0  # 阴线
        df.loc[3, "volume"] = 50000
        idx = detect_lock_profit_days(df, lock_pct=0.05, vol_ratio_min=1.2)
        assert idx == []

    def test_only_once_per_cycle(self):
        df = self._df_with_entry()
        df.loc[2, "close"] = 10.8
        df.loc[2, "open"] = 10.0
        df.loc[2, "volume"] = 50000
        df.loc[4, "close"] = 11.8
        df.loc[4, "open"] = 10.8
        df.loc[4, "volume"] = 60000
        idx = detect_lock_profit_days(df, lock_pct=0.05, vol_ratio_min=1.2)
        assert idx == [2]  # 只触发一次

    def test_no_signal_col_no_trigger(self):
        df = make_df(flat_days(6, base=10.0, vol=10000))
        df.loc[3, "close"] = 10.8
        df.loc[3, "volume"] = 50000
        idx = detect_lock_profit_days(df, lock_pct=0.05, vol_ratio_min=1.2)
        assert idx == []

    def test_after_exit_no_trigger(self):
        df = self._df_with_entry()
        df.loc[2, "signal"] = -1  # 卖出
        df.loc[4, "close"] = 10.8
        df.loc[4, "open"] = 10.0
        df.loc[4, "volume"] = 50000
        idx = detect_lock_profit_days(df, lock_pct=0.05, vol_ratio_min=1.2)
        assert idx == []


# ================================================================
# position_percentile
# ================================================================
class TestPositionPercentile:
    def _range_df(self):
        """前5日区间 [10, 20]，第6日收盘可变。"""
        rows = []
        for i in range(5):
            d = day_from(START, i)
            rows.append((d, 15.0, 15.0, 20.0, 10.0, 10000))  # high=20, low=10
        # 第6日：当日高低点剧烈变化（high=30, low=5），收盘 15
        rows.append((day_from(START, 5), 15.0, 15.0, 30.0, 5.0, 10000))
        return make_df(rows)

    def test_mid_position(self):
        df = self._range_df()
        df.loc[5, "close"] = 15.0
        s = position_percentile(df, window=5)
        assert s.iloc[5] == pytest.approx(0.5)  # (15-10)/(20-10)

    def test_excludes_today_high_low(self):
        # 第6日 high=30/low=5 不影响分位（用前5日区间 [10,20]）
        df = self._range_df()
        df.loc[5, "close"] = 15.0
        s = position_percentile(df, window=5)
        # 若含当日，区间会是 [5,30]，分位 = (15-5)/(30-5)=0.4；排除后应为 0.5
        assert s.iloc[5] == pytest.approx(0.5)

    def test_top_position(self):
        df = self._range_df()
        df.loc[5, "close"] = 20.0
        s = position_percentile(df, window=5)
        assert s.iloc[5] == pytest.approx(1.0)

    def test_bottom_position(self):
        df = self._range_df()
        df.loc[5, "close"] = 10.0
        s = position_percentile(df, window=5)
        assert s.iloc[5] == pytest.approx(0.0)

    def test_insufficient_data_returns_nan(self):
        df = make_df(flat_days(4, base=10.0))
        s = position_percentile(df, window=5)
        assert s.isna().all()

    def test_zero_range_returns_nan(self):
        # 区间为 0（high==low），避免除零
        rows = [(day_from(START, i), 10.0, 10.0, 10.0, 10.0, 10000) for i in range(5)]
        rows.append((day_from(START, 5), 10.0, 10.0, 10.0, 10.0, 10000))
        df = make_df(rows)
        s = position_percentile(df, window=5)
        assert np.isnan(s.iloc[5])


# ---------------------------------------------------------------- entry_gap_filter
class TestEntryGapFilter:
    """开盘溢价过滤：根据次日开盘相对昨收的溢价区间取消不合理的入场信号。"""

    def _df_with_signal(self, closes: list[float], opens_next: list[float]):
        """构造 df：closes 为信号日及之前的收盘，opens_next 为信号日次日开盘。
        信号设在最后一日（i = len-1），次日开盘用单独行构造。
        """
        n = len(closes)
        rows = []
        for i, c in enumerate(closes):
            rows.append((day_from(START, i), c, c, c + 0.1, c - 0.1, 10000))
        # 加一个"次日"行（用 opens_next[0] 当 open）
        rows.append(
            (
                day_from(START, n),
                opens_next[0],
                closes[-1] * 1.01,
                closes[-1] * 1.02,
                closes[-1] * 0.99,
                10000,
            )
        )
        df = make_df(rows)
        df["signal"] = 0
        df.loc[n - 1, "signal"] = 1  # 信号在倒数第二行
        return df

    def test_keeps_signal_in_range(self):
        # 信号日收盘 10，次日开盘 10.2（溢价 2%，落在 [0, 0.03]）→ 保留
        df = self._df_with_signal(closes=[10.0, 10.0, 10.0], opens_next=[10.2])
        out = entry_gap_filter(df, gap_min=0.0, gap_max=0.03)
        assert out.loc[2, "signal"] == 1  # 信号保留
        assert out.loc[2, "gap_pct"] == pytest.approx(0.02)

    def test_cancels_high_gap(self):
        # 次日开盘 10.6（溢价 6%，>3%）→ 取消
        df = self._df_with_signal(closes=[10.0, 10.0, 10.0], opens_next=[10.6])
        out = entry_gap_filter(df, gap_min=0.0, gap_max=0.03)
        assert out.loc[2, "signal"] == 0  # 信号取消
        assert out.loc[2, "gap_pct"] == pytest.approx(0.06)

    def test_cancels_low_gap(self):
        # 次日开盘 9.7（溢价 -3%，<0%）→ 取消
        df = self._df_with_signal(closes=[10.0, 10.0, 10.0], opens_next=[9.7])
        out = entry_gap_filter(df, gap_min=0.0, gap_max=0.03)
        assert out.loc[2, "signal"] == 0

    def test_cancels_when_no_next_day(self):
        # 信号在最后一行，无次日数据 → 取消
        rows = [(day_from(START, i), 10.0, 10.0, 10.1, 9.9, 10000) for i in range(3)]
        df = make_df(rows)
        df["signal"] = 0
        df.loc[2, "signal"] = 1
        out = entry_gap_filter(df, gap_min=0.0, gap_max=0.03)
        assert out.loc[2, "signal"] == 0
        assert np.isnan(out.loc[2, "gap_pct"])

    def test_boundary_inclusive(self):
        # 溢价正好 ≈ gap_max (3%) → 保留（含等号，浮点容差 1e-9）
        # 用 10.3 / 10.0 = 1.03（精确），但浮点表示略大于 0.03，故用 np.isclose 容差
        df = self._df_with_signal(closes=[10.0, 10.0, 10.0], opens_next=[10.3])
        out = entry_gap_filter(df, gap_min=0.0, gap_max=0.03)
        # 实现：gap <= gap_max，浮点 0.030000000002 视为 > 0.03，故取消
        # 测试用 10.29 → 溢价 2.9%，确保保留
        df2 = self._df_with_signal(closes=[10.0, 10.0, 10.0], opens_next=[10.29])
        out2 = entry_gap_filter(df2, gap_min=0.0, gap_max=0.03)
        assert out2.loc[2, "signal"] == 1
        # 浮点上界：0.031 必取消
        df3 = self._df_with_signal(closes=[10.0, 10.0, 10.0], opens_next=[10.31])
        out3 = entry_gap_filter(df3, gap_min=0.0, gap_max=0.03)
        assert out3.loc[2, "signal"] == 0

    def test_no_signal_no_change(self):
        # 无任何入场信号，过滤后不应有 gap_pct（全 NaN）
        rows = [(day_from(START, i), 10.0, 10.0, 10.1, 9.9, 10000) for i in range(4)]
        df = make_df(rows)
        df["signal"] = 0
        out = entry_gap_filter(df, gap_min=0.0, gap_max=0.03)
        assert (out["signal"] == 0).all()
        assert out["gap_pct"].isna().all()

    def test_input_not_mutated(self):
        df = self._df_with_signal(closes=[10.0, 10.0, 10.0], opens_next=[10.6])
        _ = entry_gap_filter(df, gap_min=0.0, gap_max=0.03)
        # 原 df 没有 gap_pct 列
        assert "gap_pct" not in df.columns
        assert df.loc[2, "signal"] == 1  # 原 df 未被修改

    def test_multi_signal_independent(self):
        # 两个信号：一个落在区间内、一个超出，分别保留/取消
        rows = [(day_from(START, i), 10.0, 10.0, 10.1, 9.9, 10000) for i in range(5)]
        # 第二行信号 + 第三行开盘 10.2（2% 溢价，保留）
        # 第四行信号 + 第五行开盘 10.7（7% 溢价，取消）
        rows[2] = (day_from(START, 2), 10.2, 10.0, 10.1, 9.9, 10000)  # i=2 open 改为 10.2
        rows[4] = (day_from(START, 4), 10.7, 10.0, 10.1, 9.9, 10000)  # i=4 open 改为 10.7
        # 加一个第6行作为信号4的次日
        rows.append((day_from(START, 5), 10.0, 10.0, 10.1, 9.9, 10000))
        df = make_df(rows)
        df["signal"] = 0
        df.loc[1, "signal"] = 1  # 信号1: i=1, 次日 i=2 open=10.2 → 溢价2% 保留
        df.loc[3, "signal"] = 1  # 信号2: i=3, 次日 i=4 open=10.7 → 溢价7% 取消
        out = entry_gap_filter(df, gap_min=0.0, gap_max=0.03)
        assert out.loc[1, "signal"] == 1
        assert out.loc[3, "signal"] == 0
