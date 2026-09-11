"""NewsFetcher 单测：akshare 接口用法与 pandas 字符串后端防护。

回归背景（2026-09-11 告警评估）：
1. `ak.stock_notice_report(symbol=<股票代码>)` 必然 KeyError
   （symbol 是「公告类型」），公告源 100% 失效 → 改为按日期全市场看板 + 代码过滤。
2. pandas 3.x arrow 字符串后端 + akshare 内部 `re` 语义正则 → ArrowInvalid，
   新闻源 100% 失效 → 调用期间临时切回 python 字符串后端。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import pytest

from gugu.analysis.sentiment.fetcher import (
    NewsFetcher,
    _python_string_storage,
)
from gugu.utils.calendar import is_trading_day


class FakeAk:
    """最小 akshare 替身：记录调用参数，返回可控 DataFrame。"""

    def __init__(self, notice_df: pd.DataFrame | None = None, news_df: pd.DataFrame | None = None):
        self.notice_df = notice_df if notice_df is not None else _notice_df()
        self.news_df = news_df
        self.notice_calls: list[tuple[str, str]] = []
        self.news_calls: list[str] = []

    def stock_notice_report(self, symbol: str, date: str) -> pd.DataFrame:
        self.notice_calls.append((symbol, date))
        return self.notice_df

    def stock_news_em(self, symbol: str) -> pd.DataFrame:
        self.news_calls.append(symbol)
        if self.news_df is None:
            return pd.DataFrame()
        return self.news_df


def _notice_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "代码": ["600519", "600519", "000001"],
            "名称": ["贵州茅台", "贵州茅台", "平安银行"],
            "公告标题": ["贵州茅台:2026年中期分红公告", "贵州茅台:监事会决议", "平安银行:季报"],
            "公告类型": ["分红", "其他", "财务报告"],
            "公告日期": ["2026-09-10", "2026-09-10", "2026-09-10"],
            "网址": ["http://x/1", "http://x/2", "http://x/3"],
        }
    )


def _use_ak(fetcher: NewsFetcher, fake: Any) -> Any:
    """注入 akshare 替身（_ak 声明类型为 module，测试忽略该类型约束）。"""
    fetcher._ak = fake  # type: ignore[assignment]
    return fake


@pytest.fixture()
def fetcher(tmp_path) -> NewsFetcher:
    return NewsFetcher(cache_dir=tmp_path)


# ── pandas 字符串后端防护 ────────────────────────────


def test_python_string_storage_sets_and_restores() -> None:
    prev = pd.options.mode.string_storage
    with _python_string_storage():
        assert pd.options.mode.string_storage == "python"
    assert pd.options.mode.string_storage == prev


def test_python_string_storage_restores_on_exception() -> None:
    prev = pd.options.mode.string_storage
    with pytest.raises(RuntimeError), _python_string_storage():
        raise RuntimeError("boom")
    assert pd.options.mode.string_storage == prev


# ── 新闻源 ───────────────────────────────────────────


def test_fetch_em_news_parses_rows(fetcher: NewsFetcher) -> None:
    _use_ak(
        fetcher,
        FakeAk(
            news_df=pd.DataFrame(
                {
                    "新闻标题": ["标题A", ""],
                    "新闻内容": ["内容A", "空标题应跳过"],
                    "文章来源": ["东财", "东财"],
                    "发布时间": ["2026-09-10 09:00:00", "2026-09-10 10:00:00"],
                    "新闻链接": ["http://n/1", "http://n/2"],
                }
            )
        ),
    )
    items = fetcher._fetch_em_news("600519", max_news=10)
    assert [i["title"] for i in items] == ["标题A"]
    assert items[0]["type"] == "news"


def test_fetch_stock_news_returns_news_without_notices_fallback(fetcher: NewsFetcher) -> None:
    ak = _use_ak(
        fetcher,
        FakeAk(
            news_df=pd.DataFrame(
                {
                    "新闻标题": [f"标题{i}" for i in range(10)],
                    "新闻内容": ["内容"] * 10,
                    "文章来源": ["东财"] * 10,
                    "发布时间": ["2026-09-10 09:00:00"] * 10,
                    "新闻链接": ["http://n"] * 10,
                }
            )
        ),
    )
    items = fetcher.fetch_stock_news("600519", max_news=5)
    assert len(items) == 5
    assert ak.notice_calls == []  # 新闻已足够 → 不触发公告拉取


# ── 公告源（回归：symbol 必须是公告类型 + date）───────


def test_fetch_notices_uses_all_symbol_and_date(fetcher: NewsFetcher) -> None:
    ak = _use_ak(fetcher, FakeAk())
    items = fetcher._fetch_notices("600519", max_news=5)

    assert [i["title"] for i in items] == ["贵州茅台:2026年中期分红公告", "贵州茅台:监事会决议"]
    assert all(i["type"] == "notice" and i["source"] == "公告" for i in items)
    assert ak.notice_calls, "公告接口未被调用"
    symbol, day = ak.notice_calls[0]
    assert symbol == "全部", "symbol 必须是公告类型而非股票代码（否则 KeyError）"
    assert len(day) == 8 and day.isdigit()
    assert day <= date.today().strftime("%Y%m%d")


def test_notice_board_cached_per_day(fetcher: NewsFetcher) -> None:
    ak = _use_ak(fetcher, FakeAk())
    fetcher._fetch_notices("600519", max_news=5)
    calls_after_first = len(ak.notice_calls)
    assert calls_after_first > 0
    fetcher._fetch_notices("000001", max_news=5)
    assert len(ak.notice_calls) == calls_after_first, "同一进程同一天不应重复拉取公告看板"


def test_notice_board_skips_non_trading_days(fetcher: NewsFetcher) -> None:
    ak = _use_ak(fetcher, FakeAk())
    fetcher._load_notice_board()
    assert ak.notice_calls, "未调用公告接口"
    for _, day in ak.notice_calls:
        d = date(int(day[:4]), int(day[4:6]), int(day[6:]))
        assert is_trading_day(d), f"不应请求非交易日 {day}"
        assert d <= date.today()
        assert date.today() - d <= timedelta(days=6)


def test_fetch_notices_empty_when_no_akshare(fetcher: NewsFetcher) -> None:
    fetcher._get_ak = lambda: None  # type: ignore[method-assign]
    assert fetcher._load_notice_board() == {}


def test_fetch_notices_api_error_degrades_to_empty(fetcher: NewsFetcher) -> None:
    class BoomAk(FakeAk):
        def stock_notice_report(self, symbol: str, date: str) -> pd.DataFrame:
            raise RuntimeError("upstream down")

    _use_ak(fetcher, BoomAk())
    assert fetcher._load_notice_board() == {}


def test_fetch_notices_empty_when_no_board_for_code(fetcher: NewsFetcher) -> None:
    _use_ak(fetcher, FakeAk())
    assert fetcher._fetch_notices("999999", max_news=5) == []


@pytest.mark.parametrize("code", ["600519", "sz600519", "sh600519", "600519.SH"])
def test_fetch_notices_normalizes_prefixed_code(fetcher: NewsFetcher, code: str) -> None:
    _use_ak(fetcher, FakeAk())
    items = fetcher._fetch_notices(code, max_news=5)
    assert [i["title"] for i in items] == ["贵州茅台:2026年中期分红公告", "贵州茅台:监事会决议"]


def test_merge_notice_board_dedups_by_title() -> None:
    target: dict[str, list[dict[str, Any]]] = {"600519": [{"title": "A"}]}
    extra = {"600519": [{"title": "A"}, {"title": "B"}], "000001": [{"title": "C"}]}
    NewsFetcher._merge_notice_board(target, extra)
    assert [i["title"] for i in target["600519"]] == ["A", "B"]
    assert [i["title"] for i in target["000001"]] == ["C"]
