"""asset_combo 认知资产组合策略单元测试（合成 K 线，不依赖网络）。

覆盖：
  1. 首板 + 活跃度达标 + 位置非高位 → 买入信号
  2. 活跃度不足 / 位置过高 / 连板 → 无信号
  3. 破位止损（收盘破入场日 open）
  4. 时间止损（max_hold_days 强制退出）
  5. 板上减半（放量大阳 → lock_profit=1）

注：position_percentile 用 shift(1) 排除当日，需 window+1 天数据才有值，
故测试统一用 6 天横盘（index 0-5）+ 第 7 天（index 6）首板。
"""

import datetime as dt

import pandas as pd

from gugu.strategies.asset_combo import AssetComboStrategy

START = dt.date(2026, 1, 1)


def _day(offset, close, open_=None, high=None, low=None, volume=10000):
    d = (START + dt.timedelta(days=offset)).isoformat()
    o = open_ if open_ is not None else close
    h = high if high is not None else close + 0.5
    l = low if low is not None else close - 0.5
    return (d, o, close, h, l, volume)


def _make_df(rows):
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _params(**overrides):
    base = {
        "limit_up_pct": 0.095,
        "limit_up_lookback": 5,
        "min_limit_ups": 1,
        "position_window": 5,
        "position_max": 0.95,
        "max_hold_days": 3,
        # 次日开盘溢价区间：首板日 close=11，次日 open 应落在 [11.0, 11.33]
        "gap_min": 0.0,
        "gap_max": 0.03,
        # 关闭 regime 过滤（测试用合成数据，无 HS300 上下文）
        "only_weak_regime": False,
    }
    base.update(overrides)
    return base


class TestAssetComboBuy:
    def test_buy_signal(self):
        # 前6日横盘(close=10)，第7日涨停首板(close=11)，次日开盘 11.1（溢价 0.9%，落在区间）
        rows = [_day(i, 10.0) for i in range(6)]
        rows.append(_day(6, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))
        # 加一个"次日"行（index 7），open=11.1（平开~小高开 0.9%）
        rows.append(_day(7, 11.1, open_=11.1, high=11.2, low=11.0, volume=20000))
        df = _make_df(rows)
        s = AssetComboStrategy(_params())
        out = s.generate_signals(df, is_backtest=True)
        assert out.loc[6, "signal"] == 1

    def test_no_signal_insufficient_activity(self):
        rows = [_day(i, 10.0) for i in range(6)]
        rows.append(_day(6, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))
        rows.append(_day(7, 11.1, open_=11.1, high=11.2, low=11.0, volume=20000))
        df = _make_df(rows)
        s = AssetComboStrategy(_params(min_limit_ups=3))
        out = s.generate_signals(df, is_backtest=True)
        assert (out["signal"] == 1).sum() == 0

    def test_no_signal_high_position(self):
        # 前6日 close 贴近 high（位置分位高），第7日涨停
        rows = [_day(i, 10.4, high=10.5, low=9.5) for i in range(6)]
        rows.append(_day(6, 11.44, open_=10.6, high=11.44, low=10.6, volume=50000))
        rows.append(_day(7, 11.5, open_=11.5, high=11.6, low=11.4, volume=20000))
        df = _make_df(rows)
        s = AssetComboStrategy(_params(position_max=0.7))
        out = s.generate_signals(df, is_backtest=True)
        assert (out["signal"] == 1).sum() == 0

    def test_not_first_board(self):
        # 第6日涨停，第7日再涨停（连板，非首板）
        rows = [_day(i, 10.0) for i in range(5)]
        rows.append(_day(5, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))  # 第6日涨停
        rows.append(_day(6, 12.1, open_=11.2, high=12.1, low=11.2, volume=50000))  # 第7日涨停
        rows.append(_day(7, 12.2, open_=12.2, high=12.3, low=12.1, volume=20000))
        df = _make_df(rows)
        s = AssetComboStrategy(_params())
        out = s.generate_signals(df, is_backtest=True)
        assert out.loc[6, "signal"] == 0  # 第7日不是首板

    def test_no_signal_high_gap(self):
        # 次日大幅高开 (>3%)，信号应被取消
        rows = [_day(i, 10.0) for i in range(6)]
        rows.append(_day(6, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))
        # 次日 open=11.5（溢价 4.5%，超出 [0, 0.03]）
        rows.append(_day(7, 11.6, open_=11.5, high=11.6, low=11.4, volume=20000))
        df = _make_df(rows)
        s = AssetComboStrategy(_params())
        out = s.generate_signals(df, is_backtest=True)
        assert out.loc[6, "signal"] == 0  # 溢价过滤取消信号

    def test_no_signal_low_gap(self):
        # 次日低开 (<0%)，信号应被取消
        rows = [_day(i, 10.0) for i in range(6)]
        rows.append(_day(6, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))
        # 次日 open=10.5（溢价 -4.5%）
        rows.append(_day(7, 10.5, open_=10.5, high=10.6, low=10.4, volume=20000))
        df = _make_df(rows)
        s = AssetComboStrategy(_params())
        out = s.generate_signals(df, is_backtest=True)
        assert out.loc[6, "signal"] == 0  # 低开过滤取消信号

    def test_regime_filter_cancels_strong_market(self):
        """regime 过滤：强势期信号应被取消（但因 only_weak_regime=False 默认关闭，此测试需手动启用）。
        这里直接验证 _apply_regime_filter 逻辑：传入 mock HS300 数据。
        """
        rows = [_day(i, 10.0) for i in range(6)]
        rows.append(_day(6, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))
        rows.append(_day(7, 11.1, open_=11.1, high=11.2, low=11.0, volume=20000))
        df = _make_df(rows)
        df["signal"] = 0
        df.loc[6, "signal"] = 1
        # mock HS300: 信号日为强势
        hs = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    [(START + dt.timedelta(days=i)).isoformat() for i in range(8)]
                ),
                "regime": [0, 0, 0, 0, 0, 0, 1, 1],  # 第7日（index 6）开始强势
            }
        )
        s = AssetComboStrategy(_params())
        out = s._apply_regime_filter(df, hs, only_weak=True)
        assert out.loc[6, "signal"] == 0  # 强势期取消信号
        assert out.loc[6, "regime"] == 1


