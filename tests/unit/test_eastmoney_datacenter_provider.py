"""东财数据中心 Provider 与另类 Collector 契约测试。"""

from __future__ import annotations

import pandas as pd

from gugu.data.collectors.altalpha.eastmoney_datacenter_collectors import (
    EastmoneyBlockTradeCollector,
    EastmoneyDragonTigerCollector,
    EastmoneyMarginTradeCollector,
    EastmoneyShareUnlockCollector,
)
from gugu.data.providers.eastmoney_datacenter import EastmoneyDataCenter


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, *, params=None):
        self.calls.append((url, params))
        return self.payload


def test_datacenter_query_returns_only_dict_records():
    transport = FakeTransport({"result": {"data": [{"A": 1}, "bad", {"B": 2}]}})
    provider = EastmoneyDataCenter(transport=transport)

    assert provider.query("REPORT", page_size=10) == [{"A": 1}, {"B": 2}]
    assert transport.calls[0][1]["reportName"] == "REPORT"


def test_datacenter_query_empty_result_is_safe():
    provider = EastmoneyDataCenter(transport=FakeTransport({"result": {"data": None}}))
    assert provider.query("REPORT") == []


def test_dragon_tiger_collector_maps_units_and_evidence(tmp_path):
    provider = EastmoneyDataCenter(
        transport=FakeTransport(
            {
                "result": {
                    "data": [
                        {
                            "SECURITY_CODE": "002475",
                            "SECURITY_NAME_ABBR": "立讯测试",
                            "TRADE_DATE": "2026-08-17 00:00:00",
                            "BILLBOARD_NET_AMT": 100000000,
                            "BILLBOARD_BUY_AMT": 200000000,
                            "BILLBOARD_SELL_AMT": 100000000,
                        }
                    ]
                }
            }
        )
    )
    collector = EastmoneyDragonTigerCollector(provider=provider, cache_dir=tmp_path)
    df = __import__("asyncio").run(collector.fetch_detail("2026-08-17", "2026-08-17"))

    assert df.iloc[0]["code"] == "002475"
    assert df.iloc[0]["lhb_net_buy"] == 10000
    assert df.attrs["data_source"] == "eastmoney_datacenter"
    assert df.attrs["unit"] == "万元"
    assert df.attrs["quality_status"] == "validated"
    assert "TRADE_DATE>='2026-08-17'" in provider.transport.calls[0][1]["filter"]
    assert "TRADE_DATE<='2026-08-17'" in provider.transport.calls[0][1]["filter"]


def test_unlock_collector_empty_data_has_explicit_unavailable_state(tmp_path):
    provider = EastmoneyDataCenter(transport=FakeTransport({"result": {"data": []}}))
    collector = EastmoneyShareUnlockCollector(provider=provider, cache_dir=tmp_path)
    df = __import__("asyncio").run(collector.fetch_upcoming(30))

    assert isinstance(df, pd.DataFrame)
    assert df.empty
    assert df.attrs["data_available"] is False
    assert df.attrs["quality_status"] == "empty"


def test_margin_collector_maps_daily_fields(tmp_path):
    provider = EastmoneyDataCenter(
        transport=FakeTransport(
            {"result": {"data": [{"SCODE": "600519", "DATE": "2026-08-17", "RZYE": 123}]}}
        )
    )
    collector = EastmoneyMarginTradeCollector(provider=provider, cache_dir=tmp_path)
    df = __import__("asyncio").run(collector.fetch_detail("600519"))

    assert df.iloc[0]["code"] == "600519"
    assert df.iloc[0]["margin_balance"] == 123
    assert df.attrs["unit"] == "元"


def test_block_collector_calculates_premium(tmp_path):
    provider = EastmoneyDataCenter(
        transport=FakeTransport(
            {
                "result": {
                    "data": [
                        {
                            "SECURITY_CODE": "600519",
                            "TRADE_DATE": "2026-08-17",
                            "CLOSE_PRICE": 100,
                            "DEAL_PRICE": 95,
                            "DEAL_VOLUME": 10,
                        }
                    ]
                }
            }
        )
    )
    collector = EastmoneyBlockTradeCollector(provider=provider, cache_dir=tmp_path)
    df = __import__("asyncio").run(collector.fetch_detail("600519"))

    assert df.iloc[0]["premium_pct"] == -5
    assert df.attrs["period"] == "event"


def test_datacenter_collector_cache_hit_preserves_evidence(tmp_path):
    transport = FakeTransport(
        {
            "result": {
                "data": [
                    {
                        "SECURITY_CODE": "600519",
                        "TRADE_DATE": "2026-08-17",
                        "CLOSE_PRICE": 100,
                        "DEAL_PRICE": 95,
                        "DEAL_VOLUME": 10,
                    }
                ]
            }
        }
    )
    provider = EastmoneyDataCenter(transport=transport)
    collector = EastmoneyBlockTradeCollector(provider=provider, cache_dir=tmp_path)
    first = __import__("asyncio").run(collector.fetch_detail("600519"))
    second = __import__("asyncio").run(collector.fetch_detail("600519"))

    assert not second.empty
    assert len(transport.calls) == 1
    assert second.attrs["data_source"] == "eastmoney_datacenter"
    assert second.attrs["data_stale"] is False
    assert second.attrs["data_available"] is True
    assert second.attrs["fetch_timestamp"] == first.attrs["fetch_timestamp"]


def test_datacenter_collector_returns_stale_cache_on_provider_failure(tmp_path):
    provider = EastmoneyDataCenter(transport=FakeTransport({"result": {"data": []}}))
    collector = EastmoneyBlockTradeCollector(provider=provider, cache_dir=tmp_path)
    cache_key = collector._build_cache_key(collector.fetch_detail, "600519", 20)
    cached = pd.DataFrame({"code": ["600519"], "premium_pct": [-5.0]})
    from gugu.data.evidence import set_evidence

    cached = set_evidence(
        cached,
        data_source="eastmoney_datacenter",
        period="event",
        unit="元/股",
        fetch_timestamp="2026-08-17T15:00:00",
    )
    collector._write_cache(cached, cache_key=cache_key)
    collector._cache_ttl_hours = -1
    collector._provider.query = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("upstream unavailable")
    )

    stale = __import__("asyncio").run(collector.fetch_detail("600519"))

    assert not stale.empty
    assert stale.attrs["data_stale"] is True
    assert stale.attrs["quality_status"] == "stale"
    assert stale.attrs["data_error"] == "api_failed_using_stale_cache"


def test_datacenter_collector_marks_unavailable_without_cache(tmp_path):
    provider = EastmoneyDataCenter(transport=FakeTransport({"result": {"data": []}}))
    collector = EastmoneyBlockTradeCollector(provider=provider, cache_dir=tmp_path)
    collector._provider.query = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("upstream unavailable")
    )

    unavailable = __import__("asyncio").run(collector.fetch_detail("600519"))

    assert unavailable.empty
    assert unavailable.attrs["data_available"] is False
    assert unavailable.attrs["quality_status"] == "unavailable"
    assert unavailable.attrs["data_error"] == "api_failed_no_cache"
