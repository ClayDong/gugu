"""融资融券采集器时间列测试（2026-11-05）。

修的是一个会让半个市场悄悄消失的 bug：沪深两市接口字段不同，深市不返回交易日期。
原有代码只给沪市做了 ``信用交易日期 → trade_date`` 的映射，深市整边 ``trade_date``
为 NaN，concat 之后按日对齐时深市 47% 的行直接失效。

这类 bug 不会报错、不会抛异常，只会在下游"某天只有一半股票"时变得不可解释，
所以必须锁死：**两个市场都必须带交易日期**。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pandas as pd

from gugu.data.collectors.altalpha.margin_trade import MarginTradeCollector

DATE = "20260714"


def _sh_frame() -> pd.DataFrame:
    """沪市接口返回列，含信用交易日期。"""
    return pd.DataFrame(
        {
            "信用交易日期": [DATE, DATE],
            "标的证券代码": ["600030", "600519"],
            "标的证券简称": ["中信证券", "贵州茅台"],
            "融资余额": [100.0, 200.0],
            "融资买入额": [10.0, 20.0],
            "融资偿还额": [5.0, 8.0],
            "融券余量": [1.0, 2.0],
            "融券卖出量": [0.0, 1.0],
            "融券偿还量": [0.0, 0.0],
        }
    )


def _sz_frame() -> pd.DataFrame:
    """深市接口返回列：**没有**交易日期字段。"""
    return pd.DataFrame(
        {
            "证券代码": ["000001", "300750"],
            "证券简称": ["平安银行", "宁德时代"],
            "融资买入额": [30.0, 40.0],
            "融资余额": [300.0, 400.0],
            "融券卖出量": [0.0, 0.0],
            "融券余量": [0.0, 3.0],
            "融券余额": [0.0, 5.0],
            "融资融券余额": [300.0, 405.0],
        }
    )


def _fake_akshare() -> types.ModuleType:
    module = types.ModuleType("akshare")
    module.stock_margin_detail_sse = lambda date=None: _sh_frame()
    module.stock_margin_detail_szse = lambda date=None: _sz_frame()
    return module


def _fetch(tmp_path) -> pd.DataFrame:
    collector = MarginTradeCollector(cache_dir=tmp_path)
    return asyncio.run(collector.fetch_detail(DATE))


def test_both_markets_carry_trade_date(tmp_path, monkeypatch):
    # 深市接口本来没有日期列，采集器必须自己补上，否则整边都是 NaN
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare())
    frame = _fetch(tmp_path)

    assert set(frame["market"]) == {"sh", "sz"}
    assert frame["trade_date"].notna().all()
    assert (frame["trade_date"] == DATE).all()


def test_market_rows_are_both_present(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare())
    frame = _fetch(tmp_path)

    assert len(frame) == 4
    assert frame[frame["market"] == "sh"]["code"].tolist() == ["600030", "600519"]
    assert frame[frame["market"] == "sz"]["code"].tolist() == ["000001", "300750"]


def test_codes_are_zero_padded_to_six_digits(tmp_path, monkeypatch):
    # 深市代码 000001 被读成整型时不能退化成 1
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare())
    frame = _fetch(tmp_path)
    assert all(len(code) == 6 for code in frame["code"])
    assert "000001" in set(frame["code"])


def test_empty_sz_side_still_returns_sh(tmp_path, monkeypatch):
    # 单边失败不该让整次采集无结果
    module = types.ModuleType("akshare")
    module.stock_margin_detail_sse = lambda date=None: _sh_frame()

    def boom(date=None):
        raise RuntimeError("szse down")

    module.stock_margin_detail_szse = boom
    monkeypatch.setitem(sys.modules, "akshare", module)

    frame = _fetch(tmp_path)
    assert set(frame["market"]) == {"sh"}
    assert frame["trade_date"].notna().all()