class TestAssetComboExit:
    def test_support_break_stop(self):
        # 首板第7日(open=10.2,close=11)，次日开盘 11.1（合规溢价 0.9%），第8日收盘跌破 11.1 → 破位卖出
        rows = [_day(i, 10.0) for i in range(6)]
        rows.append(_day(6, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))
        # 入场日 open=11.1（首板次日开盘）
        rows.append(_day(7, 10.5, open_=11.1, high=11.2, low=10.4, volume=30000))  # 破位
        df = _make_df(rows)
        s = AssetComboStrategy(_params())
        out = s.generate_signals(df, is_backtest=True)
        # signal[i=6]=1,支撑价=入场日 open=11.1,第7日 close=10.5<11.1 → 破位卖出
        assert out.loc[7, "signal"] == -1

    def test_time_stop(self):
        # max_hold_days=3，首板第7日(index 6)，强制卖出在 i + max_hold_days = 9
        rows = [_day(i, 10.0) for i in range(6)]
        rows.append(_day(6, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))
        # 次日 open=11.1（合规）
        rows.append(_day(7, 11.0, open_=11.1, high=11.2, low=11.0, volume=20000))
        for i in range(8, 11):
            rows.append(_day(i, 11.0))  # 平盘持仓
        df = _make_df(rows)
        s = AssetComboStrategy(_params(max_hold_days=3))
        out = s.generate_signals(df, is_backtest=True)
        assert out.loc[9, "signal"] == -1  # i=6+3=9 强制卖出

    def test_lock_profit(self):
        # 首板第7日买入，第8日放量大阳 → lock_profit=1（后续平盘避免时间止损干扰）
        rows = [_day(i, 10.0) for i in range(6)]
        rows.append(_day(6, 11.0, open_=10.2, high=11.0, low=10.2, volume=50000))
        # 第7日(index 7)是入场日 open=11.1，close=11.0（持有）
        rows.append(_day(7, 11.0, open_=11.1, high=11.2, low=11.0, volume=20000))
        # 第8日(index 8)放量大阳：open=11.0,close=11.66（涨 6%），volume 放大
        rows.append(_day(8, 11.66, open_=11.0, high=11.66, low=11.0, volume=60000))
        for i in range(9, 13):
            rows.append(_day(i, 11.66))  # 平盘持仓
        df = _make_df(rows)
        s = AssetComboStrategy(_params(max_hold_days=5))
        out = s.generate_signals(df, is_backtest=True)
        assert out.loc[8, "lock_profit"] == 1
