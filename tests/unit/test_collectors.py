"""采集器单元测试：AkshareCollector + SinaCollector。

仅 Mock 外部 IO（akshare API），不 Mock 业务逻辑。
验证采集器列名归一化、异常处理、降级源差异。
"""
from __future__ import annotations

import pandas as pd
import pytest

from gugu.data.collectors.akshare_collector import AkshareCollector
from gugu.data.collectors.base import BaseCollector
from gugu.data.collectors.fallback import SinaCollector


# ========== Fixture 数据 ==========

def make_history_df(rows: int = 60) -> pd.DataFrame:
    """构造模拟历史行情 DataFrame（带微小波动以通过 PlausiblePriceRule）。"""
    prices = [100.0 + (i * 0.02) for i in range(rows)]
    return pd.DataFrame({
        "日期": pd.date_range("2024-01-01", periods=rows, freq="D").strftime("%Y-%m-%d"),
        "开盘": [p - 0.1 for p in prices],
        "收盘": prices,
        "最高": [p + 0.2 for p in prices],
        "最低": [p - 0.2 for p in prices],
        "成交量": [1_000_000] * rows,
        "成交额": [100_000_000] * rows,
        "涨跌幅": [1.0] * rows,
    })


def make_realtime_df() -> pd.DataFrame:
    """构造合法的实时行情 DataFrame（akshare 中文列名）。"""
    return pd.DataFrame({
        "代码": ["600519", "000858"],
        "名称": ["贵州茅台", "五粮液"],
        "最新价": [1500.0, 100.0],
        "涨跌幅": [1.5, -0.5],
        "成交量": [2_000_000, 3_000_000],
        "成交额": [3_000_000_000.0, 300_000_000.0],
    })


# ========== AkshareCollector 测试 ==========

class TestAkshareCollectorSource:
    """AkshareCollector 基本属性。"""

    def test_source_is_akshare(self):
        assert AkshareCollector.source == "akshare"

    def test_inherits_base_collector(self):
        collector = AkshareCollector()
        assert isinstance(collector, BaseCollector)

    def test_normalize_symbol(self):
        assert BaseCollector.normalize_symbol("519") == "000519"
        assert BaseCollector.normalize_symbol("600519") == "600519"
        assert BaseCollector.normalize_symbol(" 600519 ") == "600519"

    def test_symbol_with_prefix(self):
        assert BaseCollector.symbol_with_prefix("600519") == "sh600519"
        assert BaseCollector.symbol_with_prefix("000858") == "sz000858"
        assert BaseCollector.symbol_with_prefix("430047") == "bj430047"


class TestAkshareCollectorHistory:
    """AkshareCollector 历史行情采集。"""

    def test_fetch_stock_history_returns_normalized_columns(self, monkeypatch):
        """历史行情应返回统一列名 date/open/high/low/close/volume/amount。"""
        df_raw = make_history_df(60)
        monkeypatch.setattr(
            "gugu.data.collectors.akshare_collector.ak.stock_zh_a_hist",
            lambda **kwargs: df_raw,
        )

        collector = AkshareCollector()
        df = collector.fetch_stock_history("600519", days=60)

        assert not df.empty
        for col in ("date", "open", "high", "low", "close", "volume", "amount"):
            assert col in df.columns, f"缺少列 {col}"

    def test_fetch_stock_history_limits_rows(self, monkeypatch):
        """应返回最多 days 行。"""
        df_raw = make_history_df(100)
        monkeypatch.setattr(
            "gugu.data.collectors.akshare_collector.ak.stock_zh_a_hist",
            lambda **kwargs: df_raw,
        )

        collector = AkshareCollector()
        df = collector.fetch_stock_history("600519", days=30)
        assert len(df) <= 30

    def test_fetch_stock_history_empty_symbol(self, monkeypatch):
        """空 DataFrame 输入应原样返回。"""
        monkeypatch.setattr(
            "gugu.data.collectors.akshare_collector.ak.stock_zh_a_hist",
            lambda **kwargs: pd.DataFrame(),
        )

        collector = AkshareCollector()
        df = collector.fetch_stock_history("600519", days=60)
        assert df.empty


