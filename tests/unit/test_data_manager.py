"""DataManager 单元测试：主源/降级源切换、缓存、数据质量校验。

只 Mock 外部 IO（采集器），不 Mock 业务逻辑（DataManager 本身）。
"""
from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from gugu.data.cache import cache
from gugu.data.collectors.akshare_collector import AkshareCollector
from gugu.data.collectors.fallback import SinaCollector
from gugu.data.manager import DataManager


def _make_basic_df(n: int = 60) -> pd.DataFrame:
    """构造基础 OHLCV DataFrame。"""
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="D"),
        "open": [10.0] * n,
        "high": [11.0] * n,
        "low": [9.0] * n,
        "close": [10.0] * n,
        "volume": [1_000_000] * n,
        "amount": [10_000_000] * n,
    })


def _fresh_dm() -> DataManager:
    """创建一个干净的 DataManager 实例。"""
    dm = DataManager()
    dm._fail_count = 0
    dm._degraded_until = 0.0
    return dm


@mock.patch.object(AkshareCollector, "fetch_stock_history", return_value=_make_basic_df())
@mock.patch.object(SinaCollector, "fetch_stock_history", return_value=_make_basic_df())
@pytest.mark.asyncio
async def test_primary_success_not_degraded(
    mock_sina: mock.MagicMock,
    mock_ak: mock.MagicMock,
) -> None:
    """主源成功时不应调用降级源。"""
    cache().clear()
    dm = _fresh_dm()
    df = await dm.fetch_stock_history("600519", days=60)
    assert not df.empty
    mock_ak.assert_called()
    assert mock_sina.call_count == 0


@mock.patch.object(AkshareCollector, "fetch_stock_history", side_effect=RuntimeError("api error"))
@mock.patch.object(SinaCollector, "fetch_stock_history", return_value=_make_basic_df())
@pytest.mark.asyncio
async def test_fallback_after_failures(
    mock_sina: mock.MagicMock,
    mock_ak: mock.MagicMock,
) -> None:
    """主源连续失败 fail_threshold 次后切换到降级源。

    注意：因缓存机制，第二次相同参数调用会命中缓存而不触发降级。
    故用不同 symbol 测试第二次调用。
    """
    cache().clear()
    dm = _fresh_dm()
    dm._fail_threshold = 2

    # 第1次：主源失败 -> fallback 成功，fail_count=1，未降级
    df1 = await dm.fetch_stock_history("600519", days=60)
    assert not df1.empty
    assert dm._fail_count == 1
    assert not dm.is_degraded

    # 第2次：用不同 symbol 避免缓存命中 -> 主源再失败 -> fail_count=2 -> 降级
    df2 = await dm.fetch_stock_history("000858", days=60)
    assert not df2.empty
    assert dm._fail_count == 2
    assert dm.is_degraded


@mock.patch.object(AkshareCollector, "fetch_stock_history", side_effect=RuntimeError("api error"))
@mock.patch.object(SinaCollector, "fetch_stock_history", side_effect=RuntimeError("sina error"))
@pytest.mark.asyncio
async def test_all_sources_fail(
    mock_sina: mock.MagicMock,
    mock_ak: mock.MagicMock,
) -> None:
    """所有数据源均失败时返回空 DataFrame。"""
    cache().clear()
    dm = _fresh_dm()
    dm._fail_threshold = 1

    df = await dm.fetch_stock_history("600519", days=60)
    assert df.empty


@mock.patch.object(AkshareCollector, "fetch_stock_history", return_value=_make_basic_df())
@mock.patch.object(SinaCollector, "fetch_stock_history", return_value=_make_basic_df())
@pytest.mark.asyncio
async def test_cache_hit(
    mock_sina: mock.MagicMock,
    mock_ak: mock.MagicMock,
) -> None:
    """相同参数的第二次调用应命中缓存，不重复调用采集器。"""
    cache().clear()
    dm = _fresh_dm()

    # 第1次：走采集
    df1 = await dm.fetch_stock_history("600519", days=60)
    assert not df1.empty

    # 第2次：相同 key，应走缓存
    mock_ak.reset_mock()
    mock_sina.reset_mock()
    df2 = await dm.fetch_stock_history("600519", days=60)
    assert not df2.empty
    assert mock_ak.call_count == 0
    assert mock_sina.call_count == 0


@mock.patch.object(AkshareCollector, "fetch_stock_history", side_effect=RuntimeError("api error"))
@pytest.mark.asyncio
async def test_fallback_sector_flow_empty(mock_ak: mock.MagicMock) -> None:
    """降级源不支持的资金流接口应返回空 DataFrame 而不抛异常。"""
    cache().clear()
    with mock.patch.object(SinaCollector, "fetch_sector_flow", return_value=pd.DataFrame()):
        dm = _fresh_dm()
        dm._fail_threshold = 1
        df = await dm.fetch_sector_flow()
        assert df.empty