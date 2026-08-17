"""采集器单元测试：AkshareCollector + SinaCollector。

仅 Mock 外部 IO（akshare API），不 Mock 业务逻辑。
验证采集器列名归一化、异常处理、降级源差异。
"""

from __future__ import annotations

import pandas as pd
import pytest

from gugu.data.collectors.akshare_collector import AkshareCollector
from gugu.data.collectors.base import BaseCollector
from gugu.data.collectors.eastmoney_collector import EastmoneyCollector
from gugu.data.collectors.fallback import SinaCollector
from gugu.data.collectors.tencent_collector import TencentCollector


def test_eastmoney_stock_flow_main_pct_is_unavailable_when_not_provided(monkeypatch):
    collector = EastmoneyCollector()
    payload = {"data": {"klines": ["2026-08-03,100000000,1,2,3,4"]}}

    class MockResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class MockClient:
        def get(self, *args, **kwargs):
            return MockResponse()

    monkeypatch.setattr(collector, "_get_client", lambda: MockClient())
    df = collector.fetch_stock_flow("600519")
    assert pd.isna(df.iloc[0]["main_pct"])


# ========== Fixture 数据 ==========


def make_history_df(rows: int = 60) -> pd.DataFrame:
    """构造模拟历史行情 DataFrame（带微小波动以通过 PlausiblePriceRule）。
    日期使用近 60 天，避免 FreshnessRule 拒绝失效数据。
    """
    import datetime

    today = datetime.date.today()
    start = today - datetime.timedelta(days=rows)
    prices = [100.0 + (i * 0.02) for i in range(rows)]
    return pd.DataFrame(
        {
            "日期": pd.date_range(start, periods=rows, freq="D").strftime("%Y-%m-%d"),
            "开盘": [p - 0.1 for p in prices],
            "收盘": prices,
            "最高": [p + 0.2 for p in prices],
            "最低": [p - 0.2 for p in prices],
            "成交量": [1_000_000] * rows,
            "成交额": [100_000_000] * rows,
            "涨跌幅": [1.0] * rows,
        }
    )


def make_realtime_df() -> pd.DataFrame:
    """构造合法的实时行情 DataFrame（akshare 中文列名）。"""
    return pd.DataFrame(
        {
            "代码": ["600519", "000858"],
            "名称": ["贵州茅台", "五粮液"],
            "最新价": [1500.0, 100.0],
            "涨跌幅": [1.5, -0.5],
            "成交量": [2_000_000, 3_000_000],
            "成交额": [3_000_000_000.0, 300_000_000.0],
        }
    )


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

    def test_symbol_with_prefix_index(self):
        """v0.9.7 P0：沪深300(000300) 自动识别为 sh；深证成指(399001) 为 sz。"""
        assert BaseCollector.symbol_with_prefix("000300") == "sh000300"
        assert BaseCollector.symbol_with_prefix("399001") == "sz399001"
        assert BaseCollector.symbol_with_prefix("399006") == "sz399006"

    def test_symbol_with_prefix_explicit_prefix_wins(self):
        """显式 sh/sz 前缀优先于白名单（000688 国城矿业 是深市股票）。"""
        assert BaseCollector.symbol_with_prefix("sh000688") == "sh000688"
        assert BaseCollector.symbol_with_prefix("sz000688") == "sz000688"
        # 000688 不在指数白名单（国城矿业=深市股票），默认 sz
        assert BaseCollector.symbol_with_prefix("000688") == "sz000688"

    def test_is_index_code(self):
        assert BaseCollector.is_index_code("000300") is True
        assert BaseCollector.is_index_code("399001") is True
        assert BaseCollector.is_index_code("600519") is False
        # 000001 平安银行（深市股票）不在白名单
        assert BaseCollector.is_index_code("000001") is False