class TestAkshareCollectorRealtime:
    """AkshareCollector 实时行情采集。"""

    def test_fetch_stock_realtime_returns_normalized_columns(self, monkeypatch):
        """实时行情应返回统一列名 symbol/name/price/change_pct/volume/amount。"""
        df_raw = make_realtime_df()
        monkeypatch.setattr(
            "gugu.data.collectors.akshare_collector.ak.stock_zh_a_spot_em",
            lambda: df_raw,
        )

        collector = AkshareCollector()
        df = collector.fetch_stock_realtime(["600519", "000858"])

        assert not df.empty
        for col in ("symbol", "name", "price", "change_pct", "volume", "amount"):
            assert col in df.columns
        assert "600519" in df["symbol"].values

    def test_fetch_stock_realtime_filters_by_symbols(self, monkeypatch):
        """应按传入 symbols 过滤。"""
        df_raw = make_realtime_df()
        monkeypatch.setattr(
            "gugu.data.collectors.akshare_collector.ak.stock_zh_a_spot_em",
            lambda: df_raw,
        )

        collector = AkshareCollector()
        df = collector.fetch_stock_realtime(["600519"])
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "600519"

    def test_fetch_stock_realtime_empty_market(self, monkeypatch):
        """全市场快照为空时返回空 DataFrame。"""
        # akshare 真实场景：API 返回带列名但无行的 DataFrame
        empty_df = pd.DataFrame(columns=["代码", "名称", "最新价", "涨跌幅", "成交量", "成交额"])
        monkeypatch.setattr(
            "gugu.data.collectors.akshare_collector.ak.stock_zh_a_spot_em",
            lambda: empty_df,
        )

        collector = AkshareCollector()
        df = collector.fetch_stock_realtime(["600519"])
        assert df.empty


# ========== SinaCollector 测试 ==========

class TestSinaCollectorSource:
    """SinaCollector 基本属性。"""

    def test_source_is_sina(self):
        assert SinaCollector.source == "sina"

    def test_source_differs_from_akshare(self):
        """降级源必须与主源不同。"""
        assert SinaCollector.source != AkshareCollector.source


class TestSinaCollectorRealtime:
    """SinaCollector 实时行情采集。"""

    def test_fetch_stock_realtime_returns_dataframe(self, monkeypatch):
        """新浪实时行情应返回 DataFrame。"""
        df_raw = make_realtime_df()
        monkeypatch.setattr(
            "gugu.data.collectors.fallback.ak.stock_zh_a_spot",
            lambda: df_raw,
        )

        collector = SinaCollector()
        df = collector.fetch_stock_realtime(["600519"])

        assert not df.empty
        assert "symbol" in df.columns
        assert "price" in df.columns
        assert df.iloc[0]["symbol"] == "600519"

    def test_fetch_stock_realtime_empty_symbols(self):
        """空 symbols 列表应返回空 DataFrame。"""
        collector = SinaCollector()
        df = collector.fetch_stock_realtime([])
        assert df.empty

    def test_fetch_stock_realtime_handles_api_error(self, monkeypatch):
        """API 异常应返回空 DataFrame，不抛出。"""
        def raise_error():
            raise RuntimeError("network error")

        monkeypatch.setattr(
            "gugu.data.collectors.fallback.ak.stock_zh_a_spot",
            lambda: raise_error(),
        )

        collector = SinaCollector()
        df = collector.fetch_stock_realtime(["600519"])
        assert df.empty


class TestSinaCollectorUnsupported:
    """SinaCollector 不支持的接口应返回空。"""

    def test_fetch_sector_flow_returns_empty(self):
        collector = SinaCollector()
        df = collector.fetch_sector_flow()
        assert df.empty

    def test_fetch_stock_flow_returns_empty(self):
        collector = SinaCollector()
        df = collector.fetch_stock_flow("600519")
        assert df.empty
