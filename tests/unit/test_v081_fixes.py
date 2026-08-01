"""v0.8.1 新增修复的针对性测试：持仓价格持久化 + 权重保护 + 腾讯基本面源。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from gugu.analysis.strategy_pool import MAX_WEIGHT, MIN_WEIGHT, StrategyPool


# ========== P0-1: LivePositionTracker.persist_prices ==========
def test_persist_prices_writes_real_prices(tmp_path, monkeypatch):
    """persist_prices 应将内存实时价写回 live_positions.json。"""
    from gugu.execution.track_live import LivePositionTracker

    state_file = tmp_path / "live_positions.json"
    initial = {
        "mode": "track_live",
        "initial_capital": 100000.0,
        "cash": 50000.0,
        "positions": {
            "510500": {
                "symbol": "510500",
                "name": "中证500ETF",
                "quantity": 400,
                "available": 400,
                "avg_cost": 8.715,
                "current_price": 8.90,  # 陈旧手动价
                "stop_loss_price": 7.94,
                "prev_close": 0.0,
                "is_st": False,
                "is_suspended": False,
                "lowest_price": 8.90,
            }
        },
        "daily_start_value": 100000.0,
        "updated_at": "2026-07-31T09:30:00",
    }
    state_file.write_text(json.dumps(initial), encoding="utf-8")

    tracker = LivePositionTracker(state_file=state_file)
    # 模拟实时行情刷新（真实价 7.50）
    tracker.update_price("510500", 7.50)
    assert tracker.get_portfolio()["510500"].current_price == pytest.approx(7.50)

    # 持久化应把实时价写回文件
    ok = tracker.persist_prices()
    assert ok is True
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["positions"]["510500"]["current_price"] == pytest.approx(7.50)
    # 用户手动维护字段不受影响
    assert saved["positions"]["510500"]["avg_cost"] == pytest.approx(8.715)
    assert saved["positions"]["510500"]["quantity"] == 400


def test_persist_prices_skips_all_zero(tmp_path):
    """所有现价为 0 时不持久化（防止用异常值覆盖文件）。"""
    from gugu.execution.track_live import LivePositionTracker

    state_file = tmp_path / "live_positions.json"
    initial = {
        "mode": "track_live",
        "initial_capital": 100000.0,
        "cash": 50000.0,
        "positions": {
            "510500": {
                "symbol": "510500",
                "name": "中证500ETF",
                "quantity": 400,
                "available": 400,
                "avg_cost": 8.715,
                "current_price": 8.90,
                "stop_loss_price": 7.94,
                "prev_close": 0.0,
                "is_st": False,
                "is_suspended": False,
                "lowest_price": 8.90,
            }
        },
        "daily_start_value": 100000.0,
        "updated_at": "2026-07-31T09:30:00",
    }
    state_file.write_text(json.dumps(initial), encoding="utf-8")
    tracker = LivePositionTracker(state_file=state_file)
    # 模拟异常：现价被清零
    tracker.update_price("510500", 0.0)
    ok = tracker.persist_prices()
    assert ok is False
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    # 文件保持原值不被覆盖
    assert saved["positions"]["510500"]["current_price"] == pytest.approx(8.90)


# ========== P1-3: 权重保护 ==========
@pytest.fixture
def mock_strategy_config():
    cfg = mock.MagicMock()
    cfg.enabled = ["box_breakout", "double_yin", "rsi_reversal"]
    cfg.weights = {
        "box_breakout": 0.40,
        "double_yin": 0.35,
        "rsi_reversal": 0.25,
    }
    cfg.verified_strategies = []
    cfg.experimental_strategies = []
    cfg.experimental_weights = {}
    return cfg


def test_weight_never_exceeds_max(mock_strategy_config, tmp_path, monkeypatch):
    """单策略权重不得超过 MAX_WEIGHT（防止塌缩到 94%）。"""
    monkeypatch.setattr(
        "gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", tmp_path / "w.json"
    )
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool = StrategyPool()
        # 反复高胜率 boost，权重应被 MAX_WEIGHT 钳制
        for _ in range(50):
            pool.update_performance("box_breakout", 0.05)
    w = pool.get_weights()["box_breakout"]
    assert w <= MAX_WEIGHT + 1e-6
    assert w <= 0.40 + 1e-6


def test_weight_never_below_min(mock_strategy_config, tmp_path, monkeypatch):
    """单策略权重不得低于 MIN_WEIGHT（防止被完全忽略）。"""
    monkeypatch.setattr(
        "gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", tmp_path / "w.json"
    )
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool = StrategyPool()
        # 反复亏损 + 连续亏损降级
        for _ in range(50):
            pool.update_performance("box_breakout", -0.05)
    w = pool.get_weights()["box_breakout"]
    assert w >= MIN_WEIGHT - 1e-6


# ========== P2-2: 腾讯 PE/PB 源 ==========
def test_tencent_pe_pb_skip_etf():
    """ETF 代码应跳过 PE/PB 获取（无估值概念）。"""
    from gugu.filters.fundamental import FundamentalFilter

    f = FundamentalFilter()
    pe, pb = f._fetch_pe_pb_tencent("510500")
    assert pe is None
    assert pb is None


def test_tencent_pe_pb_success():
    """腾讯源应能返回真实 PE/PB（贵州茅台，非交易时段可能为空不阻塞）。"""
    from gugu.filters.fundamental import FundamentalFilter

    f = FundamentalFilter()
    pe, pb = f._fetch_pe_pb_tencent("600519")
    # 非交易时段腾讯接口可能返回 None，此时跳过不阻塞测试
    if pe is None and pb is None:
        import warnings

        warnings.warn("腾讯 PE/PB 接口在非交易时段返回 None，跳过断言")
        return
    # 正常情形：茅台 PE 应 > 0（正常 15-40 区间），PB 应 > 1
    assert pe is not None and pe > 0
    assert pb is not None and pb > 1


def test_fetch_pe_pb_tencent_first(mock_strategy_config, tmp_path):
    """_fetch_pe_pb 应优先使用腾讯源（稳定源优先）。"""
    from gugu.filters.fundamental import FundamentalFilter

    f = FundamentalFilter()
    with mock.patch.object(f, "_fetch_pe_pb_tencent", return_value=(20.0, 7.0)) as m_tx:
        with mock.patch.object(
            f, "_fetch_pe_pb_akshare", return_value=(None, None)
        ) as m_ak:
            pe, pb = f._fetch_pe_pb("600519")
    m_tx.assert_called_once()
    m_ak.assert_not_called()
    assert pe == pytest.approx(20.0)
    assert pb == pytest.approx(7.0)
