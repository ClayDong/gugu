"""测试：宏观数据增强器。"""

from __future__ import annotations

import pytest

from gugu.analysis.macro_regime_enhancer import MacroRegimeEnhancer


def test_enhancer_init() -> None:
    enhancer = MacroRegimeEnhancer()
    assert enhancer is not None
    assert enhancer._macro_snapshot is None


@pytest.mark.asyncio
async def test_enhancer_with_empty_macro() -> None:
    """宏观数据不可用时，不修改 regime 结果。"""
    enhancer = MacroRegimeEnhancer()
    enhancer._macro_snapshot = {}  # 预置空数据，防止触发真实采集
    base_result = {
        "regime": "bull",
        "total_position_limit": 0.80,
        "buy_signal_allowed": True,
        "sell_signal_required": False,
        "confidence": 0.7,
        "score": 0.5,
    }

    # 没有宏观数据时，enhance 不应改变结果
    result = await enhancer.enhance(base_result)
    assert result["regime"] == "bull"
    assert result["total_position_limit"] == 0.80
    assert "macro_adjustment" in result


@pytest.mark.asyncio
async def test_enhancer_gold_high_signal() -> None:
    """模拟黄金暴涨场景。"""
    enhancer = MacroRegimeEnhancer()
    # 手动注入宏观数据，模拟黄金暴涨
    enhancer._macro_snapshot = {
        "gold": {"gold_price": 3500.0, "gold_change_pct": 5.2},
        "fx": {"usd_cny": 7.15, "usd_index": 104.0},
        "bond": {"us10y_yield": 4.2, "inverted": False},
        "derivative": {"vix": 22.0},
    }

    base_result = {
        "regime": "bull",
        "total_position_limit": 0.80,
        "buy_signal_allowed": True,
        "sell_signal_required": False,
        "confidence": 0.7,
    }

    result = await enhancer.enhance(base_result)
    adj = result.get("macro_adjustment", {})
    assert adj.get("reduction", 0) > 0
    assert any("黄金" in r for r in adj.get("reasons", []))
    assert result["total_position_limit"] < 0.80


@pytest.mark.asyncio
async def test_enhancer_multiple_risks() -> None:
    """模拟多重风险叠加。"""
    enhancer = MacroRegimeEnhancer()
    enhancer._macro_snapshot = {
        "gold": {"gold_price": 3200.0},
        "fx": {"usd_cny": 7.6},
        "bond": {"us10y_yield": 5.2, "inverted": True},
        "derivative": {"vix": 35.0},
    }

    base_result = {
        "regime": "sideways",
        "total_position_limit": 0.40,
        "buy_signal_allowed": True,
        "sell_signal_required": False,
        "confidence": 0.3,
    }

    result = await enhancer.enhance(base_result)
    adj = result.get("macro_adjustment", {})

    # 多重风险叠加，但最多降低30%
    assert adj.get("reduction", 0) <= 0.30
    assert len(adj.get("reasons", [])) >= 1
    # 仓位上限不会降到0（最多降低30%）
    assert result["total_position_limit"] >= 0.10


@pytest.mark.asyncio
async def test_enhancer_no_improvement_on_clear_skies() -> None:
    """宏观环境良好时不调整。"""
    enhancer = MacroRegimeEnhancer()
    enhancer._macro_snapshot = {
        "gold": {"gold_price": 2600.0},
        "fx": {"usd_cny": 7.0, "usd_index": 100.0},
        "bond": {"us10y_yield": 3.8, "inverted": False},
        "derivative": {"vix": 15.0},
    }

    base_result = {
        "regime": "bull",
        "total_position_limit": 0.80,
        "confidence": 0.8,
    }

    result = await enhancer.enhance(base_result)
    adj = result.get("macro_adjustment", {})
    assert adj.get("reduction", 0) == 0.0
    assert result["total_position_limit"] == 0.80
