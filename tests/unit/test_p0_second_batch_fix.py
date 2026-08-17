"""第二批 P0/P1 修复的针对性测试（2026-08-05 审计后）。

覆盖：
1. P0-1: 120d 缓存陈旧修复——盘中/盘后/非交易日的新鲜度判定
2. P0-2: router 全链路日志——votes 空/当日无信号可观测
3. P1-1: 模拟盘 updated_at 字段 + GUGU_TEST_MODE 状态文件隔离
4. P1-2: box_breakout 日志口径（历史窗口 vs 当日信号）
5. P1-3: auto_tune OR 判定（超额为负即收紧，不再被 AND 卡死）
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ========== P0-1: 缓存新鲜度 ==========


def _make_df(last_date: str, n: int = 40) -> pd.DataFrame:
    dates = pd.date_range(end=last_date, periods=n, freq="D")
    # 带波动的价格序列（PlausiblePriceRule 要求 60 天波动 >0.5%，否则判为数据封锁）
    base = 100.0
    close = [base]
    for i in range(1, n):
        close.append(close[-1] * (1 + (0.01 if i % 3 == 0 else -0.008)))
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [1_000_000] * n,
            "amount": [c * 1_000_000 for c in close],
        }
    )


def test_cache_intraday_requires_today(tmp_path: Path):
    """盘中：昨日 bar 命中（源无当日 bar，重拉也白拉），两天前过期。"""
    from gugu.data.cache import ParquetPersistentCache

    cache = ParquetPersistentCache(cache_dir=tmp_path)
    df = _make_df("2026-08-04")  # 昨日数据
    cache.set("600519", 120, df, "none")

    # 模拟 2026-08-05 10:00（交易日盘中）
    now = datetime(2026, 8, 5, 10, 0, 0)
    with patch("gugu.utils.calendar.is_trading_day", return_value=True):
        result = cache.get("600519", 120, "none", now=now)
    assert result is not None, "盘中允许昨日 bar（K线源无当日，重拉也拿不到当日）"

    # 两天前的数据 → 必须过期
    cache2 = ParquetPersistentCache(cache_dir=tmp_path)
    df2 = _make_df("2026-08-03")
    cache2.set("600519", 120, df2, "none")
    with patch("gugu.utils.calendar.is_trading_day", return_value=True):
        result2 = cache2.get("600519", 120, "none", now=now)
    assert result2 is None, "盘中两天前 bar 必须过期"
    assert not (tmp_path / "600519_120d_none.parquet").exists(), "过期缓存应被删除"


def test_cache_intraday_hits_today_bar(tmp_path: Path):
    """盘中：含当日 bar 的缓存命中。"""
    from gugu.data.cache import ParquetPersistentCache

    cache = ParquetPersistentCache(cache_dir=tmp_path)
    df = _make_df("2026-08-05")
    cache.set("600519", 120, df, "none")

    now = datetime(2026, 8, 5, 10, 0, 0)
    with patch("gugu.utils.calendar.is_trading_day", return_value=True):
        result = cache.get("600519", 120, "none", now=now)
    assert result is not None, "含当日 bar 应命中"


def test_cache_after_close_requires_today(tmp_path: Path):
    """盘后（16:57）：昨日 bar 必须过期（收盘验证/绩效用错数据根因）。"""
    from gugu.data.cache import ParquetPersistentCache

    cache = ParquetPersistentCache(cache_dir=tmp_path)
    df = _make_df("2026-08-04")  # 盘中写入的旧缓存
    cache.set("600519", 120, df, "none")

    now = datetime(2026, 8, 5, 16, 57, 0)  # 盘后
    with patch("gugu.utils.calendar.is_trading_day", return_value=True):
        result = cache.get("600519", 120, "none", now=now)
    assert result is None, "盘后必须含当日 bar（原实现 7 天容忍导致 16:57 仍命中 8/4）"


def test_cache_weekend_tolerates_stale(tmp_path: Path):
    """非交易日：容忍 7 天老化（长假场景）。"""
    from gugu.data.cache import ParquetPersistentCache

    cache = ParquetPersistentCache(cache_dir=tmp_path)
    df = _make_df("2026-08-03")
    cache.set("600519", 120, df, "none")

    now = datetime(2026, 8, 8, 12, 0, 0)  # 周六
    with patch("gugu.utils.calendar.is_trading_day", return_value=False):
        result = cache.get("600519", 120, "none", now=now)
    assert result is not None, "非交易日容忍陈旧"


# ========== P0-2: router 可观测性 ==========


def test_router_logs_all_strategies_no_signal():
    """所有策略当日无信号 → 明确日志（区别于被过滤）。"""
    from gugu.engine.signal_router import SignalRouter
    from loguru import logger as loguru_logger

    df = _make_df("2026-08-05")

    # 构造两个永远返回 signal=0 的策略
    class NoSignalStrategy:
        name = "no_signal_a"
        category = "breakout"

        def generate_signals(self, df):
            out = df.copy()
            out["signal"] = 0
            out["confidence"] = 0.0
            return out

    # 用 loguru sink 捕获日志（gugu 日志系统是 loguru，caplog 捕获不到）
    import io

    sink = io.StringIO()
    sink_id = loguru_logger.add(sink, level="INFO")
    try:
        router = SignalRouter([NoSignalStrategy(), NoSignalStrategy()], fusion_rule="any")
        result = router.route(df, "600519", name="贵州茅台")
    finally:
        loguru_logger.remove(sink_id)

    assert result is None
    assert "所有策略无信号" in sink.getvalue(), "应输出'所有策略无信号'日志"


def test_router_logs_votes_when_fusion_hold():
    """策略有信号但加权融合无方向 → 明确日志（区别于策略未触发）。"""
    from gugu.engine.signal_router import SignalRouter
    from loguru import logger as loguru_logger

    df = _make_df("2026-08-05")

    class BuyStrategy:
        name = "buy_strat"
        category = "breakout"

        def generate_signals(self, df):
            out = df.copy()
            out["signal"] = 0
            out["signal"] = out["signal"].astype(int)
            out.iloc[-1, out.columns.get_loc("signal")] = 1
            out["confidence"] = 0.1  # 低置信度，融合内部门槛不过
            return out

    # 用 weighted 融合 + mock pool 返回 hold
    import io

    sink = io.StringIO()
    sink_id = loguru_logger.add(sink, level="INFO")
    try:
        with patch("gugu.engine.signal_router.StrategyPool") as MockPool:
            pool = MagicMock()
            pool.weighted_fusion.return_value = {"direction": "hold", "confidence": 0.1}
            MockPool.return_value = pool
            router = SignalRouter([BuyStrategy()], fusion_rule="weighted", strategy_pool=pool)

            result = router.route(df, "600519", name="贵州茅台")
    finally:
        loguru_logger.remove(sink_id)

    assert result is None
    assert "加权融合无方向" in sink.getvalue(), "应输出'加权融合无方向'日志"


# ========== P1-1: 模拟盘完整性 ==========


def test_paper_state_has_updated_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """_save_state 写入 updated_at 字段。"""
    import gugu.execution.paper as paper_mod
    from gugu.execution.paper import PaperBroker

    monkeypatch.delenv("GUGU_TEST_MODE", raising=False)
    monkeypatch.setattr(paper_mod, "STATE_FILE", tmp_path / "paper_broker_state.json")
    broker = PaperBroker()
    broker._save_state()
    data = json.loads((tmp_path / "paper_broker_state.json").read_text())
    assert "updated_at" in data, "状态文件必须含 updated_at"


def test_paper_test_mode_isolates_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GUGU_TEST_MODE 下状态文件隔离到临时目录（防污染生产账本）。"""
    import gugu.execution.paper as paper_mod
    from gugu.execution.paper import PaperBroker

    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    prod_file = tmp_path / "paper_broker_state.json"
    monkeypatch.setattr(paper_mod, "STATE_FILE", prod_file)
    prod_file.write_text(json.dumps({"cash": 1000000, "positions": {}, "trades": []}))

    broker = PaperBroker()
    assert broker._state_file != prod_file, "测试模式必须隔离状态文件"
    assert not prod_file.read_text().startswith('{"cash": 0'), "生产文件不应被测试改写"


