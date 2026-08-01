"""策略能力圈标注单测（2026-08-01 P2）。"""

from __future__ import annotations

from gugu.strategies.capability import (
    annotate_signal,
    format_capability_line,
    get_profile,
)


def test_get_profile_known_strategy():
    """已知策略返回对应画像。"""
    p = get_profile("turtle")
    assert p.explainability == "technical"
    assert p.position_cap == 0.20


def test_get_profile_unknown_strategy_fallback():
    """未收录策略返回兜底画像（不崩溃）。"""
    p = get_profile("nonexistent_strategy")
    assert p.position_cap == 0.10
    assert p.validation_status == "in_review"


def test_annotate_signal_single_strategy():
    """单策略信号注入能力圈标注。"""
    sig = {"symbol": "600519", "direction": "buy", "strategies": ["turtle"]}
    out = annotate_signal(sig)
    cap = out["capability"]
    assert cap["explainability"] == "technical"
    assert cap["position_cap"] == 0.20
    assert cap["validation_status"] == "conditional"


def test_annotate_signal_string_strategies():
    """strategies 为逗号分隔字符串时也能处理。"""
    sig = {"symbol": "600519", "direction": "buy", "strategies": "turtle,dual_ma"}
    out = annotate_signal(sig)
    assert len(out["capability"]["strategy_names"]) == 2


def test_annotate_signal_takes_most_conservative():
    """多策略共振时取最保守画像（仓位上限最小 + 验证等级最低）。"""
    # chenxuezhang(rule, conditional, 0.10) + turtle(technical, conditional, 0.20)
    sig = {"symbol": "600519", "direction": "buy", "strategies": ["turtle", "chenxuezhang"]}
    out = annotate_signal(sig)
    cap = out["capability"]
    assert cap["position_cap"] == 0.10  # 取最小
    assert cap["explainability"] == "rule"  # 取最低可解释性


def test_annotate_signal_ml_lowest_explainability():
    """ML 策略标记为黑箱。"""
    sig = {"symbol": "600519", "direction": "buy", "strategies": ["ml_gradient_boost"]}
    out = annotate_signal(sig)
    assert out["capability"]["explainability"] == "ml"
    assert out["capability"]["validation_status"] == "experimental"
    assert out["capability"]["position_cap"] == 0.05


def test_annotate_signal_no_strategies_noop():
    """无策略时不注入（不崩溃）。"""
    sig = {"symbol": "600519", "direction": "buy"}
    out = annotate_signal(sig)
    assert "capability" not in out


def test_format_capability_line():
    """格式化能力圈行。"""
    sig = annotate_signal({"symbol": "600519", "strategies": ["turtle"]})
    line = format_capability_line(sig)
    assert "技术面" in line
    assert "仓位上限" in line


def test_format_capability_line_empty():
    """无标注时返回空串。"""
    assert format_capability_line({"symbol": "600519"}) == ""
