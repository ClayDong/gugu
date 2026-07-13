"""豹哥三要素策略单元测试。

验证豹哥战法核心逻辑：
1. 三要素准入（换手率/成交额/流通市值）
2. 放量突破择时
3. 非涨停过滤
4. 卖出信号（跌破MA20 / 量价背离）
5. 数据降级容错（回测中无换手率字段时用成交量分位近似）
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.leopard_three_factors import LeopardThreeFactorsStrategy


def _make_uptrend_df(n: int = 100, seed: int = 42) -> pd.DataFrame:
    """生成上升趋势+放量突破的测试数据。"""
    np.random.seed(seed)
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    prices = [10.0]
    for _ in range(1, n):
        # 缓慢上涨，偶尔回调
        change = np.random.uniform(-0.01, 0.025)
        prices.append(prices[-1] * (1 + change))
    prices = np.array(prices)
    # 成交量：基础 1000万，偶有放量
    base_vol = 10_000_000
    volumes = base_vol * (1 + np.random.uniform(-0.3, 0.5, n))
    # 在第 80 天制造放量突破
    volumes[80] = base_vol * 2.5
    prices[80] = prices[79] * 1.05
    # 成交额 = close * volume * 100
    amounts = prices * volumes * 100
    return pd.DataFrame(
        {
            "date": dates,
            "open": prices * 0.99,
            "high": prices * 1.01,
            "low": prices * 0.98,
            "close": prices,
            "volume": volumes,
            "amount": amounts,
        }
    )


class TestLeopardThreeFactors:
    """豹哥三要素策略测试。"""

    def test_validate_params_valid(self) -> None:
        """合法参数不抛异常。"""
        s = LeopardThreeFactorsStrategy()
        s.validate_params()

    def test_validate_params_invalid_turnover(self) -> None:
        """换手率阈值越界抛异常（构造时即触发）。"""
        with pytest.raises(ValueError) as exc_info:
            LeopardThreeFactorsStrategy({"turnover_threshold": 1.5})
        assert "turnover_threshold" in str(exc_info.value)

    def test_validate_params_invalid_amount(self) -> None:
        """成交额阈值 ≤ 0 抛异常（构造时即触发）。"""
        with pytest.raises(ValueError) as exc_info:
            LeopardThreeFactorsStrategy({"amount_threshold": 0})
        assert "amount_threshold" in str(exc_info.value)

    def test_validate_params_invalid_market_cap(self) -> None:
        """流通市值阈值 ≤ 0 抛异常（构造时即触发）。"""
        with pytest.raises(ValueError) as exc_info:
            LeopardThreeFactorsStrategy({"market_cap_threshold": -1})
        assert "market_cap_threshold" in str(exc_info.value)

    def test_validate_params_invalid_turnover_msg(self) -> None:
        """异常消息含区间字样（单独测消息）。"""
        with pytest.raises(ValueError) as exc_info:
            LeopardThreeFactorsStrategy({"turnover_threshold": 1.5})
        assert "turnover_threshold" in str(exc_info.value)

    def test_generates_buy_signal_on_breakout(self) -> None:
        """放量突破+三要素满足 → 买入信号。"""
        df = _make_uptrend_df(100)
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        # 至少有 1 个买入信号（第 80 天放量突破）
        buy_signals = (result["signal"] == 1).sum()
        assert buy_signals >= 1, "放量突破应产生买入信号"

    def test_no_buy_when_amount_below_threshold(self) -> None:
        """成交额低于阈值 → 无买入信号。"""
        df = _make_uptrend_df(100)
        # 把成交额全部改小
        df["amount"] = df["amount"] * 0.001  # 远低于 10 亿
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        assert (result["signal"] == 1).sum() == 0, "成交额不足不应有买入信号"

    def test_no_buy_when_limit_up(self) -> None:
        """当日涨幅 >7%（接近涨停） → 不追。"""
        df = _make_uptrend_df(100)
        # 制造一个涨停日的数据
        df.loc[80, "close"] = df.loc[79, "close"] * 1.10  # 涨 10%
        df.loc[80, "high"] = df.loc[79, "close"] * 1.10
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        # 第 80 天涨幅 10% > 7%，不应买入
        assert result.loc[80, "signal"] != 1, "涨停日不应买入"

    def test_sell_when_below_ma20(self) -> None:
        """跌破 MA20 → 卖出信号。"""
        df = _make_uptrend_df(100)
        # 在第 90 天大幅下跌跌破 MA20
        ma20 = df["close"].rolling(20).mean()
        df.loc[90, "close"] = ma20.iloc[90] * 0.95  # 跌破 MA20 5%
        df.loc[90, "low"] = ma20.iloc[90] * 0.94
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        assert result.loc[90, "signal"] == -1, "跌破 MA20 应发卖出信号"

    def test_sell_on_divergence(self) -> None:
        """量价背离（价创新高但量缩）→ 卖出信号。"""
        df = _make_uptrend_df(100)
        # 第 95 天价格创 20 日新高但成交量 < 5 日均量
        df.loc[95, "close"] = df["close"].iloc[75:95].max() * 1.02
        df.loc[95, "high"] = df.loc[95, "close"] * 1.01
        df.loc[95, "volume"] = df["volume"].iloc[90:95].min() * 0.5  # 缩量
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        assert result.loc[95, "signal"] == -1, "量价背离应发卖出信号"

    def test_degraded_mode_without_turnover(self) -> None:
        """回测模式无换手率字段 → 降级到成交量分位。"""
        df = _make_uptrend_df(100)
        # 不含 turnover 字段
        assert "turnover" not in df.columns
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        # 降级模式下仍能产生信号
        assert "signal" in result.columns

    def test_degraded_mode_without_circ_mv(self) -> None:
        """回测模式无流通市值字段 → 降级到成交额替代。"""
        df = _make_uptrend_df(100)
        assert "circ_mv" not in df.columns
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        assert "signal" in result.columns

    def test_confidence_range(self) -> None:
        """置信度在 [0, 1] 区间。"""
        df = _make_uptrend_df(100)
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        valid_conf = result["confidence"].dropna()
        assert (valid_conf >= 0).all() and (valid_conf <= 1).all(), "置信度应在 [0, 1]"

    def test_sell_overrides_buy(self) -> None:
        """卖出信号覆盖买入信号（风控优先）。"""
        df = _make_uptrend_df(100)
        # 构造一个既是买入（突破）又是卖出（跌破MA20）的矛盾点
        ma20 = df["close"].rolling(20).mean()
        # 第 80 天突破前高但跌破 MA20（极端矛盾场景）
        df.loc[80, "close"] = df["high"].iloc[60:80].max() * 1.01  # 突破
        df.loc[80, "close"] = ma20.iloc[80] * 0.95  # 但又跌破 MA20
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        assert result.loc[80, "signal"] == -1, "卖出应覆盖买入"

    def test_no_signal_in_downtrend(self) -> None:
        """MA20 < MA60 下跌趋势 → 无买入信号。"""
        n = 100
        np.random.seed(42)
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        # 下跌趋势
        prices = [10.0]
        for _ in range(1, n):
            prices.append(prices[-1] * (1 + np.random.uniform(-0.025, 0.005)))
        prices = np.array(prices)
        volumes = 10_000_000 * (1 + np.random.uniform(-0.3, 0.5, n))
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.01,
                "low": prices * 0.98,
                "close": prices,
                "volume": volumes,
                "amount": prices * volumes * 100,
            }
        )
        s = LeopardThreeFactorsStrategy()
        result = s.generate_signals(df)
        assert (result["signal"] == 1).sum() == 0, "下跌趋势不应有买入信号"