class TestAkshareCollectorFlowBreaker:
    """资金流接口熔断（2026-08-07：东财限流 RemoteDisconnected 全天刷 1705 次 ERROR）。

    熔断逻辑在 BaseCollector（akshare/eastmoney 同源东财共享）。
    连续失败达阈值 → 熔断冷却期（默认 30 分钟）→ 冷却期内静默返回空 DataFrame
    （不调 API 不打 ERROR）→ 到期自动恢复。成功即重置计数。
    """

    def _collector(self, monkeypatch):
        import time

        collector = AkshareCollector()
        # 重置熔断状态（类级变量，防止测试间污染）
        collector._flow_fail_count = 0
        collector._flow_breaker_until = 0.0
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        return collector

    def test_flow_breaker_activates_after_threshold(self, monkeypatch):
        collector = self._collector(monkeypatch)
        # 连续失败 3 次（阈值）→ 熔断
        for _ in range(BaseCollector._FLOW_BREAKER_THRESHOLD):
            collector._flow_record_failure()
        assert collector._flow_breaker_active() is True

    def test_flow_breaker_guard_returns_empty_without_calling_api(self, monkeypatch):
        collector = self._collector(monkeypatch)
        # 手动进入熔断状态
        collector._flow_breaker_until = 9999.0
        df = collector.fetch_stock_flow("600519")
        assert df.empty
        assert collector._flow_breaker_active() is True

    def test_flow_success_resets_counter(self, monkeypatch):
        collector = self._collector(monkeypatch)
        collector._flow_fail_count = 2
        collector._flow_record_success()
        assert collector._flow_fail_count == 0
        assert collector._flow_breaker_until == 0.0

    def test_flow_success_does_not_clear_active_breaker(self, monkeypatch):
        """2026-08-10 修复：熔断冷却期内 success 不提前解除熔断。

        原实现 `_flow_breaker_until = 0.0` 导致振荡——sector_flow 偶尔成功
        立即解除熔断 → 下一轮 stock_flow 又连续失败重新熔断（09:40 两次熔断
        相隔 5 秒实证）。冷却期内成功只清失败计数，熔断自然到期。
        """
        collector = self._collector(monkeypatch)
        collector._flow_breaker_until = 9999.0  # 熔断冷却中（time=1000）
        collector._flow_fail_count = 2
        collector._flow_record_success()
        assert collector._flow_fail_count == 0, "成功应清零失败计数"
        assert collector._flow_breaker_active() is True, "冷却期内成功不应解除熔断"

    def test_flow_breaker_expires_after_cooldown(self, monkeypatch):
        collector = self._collector(monkeypatch)
        collector._flow_breaker_until = 500.0  # time.time()=1000 已过期
        assert collector._flow_breaker_active() is False
        # 冷却到期后的 success 才允许复位 until（自然恢复）
        collector._flow_record_success()
        assert collector._flow_breaker_until == 0.0

    def test_realtime_breaker_guard_returns_empty_without_calling_api(self, monkeypatch):
        """2026-08-11 修复：fetch_stock_realtime 接入熔断。

        熔断冷却期内直接返回空 DataFrame，不调 stock_zh_a_spot_em（不刷 ERROR）。
        """
        import gugu.data.collectors.akshare_collector as ak_mod

        collector = self._collector(monkeypatch)
        collector._flow_breaker_until = 9999.0  # 熔断冷却中（time=1000）

        def _boom(*a, **kw):
            raise AssertionError("熔断期内不应调用东财全市场快照 API")

        monkeypatch.setattr(ak_mod.ak, "stock_zh_a_spot_em", _boom)
        df = collector.fetch_stock_realtime(["600519"])
        assert df.empty
        assert df.attrs.get("flow_breaker_active") is True
        assert collector._flow_breaker_active() is True

    def test_realtime_failure_triggers_breaker(self, monkeypatch):
        """2026-08-11 修复：实时行情失败计入熔断计数，达阈值后熔断。"""
        import gugu.data.collectors.akshare_collector as ak_mod
        import tenacity.nap
        from tenacity import RetryError

        # tenacity retry 的 wait_exponential 会真实 sleep（1-8s），测试禁掉
        monkeypatch.setattr(tenacity.nap, "sleep", lambda *a, **kw: None)

        collector = self._collector(monkeypatch)

        def _boom(*a, **kw):
            raise ConnectionError("RemoteDisconnected('Remote end closed connection')")

        monkeypatch.setattr(ak_mod.ak, "stock_zh_a_spot_em", _boom)
        # 单次调用：stop_after_attempt(3) 内部 3 次失败即达熔断阈值 3
        with pytest.raises(RetryError):
            collector.fetch_stock_realtime(["600519"])
        assert collector._flow_breaker_active() is True

    def test_realtime_success_resets_failure_count(self, monkeypatch):
        """2026-08-11 修复：实时行情成功后重置失败计数（与资金流方法同语义）。"""
        import gugu.data.collectors.akshare_collector as ak_mod
        import pandas as pd

        collector = self._collector(monkeypatch)
        collector._flow_fail_count = 2

        monkeypatch.setattr(
            ak_mod.ak,
            "stock_zh_a_spot_em",
            lambda *a, **kw: pd.DataFrame(
                {
                    "代码": ["600519"],
                    "名称": ["贵州茅台"],
                    "最新价": [1500.0],
                    "涨跌幅": [1.2],
                    "成交量": [1000],
                    "成交额": [1.5e9],
                }
            ),
        )
        df = collector.fetch_stock_realtime(["600519"])
        assert not df.empty
        assert collector._flow_fail_count == 0

    def test_eastmoney_inherits_breaker_guard(self, monkeypatch):
        """eastmoney 同源东财，熔断期间两个资金流接口都静默返回空。"""
        import time

        em = EastmoneyCollector()
        em._flow_fail_count = 0
        em._flow_breaker_until = 9999.0
        monkeypatch.setattr(time, "time", lambda: 1000.0)
        df = em.fetch_stock_flow("600519")
        assert df.empty
        assert em._flow_breaker_active() is True


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
        # 新浪实时行情返回 33+ 个逗号分隔字段（mock 至少 32 个）
        fields = [
            "贵州茅台",
            "1734.00",
            "1732.00",
            "1735.00",
            "1728.00",
            "1730.00",
            "1726.00",
            "1000000",
            "50000000",
        ] + [""] * 24
        mock_text = 'var hq_str_sh600519="' + ",".join(fields) + '";\n'

        class MockResponse:
            def __init__(self):
                self.text = mock_text
                self.status_code = 200

            def raise_for_status(self):
                pass

        class MockClient:
            def get(self, url, **kwargs):
                return MockResponse()

        monkeypatch.setattr(
            "gugu.data.collectors.fallback.SinaCollector._get_client",
            lambda self: MockClient(),
        )

        collector = SinaCollector()
        df = collector.fetch_stock_realtime(["600519"])

        assert not df.empty
        assert "symbol" in df.columns
        assert "price" in df.columns
        assert df.iloc[0]["symbol"] == "600519"
        assert "name" in df.columns
        assert df.iloc[0]["name"] == "贵州茅台"


