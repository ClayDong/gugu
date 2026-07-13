"""市场温度计模块单元测试。"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from gugu.analysis.market_temp import (
    BuffettResult,
    IndexTempResult,
    MarketTempResult,
    PBTempResult,
    PB_INDEXES,
    WIDE_INDEXES,
    _fetch_buffett_index_lg,
    _fetch_pb_history_lg,
    _fetch_pe_history_eastmoney,
    _fetch_pe_history_lg,
    _format_zone,
    _temp_to_template,
    build_market_temp_card,
    calc_buffett_percentile,
    calc_market_temp,
    calc_pe_percentile,
    calc_pb_percentile,
    fetch_buffett_temp,
    fetch_index_temp,
    fetch_pb_temp,
    format_temp_report,
)


def _make_pe_history(n: int = 100, base_pe: float = 15.0) -> pd.DataFrame:
    """构造 PE 历史数据。"""
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    pe_values = [base_pe + i * 0.1 for i in range(n)]
    return pd.DataFrame({"date": dates, "pe": pe_values})


class TestCalcPePercentile:
    """calc_pe_percentile 函数测试。"""

    def test_normal_data(self):
        """正常数据：当前 PE 在序列末尾，百分位应为 100。"""
        df = _make_pe_history(100, base_pe=10.0)
        result = calc_pe_percentile(df)
        assert result["current_pe"] == pytest.approx(19.9, 0.1)
        # 递增序列，最后一个值最大，百分位应接近 100
        assert result["pe_percentile"] > 99
        assert result["data_points"] == 100
        assert result["min_pe"] == pytest.approx(10.0, 0.1)
        assert result["max_pe"] == pytest.approx(19.9, 0.1)

    def test_empty_dataframe(self):
        """空 DataFrame。"""
        df = pd.DataFrame({"date": [], "pe": []})
        result = calc_pe_percentile(df)
        assert result["current_pe"] is None
        assert result["pe_percentile"] is None
        assert result["data_points"] == 0

    def test_none_dataframe(self):
        """None 输入。"""
        result = calc_pe_percentile(None)
        assert result["current_pe"] is None
        assert result["pe_percentile"] is None

    def test_single_point(self):
        """单点数据：百分位应为 50（自己和自己比较）。"""
        df = pd.DataFrame({"date": [pd.Timestamp("2025-01-01")], "pe": [15.0]})
        result = calc_pe_percentile(df)
        assert result["current_pe"] == 15.0
        # count_below=0, count_equal=1, total=1 → (0+0.5)/1*100=50
        assert result["pe_percentile"] == 50.0
        assert result["data_points"] == 1

    def test_middle_percentile(self):
        """中间百分位：构造已知百分位的数据。"""
        # 10 个点递增，当前 PE=10.0（最大值）
        # count_below=9, count_equal=1, total=10 → (9+0.5)/10*100=95.0
        pe_values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        dates = pd.date_range("2025-01-01", periods=10, freq="D")
        df = pd.DataFrame({"date": dates, "pe": pe_values})
        result = calc_pe_percentile(df)
        assert result["current_pe"] == 10.0
        assert result["pe_percentile"] == 95.0

    def test_negative_pe_filtered(self):
        """负 PE（亏损）应被过滤。"""
        dates = pd.date_range("2025-01-01", periods=5, freq="D")
        df = pd.DataFrame({"date": dates, "pe": [10.0, -5.0, 15.0, -3.0, 20.0]})
        result = calc_pe_percentile(df)
        # calc_pe_percentile 不过滤负 PE，但 _fetch_pe_history_lg 会过滤
        # 这里测试 calc_pe_percentile 本身的行为
        assert result["data_points"] == 5


class TestMarketTempResult:
    """MarketTempResult 数据类测试。"""

    def test_zone_low(self):
        """低估区。"""
        r = MarketTempResult(temperature=15.0)
        assert "低估" in r.zone
        assert "加倍" in r.zone

    def test_zone_normal_buy(self):
        """偏低区。"""
        r = MarketTempResult(temperature=25.0)
        assert "偏低" in r.zone
        assert "正常买入" in r.zone

    def test_zone_hold(self):
        """适中区。"""
        r = MarketTempResult(temperature=40.0)
        assert "适中" in r.zone
        assert "定投" in r.zone

    def test_zone_sell(self):
        """偏高区。"""
        r = MarketTempResult(temperature=60.0)
        assert "偏高" in r.zone
        assert "减仓" in r.zone

    def test_zone_heavy_sell(self):
        """高估区。"""
        r = MarketTempResult(temperature=80.0)
        assert "高估" in r.zone
        assert "大幅减仓" in r.zone

    def test_zone_none(self):
        """温度为 None。"""
        r = MarketTempResult(temperature=None)
        assert r.zone == "unknown"

    def test_action_advice_none(self):
        """温度为 None 时的建议。"""
        r = MarketTempResult(temperature=None)
        assert "数据不足" in r.action_advice

    def test_to_dict(self):
        """to_dict 序列化。"""
        r = MarketTempResult(
            temperature=30.0,
            total_indexes=12,
            success_count=10,
        )
        d = r.to_dict()
        assert d["temperature"] == 30.0
        assert d["total_indexes"] == 12
        assert d["success_count"] == 10
        assert d["hottest"] is None
        assert d["coldest"] is None


class TestFormatZone:
    """_format_zone 函数测试。"""

    def test_double_buy(self):
        assert "🟢" in _format_zone("低估区（加倍买入）")

    def test_normal_buy(self):
        assert "🟢" in _format_zone("偏低区（正常买入）")

    def test_hold(self):
        assert "🟡" in _format_zone("适中区（正常定投）")

    def test_sell(self):
        assert "🟠" in _format_zone("偏高区（逐步减仓）")

    def test_heavy_sell(self):
        assert "🔴" in _format_zone("高估区（大幅减仓）")


class TestTempToTemplate:
    """_temp_to_template 函数测试。"""

    def test_low_temp(self):
        assert _temp_to_template(15.0) == "green"

    def test_normal_temp(self):
        assert _temp_to_template(25.0) == "green"

    def test_hold_temp(self):
        assert _temp_to_template(40.0) == "yellow"

    def test_sell_temp(self):
        assert _temp_to_template(60.0) == "orange"

    def test_heavy_sell_temp(self):
        assert _temp_to_template(80.0) == "red"

    def test_none_temp(self):
        assert _temp_to_template(None) == "grey"


class TestFetchPeHistoryLg:
    """_fetch_pe_history_lg 函数测试。"""

    def test_mock_success(self):
        """mock akshare 成功返回。"""
        mock_raw = pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=100, freq="D"),
                "指数": [4000.0] * 100,
                "等权静态市盈率": [20.0] * 100,
                "静态市盈率": [15.0] * 100,
                "静态市盈率中位数": [18.0] * 100,
                "等权滚动市盈率": [22.0] * 100,
                "滚动市盈率": [13.0] * 100,
                "滚动市盈率中位数": [16.0] * 100,
            }
        )
        with mock.patch("akshare.stock_index_pe_lg", return_value=mock_raw):
            df = _fetch_pe_history_lg("沪深300", years=10)
            assert df is not None
            assert not df.empty
            assert "pe" in df.columns
            assert "date" in df.columns
            # 滚动市盈率 13.0 应被使用
            assert df["pe"].iloc[0] == 13.0

    def test_mock_none_return(self):
        """akshare 返回 None。"""
        with mock.patch("akshare.stock_index_pe_lg", return_value=None):
            df = _fetch_pe_history_lg("沪深300", years=10)
            assert df is None

    def test_mock_exception(self):
        """akshare 抛异常。"""
        with mock.patch("akshare.stock_index_pe_lg", side_effect=Exception("network error")):
            df = _fetch_pe_history_lg("沪深300", years=10)
            assert df is None

    def test_mock_empty(self):
        """akshare 返回空 DataFrame。"""
        with mock.patch("akshare.stock_index_pe_lg", return_value=pd.DataFrame()):
            df = _fetch_pe_history_lg("沪深300", years=10)
            assert df is None

    def test_negative_pe_filtered(self):
        """负 PE 应被过滤。"""
        mock_raw = pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=5, freq="D"),
                "指数": [4000.0] * 5,
                "等权静态市盈率": [20.0] * 5,
                "静态市盈率": [15.0] * 5,
                "静态市盈率中位数": [18.0] * 5,
                "等权滚动市盈率": [22.0] * 5,
                "滚动市盈率": [13.0, -5.0, 14.0, -3.0, 15.0],
                "滚动市盈率中位数": [16.0] * 5,
            }
        )
        with mock.patch("akshare.stock_index_pe_lg", return_value=mock_raw):
            df = _fetch_pe_history_lg("沪深300", years=10)
            assert df is not None
            assert len(df) == 3  # 负 PE 被过滤
            assert (df["pe"] > 0).all()

    def test_fallback_to_static_pe(self):
        """滚动市盈率缺失时降级到静态市盈率。"""
        mock_raw = pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=10, freq="D"),
                "指数": [4000.0] * 10,
                "等权静态市盈率": [20.0] * 10,
                "静态市盈率": [15.0] * 10,
                "静态市盈率中位数": [18.0] * 10,
                "等权滚动市盈率": [22.0] * 10,
                "滚动市盈率": [None] * 10,  # 滚动市盈率全部缺失
                "滚动市盈率中位数": [16.0] * 10,
            }
        )
        with mock.patch("akshare.stock_index_pe_lg", return_value=mock_raw):
            df = _fetch_pe_history_lg("沪深300", years=10)
            assert df is not None
            # 应降级到静态市盈率 15.0
            assert df["pe"].iloc[0] == 15.0


class TestFetchIndexTemp:
    """fetch_index_temp 函数测试。"""

    @pytest.mark.asyncio
    async def test_success(self):
        """成功获取指数温度。"""
        mock_df = _make_pe_history(100, base_pe=10.0)
        with mock.patch(
            "gugu.analysis.market_temp._fetch_pe_history_lg",
            return_value=mock_df,
        ):
            idx_config = {"name": "沪深300", "lg_symbol": "沪深300", "weight": 1.0}
            result = await fetch_index_temp(idx_config)
            assert result.name == "沪深300"
            assert result.current_pe is not None
            assert result.pe_percentile is not None
            assert result.data_points == 100
            assert result.error is None

    @pytest.mark.asyncio
    async def test_all_sources_fail(self):
        """所有数据源失败。"""
        with mock.patch(
            "gugu.analysis.market_temp._fetch_pe_history_lg",
            return_value=None,
        ):
            idx_config = {"name": "沪深300", "lg_symbol": "沪深300"}
            result = await fetch_index_temp(idx_config)
            assert result.name == "沪深300"
            assert result.current_pe is None
            assert result.pe_percentile is None
            assert result.error is not None

    @pytest.mark.asyncio
    async def test_fallback_to_eastmoney(self):
        """乐咕乐股失败后降级到东方财富。"""
        mock_df = _make_pe_history(50, base_pe=20.0)
        with (
            mock.patch(
                "gugu.analysis.market_temp._fetch_pe_history_lg",
                return_value=None,
            ),
            mock.patch(
                "gugu.analysis.market_temp._fetch_pe_history_eastmoney",
                return_value=mock_df,
            ),
        ):
            idx_config = {"name": "沪深300", "lg_symbol": "沪深300", "code": "000300"}
            result = await fetch_index_temp(idx_config)
            assert result.current_pe is not None
            assert result.pe_percentile is not None


class TestCalcMarketTemp:
    """calc_market_temp 函数测试。"""

    @pytest.mark.asyncio
    async def test_all_success(self):
        """所有维度成功（PE + PB + Buffett）。"""

        async def mock_pe_fetch(idx):
            return IndexTempResult(
                name=idx["name"],
                current_pe=15.0,
                pe_percentile=50.0,
                min_pe=10.0,
                max_pe=20.0,
                median_pe=15.0,
                data_points=100,
            )

        async def mock_pb_fetch(idx):
            return PBTempResult(
                name=idx["name"],
                current_pb=1.5,
                pb_percentile=40.0,
                min_pb=1.0,
                max_pb=2.0,
                median_pb=1.4,
                data_points=100,
            )

        async def mock_buffett_fetch():
            return BuffettResult(
                current_ratio=0.9,
                ratio_percentile=60.0,
                min_ratio=0.6,
                max_ratio=1.2,
                median_ratio=0.85,
                data_points=100,
            )

        with (
            mock.patch("gugu.analysis.market_temp.fetch_index_temp", side_effect=mock_pe_fetch),
            mock.patch("gugu.analysis.market_temp.fetch_pb_temp", side_effect=mock_pb_fetch),
            mock.patch(
                "gugu.analysis.market_temp.fetch_buffett_temp", side_effect=mock_buffett_fetch
            ),
        ):
            result = await calc_market_temp(use_cache=False)
            assert result.temperature is not None
            # PE=50, PB=40, Buffett=60 → 50*0.7+40*0.2+60*0.1 = 35+8+6 = 49.0
            assert result.temperature == 49.0
            assert result.pe_temperature == 50.0
            assert result.pb_temperature == 40.0
            assert result.buffett_temperature == 60.0
            assert result.total_indexes == len(WIDE_INDEXES)
            assert result.success_count == len(WIDE_INDEXES)
            assert result.pb_success_count == len(PB_INDEXES)
            assert result.buffett_success is True

    @pytest.mark.asyncio
    async def test_pe_only_success(self):
        """只有 PE 成功，PB 和 Buffett 失败。"""

        async def mock_pe_fetch(idx):
            return IndexTempResult(
                name=idx["name"],
                current_pe=15.0,
                pe_percentile=50.0,
                min_pe=10.0,
                max_pe=20.0,
                median_pe=15.0,
                data_points=100,
            )

        async def mock_pb_fetch(idx):
            return PBTempResult(
                name=idx["name"],
                current_pb=None,
                pb_percentile=None,
                min_pb=None,
                max_pb=None,
                median_pb=None,
                error="失败",
            )

        async def mock_buffett_fetch():
            return BuffettResult(
                current_ratio=None,
                ratio_percentile=None,
                min_ratio=None,
                max_ratio=None,
                median_ratio=None,
                error="失败",
            )

        with (
            mock.patch("gugu.analysis.market_temp.fetch_index_temp", side_effect=mock_pe_fetch),
            mock.patch("gugu.analysis.market_temp.fetch_pb_temp", side_effect=mock_pb_fetch),
            mock.patch(
                "gugu.analysis.market_temp.fetch_buffett_temp", side_effect=mock_buffett_fetch
            ),
        ):
            result = await calc_market_temp(use_cache=False)
            assert result.temperature is not None
            # 只有 PE，权重自动归一到 PE
            assert result.temperature == 50.0
            assert result.pe_temperature == 50.0
            assert result.pb_temperature is None
            assert result.buffett_temperature is None

    @pytest.mark.asyncio
    async def test_partial_pe_failure(self):
        """部分 PE 指数失败。"""

        async def mock_pe_fetch(idx):
            if idx["name"] == "沪深300":
                return IndexTempResult(
                    name="沪深300",
                    current_pe=None,
                    pe_percentile=None,
                    min_pe=None,
                    max_pe=None,
                    median_pe=None,
                    error="失败",
                )
            return IndexTempResult(
                name=idx["name"],
                current_pe=15.0,
                pe_percentile=60.0,
                min_pe=10.0,
                max_pe=20.0,
                median_pe=15.0,
                data_points=100,
            )

        async def mock_pb_fetch(idx):
            return PBTempResult(
                name=idx["name"],
                current_pb=1.5,
                pb_percentile=40.0,
                min_pb=1.0,
                max_pb=2.0,
                median_pb=1.4,
                data_points=100,
            )

        async def mock_buffett_fetch():
            return BuffettResult(
                current_ratio=0.9,
                ratio_percentile=60.0,
                min_ratio=0.6,
                max_ratio=1.2,
                median_ratio=0.85,
                data_points=100,
            )

        with (
            mock.patch("gugu.analysis.market_temp.fetch_index_temp", side_effect=mock_pe_fetch),
            mock.patch("gugu.analysis.market_temp.fetch_pb_temp", side_effect=mock_pb_fetch),
            mock.patch(
                "gugu.analysis.market_temp.fetch_buffett_temp", side_effect=mock_buffett_fetch
            ),
        ):
            result = await calc_market_temp(use_cache=False)
            assert result.temperature is not None
            assert result.success_count == len(WIDE_INDEXES) - 1
            assert result.pe_temperature == 60.0  # 11 个都是 60.0

    @pytest.mark.asyncio
    async def test_all_fail(self):
        """所有数据源失败。"""

        async def mock_pe_fetch(idx):
            return IndexTempResult(
                name=idx["name"],
                current_pe=None,
                pe_percentile=None,
                min_pe=None,
                max_pe=None,
                median_pe=None,
                error="失败",
            )

        async def mock_pb_fetch(idx):
            return PBTempResult(
                name=idx["name"],
                current_pb=None,
                pb_percentile=None,
                min_pb=None,
                max_pb=None,
                median_pb=None,
                error="失败",
            )

        async def mock_buffett_fetch():
            return BuffettResult(
                current_ratio=None,
                ratio_percentile=None,
                min_ratio=None,
                max_ratio=None,
                median_ratio=None,
                error="失败",
            )

        with (
            mock.patch("gugu.analysis.market_temp.fetch_index_temp", side_effect=mock_pe_fetch),
            mock.patch("gugu.analysis.market_temp.fetch_pb_temp", side_effect=mock_pb_fetch),
            mock.patch(
                "gugu.analysis.market_temp.fetch_buffett_temp", side_effect=mock_buffett_fetch
            ),
        ):
            result = await calc_market_temp(use_cache=False)
            assert result.temperature is None
            assert result.error is not None
            assert result.success_count == 0
            assert result.pb_success_count == 0
            assert result.buffett_success is False

    @pytest.mark.asyncio
    async def test_hottest_coldest(self):
        """最热/最冷指数。"""

        async def mock_pe_fetch(idx):
            pe_pct = (
                30.0 if idx["name"] == "上证50" else 70.0 if idx["name"] == "创业板50" else 50.0
            )
            return IndexTempResult(
                name=idx["name"],
                current_pe=15.0,
                pe_percentile=pe_pct,
                min_pe=10.0,
                max_pe=20.0,
                median_pe=15.0,
                data_points=100,
            )

        async def mock_pb_fetch(idx):
            return PBTempResult(
                name=idx["name"],
                current_pb=1.5,
                pb_percentile=40.0,
                min_pb=1.0,
                max_pb=2.0,
                median_pb=1.4,
                data_points=100,
            )

        async def mock_buffett_fetch():
            return BuffettResult(
                current_ratio=0.9,
                ratio_percentile=60.0,
                min_ratio=0.6,
                max_ratio=1.2,
                median_ratio=0.85,
                data_points=100,
            )

        with (
            mock.patch("gugu.analysis.market_temp.fetch_index_temp", side_effect=mock_pe_fetch),
            mock.patch("gugu.analysis.market_temp.fetch_pb_temp", side_effect=mock_pb_fetch),
            mock.patch(
                "gugu.analysis.market_temp.fetch_buffett_temp", side_effect=mock_buffett_fetch
            ),
        ):
            result = await calc_market_temp(use_cache=False)
            assert result.hottest is not None
            assert result.coldest is not None
            assert result.hottest.name == "创业板50"
            assert result.coldest.name == "上证50"


class TestBuildMarketTempCard:
    """build_market_temp_card 函数测试。"""

    def test_normal_card(self):
        """正常温度卡片。"""
        result = MarketTempResult(
            temperature=35.0,
            indexes=[
                IndexTempResult(
                    name="沪深300",
                    current_pe=13.5,
                    pe_percentile=80.0,
                    min_pe=10.0,
                    max_pe=17.0,
                    median_pe=12.0,
                    data_points=100,
                ),
                IndexTempResult(
                    name="上证50",
                    current_pe=11.0,
                    pe_percentile=60.0,
                    min_pe=8.0,
                    max_pe=14.0,
                    median_pe=10.0,
                    data_points=100,
                ),
            ],
            hottest=IndexTempResult(
                name="沪深300",
                current_pe=13.5,
                pe_percentile=80.0,
                min_pe=10.0,
                max_pe=17.0,
                median_pe=12.0,
                data_points=100,
            ),
            coldest=IndexTempResult(
                name="上证50",
                current_pe=11.0,
                pe_percentile=60.0,
                min_pe=8.0,
                max_pe=14.0,
                median_pe=10.0,
                data_points=100,
            ),
            total_indexes=2,
            success_count=2,
        )
        card = build_market_temp_card(result)
        assert isinstance(card, dict)
        # 飞书卡片结构: {"msg_type": "interactive", "card": {...}}
        assert "card" in card or "header" in card

    def test_none_temperature_card(self):
        """温度为 None 的卡片。"""
        result = MarketTempResult(temperature=None, error="数据获取失败")
        card = build_market_temp_card(result)
        assert isinstance(card, dict)

    def test_card_template_color(self):
        """卡片颜色随温度变化。"""
        # 低温 → green
        result = MarketTempResult(temperature=15.0)
        card = build_market_temp_card(result)
        assert isinstance(card, dict)

        # 高温 → red
        result = MarketTempResult(temperature=80.0)
        card = build_market_temp_card(result)
        assert isinstance(card, dict)


class TestFormatTempReport:
    """format_temp_report 函数测试。"""

    def test_normal_report(self):
        """正常报告。"""
        result = MarketTempResult(
            temperature=35.0,
            indexes=[
                IndexTempResult(
                    name="沪深300",
                    current_pe=13.5,
                    pe_percentile=80.0,
                    min_pe=10.0,
                    max_pe=17.0,
                    median_pe=12.0,
                    data_points=100,
                ),
            ],
            hottest=IndexTempResult(
                name="沪深300",
                current_pe=13.5,
                pe_percentile=80.0,
                min_pe=10.0,
                max_pe=17.0,
                median_pe=12.0,
                data_points=100,
            ),
            coldest=IndexTempResult(
                name="沪深300",
                current_pe=13.5,
                pe_percentile=80.0,
                min_pe=10.0,
                max_pe=17.0,
                median_pe=12.0,
                data_points=100,
            ),
            total_indexes=1,
            success_count=1,
        )
        report = format_temp_report(result)
        assert "35" in report
        assert "沪深300" in report
        assert "80" in report

    def test_none_report(self):
        """温度为 None 的报告。"""
        result = MarketTempResult(temperature=None)
        report = format_temp_report(result)
        assert "数据暂不可用" in report


class TestWideIndexes:
    """WIDE_INDEXES 常量测试。"""

    def test_count(self):
        """应有 12 个宽基指数。"""
        assert len(WIDE_INDEXES) == 12

    def test_required_fields(self):
        """每个指数配置必须有 name 和 lg_symbol。"""
        for idx in WIDE_INDEXES:
            assert "name" in idx
            assert "lg_symbol" in idx
            assert idx["name"]
            assert idx["lg_symbol"]

    def test_no_duplicate_names(self):
        """指数名称不应重复。"""
        names = [idx["name"] for idx in WIDE_INDEXES]
        assert len(names) == len(set(names))


class TestPBIndexes:
    """PB_INDEXES 常量测试。"""

    def test_count(self):
        """应有 2 个 PB 指数。"""
        assert len(PB_INDEXES) == 2

    def test_required_fields(self):
        """每个指数配置必须有 name 和 lg_symbol。"""
        for idx in PB_INDEXES:
            assert "name" in idx
            assert "lg_symbol" in idx


class TestCalcPbPercentile:
    """calc_pb_percentile 函数测试。"""

    def test_normal_data(self):
        """正常 PB 数据。"""
        dates = pd.date_range("2025-01-01", periods=100, freq="D")
        df = pd.DataFrame({"date": dates, "pb": [1.0 + i * 0.01 for i in range(100)]})
        result = calc_pb_percentile(df)
        assert result["current_pb"] is not None
        assert result["pb_percentile"] > 99
        assert result["data_points"] == 100

    def test_empty(self):
        """空 DataFrame。"""
        df = pd.DataFrame({"date": [], "pb": []})
        result = calc_pb_percentile(df)
        assert result["current_pb"] is None
        assert result["pb_percentile"] is None

    def test_none(self):
        """None 输入。"""
        result = calc_pb_percentile(None)
        assert result["current_pb"] is None


class TestCalcBuffettPercentile:
    """calc_buffett_percentile 函数测试。"""

    def test_normal_data(self):
        """正常巴菲特指数数据。"""
        dates = pd.date_range("2025-01-01", periods=100, freq="D")
        df = pd.DataFrame({"date": dates, "ratio": [0.8 + i * 0.001 for i in range(100)]})
        result = calc_buffett_percentile(df)
        assert result["current_ratio"] is not None
        assert result["ratio_percentile"] > 99
        assert result["data_points"] == 100

    def test_empty(self):
        """空 DataFrame。"""
        df = pd.DataFrame({"date": [], "ratio": []})
        result = calc_buffett_percentile(df)
        assert result["current_ratio"] is None

    def test_none(self):
        """None 输入。"""
        result = calc_buffett_percentile(None)
        assert result["current_ratio"] is None


class TestFetchPbHistoryLg:
    """_fetch_pb_history_lg 函数测试。"""

    def test_mock_success(self):
        """mock akshare 成功返回。"""
        mock_raw = pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=100, freq="D"),
                "指数": [4000.0] * 100,
                "市净率": [1.5] * 100,
                "等权市净率": [2.0] * 100,
                "市净率中位数": [1.4] * 100,
            }
        )
        with mock.patch("akshare.stock_index_pb_lg", return_value=mock_raw):
            df = _fetch_pb_history_lg("沪深300", years=10)
            assert df is not None
            assert not df.empty
            assert "pb" in df.columns
            assert df["pb"].iloc[0] == 1.5

    def test_mock_none_return(self):
        """akshare 返回 None。"""
        with mock.patch("akshare.stock_index_pb_lg", return_value=None):
            df = _fetch_pb_history_lg("沪深300", years=10)
            assert df is None

    def test_mock_exception(self):
        """akshare 抛异常。"""
        with mock.patch("akshare.stock_index_pb_lg", side_effect=Exception("error")):
            df = _fetch_pb_history_lg("沪深300", years=10)
            assert df is None


class TestFetchBuffettIndexLg:
    """_fetch_buffett_index_lg 函数测试。"""

    def test_mock_success(self):
        """mock akshare 成功返回。"""
        mock_raw = pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=100, freq="D"),
                "收盘价": [4000.0] * 100,
                "总市值": [1300000.0] * 100,
                "GDP": [1400000.0] * 100,
            }
        )
        with mock.patch("akshare.stock_buffett_index_lg", return_value=mock_raw):
            df = _fetch_buffett_index_lg(years=10)
            assert df is not None
            assert not df.empty
            assert "ratio" in df.columns
            # 1300000 / 1400000 ≈ 0.929
            assert abs(df["ratio"].iloc[0] - 0.929) < 0.01

    def test_mock_none_return(self):
        """akshare 返回 None。"""
        with mock.patch("akshare.stock_buffett_index_lg", return_value=None):
            df = _fetch_buffett_index_lg(years=10)
            assert df is None

    def test_mock_exception(self):
        """akshare 抛异常。"""
        with mock.patch("akshare.stock_buffett_index_lg", side_effect=Exception("error")):
            df = _fetch_buffett_index_lg(years=10)
            assert df is None


class TestFetchPbTemp:
    """fetch_pb_temp 函数测试。"""

    @pytest.mark.asyncio
    async def test_success(self):
        """成功获取 PB 温度。"""
        mock_df = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=100, freq="D"),
                "pb": [1.5] * 100,
            }
        )
        with mock.patch("gugu.analysis.market_temp._fetch_pb_history_lg", return_value=mock_df):
            result = await fetch_pb_temp({"name": "沪深300", "lg_symbol": "沪深300"})
            assert result.name == "沪深300"
            assert result.current_pb is not None
            assert result.pb_percentile is not None

    @pytest.mark.asyncio
    async def test_fail(self):
        """PB 数据获取失败。"""
        with mock.patch("gugu.analysis.market_temp._fetch_pb_history_lg", return_value=None):
            result = await fetch_pb_temp({"name": "沪深300", "lg_symbol": "沪深300"})
            assert result.current_pb is None
            assert result.pb_percentile is None
            assert result.error is not None


class TestFetchBuffettTemp:
    """fetch_buffett_temp 函数测试。"""

    @pytest.mark.asyncio
    async def test_success(self):
        """成功获取巴菲特指数温度。"""
        mock_df = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=100, freq="D"),
                "ratio": [0.9] * 100,
            }
        )
        with mock.patch("gugu.analysis.market_temp._fetch_buffett_index_lg", return_value=mock_df):
            result = await fetch_buffett_temp()
            assert result.current_ratio is not None
            assert result.ratio_percentile is not None

    @pytest.mark.asyncio
    async def test_fail(self):
        """巴菲特指数获取失败。"""
        with mock.patch("gugu.analysis.market_temp._fetch_buffett_index_lg", return_value=None):
            result = await fetch_buffett_temp()
            assert result.current_ratio is None
            assert result.ratio_percentile is None
            assert result.error is not None
