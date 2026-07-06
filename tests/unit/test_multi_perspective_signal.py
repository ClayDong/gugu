"""测试多视角共识分析注入。

测试场景：
1. 高共识时置信度增强（>70% 同意买入 → confidence +0.1）
2. 低共识时置信度降低（<30% 同意买入 → confidence -0.1）
3. book_router 异常时跳过不阻塞
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from gugu.wisdom.advisor import WisdomAdvisor
from gugu.wisdom.book_router import BookPerspectiveRouter


@pytest.fixture
def base_signal() -> dict:
    """基础买入信号。"""
    return {
        "symbol": "000001",
        "name": "平安银行",
        "direction": "buy",
        "confidence": 0.7,
        "price": 12.50,
        "strategy": "test_strategy",
        "strategies": ["test_strategy"],
        "suggested_position_ratio": 0.1,
        "has_position": False,
        "current_position_ratio": 0.0,
    }


def _make_mock_router(advice_result: dict) -> MagicMock:
    """创建一个模拟的 BookPerspectiveRouter。"""
    router = MagicMock(spec=BookPerspectiveRouter)
    router.get_advice.return_value = advice_result
    return router


def test_high_consensus_boosts_confidence(base_signal: dict) -> None:
    """高共识（>70% 同意买入）→ 置信度增强 +0.1。"""
    mock_advice = {
        "perspectives": [
            {"name": "巴菲特", "action": "buy", "reason": "估值合理", "confidence": 0.8},
            {"name": "芒格", "action": "buy", "reason": "安全边际充足", "confidence": 0.7},
            {"name": "利弗莫尔", "action": "buy", "reason": "趋势确认", "confidence": 0.6},
        ],
        "consensus": {"buy_pct": 1.0, "sell_pct": 0.0, "hold_pct": 0.0},
        "conflicts": [],
    }
    router = _make_mock_router(mock_advice)
    advisor = WisdomAdvisor(book_router=router)

    result = advisor._inject_perspective_consensus(base_signal.copy())

    # 置信度应从 0.7 提升到 0.8
    assert result["confidence"] == pytest.approx(0.8, abs=0.01)

    mp = result.get("mentor_perspectives", {})
    assert mp["analyzed_count"] == 3
    assert mp["buy_agreement_pct"] == 1.0
    assert mp["consensus"] == "strong_buy"
    assert mp["buy_perspectives"] == ["巴菲特", "芒格", "利弗莫尔"]
    assert len(mp["top_reasons"]) == 3


def test_low_consensus_reduces_confidence(base_signal: dict) -> None:
    """低共识（<30% 同意买入）→ 置信度降低 -0.1。"""
    mock_advice = {
        "perspectives": [
            {"name": "巴菲特", "action": "hold", "reason": "安全边际不足", "confidence": 0.3},
            {"name": "芒格", "action": "hold", "reason": "等待更好时机", "confidence": 0.4},
            {"name": "利弗莫尔", "action": "buy", "reason": "趋势确认", "confidence": 0.6},
            {"name": "海龟", "action": "hold", "reason": "风险偏高", "confidence": 0.3},
        ],
        "consensus": {"buy_pct": 0.25, "sell_pct": 0.0, "hold_pct": 0.75},
        "conflicts": [],
    }
    router = _make_mock_router(mock_advice)
    advisor = WisdomAdvisor(book_router=router)

    signal = base_signal.copy()
    signal["confidence"] = 0.7
    result = advisor._inject_perspective_consensus(signal)

    # 置信度应从 0.7 降低到 0.6
    assert result["confidence"] == pytest.approx(0.6, abs=0.01)

    mp = result.get("mentor_perspectives", {})
    assert mp["buy_agreement_pct"] == 0.25
    assert mp["buy_perspectives"] == ["利弗莫尔"]


def test_low_consensus_min_floor(base_signal: dict) -> None:
    """低共识时置信度不低于 0.1 下限。"""
    mock_advice = {
        "perspectives": [
            {"name": "巴菲特", "action": "hold", "reason": "观望", "confidence": 0.2},
        ],
        "consensus": {"buy_pct": 0.0, "sell_pct": 0.0, "hold_pct": 1.0},
        "conflicts": [],
    }
    router = _make_mock_router(mock_advice)
    advisor = WisdomAdvisor(book_router=router)

    signal = base_signal.copy()
    signal["confidence"] = 0.05  # 已经很低的置信度
    result = advisor._inject_perspective_consensus(signal)

    # 不应低于 0.1
    assert result["confidence"] >= 0.1


def test_high_consensus_max_cap(base_signal: dict) -> None:
    """高共识时置信度不超过 1.0 上限。"""
    mock_advice = {
        "perspectives": [
            {"name": "巴菲特", "action": "buy", "reason": "完美机会", "confidence": 1.0},
        ],
        "consensus": {"buy_pct": 1.0, "sell_pct": 0.0, "hold_pct": 0.0},
        "conflicts": [],
    }
    router = _make_mock_router(mock_advice)
    advisor = WisdomAdvisor(book_router=router)

    signal = base_signal.copy()
    signal["confidence"] = 0.95  # 已在上限附近
    result = advisor._inject_perspective_consensus(signal)

    # 不应超过 1.0
    assert result["confidence"] <= 1.0


def test_router_exception_skips_gracefully(base_signal: dict) -> None:
    """book_router 异常时跳过，不阻塞信号流。"""
    router = MagicMock(spec=BookPerspectiveRouter)
    router.get_advice.side_effect = RuntimeError("模拟异常")

    advisor = WisdomAdvisor(book_router=router)

    # 不应抛出异常
    result = advisor._inject_perspective_consensus(base_signal.copy())

    # mentor_perspectives 不应设置
    assert "mentor_perspectives" not in result
    # 原始字段不被污染
    assert result["direction"] == "buy"
    assert result["confidence"] == 0.7


def test_no_router_passthrough(base_signal: dict) -> None:
    """未配置 book_router 时直接返回原始信号。"""
    advisor = WisdomAdvisor(book_router=None)

    result = advisor._inject_perspective_consensus(base_signal.copy())

    assert "mentor_perspectives" not in result
    assert result["confidence"] == 0.7


def test_sell_signal_perspectives() -> None:
    """卖出信号也能正常分析。"""
    sell_signal = {
        "symbol": "000001",
        "name": "平安银行",
        "direction": "sell",
        "confidence": 0.7,
        "price": 14.50,
        "strategy": "test_strategy",
        "strategies": ["test_strategy"],
        "has_position": True,
        "current_position_ratio": 0.15,
    }

    mock_advice = {
        "perspectives": [
            {"name": "利弗莫尔", "action": "sell", "reason": "趋势反转", "confidence": 0.8},
            {"name": "陈江挺", "action": "sell", "reason": "止损触发", "confidence": 0.7},
            {"name": "海龟", "action": "sell", "reason": "系统信号卖出", "confidence": 0.75},
        ],
        "consensus": {"buy_pct": 0.0, "sell_pct": 1.0, "hold_pct": 0.0},
        "conflicts": [],
    }
    router = _make_mock_router(mock_advice)
    advisor = WisdomAdvisor(book_router=router)

    result = advisor._inject_perspective_consensus(sell_signal.copy())

    mp = result.get("mentor_perspectives", {})
    assert mp["analyzed_count"] == 3
    assert mp["sell_agreement_pct"] == 1.0
    assert mp["consensus"] == "strong_sell"
    assert mp["sell_perspectives"] == ["利弗莫尔", "陈江挺", "海龟"]


def test_conflict_consensus() -> None:
    """买卖视角接近时标注为 conflict。"""
    conflict_signal = {
        "symbol": "000001",
        "direction": "buy",
        "confidence": 0.5,
        "price": 13.0,
        "strategy": "test",
        "strategies": ["test"],
    }

    mock_advice = {
        "perspectives": [
            {"name": "巴菲特", "action": "buy", "reason": "低估", "confidence": 0.6},
            {"name": "利弗莫尔", "action": "sell", "reason": "趋势向下", "confidence": 0.6},
            {"name": "陈江挺", "action": "hold", "reason": "不明朗", "confidence": 0.5},
        ],
        "consensus": {"buy_pct": 0.33, "sell_pct": 0.33, "hold_pct": 0.33},
        "conflicts": [],
    }
    router = _make_mock_router(mock_advice)
    advisor = WisdomAdvisor(book_router=router)

    result = advisor._inject_perspective_consensus(conflict_signal.copy())

    mp = result.get("mentor_perspectives", {})
    assert mp["consensus"] == "conflict"


def test_mentor_perspectives_structure() -> None:
    """验证 mentor_perspectives 字段结构的完整性。"""
    signal = {
        "symbol": "000001",
        "direction": "buy",
        "confidence": 0.6,
        "price": 12.0,
        "strategy": "test",
        "strategies": ["test"],
    }

    mock_advice = {
        "perspectives": [
            {"name": "巴菲特", "action": "buy", "reason": "低估", "confidence": 0.7},
        ],
        "consensus": {"buy_pct": 1.0, "sell_pct": 0.0, "hold_pct": 0.0},
        "conflicts": [],
    }
    router = _make_mock_router(mock_advice)
    advisor = WisdomAdvisor(book_router=router)

    result = advisor._inject_perspective_consensus(signal.copy())

    mp = result.get("mentor_perspectives", {})
    # 所有要求的字段必须存在
    expected_keys = {
        "analyzed_count",
        "buy_agreement_pct",
        "sell_agreement_pct",
        "hold_agreement_pct",
        "buy_perspectives",
        "sell_perspectives",
        "hold_perspectives",
        "top_reasons",
        "consensus",
    }
    assert expected_keys.issubset(mp.keys()), f"缺少字段: {expected_keys - set(mp.keys())}"
