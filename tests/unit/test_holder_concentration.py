"""筹码集中度过滤器单元测试。

覆盖场景：
- 数据缺失默认放行（慢变量，不应拒绝信号）
- 避雷针信号（户数激增 / 户均持股骤减 / 连续增长）
- 潜伏者信号（连续下降 + 户均持股增加）
- 中性场景（正常户数变化）
- 缓存跨日失效
- 元数据 describe()
"""

from __future__ import annotations

from datetime import date as date_cls
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from gugu.filters.holder_concentration import HolderConcentrationFilter


# ── 测试数据构造 ──────────────────────────────────────────────


def _make_holder_df(
    holder_num: float = 100000,
    holder_num_pct: float = 5.0,
    avg_hold_change: float = 0.0,
    end_date: str = "2024-09-30",
    org_hold_pct: float = 5.0,
) -> pd.DataFrame:
    """构造最新一期股东户数快照。"""
    return pd.DataFrame(
        [
            {
                "symbol": "600519",
                "name": "贵州茅台",
                "holder_num": holder_num,
                "holder_num_change": holder_num * holder_num_pct / 100,
                "holder_num_pct": holder_num_pct,
                "avg_hold_amount": 1000,
                "avg_hold_change": avg_hold_change,
                "end_date": end_date,
                "org_hold_pct": org_hold_pct,
            }
        ]
    )


def _make_history_df(
    pcts: list[float],
    dates: list[str] | None = None,
) -> pd.DataFrame:
    """构造历史筹码数据（按 end_date 降序，与采集器输出一致）。

    Args:
        pcts: 各季度户数变化率%（从最近到最远）。
        dates: 对应的 end_date，默认用最近几个季度。
    """
    if dates is None:
        default_dates = [
            "2024-09-30",
            "2024-06-30",
            "2024-03-31",
            "2023-12-31",
        ]
        dates = default_dates[: len(pcts)]
    return pd.DataFrame(
        [{"holder_num_pct": p, "end_date": d, "holder_num": 100000} for d, p in zip(dates, pcts)]
    )


@pytest.fixture
def mock_collector():
    """构造 mock 采集器，避免实际调用 akshare API。"""
    collector = AsyncMock()
    collector.fetch_holder_structure = AsyncMock(return_value=_make_holder_df())
    collector.fetch_holder_history = AsyncMock(return_value=None)
    return collector


@pytest.fixture
def holder_filter(mock_collector):
    """构造已注入 mock 采集器的过滤器。"""
    f = HolderConcentrationFilter()
    f._collector = mock_collector
    return f


# ── 测试用例 ──────────────────────────────────────────────────


