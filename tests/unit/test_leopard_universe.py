"""豹哥三要素选股池过滤器单元测试。

来源：豹哥战法（微信公众号文章）
三要素是选股池准入（非择时信号）：
1. 换手率 ≥ 15%
2. 成交额 ≥ 10亿
3. 流通市值 ≥ 70亿
"""

from __future__ import annotations

import pandas as pd
import pytest

from gugu.filters.leopard_universe import LeopardUniverseFilter


@pytest.fixture
def sample_market_df() -> pd.DataFrame:
    """模拟全市场快照（含换手率/成交额/流通市值）。

    单位说明：
    - amount: 元（1e8 = 1亿，1e9 = 10亿）
    - circ_mv: 元（1e8 = 1亿，7e9 = 70亿）
    - turnover_pct: 百分比（15.0 = 15%）
    """
    return pd.DataFrame(
        [
            # 符合三要素（换手率20% + 成交额15亿 + 流通市值80亿）
            {
                "symbol": "000001",
                "name": "平安银行",
                "price": 12.5,
                "amount": 1.5e9,
                "turnover_pct": 20.0,
                "circ_mv": 8e9,
            },
            # 换手率不足（10% < 15%）
            {
                "symbol": "000002",
                "name": "万科A",
                "price": 8.3,
                "amount": 1.2e9,
                "turnover_pct": 10.0,
                "circ_mv": 9e9,
            },
            # 成交额不足（5亿 < 10亿）
            {
                "symbol": "000003",
                "name": "A小盘",
                "price": 15.0,
                "amount": 5e8,
                "turnover_pct": 25.0,
                "circ_mv": 7.5e9,
            },
            # 流通市值不足（50亿 < 70亿）
            {
                "symbol": "000004",
                "name": "B小盘",
                "price": 6.0,
                "amount": 1.1e9,
                "turnover_pct": 18.0,
                "circ_mv": 5e9,
            },
            # 符合三要素（边界值：换手率15% + 成交额10亿 + 流通市值70亿）
            {
                "symbol": "000005",
                "name": "C边缘",
                "price": 10.0,
                "amount": 1e9,
                "turnover_pct": 15.0,
                "circ_mv": 7e9,
            },
            # 符合三要素（大幅超标）
            {
                "symbol": "600519",
                "name": "贵州茅台",
                "price": 1500.0,
                "amount": 5e9,
                "turnover_pct": 30.0,
                "circ_mv": 2e11,
            },
        ]
    )


