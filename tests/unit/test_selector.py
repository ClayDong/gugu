"""StockSelector 自动选股模块单元测试。

覆盖：
- _filter_basic() 基础过滤规则（价格 > 0、成交额 > 1000 万、非 ST）
- select() 全市场快照获取、按 main_pct 排序、max_candidates 限制
- 涨跌停过滤（调用 risk.is_tradable）
- 异常路径（快照为空、单股处理失败）

Mock 策略：仅 mock DataManager 的网络 IO（fetch_stock_realtime /
fetch_stock_history），业务逻辑（_filter_basic、SignalRouter.route、
RiskManager.is_tradable）均使用真实实现。为便于在测试数据上稳定产生
信号，将 SignalRouter 配置为单策略 + any 融合（仍为真实路由，非 mock）。
"""
from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from gugu.engine.signal_router import SignalRouter
from gugu.selector.stock_selector import StockSelector
from gugu.strategies.trend import DualMAStrategy


def _mock_ranker() -> mock.AsyncMock:
    """创建一个模拟 StockRanker，返回空排名结果。

    避免单元测试中触发真实网络 IO（DataManager 获取行情 / 资金流 / 基本面），
    保持 selector 测试聚焦在选股流程本身（过滤、排序、限流）。
    """
    ranker = mock.AsyncMock()
    ranker.rank.return_value = []  # 空排名 → 跳过评分合并，直接返回原始信号
    return ranker


def _golden_cross_df() -> pd.DataFrame:
    """构造 60 日行情，最后一日触发 dual_ma 金叉买入信号。

    前 59 日收盘价恒为 10.0，第 60 日跳涨至 20.0，
    使 5 日均线上穿 20 日均线，产生买入信号。
    前一日收盘价（prev_close）= 10.0，用于涨跌停判断。
    """
    n = 60
    close = [10.0] * (n - 1) + [20.0]
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": close,
            "high": [c + 1.0 for c in close],
            "low": [c - 1.0 for c in close],
            "close": close,
            "volume": [1_000_000] * n,
            "amount": [c * 1_000_000 for c in close],
        }
    )


def _make_selector(
    dm: object,
    *,
    top_n: int = 50,
    max_candidates: int = 10,
) -> StockSelector:
    """构造 StockSelector，注入 mock DataManager。

    使用真实 SignalRouter（单策略 + any 融合）和真实 RiskManager，
    仅替换外部 IO（DataManager）。
    """
    selector = StockSelector(
        data_manager=dm,  # type: ignore[arg-type]
        top_n=top_n,
        max_candidates=max_candidates,
    )
    # 使用真实路由器，配置为单策略 + any 融合，便于在测试数据上稳定产生信号
    selector._router = SignalRouter(
        strategies=[DualMAStrategy()],
        fusion_rule="any",
        min_confidence=0.0,
    )
    # 注入 mock ranker，避免真实网络 IO（行情/资金流/基本面请求）
    selector._ranker = _mock_ranker()
    return selector


# ========== _filter_basic 过滤规则 ==========


def test_filter_basic_removes_st_stocks() -> None:
    """ST 股票（名称含 ST，不区分大小写）应被过滤。"""
    df = pd.DataFrame(
        {
            "symbol": ["000001", "000002", "000003"],
            "name": ["正常股", "ST退市", "另一个st"],
            "price": [10.0, 5.0, 8.0],
            "amount": [20_000_000, 20_000_000, 20_000_000],
        }
    )
    result = StockSelector._filter_basic(df)
    assert len(result) == 1
    assert result.iloc[0]["symbol"] == "000001"


def test_filter_basic_removes_zero_price() -> None:
    """价格为 0 的股票应被过滤。"""
    df = pd.DataFrame(
        {
            "symbol": ["000001", "000002"],
            "name": ["正常股", "零价格"],
            "price": [10.0, 0.0],
            "amount": [20_000_000, 20_000_000],
        }
    )
    result = StockSelector._filter_basic(df)
    assert len(result) == 1
    assert result.iloc[0]["price"] == 10.0


def test_filter_basic_removes_low_amount() -> None:
    """成交额 <= 1000 万的股票应被过滤（严格大于 1000 万才保留）。"""
    df = pd.DataFrame(
        {
            "symbol": ["000001", "000002", "000003"],
            "name": ["高成交", "刚好千万", "低成交"],
            "price": [10.0, 10.0, 10.0],
            "amount": [20_000_000, 10_000_000, 5_000_000],
        }
    )
    result = StockSelector._filter_basic(df)
    # amount > 10_000_000（严格大于），刚好 1000 万也被过滤
    assert len(result) == 1
    assert result.iloc[0]["symbol"] == "000001"


# ========== select() 选股流程 ==========


