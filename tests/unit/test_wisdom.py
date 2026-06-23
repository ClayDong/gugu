"""WisdomAdvisor 单元测试。

验证 LLM 决策和 fallback 两种模式：
1. LLM 模式：调用真实 LLM API，验证结构化 JSON 输出
2. Fallback 模式：LLM 不可用时，硬编码规则决策

测试策略：
- LLM 决策结果不确定，验证结构而非具体值
- Fallback 模式验证精确数值
- 两种模式都验证核心安全约束（仓位上限、止损价）
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gugu.wisdom.advisor import (
    FALLBACK_ADD_RATIO,
    FALLBACK_ENTRY_MIN_CONFIDENCE,
    FALLBACK_STOP_LOSS_PCT,
    FALLBACK_TRIAL_RATIO,
    WisdomAdvisor,
    _max_single_position,
)


# ========== Fallback 模式测试（LLM 不可用） ==========


@pytest.fixture
def fallback_advisor() -> WisdomAdvisor:
    """构造一个强制使用 fallback 模式的 WisdomAdvisor。"""
    advisor = WisdomAdvisor()
    advisor._llm_available = False
    return advisor


class TestFallbackMode:
    """硬编码规则 fallback 模式测试。"""

    def test_fallback_buy_adjusts_position(self, fallback_advisor: WisdomAdvisor) -> None:
        """买入信号应调整仓位为试仓比例。"""
        signal = {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.24,
        }
        result = fallback_advisor.advise(signal)

        decision = result["wisdom_decision"]
        assert decision["source"] == "fallback"
        assert "adjusted_position_ratio" in decision
        assert decision["position_strategy"] == "trial"
        # 试仓比例 = 0.24 * 0.20 = 0.048
        assert abs(decision["adjusted_position_ratio"] - 0.24 * FALLBACK_TRIAL_RATIO) < 0.001

    def test_fallback_buy_sets_stop_loss(self, fallback_advisor: WisdomAdvisor) -> None:
        """买入信号应预设止损价。"""
        signal = {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.24,
        }
        result = fallback_advisor.advise(signal)

        decision = result["wisdom_decision"]
        assert "stop_loss_price" in decision
        expected_stop = round(1500.0 * (1 - FALLBACK_STOP_LOSS_PCT), 2)
        assert decision["stop_loss_price"] == expected_stop

    def test_fallback_low_confidence_filtered(self, fallback_advisor: WisdomAdvisor) -> None:
        """低置信度买入信号应被入场过滤。"""
        signal = {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.4,
            "suggested_position_ratio": 0.24,
        }
        result = fallback_advisor.advise(signal)

        decision = result["wisdom_decision"]
        assert decision.get("entry_filtered") is True
        assert result["wisdom_filtered"] is True

    def test_fallback_high_confidence_not_filtered(self, fallback_advisor: WisdomAdvisor) -> None:
        """高置信度买入信号不应被过滤。"""
        signal = {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.9,
            "suggested_position_ratio": 0.24,
        }
        result = fallback_advisor.advise(signal)

        decision = result["wisdom_decision"]
        assert not decision.get("entry_filtered", False)

    def test_fallback_sell_has_advice(self, fallback_advisor: WisdomAdvisor) -> None:
        """卖出信号应包含止盈/追踪止损建议。"""
        signal = {
            "symbol": "600519",
            "direction": "sell",
            "price": 1600.0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.24,
        }
        result = fallback_advisor.advise(signal)

        wisdom = result["wisdom"]
        assert "profit_taking" in wisdom
        assert "trailing_stop" in wisdom

    def test_fallback_adds_skill_advice(self, fallback_advisor: WisdomAdvisor) -> None:
        """信号应包含 skill 建议文本。"""
        signal = {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.24,
        }
        result = fallback_advisor.advise(signal)

        wisdom = result["wisdom"]
        assert "entry_check" in wisdom
        assert "stop_loss" in wisdom
        assert "position_sizing" in wisdom

    def test_fallback_no_price_no_stop_loss(self, fallback_advisor: WisdomAdvisor) -> None:
        """无价格的信号不应预设止损价。"""
        signal = {
            "symbol": "600519",
            "direction": "buy",
            "price": 0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.24,
        }
        result = fallback_advisor.advise(signal)

        decision = result["wisdom_decision"]
        assert "stop_loss_price" not in decision

    def test_fallback_position_cap_with_existing(self, fallback_advisor: WisdomAdvisor) -> None:
        """已有持仓时加仓不应超过上限。"""
        signal = {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.24,
            "has_position": True,
            "current_position_ratio": 0.28,
        }
        result = fallback_advisor.advise(signal)

        # 加码后不应超过上限
        adjusted = result.get("suggested_position_ratio", 0)
        max_ratio = _max_single_position()
        assert adjusted + 0.28 <= max_ratio + 0.001


# ========== LLM 模式测试 ==========


class TestLLMMode:
    """LLM 决策模式测试。

    使用 mock LLM 响应，验证结构化决策解析和应用逻辑。
    """

    def _make_advisor_with_mock_llm(self) -> WisdomAdvisor:
        """构造一个 mock LLM 的 WisdomAdvisor。"""
        advisor = WisdomAdvisor()
        advisor._llm_available = True
        return advisor

    def test_llm_buy_decision_applied(self) -> None:
        """LLM 返回 buy 决策应正确应用仓位和止损。"""
        advisor = self._make_advisor_with_mock_llm()

        mock_response = {
            "action": "buy",
            "position_ratio": 0.06,
            "stop_loss_price": 1380.0,
            "reason": "三层过滤通过，建议试仓",
        }

        with patch.object(advisor, "_advise_llm") as mock_llm:
            mock_llm.return_value = advisor._apply_llm_decision(
                {
                    "symbol": "600519",
                    "direction": "buy",
                    "price": 1500.0,
                    "confidence": 0.8,
                    "suggested_position_ratio": 0.24,
                    "current_position_ratio": 0.0,
                },
                mock_response,
            )
            result = advisor.advise({
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.8,
                "suggested_position_ratio": 0.24,
            })

        decision = result["wisdom_decision"]
        assert decision["source"] == "llm"
        assert decision["action"] == "buy"
        assert result["suggested_position_ratio"] == 0.06
        assert result["stop_loss_price"] == 1380.0

    def test_llm_filter_decision(self) -> None:
        """LLM 返回 filter 决策应过滤信号。"""
        advisor = self._make_advisor_with_mock_llm()

        mock_response = {
            "action": "filter",
            "position_ratio": 0,
            "stop_loss_price": None,
            "reason": "缺乏基础分析，不满足三层过滤",
        }

        result = advisor._apply_llm_decision(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.4,
                "suggested_position_ratio": 0.24,
                "current_position_ratio": 0.0,
            },
            mock_response,
        )

        assert result["wisdom_filtered"] is True
        assert result["suggested_position_ratio"] == 0.0
        assert "filter_reason" in result["wisdom_decision"]

    def test_llm_hold_decision(self) -> None:
        """LLM 返回 hold 决策应过滤信号。"""
        advisor = self._make_advisor_with_mock_llm()

        mock_response = {
            "action": "hold",
            "position_ratio": 0,
            "stop_loss_price": None,
            "reason": "阶段不明朗，建议观望",
        }

        result = advisor._apply_llm_decision(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.7,
                "suggested_position_ratio": 0.24,
                "current_position_ratio": 0.0,
            },
            mock_response,
        )

        assert result["wisdom_filtered"] is True

    def test_llm_sell_decision(self) -> None:
        """LLM 返回 sell 决策应确认卖出。"""
        advisor = self._make_advisor_with_mock_llm()

        mock_response = {
            "action": "sell",
            "position_ratio": 0,
            "stop_loss_price": None,
            "reason": "趋势反转，建议卖出",
        }

        result = advisor._apply_llm_decision(
            {
                "symbol": "600519",
                "direction": "sell",
                "price": 1400.0,
                "confidence": 0.8,
                "suggested_position_ratio": 0.0,
            },
            mock_response,
        )

        decision = result["wisdom_decision"]
        assert decision["action"] == "sell"
        assert "sell_reason" in decision

    def test_llm_buy_rejected_when_suggests_sell(self) -> None:
        """策略建议买入但 LLM 建议卖出时应否决买入。"""
        advisor = self._make_advisor_with_mock_llm()

        mock_response = {
            "action": "sell",
            "position_ratio": 0,
            "stop_loss_price": None,
            "reason": "股票处于最后阶段，应卖出",
        }

        result = advisor._apply_llm_decision(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.8,
                "suggested_position_ratio": 0.24,
                "current_position_ratio": 0.0,
            },
            mock_response,
        )

        assert result["wisdom_filtered"] is True

    def test_llm_position_capped_by_risk(self) -> None:
        """LLM 建议的仓位不应超过风控上限。"""
        advisor = self._make_advisor_with_mock_llm()

        mock_response = {
            "action": "buy",
            "position_ratio": 0.50,  # 超过风控上限 0.30
            "stop_loss_price": 1350.0,
            "reason": "强烈看好",
        }

        result = advisor._apply_llm_decision(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.9,
                "suggested_position_ratio": 0.24,
                "current_position_ratio": 0.0,
            },
            mock_response,
        )

        max_ratio = _max_single_position()
        assert result["suggested_position_ratio"] <= max_ratio

    def test_llm_fallback_stop_loss(self) -> None:
        """LLM 未给出止损价时应使用 fallback 规则。"""
        advisor = self._make_advisor_with_mock_llm()

        mock_response = {
            "action": "buy",
            "position_ratio": 0.06,
            "stop_loss_price": None,  # 未给出止损价
            "reason": "建议试仓",
        }

        result = advisor._apply_llm_decision(
            {
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.8,
                "suggested_position_ratio": 0.24,
                "current_position_ratio": 0.0,
            },
            mock_response,
        )

        # 应使用 fallback 止损价
        expected_stop = round(1500.0 * (1 - FALLBACK_STOP_LOSS_PCT), 2)
        assert result["stop_loss_price"] == expected_stop
        assert result["wisdom_decision"]["stop_loss_source"] == "fallback"

    def test_parse_llm_json_response(self) -> None:
        """应正确解析 LLM JSON 输出。"""
        advisor = self._make_advisor_with_mock_llm()

        # 正常 JSON
        result = advisor._parse_llm_response(
            '{"action": "buy", "position_ratio": 0.06, "stop_loss_price": 1380, "reason": "test"}'
        )
        assert result is not None
        assert result["action"] == "buy"

        # JSON 在 code block 中
        result = advisor._parse_llm_response(
            '```json\n{"action": "filter", "position_ratio": 0, "stop_loss_price": null, "reason": "test"}\n```'
        )
        assert result is not None
        assert result["action"] == "filter"

        # 无效 JSON
        result = advisor._parse_llm_response("not json")
        assert result is None

        # 缺少 action 字段
        result = advisor._parse_llm_response('{"position_ratio": 0.06}')
        assert result is None

    def test_llm_fallback_on_failure(self) -> None:
        """LLM 调用失败时应降级到 fallback。"""
        advisor = self._make_advisor_with_mock_llm()

        with patch.object(advisor, "_advise_llm", side_effect=Exception("LLM timeout")):
            result = advisor.advise({
                "symbol": "600519",
                "direction": "buy",
                "price": 1500.0,
                "confidence": 0.8,
                "suggested_position_ratio": 0.24,
            })

        # 应降级到 fallback
        decision = result["wisdom_decision"]
        assert decision["source"] == "fallback"


# ========== 通用测试 ==========


class TestAdvisorCommon:
    """不依赖决策模式的通用测试。"""

    def test_advisor_loads_skills(self) -> None:
        """应加载到 6 个 skill。"""
        advisor = WisdomAdvisor()
        assert len(advisor._skills) >= 6
        assert "stock-entry-decision" in advisor._skills
        assert "stock-stop-loss-decision" in advisor._skills
        assert "stock-position-sizing" in advisor._skills

    def test_signal_not_mutated(self) -> None:
        """advise 不应修改原始信号。"""
        advisor = WisdomAdvisor()
        advisor._llm_available = False
        original = {
            "symbol": "600519",
            "direction": "buy",
            "price": 1500.0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.24,
        }
        original_copy = dict(original)
        advisor.advise(original)
        assert original == original_copy

    def test_extract_decision_rules(self) -> None:
        """应从 SKILL.md 提取 I/E/B 段，跳过 R/A1 段。"""
        advisor = WisdomAdvisor()
        content = """# Test Skill

## R — 原文 (Reading)
这是原文引用，应被跳过。

## I — 方法论骨架 (Interpretation)
这是方法论，应被保留。

## A1 — 书中的应用 (Past Application)
这是历史案例，应被跳过。

## E — 执行步骤 (Execution)
这是执行步骤，应被保留。

## B — 边界 (Boundary)
这是边界，应被保留。
"""
        result = advisor._extract_decision_rules(content)
        assert "方法论" in result
        assert "执行步骤" in result
        assert "边界" in result
        assert "原文" not in result
        assert "历史案例" not in result