class TestSinaCollectorFullMarketSnapshot:
    """新浪全市场快照（v0.9.10：东财限流保底源）。"""

    def _mock_client(self, monkeypatch, pages):
        """mock Market_Center 分页接口，返回 pages 个页面（每页 2 条）。"""

        class MockResponse:
            def __init__(self, data):
                self._data = data
                self.status_code = 200

            def json(self):
                return self._data

            def raise_for_status(self):
                pass

        class MockClient:
            def __init__(self, pages):
                self._pages = pages

            def get(self, url, **kwargs):
                page = int(url.split("page=")[1].split("&")[0])
                data = self._pages.get(page, [])
                return MockResponse(data)

        monkeypatch.setattr(
            "gugu.data.collectors.fallback.SinaCollector._get_client",
            lambda self: MockClient(pages),
        )

    def _sample_item(self, code, name, nmc_wan):
        return {
            "code": code,
            "name": name,
            "trade": "10.5",
            "changepercent": "1.2",
            "volume": "100000",
            "amount": "1000000",
            "turnoverratio": "5.5",
            "nmc": str(nmc_wan),  # 万元
            "mktcap": str(nmc_wan * 2),
            "pb": "2.0",
            "per": "15.0",
        }

    def test_full_market_snapshot_pagination(self, monkeypatch):
        """分页拉取全市场，字段映射正确。"""
        pages = {
            1: [self._sample_item("600519", "贵州茅台", 2000000)],
            2: [self._sample_item("000001", "平安银行", 3000000)],
            3: [],  # 结束页
        }
        self._mock_client(monkeypatch, pages)

        collector = SinaCollector()
        df = collector.fetch_stock_realtime([])

        assert not df.empty
        assert len(df) == 2
        assert df.iloc[0]["symbol"] == "600519"
        assert df.iloc[0]["name"] == "贵州茅台"
        # 单位转换：新浪 nmc 万元 → 元（×1e4）
        assert df.iloc[0]["circ_mv"] == 2000000 * 1e4
        assert df.iloc[0]["mktcap"] == 4000000 * 1e4
        assert "price" in df.columns
        assert "change_pct" in df.columns
        assert "turnoverratio" in df.columns

    def test_full_market_snapshot_empty(self, monkeypatch):
        """无数据时返回空 DataFrame。"""
        self._mock_client(monkeypatch, {1: [], 2: []})

        collector = SinaCollector()
        df = collector.fetch_stock_realtime([])
        assert df.empty

    def test_full_market_snapshot_http_error_stops_pagination(self, monkeypatch):
        """某页失败即停止分页（避免无限重试），返回已拉取数据。"""

        class MockResponse:
            def json(self):
                return [{"code": "600519", "name": "贵州茅台", "trade": "10.5"}]

            def raise_for_status(self):
                raise Exception("connection aborted")

        class MockClient:
            def get(self, url, **kwargs):
                return MockResponse()

        monkeypatch.setattr(
            "gugu.data.collectors.fallback.SinaCollector._get_client",
            lambda self: MockClient(),
        )

        collector = SinaCollector()
        df = collector.fetch_stock_realtime([])
        assert df.empty  # 首页失败 → 无数据（不崩溃）

    def test_realtime_attrs_are_consistent(self, monkeypatch):
        fields = [
            "贵州茅台",
            "1734.00",
            "1732.00",
            "1735.00",
            "1728.00",
            "1730.00",
            "1726.00",
            "1000000",
            "50000000",
        ] + [""] * 24
        mock_text = 'var hq_str_sh600519="' + ",".join(fields) + '";\n'

        class MockResponse:
            text = mock_text
            status_code = 200

            def raise_for_status(self):
                pass

        class MockClient:
            def get(self, url, **kwargs):
                return MockResponse()

        monkeypatch.setattr(
            "gugu.data.collectors.fallback.SinaCollector._get_client", lambda self: MockClient()
        )
        df = SinaCollector().fetch_stock_realtime(["600519"])
        assert df.attrs["data_source"] == "sina"
        assert df.attrs["adjust_type"] == "none"
        assert "fetch_timestamp" in df.attrs

    def test_fetch_stock_realtime_empty_symbols(self, monkeypatch):
        """空 symbols = 全市场快照；数据源无数据时返回空 DataFrame（v0.9.10 语义变更）。"""

        class MockResponse:
            def json(self):
                return []

            def raise_for_status(self):
                pass

        class MockClient:
            def get(self, url, **kwargs):
                return MockResponse()

        monkeypatch.setattr(
            "gugu.data.collectors.fallback.SinaCollector._get_client",
            lambda self: MockClient(),
        )

        collector = SinaCollector()
        df = collector.fetch_stock_realtime([])
        assert df.empty

    def test_fetch_stock_realtime_handles_api_error(self, monkeypatch):
        """API 异常应返回空 DataFrame，不抛出。"""

        class ErrorClient:
            def get(self, url, **kwargs):
                raise RuntimeError("network error")

        monkeypatch.setattr(
            "gugu.data.collectors.fallback.SinaCollector._get_client",
            lambda self: ErrorClient(),
        )

        collector = SinaCollector()
        df = collector.fetch_stock_realtime(["600519"])
        assert df.empty