# ========== P1-2: 策略日志口径 ==========


def test_box_breakout_log_distinguishes_daily_signal():
    """box_breakout 日志标注[历史窗口]+当日最新信号。"""
    from gugu.strategies.breakout import BoxBreakoutStrategy
    from loguru import logger as loguru_logger

    strategy = BoxBreakoutStrategy()
    # 构造 120 天数据：历史某天大幅上涨（突破上沿），最后一天回落（无信号）
    n = 120
    dates = pd.date_range(end="2026-08-05", periods=n, freq="D")
    close = [100.0] * n
    # 第 60 天突然大涨（历史信号）
    close[60] = 130.0
    for i in range(61, n):
        close[i] = close[i - 1] * (1 + (0.002 if i % 2 else -0.001))
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [2_000_000] * n,
            "amount": [c * 2_000_000 for c in close],
        }
    )

    import io

    sink = io.StringIO()
    sink_id = loguru_logger.add(sink, level="INFO")
    try:
        strategy.generate_signals(df)
    finally:
        loguru_logger.remove(sink_id)
    log_text = sink.getvalue()
    assert "120天窗口" in log_text, "日志必须标注统计窗口"
    assert "当日" in log_text and "最新信号" in log_text, "日志必须区分当日信号"


# ========== P1-3: auto_tune OR 判定 ==========


def test_auto_tune_tightens_when_excess_negative_but_winrate_ok():
    """超额显著为负但胜率略高 → 必须收紧（OR 判定修复）。"""
    from gugu.screener.auto_tune import TuneMetrics, generate_adjustments
    from gugu.screener.params import ScreenerParams

    params = ScreenerParams()
    metrics = TuneMetrics(
        avg_open_excess=-0.35,  # 显著为负（< -0.3）
        win_rate=0.28,  # 略高于 25% 阈值
        stop_loss_rate=0.10,
        avg_drawdown=-1.5,
        sample_size=29,
    )
    adjustments = generate_adjustments(metrics, params)
    assert adjustments, "超额为负即应收紧，不能被胜率 AND 卡死"
    assert "max_vol_ratio" in adjustments and adjustments["max_vol_ratio"] < 0


def test_auto_tune_holds_when_neutral():
    """超额中性 + 胜率中性 → 不调整。"""
    from gugu.screener.auto_tune import TuneMetrics, generate_adjustments
    from gugu.screener.params import ScreenerParams

    params = ScreenerParams()
    metrics = TuneMetrics(
        avg_open_excess=0.0,
        win_rate=0.30,
        stop_loss_rate=0.10,
        avg_drawdown=-1.0,
        sample_size=20,
    )
    adjustments = generate_adjustments(metrics, params)
    assert adjustments == {}, "中性区间不调整"
