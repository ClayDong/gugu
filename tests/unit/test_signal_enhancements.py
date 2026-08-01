"""P1/P2 信号增强集成测试（2026-08-01）：数据质量戳 + 共振 + 能力圈。

验证：
1. 完整信号卡片包含质量戳/共振/能力圈标注
2. 降级数据信号标注"仅供观察"
3. 多策略共振信号标注"多策略共振"
4. 矛盾信号单独展示
"""

from __future__ import annotations

from gugu.notifier.formatter import format_signal
from gugu.strategies.capability import annotate_signal


def _base_signal(**overrides) -> dict:
    sig = {
        "symbol": "600519",
        "name": "贵州茅台",
        "direction": "buy",
        "price": 1281.0,
        "confidence": 0.85,
        "strategy": "turtle",
        "strategies": ["turtle"],
        "reason": "测试信号",
    }
    sig.update(overrides)
    return sig


def test_card_contains_quality_badges():
    """完整信号卡片包含质量戳。"""
    sig = annotate_signal(_base_signal(data_quality="fresh"))
    card = format_signal(sig)
    text = str(card)
    assert "🟢 实时数据" in text


def test_card_degraded_marks_observation():
    """降级数据信号标注仅供观察。"""
    sig = annotate_signal(_base_signal(data_quality="degraded", observation_only=True))
    card = format_signal(sig)
    text = str(card)
    assert "🟡 数据降级" in text
    assert "👀 仅供观察" in text


def test_card_stale_marks_red():
    """非实时价信号标注红色。"""
    sig = annotate_signal(_base_signal(data_quality="stale", observation_only=True))
    card = format_signal(sig)
    text = str(card)
    assert "🔴 非实时价" in text


def test_card_resonance_strong():
    """多策略共振标注。"""
    sig = annotate_signal(
        _base_signal(
            strategies=["turtle", "dual_ma"],
            resonance=2,
            resonance_level="strong",
        )
    )
    card = format_signal(sig)
    text = str(card)
    assert "🔵 多策略共振" in text


def test_card_resonance_single():
    """单策略触发标注。"""
    sig = annotate_signal(_base_signal(resonance=1, resonance_level="single"))
    card = format_signal(sig)
    text = str(card)
    assert "⚪ 单策略触发" in text


def test_card_conflict_displayed():
    """矛盾信号单独展示。"""
    sig = annotate_signal(
        _base_signal(
            strategies=["turtle", "chenxuezhang"],
            signal_conflict=True,
            conflict_detail={"buy": ["turtle"], "sell": ["chenxuezhang"]},
        )
    )
    card = format_signal(sig)
    text = str(card)
    assert "🟠 买卖矛盾" in text
    assert "矛盾信号" in text


def test_card_capability_line():
    """能力圈标注展示。"""
    sig = annotate_signal(_base_signal())
    card = format_signal(sig)
    text = str(card)
    assert "能力圈" in text
    assert "技术面" in text


def test_card_ml_capability():
    """ML 策略标注黑箱+试验仓。"""
    sig = annotate_signal(_base_signal(strategies=["ml_gradient_boost"]))
    card = format_signal(sig)
    text = str(card)
    assert "黑箱ML" in text
    assert "试验仓" in text


def test_card_capability_conservative_for_rule_strategy():
    """博主规则策略标注保守仓位上限。"""
    sig = annotate_signal(_base_signal(strategies=["chenxuezhang"]))
    card = format_signal(sig)
    text = str(card)
    assert "规则/经验" in text
    assert "仓位上限 10%" in text