class TestTencentCollectorHistory:
    def test_bj_volume_factor(self, monkeypatch):
        # 动态日期：mock 数据用最近交易日（避免 FreshnessRule 随时间漂移拒绝）
        from datetime import date, timedelta

        mock_date = (date.today() - timedelta(days=1)).isoformat()
        payload = {"data": {"bj430047": {"qfqday": [[mock_date, "10", "12", "13", "9", "1000"]]}}}

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                import json

                return json.dumps(payload).encode()

        monkeypatch.setattr(
            "gugu.data.collectors.tencent_collector.urllib.request.urlopen",
            lambda *args, **kwargs: MockResponse(),
        )
        df = TencentCollector().fetch_stock_history("430047", days=1)
        assert df.iloc[0]["amount"] == 12000.0
        assert df.attrs["data_source"] == "tencent"
        assert df.attrs["adjust_type"] == "qfq"


class TestSinaCollectorUnsupported:
    """SinaCollector 不支持的接口应抛 NotImplementedError 触发 DataManager 降级。"""

    def test_fetch_sector_flow_raises(self):
        collector = SinaCollector()
        with pytest.raises(NotImplementedError):
            collector.fetch_sector_flow()

    def test_fetch_stock_flow_raises(self):
        collector = SinaCollector()
        with pytest.raises(NotImplementedError):
            collector.fetch_stock_flow("600519")