class TestLeopardUniverseFilter:
    """豹哥三要素选股池过滤器测试。"""

    def test_default_thresholds_match_article(self):
        """默认阈值必须与文章原文一致（换手率15% + 成交额10亿 + 流通市值70亿）。"""
        f = LeopardUniverseFilter()
        assert f.turnover_min == 15.0
        assert f.amount_min == 1_000_000_000  # 10亿（1e9 元）
        assert f.circ_mv_min == 7_000_000_000  # 70亿（7e9 元）

    def test_filter_keeps_only_three_factor_stocks(self, sample_market_df):
        """三要素全部满足才保留。"""
        f = LeopardUniverseFilter()
        result = f.filter(sample_market_df)

        # 应保留 3 只：000001, 000005(边界), 600519
        assert len(result) == 3
        symbols = set(result["symbol"])
        assert symbols == {"000001", "000005", "600519"}

    def test_filter_boundary_values_pass(self, sample_market_df):
        """边界值（=阈值）应该通过。"""
        f = LeopardUniverseFilter()
        result = f.filter(sample_market_df)
        # 000005 是边界值（15% + 10亿 + 70亿），应通过
        assert "000005" in set(result["symbol"])

    def test_filter_rejects_low_turnover(self, sample_market_df):
        """换手率不足应被过滤。"""
        f = LeopardUniverseFilter()
        result = f.filter(sample_market_df)
        # 000002 换手率10%，应被过滤
        assert "000002" not in set(result["symbol"])

    def test_filter_rejects_low_amount(self, sample_market_df):
        """成交额不足应被过滤。"""
        f = LeopardUniverseFilter()
        result = f.filter(sample_market_df)
        # 000003 成交额5亿，应被过滤
        assert "000003" not in set(result["symbol"])

    def test_filter_rejects_low_circ_mv(self, sample_market_df):
        """流通市值不足应被过滤。"""
        f = LeopardUniverseFilter()
        result = f.filter(sample_market_df)
        # 000004 流通市值50亿，应被过滤
        assert "000004" not in set(result["symbol"])

    def test_custom_thresholds(self, sample_market_df):
        """自定义阈值（更严格）。"""
        f = LeopardUniverseFilter(
            params={
                "turnover_min": 25.0,  # 25%
                "amount_min": 2e9,  # 20亿
                "circ_mv_min": 1e10,  # 100亿
            }
        )
        result = f.filter(sample_market_df)
        # 只有 600519 满足（换手率30% + 成交额50亿 + 流通市值2000亿）
        assert len(result) == 1
        assert result.iloc[0]["symbol"] == "600519"

    def test_empty_dataframe_returns_empty(self):
        """空 DataFrame 应原样返回。"""
        f = LeopardUniverseFilter()
        result = f.filter(pd.DataFrame())
        assert result.empty

    def test_missing_turnover_column_degrades_gracefully(self):
        """缺少 turnover_pct 列时应优雅降级（仅用成交额+流通市值）。"""
        df = pd.DataFrame(
            [
                {"symbol": "000001", "amount": 1.5e9, "circ_mv": 8e9},
                {"symbol": "000002", "amount": 5e8, "circ_mv": 6e9},  # 不足
            ]
        )
        f = LeopardUniverseFilter()
        result = f.filter(df)
        # 降级模式：仅成交额+流通市值过滤，应保留 000001
        assert len(result) == 1
        assert result.iloc[0]["symbol"] == "000001"

    def test_missing_circ_mv_column_degrades_gracefully(self):
        """缺少 circ_mv 列时应优雅降级（仅用成交额+换手率）。"""
        df = pd.DataFrame(
            [
                {"symbol": "000001", "amount": 1.5e9, "turnover_pct": 20.0},
                {"symbol": "000002", "amount": 5e8, "turnover_pct": 25.0},  # 成交额不足
            ]
        )
        f = LeopardUniverseFilter()
        result = f.filter(df)
        assert len(result) == 1
        assert result.iloc[0]["symbol"] == "000001"

    def test_all_zeros_turnover_treated_as_missing(self):
        """换手率全为0应视为数据缺失，降级处理。"""
        df = pd.DataFrame(
            [
                {"symbol": "000001", "amount": 1.5e9, "turnover_pct": 0, "circ_mv": 8e9},
                {"symbol": "000002", "amount": 1.2e9, "turnover_pct": 0, "circ_mv": 9e9},
            ]
        )
        f = LeopardUniverseFilter()
        result = f.filter(df)
        # 换手率全为0视为缺失，仅用成交额+流通市值过滤，两只都应保留
        assert len(result) == 2

    def test_describe_returns_threshold_info(self):
        """describe() 应返回阈值描述。"""
        f = LeopardUniverseFilter()
        desc = f.describe()
        assert desc["turnover_min"] == 15.0
        assert desc["amount_min_billion"] == 10.0
        assert desc["circ_mv_min_billion"] == 70.0  # 7e9 / 1e8 = 70
        assert "选股池准入" in desc["positioning"]

    def test_filter_preserves_all_columns(self, sample_market_df):
        """过滤后应保留所有原始列。"""
        f = LeopardUniverseFilter()
        result = f.filter(sample_market_df)
        for col in ["symbol", "name", "price", "amount", "turnover_pct", "circ_mv"]:
            assert col in result.columns
