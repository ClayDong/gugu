"""DataManager 单元测试：主源/降级源切换、缓存、数据质量校验。

只 Mock 外部 IO（采集器），不 Mock 业务逻辑（DataManager 本身）。
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pandas as pd
import pytest

from gugu.data.cache import ParquetPersistentCache, cache
from gugu.data.collectors.akshare_collector import AkshareCollector
from gugu.data.collectors.fallback import SinaCollector
from gugu.data.manager import DataManager
from gugu.data.source_health import DataSourceHealthMonitor


def _make_basic_df(n: int = 60) -> pd.DataFrame:
    """构造基础 OHLCV DataFrame（带微小波动以通过 PlausiblePriceRule）。
    日期使用最近的 n 天，避免 FreshnessRule 拒绝。
    """
    import datetime

    today = datetime.date.today()
    prices = [10.0 + (i * 0.01) for i in range(n)]  # 60 天内从 10.0 缓慢涨到 10.59
    return pd.DataFrame(
        {
            "date": pd.date_range(today - datetime.timedelta(days=n - 1), periods=n, freq="D"),
            "open": [p - 0.02 for p in prices],
            "high": [p + 0.2 for p in prices],
            "low": [p - 0.2 for p in prices],
            "close": prices,
            "volume": [1_000_000] * n,
            "amount": [100_000_000] * n,
        }
    )


def _fresh_dm() -> DataManager:
    """创建一个干净的 DataManager 实例（主源 sina，降级 akshare）。"""
    dm = object.__new__(DataManager)
    dm._lock = asyncio.Lock()
    dm._primary = SinaCollector()
    dm._fallbacks = [AkshareCollector()]
    dm._fallback_semaphore = asyncio.Semaphore(1)
    dm._fail_threshold = 3
    dm._fail_cooldown = 300
    dm._fail_count = 0
    dm._degraded_until = 0.0
    dm._parquet_cache = mock.MagicMock()
    dm._parquet_cache.get.return_value = pd.DataFrame()
    dm._health = DataSourceHealthMonitor()
    return dm


@mock.patch.object(SinaCollector, "fetch_stock_history", return_value=_make_basic_df())
@mock.patch.object(AkshareCollector, "fetch_stock_history", return_value=_make_basic_df())
@pytest.mark.asyncio
async def test_primary_success_not_degraded(
    mock_ak: mock.MagicMock,
    mock_sina: mock.MagicMock,
) -> None:
    """主源（sina）成功时不应调用降级源（akshare）。"""
    cache().clear()
    dm = _fresh_dm()
    df = await dm.fetch_stock_history("600519", days=60)
    assert not df.empty
    mock_sina.assert_called()
    assert mock_ak.call_count == 0


@mock.patch.object(AkshareCollector, "fetch_stock_history", return_value=_make_basic_df())
@mock.patch.object(SinaCollector, "fetch_stock_history", side_effect=RuntimeError("api error"))
@pytest.mark.asyncio
async def test_fallback_after_failures(
    mock_ak: mock.MagicMock,
    mock_sina: mock.MagicMock,
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


@pytest.mark.asyncio
async def test_sector_validator_accepts_symbol_and_preserves_attrs() -> None:
    cache().clear()
    dm = _fresh_dm()
    df = pd.DataFrame({"sector": ["银行"], "main_net": [1.0], "main_pct": [0.1]})
    df.attrs["data_available"] = True
    validated = dm._validate("fetch_sector_flow", df, ())
    assert validated.attrs["data_available"] is True


@mock.patch.object(AkshareCollector, "fetch_stock_flow", side_effect=RuntimeError("ak error"))
@pytest.mark.asyncio
async def test_all_stock_flow_sources_fail_sets_data_attrs(mock_ak: mock.MagicMock) -> None:
    cache().clear()
    dm = _fresh_dm()
    dm._fail_threshold = 1
    with mock.patch.object(
        SinaCollector, "fetch_stock_flow", side_effect=RuntimeError("sina error")
    ):
        df = await dm.fetch_stock_flow("600519")
    assert df.empty
    assert df.attrs["data_available"] is False
    assert df.attrs["all_sources_failed"] is True
    assert df.attrs["data_error"] == "all_sources_failed"


@mock.patch.object(AkshareCollector, "fetch_stock_flow")
@pytest.mark.asyncio
async def test_breaker_active_short_circuits_no_failure_recorded(
    mock_ak: mock.MagicMock, monkeypatch
) -> None:
    """2026-08-10 修复：熔断空结果短路返回，不记录 all_sources_failed 假失败。

    根因：熔断器返回空 df（无标记）→ DataManager 判定"未成功"→ 继续走完 fallback
    循环 → record_failure("all") → 东财限流期间每分钟 19 条假失败 → 失败率 61%
    → watchdog 误告警（08-10 实证 3 次升级）。

    注意：fetch_stock_flow 在 _UNSUPPORTED_BY_PRIMARY（sina 不支持资金流），
    直接走 fallback → 熔断空必须来自 fallback 采集器（akshare）。
    """
    cache().clear()
    dm = _fresh_dm()
    dm._fail_threshold = 1

    def _breaker_empty(*args, **kwargs) -> pd.DataFrame:
        df = pd.DataFrame()
        df.attrs["flow_breaker_active"] = True
        df.attrs["flow_breaker_source"] = "akshare"
        return df

    mock_ak.side_effect = _breaker_empty
    # DataSourceHealthMonitor 是全局单例，清空共享 buffer 防测试间污染
    dm._health._buffer.clear()
    df = await dm.fetch_stock_flow("600519")

    assert df.empty
    assert df.attrs.get("data_error") == "flow_breaker_active", "应标记为熔断跳过而非失败"
    # 熔断短路不应记录 all 失败事件（假失败风暴根因）
    assert not any(
        e.event == "failure" and e.source == "all" for e in dm._health._buffer
    ), "熔断跳过不应产生 all 失败事件"


@mock.patch.object(AkshareCollector, "fetch_stock_flow")
@pytest.mark.asyncio
async def test_breaker_active_but_other_source_succeeds(mock_ak: mock.MagicMock) -> None:
    """熔断源与可用源并存时：有真实成功 → 正常返回成功结果。"""
    cache().clear()
    dm = _fresh_dm()
    dm._fail_threshold = 1

    # fallback 列表两个源：akshare（熔断空）+ sina（真实资金流数据）
    dm._fallbacks = [AkshareCollector(), SinaCollector()]

    def _breaker_empty(*args, **kwargs) -> pd.DataFrame:
        df = pd.DataFrame()
        df.attrs["flow_breaker_active"] = True
        return df

    # 合法的个股资金流 DataFrame（通过 validate_stock_flow）
    flow_df = pd.DataFrame(
        {
            "date": ["2026-08-10"],
            "main_net": [1.5],
            "main_pct": [0.03],
            "super_large_net": [0.8],
            "large_net": [0.7],
        }
    )

    mock_ak.side_effect = _breaker_empty
    with mock.patch.object(SinaCollector, "fetch_stock_flow", return_value=flow_df):
        df = await dm.fetch_stock_flow("600519")

    assert not df.empty, "降级源有真实数据时不应被熔断短路吞掉"
    assert df.attrs.get("data_error") != "flow_breaker_active"


def test_parquet_cache_key_includes_adjust_type(tmp_path) -> None:
    parquet_cache = ParquetPersistentCache(tmp_path)
    assert parquet_cache._key("600519", 60, "none") != parquet_cache._key("600519", 60, "qfq")


def test_memory_cache_key_includes_adjust_type() -> None:
    from gugu.data.manager import _stable_cache_key

    assert _stable_cache_key("fetch_stock_history", "600519", 60, "none") != _stable_cache_key(
        "fetch_stock_history", "600519", 60, "qfq"
    )


def _mk_ohlcv_df(n_days: int, last_date: str) -> pd.DataFrame:
    """构造有波动的 OHLCV 测试数据（通过质量校验规则）。"""
    import numpy as np

    dates = pd.bdate_range(end=last_date, periods=n_days)
    close = np.linspace(100, 110, n_days) + np.sin(np.arange(n_days))
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.linspace(1_000_000, 2_000_000, n_days),
        }
    )


def test_parquet_cache_intraday_freshness(tmp_path) -> None:
    """交易日盘中/盘后必须含最新 bar；非交易日容忍 7 天老化（2026-08-05 修复）。

    根因链：
    1. 旧逻辑 max_stale_days=7 导致 120d 缓存停在 7/31 时盘中扫描仍直接命中，
       策略信号基于陈旧 K线计算失真。
    2. 二次修复（v0.9.8）：盘中允许昨日 bar（K线源盘中无当日，重拉也拿不到），
       但盘后（>15:00）当日 K线已生成 → 必须含当日 bar，否则收盘验证/绩效
       用错数据（8/5 实测 16:57 仍命中 14:47 写入的 8/4 缓存）。
    """
    from datetime import datetime

    pc = ParquetPersistentCache(tmp_path)
    # 盘中（交易日 14:00）+ 缓存止于 7/31（2 天前）→ 过期刷新
    pc.set("600000", 30, _mk_ohlcv_df(22, "2026-07-31"), "none")
    assert pc.get("600000", 30, "none", now=datetime(2026, 8, 5, 14, 0)) is None
    # 盘中 + 昨日 bar（8/4）→ 命中（K线源盘中无当日，昨日是可得最新）
    pc.set("600000", 30, _mk_ohlcv_df(22, "2026-08-04"), "none")
    assert pc.get("600000", 30, "none", now=datetime(2026, 8, 5, 14, 0)) is not None
    # 盘后 20:00 + 缓存止于 7/31 → 过期（当日 K线已生成，必须含当日）
    pc.set("600000", 30, _mk_ohlcv_df(22, "2026-07-31"), "none")
    assert pc.get("600000", 30, "none", now=datetime(2026, 8, 5, 20, 0)) is None
    # 盘中 + 缓存含当日 → 命中
    pc.set("600000", 30, _mk_ohlcv_df(26, "2026-08-05"), "none")
    assert pc.get("600000", 30, "none", now=datetime(2026, 8, 5, 14, 0)) is not None
    # 周末 + 3 天前缓存 → 容忍命中
    assert pc.get("600000", 30, "none", now=datetime(2026, 8, 8, 10, 0)) is not None