class TestHolderConcentrationFilter:
    """筹码集中度过滤器测试。"""

    async def test_default_filter_passes_when_data_missing(self, mock_collector):
        """数据缺失时（fetch 返回 None）应默认放行 pass=True。"""
        mock_collector.fetch_holder_structure.return_value = None
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["pass"] is True
        assert result["data_missing"] is True

    async def test_thunderbolt_signal_rejected(self, mock_collector):
        """避雷针信号（季度户数增幅 > 30%）应被拒绝 pass=False，label='避雷针'。"""
        mock_collector.fetch_holder_structure.return_value = _make_holder_df(
            holder_num_pct=35.0,  # > 30% 阈值
            avg_hold_change=0.0,
        )
        mock_collector.fetch_holder_history.return_value = None
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["pass"] is False
        assert result["label"] == "避雷针"
        assert result["data_missing"] is False
        assert any("户数增幅" in r for r in result["reasons"])

    async def test_lurker_signal_boosted(self, mock_collector):
        """潜伏者信号（连续 2 季度下降 > 20% + 户均持股增 > 15%）应通过 pass=True。"""
        # 最新快照：户均持股增加 > 15%
        mock_collector.fetch_holder_structure.return_value = _make_holder_df(
            holder_num_pct=-5.0,  # 当前季度也在下降
            avg_hold_change=20.0,  # > 15%
        )
        # 历史 3 期：最近两期均下降，累计降幅 > 20%
        mock_collector.fetch_holder_history.return_value = _make_history_df(
            pcts=[-12.0, -10.0, 5.0],  # 最近 → 最远
        )
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["pass"] is True
        assert result["label"] == "潜伏者"
        assert result["data_missing"] is False
        assert any("confidence_boost" in r for r in result["reasons"])

    async def test_normal_holder_change_passes(self, mock_collector):
        """正常户数变化（季度增幅 10%）应通过 pass=True，label='中性'。"""
        mock_collector.fetch_holder_structure.return_value = _make_holder_df(
            holder_num_pct=10.0,
            avg_hold_change=0.0,
        )
        mock_collector.fetch_holder_history.return_value = None
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["pass"] is True
        assert result["label"] == "中性"

    async def test_avg_hold_decline_rejected(self, mock_collector):
        """户均持股减少 > 20% 应被拒绝 pass=False。"""
        mock_collector.fetch_holder_structure.return_value = _make_holder_df(
            holder_num_pct=5.0,
            avg_hold_change=-25.0,  # < -20% 阈值
        )
        mock_collector.fetch_holder_history.return_value = None
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["pass"] is False
        assert result["label"] == "避雷针"
        assert any("户均持股" in r for r in result["reasons"])

    def test_describe_returns_metadata(self):
        """describe() 应返回正确元数据。"""
        f = HolderConcentrationFilter()
        desc = f.describe()

        assert desc["name"] == "筹码集中度过滤器"
        assert desc["quarterly_change_threshold"] == 30.0
        assert desc["avg_hold_change_threshold"] == -20.0
        assert desc["lurker_consecutive_quarters"] == 2
        assert "选股池准入" in desc["positioning"]

    async def test_cache_day_invalidates(self, mock_collector, monkeypatch):
        """当日缓存跨日失效（mock date.today）。"""
        import datetime as dt

        mock_date = MagicMock()
        mock_date.today.return_value = dt.date(2024, 1, 1)
        monkeypatch.setattr("gugu.filters.holder_concentration.date", mock_date)

        mock_collector.fetch_holder_structure.return_value = _make_holder_df()
        mock_collector.fetch_holder_history.return_value = None

        f = HolderConcentrationFilter()
        f._collector = mock_collector

        # 第一次调用 → 触发采集
        await f.check("600519")
        assert mock_collector.fetch_holder_structure.call_count == 1

        # 第二次调用（同日）→ 缓存命中，不重新采集
        await f.check("600519")
        assert mock_collector.fetch_holder_structure.call_count == 1

        # 跨日 → 缓存失效，重新采集
        mock_date.today.return_value = dt.date(2024, 1, 2)
        await f.check("600519")
        assert mock_collector.fetch_holder_structure.call_count == 2

    async def test_consecutive_quarters_growth_rejected(self, mock_collector):
        """户数已连续 2 季度增长且累计 > 20% 应被拒绝。"""
        # 最新快照：增幅 15%（< 30% 阈值，不触发信号1）
        mock_collector.fetch_holder_structure.return_value = _make_holder_df(
            holder_num_pct=15.0,
            avg_hold_change=0.0,
        )
        # 历史 3 期：最近两期均增长，累计 > 20%
        mock_collector.fetch_holder_history.return_value = _make_history_df(
            pcts=[15.0, 10.0, -5.0],  # 最近 → 最远
        )
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["pass"] is False
        assert result["label"] == "避雷针"
        assert any("连续" in r and "增长" in r for r in result["reasons"])

    async def test_data_missing_returns_label(self, mock_collector):
        """数据缺失时 label 应为 '数据缺失'。"""
        mock_collector.fetch_holder_structure.return_value = None
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["label"] == "数据缺失"
        assert result["data_missing"] is True
        assert "筹码数据不可用" in result["reasons"]

    async def test_reasons_list_populated(self, mock_collector):
        """reasons 列表应包含可读消息。"""
        mock_collector.fetch_holder_structure.return_value = _make_holder_df(
            holder_num_pct=40.0,
            avg_hold_change=-25.0,
        )
        mock_collector.fetch_holder_history.return_value = None
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert isinstance(result["reasons"], list)
        assert len(result["reasons"]) >= 1
        for reason in result["reasons"]:
            assert isinstance(reason, str)
            assert len(reason) > 0

    async def test_empty_dataframe_passes(self, mock_collector):
        """fetch 返回空 DataFrame 时应默认放行。"""
        mock_collector.fetch_holder_structure.return_value = pd.DataFrame()
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["pass"] is True
        assert result["data_missing"] is True

    async def test_collector_exception_passes(self, mock_collector):
        """采集器抛异常时应默认放行（慢变量不阻塞信号）。"""
        mock_collector.fetch_holder_structure.side_effect = RuntimeError("network error")
        f = HolderConcentrationFilter()
        f._collector = mock_collector

        result = await f.check("600519")

        assert result["pass"] is True
        assert result["data_missing"] is True

    async def test_cache_hit_same_day(self, mock_collector, monkeypatch):
        """同日多次调用应命中缓存，只采集一次。"""
        import datetime as dt

        mock_date = MagicMock()
        mock_date.today.return_value = dt.date(2024, 6, 15)
        monkeypatch.setattr("gugu.filters.holder_concentration.date", mock_date)

        mock_collector.fetch_holder_structure.return_value = _make_holder_df()
        mock_collector.fetch_holder_history.return_value = None

        f = HolderConcentrationFilter()
        f._collector = mock_collector

        await f.check("600519")
        await f.check("600519")
        await f.check("600519")

        assert mock_collector.fetch_holder_structure.call_count == 1
