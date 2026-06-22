"""WisdomAdvisor 单元测试。

验证 wisdom 真正参与决策：
1. 仓位调整（分层下注，首次仅试仓 20-30%）
2. 止损价预设
3. 入场过滤（低置信度降级）
"""
from __future__ import annotations

import pytest

from gugu.wisdom.advisor import (
    MAX_SINGLE_POSITION,
    STOP_LOSS_DEFAULT_PCT,
    WisdomAdvisor,
)


@pytest.fixture
def advisor() -> WisdomAdvisor:
    """构造一个加载了项目内 skill 的 WisdomAdvisor。"""
    return WisdomAdvisor()


def test_advisor_loads_skills(advisor: WisdomAdvisor) -> None:
    """应加载到 6+ 个 skill。"""
    assert len(advisor._skills) >= 6
    assert "stock-entry-decision" in advisor._skills
    assert "stock-stop-loss-decision" in advisor._skills
    assert "stock-position-sizing" in advisor._skills


def test_advise_buy_adjusts_position(advisor: WisdomAdvisor) -> None:
    """买入信号应调整仓位为试仓比例。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "confidence": 0.8,
        "suggested_position_ratio": 0.24,  # 原始 24%
    }
    result = advisor.advise(signal)

    # 仓位应被调整
    assert "wisdom_decision" in result
    decision = result["wisdom_decision"]
    assert "adjusted_position_ratio" in decision
    assert decision["position_strategy"] == "trial"

    # 调整后仓位应小于原始仓位（试仓）
    adjusted = decision["adjusted_position_ratio"]
    assert adjusted < 0.24
    assert adjusted <= MAX_SINGLE_POSITION


def test_advise_buy_sets_stop_loss(advisor: WisdomAdvisor) -> None:
    """买入信号应预设止损价。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "confidence": 0.8,
        "suggested_position_ratio": 0.24,
    }
    result = advisor.advise(signal)

    decision = result["wisdom_decision"]
    assert "stop_loss_price" in decision
    expected_stop = round(1500.0 * (1 - STOP_LOSS_DEFAULT_PCT), 2)
    assert decision["stop_loss_price"] == expected_stop
    assert result["stop_loss_price"] == expected_stop


def test_advise_buy_low_confidence_filtered(advisor: WisdomAdvisor) -> None:
    """低置信度买入信号应被入场过滤。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "confidence": 0.4,  # 低于阈值 0.6
        "suggested_position_ratio": 0.24,
    }
    result = advisor.advise(signal)

    decision = result["wisdom_decision"]
    assert decision["entry_filtered"] is True
    assert result["wisdom_filtered"] is True
    assert "filter_reason" in decision


def test_advise_buy_high_confidence_not_filtered(advisor: WisdomAdvisor) -> None:
    """高置信度买入信号不应被过滤。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "confidence": 0.9,
        "suggested_position_ratio": 0.24,
    }
    result = advisor.advise(signal)

    decision = result["wisdom_decision"]
    assert not decision.get("entry_filtered", False)


def test_advise_sell_no_position_adjustment(advisor: WisdomAdvisor) -> None:
    """卖出信号不应调整仓位。"""
    signal = {
        "symbol": "600519",
        "direction": "sell",
        "price": 1600.0,
        "confidence": 0.8,
        "suggested_position_ratio": 0.24,
    }
    result = advisor.advise(signal)

    decision = result["wisdom_decision"]
    assert "adjusted_position_ratio" not in decision
    assert "stop_loss_price" not in decision
    assert "profit_taking" in result["wisdom"]


def test_advise_adds_wisdom_advice(advisor: WisdomAdvisor) -> None:
    """信号应包含 wisdom 建议文本。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "confidence": 0.8,
        "suggested_position_ratio": 0.24,
    }
    result = advisor.advise(signal)

    assert "wisdom" in result
    wisdom = result["wisdom"]
    assert "entry_check" in wisdom
    assert "stop_loss" in wisdom
    assert "position_sizing" in wisdom
    assert "psychology_check" in wisdom
    # 建议文本应非空
    assert len(wisdom["entry_check"]) > 0


def test_advise_no_position_ratio_no_adjustment(advisor: WisdomAdvisor) -> None:
    """无仓位比例的信号不应调整仓位。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "confidence": 0.8,
    }
    result = advisor.advise(signal)

    decision = result["wisdom_decision"]
    assert "adjusted_position_ratio" not in decision


def test_advise_no_price_no_stop_loss(advisor: WisdomAdvisor) -> None:
    """无价格的信号不应预设止损价。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 0,
        "confidence": 0.8,
        "suggested_position_ratio": 0.24,
    }
    result = advisor.advise(signal)

    decision = result["wisdom_decision"]
    assert "stop_loss_price" not in decision