@pytest.mark.asyncio
async def test_select_returns_sorted_candidates() -> None:
    """select() 应按 main_pct 降序获取候选股历史数据并返回信号。"""
    snapshot = pd.DataFrame(
        {
            "symbol": ["000001", "000002", "000003"],
            "name": ["股票A", "股票B", "股票C"],
            "price": [10.0, 10.0, 10.0],
            "amount": [20_000_000, 20_000_000, 20_000_000],
            "main_pct": [0.05, 0.15, 0.10],
        }
    )
    dm = mock.MagicMock()
    dm.fetch_stock_realtime = mock.AsyncMock(return_value=snapshot)
    dm.fetch_stock_history = mock.AsyncMock(return_value=_golden_cross_df())

    selector = _make_selector(dm)
    signals = await selector.select()

    # 三只股票均产生买入信号
    assert len(signals) == 3
    # 验证 fetch_stock_history 调用顺序按 main_pct 降序：
    # 000002(0.15) -> 000003(0.10) -> 000001(0.05)
    call_symbols = [call.args[0] for call in dm.fetch_stock_history.call_args_list]
    assert call_symbols == ["000002", "000003", "000001"]


@pytest.mark.asyncio
async def test_select_respects_max_candidates() -> None:
    """select() 返回的候选信号数不应超过 max_candidates。"""
    snapshot = pd.DataFrame(
        {
            "symbol": ["000001", "000002", "000003", "000004", "000005"],
            "name": ["股票A", "股票B", "股票C", "股票D", "股票E"],
            "price": [10.0] * 5,
            "amount": [20_000_000] * 5,
            "main_pct": [0.01, 0.02, 0.03, 0.04, 0.05],
        }
    )
    dm = mock.MagicMock()
    dm.fetch_stock_realtime = mock.AsyncMock(return_value=snapshot)
    dm.fetch_stock_history = mock.AsyncMock(return_value=_golden_cross_df())

    selector = _make_selector(dm, max_candidates=2)
    signals = await selector.select()

    # 5 只股票均能产生信号，但 max_candidates=2 限制只返回 2 个
    assert len(signals) == 2


@pytest.mark.asyncio
async def test_select_handles_empty_snapshot() -> None:
    """全市场快照为空时，select() 应返回空列表且不获取历史数据。"""
    dm = mock.MagicMock()
    dm.fetch_stock_realtime = mock.AsyncMock(return_value=pd.DataFrame())

    selector = _make_selector(dm)
    signals = await selector.select()

    assert signals == []
    dm.fetch_stock_history.assert_not_called()


@pytest.mark.asyncio
async def test_select_handles_single_stock_failure() -> None:
    """单只股票历史数据获取失败时，select() 应跳过并继续处理其他股票。"""
    snapshot = pd.DataFrame(
        {
            "symbol": ["000001", "000002"],
            "name": ["正常股", "异常股"],
            "price": [10.0, 10.0],
            "amount": [20_000_000, 20_000_000],
            "main_pct": [0.10, 0.20],
        }
    )
    golden = _golden_cross_df()

    def _history_side_effect(symbol: str, days: int = 60) -> pd.DataFrame:
        if symbol == "000002":
            raise RuntimeError("网络错误")
        return golden

    dm = mock.MagicMock()
    dm.fetch_stock_realtime = mock.AsyncMock(return_value=snapshot)
    dm.fetch_stock_history = mock.AsyncMock(side_effect=_history_side_effect)

    selector = _make_selector(dm)
    signals = await selector.select()

    # 异常股 000002 被跳过，正常股 000001 产生信号
    assert len(signals) == 1
    assert signals[0]["symbol"] == "000001"


@pytest.mark.asyncio
async def test_select_filters_limit_up_stocks() -> None:
    """涨停股票应通过 risk.is_tradable 过滤，不进入候选列表。

    600519（主板 ±10%）快照价 11.0 = 前收 10.0 * 1.1，处于涨停价，
    is_tradable 返回 False，应被跳过。
    """
    snapshot = pd.DataFrame(
        {
            "symbol": ["000001", "600519"],
            "name": ["正常股", "涨停股"],
            "price": [10.0, 11.0],
            "amount": [20_000_000, 20_000_000],
            "main_pct": [0.10, 0.20],
        }
    )
    dm = mock.MagicMock()
    dm.fetch_stock_realtime = mock.AsyncMock(return_value=snapshot)
    dm.fetch_stock_history = mock.AsyncMock(return_value=_golden_cross_df())

    selector = _make_selector(dm)
    signals = await selector.select()

    # 涨停股 600519 被过滤，仅正常股 000001 入选
    assert len(signals) == 1
    assert signals[0]["symbol"] == "000001"
