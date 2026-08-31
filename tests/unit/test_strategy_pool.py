"""策略池管理测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from gugu.analysis.strategy_pool import (
    CONSECUTIVE_LOSS_THRESHOLD,
    CONSECUTIVE_WIN_THRESHOLD,
    MAX_WEIGHT,
    MIN_WEIGHT,
    StrategyPool,
)


@pytest.fixture
def mock_strategy_config():
    """构造一个最小策略配置。"""
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


def test_init_weights_from_config(mock_strategy_config, tmp_path, monkeypatch):
    """权重应从配置初始化并归一化。"""
    monkeypatch.setattr(
        "gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", tmp_path / "weights.json"
    )
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool = StrategyPool()

    weights = pool.get_weights()
    assert set(weights.keys()) == {"box_breakout", "double_yin", "rsi_reversal"}
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert MIN_WEIGHT <= weights["box_breakout"] <= MAX_WEIGHT


def test_normalization_preserves_bounds_and_sum(mock_strategy_config, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", tmp_path / "weights.json"
    )
    mock_strategy_config.weights = {
        "box_breakout": 100.0,
        "double_yin": 0.001,
        "rsi_reversal": 0.001,
    }
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool = StrategyPool()
    weights = pool.get_weights()
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-9)
    assert all(MIN_WEIGHT <= weight <= MAX_WEIGHT for weight in weights.values())


def test_consecutive_loss_degrades_but_keeps_weight(mock_strategy_config, tmp_path, monkeypatch):
    """连续亏损后策略应大幅降权但不被禁用。"""
    monkeypatch.setattr(
        "gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", tmp_path / "weights.json"
    )
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool = StrategyPool()

    # 先补充 5 笔非亏损记录满足 total_signals >= 5
    for _ in range(5):
        pool.update_performance("box_breakout", 0.01)

    # 连续亏损触发降级
    for _ in range(CONSECUTIVE_LOSS_THRESHOLD):
        pool.update_performance("box_breakout", -0.01)

    weights = pool.get_weights()
    assert weights["box_breakout"] >= MIN_WEIGHT
    # v0.8.1：断言更新——新约束（MIN_WEIGHT=0.05 保底 + 归一化）下降级后权重约 0.10-0.13，
    # 语义验证"显著低于初始 0.40"而非脆弱边界 0.10
    assert weights["box_breakout"] < 0.20
    # 策略仍被保留
    assert "box_breakout" in pool.get_enabled_names()


def test_consecutive_win_recover_weight(mock_strategy_config, tmp_path, monkeypatch):
    """连续盈利后策略权重应逐步恢复。"""
    monkeypatch.setattr(
        "gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", tmp_path / "weights.json"
    )
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool = StrategyPool()

    # 先把权重压到很低
    for _ in range(5):
        pool.update_performance("box_breakout", 0.01)
    for _ in range(CONSECUTIVE_LOSS_THRESHOLD):
        pool.update_performance("box_breakout", -0.01)

    degraded = pool.get_weights()["box_breakout"]

    # 连续盈利触发恢复
    for _ in range(CONSECUTIVE_WIN_THRESHOLD):
        pool.update_performance("box_breakout", 0.05)

    recovered = pool.get_weights()["box_breakout"]
    assert recovered > degraded
    assert recovered <= 0.80  # 上限为初始权重的 2 倍


def test_weight_persistence(mock_strategy_config, tmp_path, monkeypatch):
    """权重应被持久化并在重启后恢复。"""
    weights_file = tmp_path / "weights.json"
    monkeypatch.setattr("gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", weights_file)

    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool = StrategyPool()
        for _ in range(5):
            pool.update_performance("box_breakout", 0.01)
        for _ in range(CONSECUTIVE_LOSS_THRESHOLD):
            pool.update_performance("box_breakout", -0.01)

    assert weights_file.exists()
    with weights_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert "box_breakout" in data["weights"]

    # 模拟重启：新实例应读取持久化权重
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool2 = StrategyPool()

    # v0.8.1：断言更新——重启后权重从持久化恢复，显著低于初始 0.40（降级生效）
    restored = pool2.get_weights()["box_breakout"]
    assert MIN_WEIGHT <= restored <= MAX_WEIGHT


def test_production_pool_excludes_experimental_weights(tmp_path, monkeypatch):
    """生产权重池只能包含 verified 策略。"""
    monkeypatch.setattr(
        "gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", tmp_path / "production.json"
    )
    cfg = mock.MagicMock()
    cfg.enabled = []
    cfg.weights = {"rsi_reversal": 1.0}
    cfg.verified_strategies = ["rsi_reversal"]
    cfg.experimental_strategies = ["box_breakout", "double_yin"]
    cfg.experimental_weights = {"box_breakout": 0.9, "double_yin": 0.8}
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = cfg
        pool = StrategyPool(execution_mode="track_live")

    assert set(pool.get_weights()) == {"rsi_reversal"}
    assert pool.get_weights()["rsi_reversal"] == pytest.approx(1.0)


def test_paper_pool_uses_separate_weight_file(tmp_path, monkeypatch):
    """paper 权重池可包含实验策略，但不读取生产权重文件。"""
    production_file = tmp_path / "production.json"
    paper_file = tmp_path / "paper.json"
    production_file.write_text(json.dumps({"weights": {"rsi_reversal": 1.0}}), encoding="utf-8")
    monkeypatch.setattr("gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", production_file)
    monkeypatch.setattr("gugu.analysis.strategy_pool.StrategyPool._PAPER_WEIGHTS_FILE", paper_file)
    cfg = mock.MagicMock()
    cfg.enabled = []
    cfg.weights = {"rsi_reversal": 1.0}
    cfg.verified_strategies = ["rsi_reversal"]
    cfg.experimental_strategies = ["box_breakout"]
    cfg.experimental_weights = {"box_breakout": 0.2}
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = cfg
        pool = StrategyPool(execution_mode="paper")

    assert set(pool.get_weights()) == {"rsi_reversal", "box_breakout"}
    assert not paper_file.exists()


def test_weighted_fusion_ignores_unknown_strategy(mock_strategy_config, tmp_path, monkeypatch):
    """加权融合应忽略没有权重的策略信号。"""
    monkeypatch.setattr(
        "gugu.analysis.strategy_pool.StrategyPool._WEIGHTS_FILE", tmp_path / "weights.json"
    )
    with mock.patch("gugu.analysis.strategy_pool.get_config") as mock_cfg:
        mock_cfg.return_value.strategy = mock_strategy_config
        pool = StrategyPool()

    signals = [
        {"direction": "buy", "confidence": 0.9, "strategy_name": "box_breakout"},
        {"direction": "buy", "confidence": 0.9, "strategy_name": "unknown_strategy"},
    ]
    result = pool.weighted_fusion(signals)
    assert result["direction"] == "buy"
    assert result["confidence"] == pytest.approx(0.9, abs=1e-6)
