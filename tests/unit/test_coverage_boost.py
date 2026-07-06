"""Coverage boost tests for under-covered modules.

Targets:
1. wisdom/advisor.py (56%) — _compute_take_profit, _advise_fallback (sell),
   _inject_perspective_consensus, _apply_llm_decision, _build_data_driven_advice,
   _parse_llm_response, advise_async, close, llm_interpret, etc.

2. wisdom/book_router.py (41%) — select_perspectives_dynamic, _dynamic_select,
   get_advice, _predict_perspective_action, get_perspective_summary,
   _extract_core_wisdom, load_all_perspectives

3. screener/report.py (39%) — format_screener_report (empty, populated),
   format_range_watchlist
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gugu.wisdom.advisor import (
    LLM_MAX_TRIAL_RATIO,
    WisdomAdvisor,
    _compute_take_profit,
    _is_profitable_signal,
)


# ==============================================================================
# wisdom/advisor.py — helper functions
# ==============================================================================

class TestComputeTakeProfit:
    """_compute_take_profit — ATR calibration paths."""

    def test_no_atr(self):
        result = _compute_take_profit(entry_price=100.0)
        assert result["target_1"] == 110.0
        assert result["target_2"] == 120.0
        assert result["target_1_pct"] == 0.10

    def test_atr_high_volatility(self):
        # ATR/price = 4% > 3% → amplify
        result = _compute_take_profit(entry_price=100.0, atr=4.0)
        assert result["target_1_pct"] == 0.15  # 10% * 1.5
        assert result["target_2_pct"] == 0.30

    def test_atr_low_volatility(self):
        # ATR/price = 0.5% < 1% → reduce
        result = _compute_take_profit(entry_price=100.0, atr=0.5)
        assert result["target_1_pct"] == 0.05  # 10% * 0.5
        assert result["target_2_pct"] == 0.10

    def test_atr_normal_volatility(self):
        # ATR/price between 1% and 3% → unchanged
        result = _compute_take_profit(entry_price=100.0, atr=2.0)
        assert result["target_1_pct"] == 0.10


class TestIsProfitableSignal:
    """_is_profitable_signal edge cases."""

    def test_no_order_success(self):
        assert _is_profitable_signal({"direction": "buy", "order_success": False}) is False

    def test_buy_with_success(self):
        assert _is_profitable_signal({"direction": "buy", "order_success": True}) is True

    def test_sell_with_success(self):
        assert _is_profitable_signal({"direction": "sell", "order_success": True}) is False  # not buy

    def test_empty_record(self):
        assert _is_profitable_signal({}) is False


# ==============================================================================
# wisdom/advisor.py — WisdomAdvisor instance tests
# ==============================================================================

class TestWisdomAdvisorConstructor:
    """Constructor with different config scenarios."""

    @patch("gugu.wisdom.advisor.get_config")
    @patch("gugu.wisdom.advisor.env")
    def test_constructor_no_llm(self, mock_env, mock_config):
        cfg = MagicMock()
        cfg.wisdom.skill_names = []
        cfg.wisdom.skill_dir = None
        cfg.wisdom.fallback_dirs = []
        cfg.wisdom.enable_multi_perspective = False
        cfg.risk.max_position_ratio = 0.30
        mock_config.return_value = cfg

        env_cfg = MagicMock()
        env_cfg.llm_api_key = ""
        env_cfg.llm_base_url = ""
        env_cfg.llm_model = ""
        mock_env.return_value = env_cfg

        advisor = WisdomAdvisor()
        assert advisor._llm_available is False
        assert advisor._book_router is None

    @patch("gugu.wisdom.advisor.get_config")
    @patch("gugu.wisdom.advisor.env")
    def test_constructor_with_llm(self, mock_env, mock_config):
        cfg = MagicMock()
        cfg.wisdom.skill_names = []
        cfg.wisdom.skill_dir = None
        cfg.wisdom.fallback_dirs = []
        cfg.wisdom.enable_multi_perspective = False
        cfg.risk.max_position_ratio = 0.30
        mock_config.return_value = cfg

        env_cfg = MagicMock()
        env_cfg.llm_api_key = "sk-test"
        env_cfg.llm_base_url = "https://test.api"
        env_cfg.llm_model = "test-model"
        mock_env.return_value = env_cfg

        advisor = WisdomAdvisor()
        assert advisor._llm_available is True


class TestWisdomAdvisorMethods:
    """Methods on WisdomAdvisor that don't require LLM calls."""

    @pytest.fixture
    def advisor(self):
        with (
            patch("gugu.wisdom.advisor.get_config") as mock_cfg,
            patch("gugu.wisdom.advisor.env") as mock_env,
        ):
            cfg = MagicMock()
            cfg.wisdom.skill_names = []
            cfg.wisdom.skill_dir = None
            cfg.wisdom.fallback_dirs = []
            cfg.wisdom.enable_multi_perspective = False
            cfg.risk.max_position_ratio = 0.30  # <-- critical: real float, not MagicMock
            mock_cfg.return_value = cfg
            env_cfg = MagicMock()
            env_cfg.llm_api_key = ""
            env_cfg.llm_base_url = ""
            env_cfg.llm_model = ""
            mock_env.return_value = env_cfg
            yield WisdomAdvisor()

    def test_close_no_client(self, advisor):
        """close() with no async client should not raise."""
        advisor.close()  # sync version — but actually it's async
        # Just verify it doesn't crash. The method is async, so we'll test separately.

    @pytest.mark.asyncio
    async def test_close_async_no_client(self, advisor):
        await advisor.close()
        assert advisor._async_client is None

    @pytest.mark.asyncio
    async def test_close_async_with_client(self, advisor):
        client = AsyncMock()
        advisor._async_client = client
        await advisor.close()
        client.aclose.assert_awaited_once()
        assert advisor._async_client is None

    def test_as_dict_from_signal(self, advisor):
        """_as_dict should convert Signal-like objects."""
        from gugu.models.signal import Signal
        sig = Signal(symbol="000001", direction="buy", price=10.0, confidence=0.8)
        result = advisor._as_dict(sig)
        assert result["symbol"] == "000001"
        assert result["direction"] == "buy"

    def test_as_dict_from_dict(self, advisor):
        result = advisor._as_dict({"symbol": "000001", "direction": "buy"})
        assert result["symbol"] == "000001"

    def test_as_dict_none_values(self, advisor):
        result = advisor._as_dict({"symbol": None, "direction": ""})
        assert result["symbol"] is None

    @patch("gugu.wisdom.advisor.BookPerspectiveRouter")
    def test_inject_perspective_consensus_no_router(self, mock_router_cls, advisor):
        """Without book_router, signal should pass through unchanged."""
        advisor._book_router = None
        signal = {"symbol": "000001", "confidence": 0.5}
        result = advisor._inject_perspective_consensus(signal)
        assert result is signal  # same object
        assert "mentor_perspectives" not in result

    @patch("gugu.wisdom.advisor.BookPerspectiveRouter")
    def test_inject_perspective_consensus_exception(self, mock_router_cls, advisor):
        """Exception in book_router should be caught."""
        router = MagicMock()
        router.get_advice.side_effect = ValueError("test error")
        advisor._book_router = router
        signal = {"symbol": "000001", "confidence": 0.5}
        result = advisor._inject_perspective_consensus(signal)
        assert result is signal

    @patch("gugu.wisdom.advisor.BookPerspectiveRouter")
    def test_inject_perspective_consensus_high_buy(self, mock_router_cls, advisor):
        """High buy consensus should boost confidence."""
        router = MagicMock()
        router.get_advice.return_value = {
            "perspectives": [
                {"name": "chen-jiang-ting-perspective", "action": "buy",
                 "reason": "good setup", "confidence": 0.8},
                {"name": "jesse-livermore-perspective", "action": "buy",
                 "reason": "trend is friend", "confidence": 0.7},
            ],
            "consensus": {"buy_pct": 0.8, "sell_pct": 0.1, "hold_pct": 0.1},
            "conflicts": [],
        }
        advisor._book_router = router
        signal = {"symbol": "000001", "confidence": 0.5, "direction": "buy"}
        result = advisor._inject_perspective_consensus(signal)
        assert "mentor_perspectives" in result
        assert result["confidence"] > 0.5  # boosted

    @patch("gugu.wisdom.advisor.BookPerspectiveRouter")
    def test_inject_perspective_consensus_low_buy(self, mock_router_cls, advisor):
        """Low buy consensus on buy signal should reduce confidence."""
        router = MagicMock()
        router.get_advice.return_value = {
            "perspectives": [
                {"name": "chen-jiang-ting-perspective", "action": "sell",
                 "reason": "danger", "confidence": 0.8},
            ],
            "consensus": {"buy_pct": 0.2, "sell_pct": 0.6, "hold_pct": 0.2},
            "conflicts": [],
        }
        advisor._book_router = router
        signal = {"symbol": "000001", "confidence": 0.5, "direction": "buy"}
        result = advisor._inject_perspective_consensus(signal)
        assert result["confidence"] < 0.5  # reduced

    @patch("gugu.wisdom.advisor.BookPerspectiveRouter")
    def test_inject_perspective_consensus_no_perspectives(self, mock_router_cls, advisor):
        """Empty perspectives list should return signal unchanged."""
        router = MagicMock()
        router.get_advice.return_value = {
            "perspectives": [],
            "consensus": {"buy_pct": 0, "sell_pct": 0, "hold_pct": 0},
            "conflicts": [],
        }
        advisor._book_router = router
        signal = {"symbol": "000001", "confidence": 0.5}
        result = advisor._inject_perspective_consensus(signal)
        assert "mentor_perspectives" not in result

    def test_parse_llm_response_valid(self, advisor):
        result = advisor._parse_llm_response('{"action": "buy", "position_ratio": 0.1}')
        assert result is not None
        assert result["action"] == "buy"

    def test_parse_llm_response_invalid_action(self, advisor):
        result = advisor._parse_llm_response('{"action": "invalid"}')
        assert result is None

    def test_parse_llm_response_missing_action(self, advisor):
        result = advisor._parse_llm_response('{"foo": "bar"}')
        assert result is None

    def test_parse_llm_response_invalid_json(self, advisor):
        result = advisor._parse_llm_response("not json")
        assert result is None

    def test_parse_llm_response_code_block(self, advisor):
        result = advisor._parse_llm_response(
            "```json\n{\"action\": \"hold\", \"reason\": \"test\"}\n```"
        )
        assert result is not None
        assert result["action"] == "hold"

    def test_parse_llm_response_code_block_no_json(self, advisor):
        result = advisor._parse_llm_response("```\nnot json\n```")
        assert result is None

    def test_llm_interpret_no_api_key(self, advisor):
        """llm_interpret returns None when no API key."""
        result = advisor.llm_interpret({"symbol": "000001"})
        assert result is None

    def test_build_interpret_prompt(self, advisor):
        prompt = advisor._build_interpret_prompt({
            "symbol": "000001", "name": "平安银行", "direction": "buy",
            "strategy": "breakout", "reason": "新高突破", "price": "15.0",
            "wisdom_decision": {"action": "buy", "llm_reason": "good signal",
                                "stop_loss_price": 14.0},
        })
        assert "平安银行" in prompt
        assert "buy" in prompt
        assert "14.0" in prompt

    def test_build_interpret_prompt_no_decision(self, advisor):
        prompt = advisor._build_interpret_prompt({
            "symbol": "000001", "direction": "buy",
        })
        assert "000001" in prompt

    def test_build_system_prompt(self, advisor):
        prompt = advisor._build_system_prompt()
        assert "28本股书" in prompt
        assert "JSON" in prompt or "json" in prompt

    def test_build_skill_context_no_skills(self, advisor):
        """With no skills loaded, should return empty header."""
        ctx = advisor._build_skill_context("buy")
        assert "交易智慧参考" in ctx

    def test_extract_decision_rules(self, advisor):
        content = (
            "## I — 方法论骨架\n"
            "rule1\n"
            "## R — 原文引用\nskip this\n"
            "## E — 执行步骤\n"
            "step1\nstep2\n"
        )
        result = advisor._extract_decision_rules(content)
        assert "方法论" in result or "I" in result
        assert "执行步骤" in result or "E" in result

    def test_extract_decision_rules_no_relevant(self, advisor):
        content = "## R — 原文引用\njust quotes\n## A1 — 历史案例\ncases\n"
        result = advisor._extract_decision_rules(content)
        assert result == ""

    def test_advise_fallback_sell_no_position(self, advisor):
        """Sell signal with no position should be filtered."""
        signal = {
            "symbol": "000001",
            "direction": "sell",
            "has_position": False,
            "price": 10.0,
            "confidence": 0.8,
            "strategies": ["test_strat"],
        }
        result = advisor._advise_fallback(signal)
        assert result["wisdom_filtered"] is True

    def test_advise_fallback_sell_with_position(self, advisor):
        """Sell signal with position should generate sell advice."""
        signal = {
            "symbol": "000001",
            "direction": "sell",
            "has_position": True,
            "price": 10.0,
            "confidence": 0.8,
            "strategies": ["test_strat"],
        }
        result = advisor._advise_fallback(signal)
        decision = result["wisdom_decision"]
        assert decision["source"] == "fallback"
        assert "stop_loss_price" in decision
        assert decision["stop_loss_price"] == 10.0  # exit price

    def test_advise_fallback_buy_has_position_add(self, advisor):
        """Buy signal with existing position should use add strategy."""
        signal = {
            "symbol": "000001",
            "direction": "buy",
            "price": 10.0,
            "confidence": 0.8,
            "has_position": True,
            "profit_pct": 0.02,  # < 5%, so normal add
            "suggested_position_ratio": 0.20,
            "strategies": ["test_strat"],
        }
        result = advisor._advise_fallback(signal)
        decision = result["wisdom_decision"]
        assert decision["position_strategy"] == "add"

    def test_advise_fallback_buy_has_position_add_confirmed(self, advisor):
        """Buy signal with profit >5% should use add_confirmed strategy."""
        signal = {
            "symbol": "000001",
            "direction": "buy",
            "price": 10.0,
            "confidence": 0.85,
            "has_position": True,
            "profit_pct": 0.08,
            "suggested_position_ratio": 0.20,
            "strategies": ["test_strat"],
        }
        result = advisor._advise_fallback(signal)
        decision = result["wisdom_decision"]
        assert decision["position_strategy"] == "add_confirmed"

    def test_advise_fallback_buy_full_position(self, advisor):
        """When remaining capacity is 0, should filter."""
        signal = {
            "symbol": "000001",
            "direction": "buy",
            "price": 10.0,
            "confidence": 0.85,
            "has_position": True,
            "current_position_ratio": 0.30,
            "suggested_position_ratio": 0.20,
            "strategies": ["test_strat"],
        }
        result = advisor._advise_fallback(signal)
        assert result["wisdom_filtered"] is True

    def test_advise_fallback_low_confidence_filter(self, advisor):
        """Confidence below threshold should be filtered."""
        signal = {
            "symbol": "000001",
            "direction": "buy",
            "price": 10.0,
            "confidence": 0.3,
            "suggested_position_ratio": 0.20,
            "strategies": ["test_strat"],
        }
        result = advisor._advise_fallback(signal)
        assert result["wisdom_filtered"] is True

    def test_advise_fallback_no_strategies_field(self, advisor):
        """Signal without strategies list should still work."""
        signal = {
            "symbol": "000001",
            "direction": "buy",
            "price": 10.0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.20,
            "strategy": "test_strat",
        }
        result = advisor._advise_fallback(signal)
        assert "wisdom_decision" in result
        assert result["wisdom_decision"]["source"] == "fallback"

    def test_get_strategy_win_rate_no_file(self, advisor):
        """When signals_history.jsonl doesn't exist, return None."""
        result = advisor._get_strategy_win_rate("test_strat")
        assert result is None

    def test_build_signal_context_buy(self, advisor):
        signal = {
            "symbol": "000001",
            "name": "平安银行",
            "direction": "buy",
            "strategy": "breakout",
            "strategies": ["breakout"],
            "reason": "突破新高",
            "price": 15.0,
            "confidence": 0.8,
            "suggested_position_ratio": 0.1,
            "has_position": True,
            "current_position_ratio": 0.05,
            "prev_close": 14.5,
            "market_context": {"data_source": "sina", "portfolio_count": 3, "cash_ratio": 0.7},
        }
        ctx = advisor._build_signal_context(signal)
        assert "平安银行" in ctx
        assert "买入" in ctx
        assert "昨收价" in ctx
        assert "sina" in ctx

    def test_build_signal_context_sell(self, advisor):
        signal = {
            "symbol": "000001",
            "direction": "sell",
            "price": 15.0,
            "stop_loss_price": 14.0,
        }
        ctx = advisor._build_signal_context(signal)
        assert "卖出" in ctx
        assert "14.00" in ctx  # stop loss price

    def test_build_data_driven_advice_buy(self, advisor):
        signal = {
            "symbol": "000001",
            "name": "平安银行",
            "price": 15.0,
            "confidence": 0.8,
            "has_position": False,
            "direction": "buy",
            "strategies": ["breakout"],
            "suggested_position_ratio": 0.05,
            "stop_loss_price": 13.8,
            "atr": 0.5,
            "stage": "bullish",
        }
        advice = advisor._build_data_driven_advice(signal, "buy")
        assert "entry_check" in advice
        assert "stop_loss" in advice
        assert "position_sizing" in advice
        assert "psychology_check" in advice

    def test_build_data_driven_advice_sell(self, advisor):
        signal = {
            "symbol": "000001",
            "name": "平安银行",
            "price": 15.0,
            "confidence": 0.8,
            "has_position": True,
            "profit_pct": 0.35,
            "direction": "sell",
            "strategies": ["breakout"],
        }
        advice = advisor._build_data_driven_advice(signal, "sell")
        assert "profit_taking" in advice
        assert "trailing_stop" in advice
        assert "psychology_check" in advice

    def test_build_data_driven_advice_with_danger_signals(self, advisor):
        """Buy advice with danger signals should include warnings."""
        signal = {
            "symbol": "000001",
            "name": "平安银行",
            "price": 15.0,
            "confidence": 0.8,
            "has_position": False,
            "direction": "buy",
            "strategies": ["breakout"],
            "suggested_position_ratio": 0.05,
            "stop_loss_price": 13.8,
            "stage": "crazy",
            "danger_signals": ["volume_price_divergence", "high_volatility"],
            "divergence": {"diverged": True},
            "money_flow": {"net_inflow": -5.0},
            "fundamental": {"pe": 25.0},
        }
        advice = advisor._build_data_driven_advice(signal, "buy")
        assert "danger_signals" in str(advice) or "危险信号" in str(advice) or "量价背离" in str(advice)

    def test_get_skill_advice_empty(self, advisor):
        result = advisor._get_skill_advice("nonexistent")
        assert result == ""

    def test_get_skill_source_empty(self, advisor):
        result = advisor._get_skill_source("nonexistent")
        assert result == ""

    def test_get_skill_source_no_frontmatter(self, advisor):
        advisor._skills["test"] = "no frontmatter here"
        result = advisor._get_skill_source("test")
        assert result == ""

    def test_get_skill_source_with_frontmatter(self, advisor):
        advisor._skills["test"] = "---\nsource_book: Test Book\ntags: [test]\n---\nContent"
        result = advisor._get_skill_source("test")
        assert result == "Test Book"

    def test_apply_llm_decision_filter_action(self, advisor):
        signal = {"symbol": "000001", "direction": "buy", "price": 10.0}
        llm_decision = {"action": "filter", "position_ratio": 0, "reason": "test filter"}
        result = advisor._apply_llm_decision(signal, llm_decision)
        assert result["wisdom_filtered"] is True
        assert result["suggested_position_ratio"] == 0.0

    def test_apply_llm_decision_hold_action(self, advisor):
        signal = {"symbol": "000001", "direction": "buy", "price": 10.0}
        llm_decision = {"action": "hold", "position_ratio": 0, "reason": "test hold"}
        result = advisor._apply_llm_decision(signal, llm_decision)
        assert result["wisdom_filtered"] is True

    def test_apply_llm_decision_buy(self, advisor):
        signal = {"symbol": "000001", "direction": "buy", "price": 10.0, "current_position_ratio": 0}
        llm_decision = {
            "action": "buy",
            "position_ratio": 0.1,
            "stop_loss_price": 9.5,
            "reason": "good signal",
        }
        result = advisor._apply_llm_decision(signal, llm_decision)
        dec = result["wisdom_decision"]
        assert dec["action"] == "buy"
        assert result["suggested_position_ratio"] > 0
        assert "stop_loss_price" in result

    def test_apply_llm_decision_position_ratio_out_of_range(self, advisor):
        """Position ratio > 1.0 should be clamped."""
        signal = {"symbol": "000001", "direction": "buy", "price": 10.0, "current_position_ratio": 0}
        llm_decision = {
            "action": "buy", "position_ratio": 5.0, "stop_loss_price": 9.5, "reason": "test",
        }
        result = advisor._apply_llm_decision(signal, llm_decision)
        # Should be clamped to max
        assert result["suggested_position_ratio"] <= 0.30

    def test_apply_llm_decision_stop_loss_too_close(self, advisor):
        """Stop loss too close to price should be adjusted."""
        signal = {"symbol": "000001", "direction": "buy", "price": 10.0, "current_position_ratio": 0}
        # stop_loss_price at 9.9, only 1% below price (< 2%)
        llm_decision = {
            "action": "buy", "position_ratio": 0.1, "stop_loss_price": 9.9, "reason": "test",
        }
        result = advisor._apply_llm_decision(signal, llm_decision)
        # The decision dict won't have stop_loss_price until later; but signal should
        assert result["stop_loss_price"] <= 9.8  # 10 * (1-0.02) = 9.8


# ==============================================================================
# wisdom/book_router.py — BookPerspectiveRouter (remaining paths)
# ==============================================================================

class TestBookPerspectiveRouter:
    """Additional tests for BookPerspectiveRouter beyond test_p0_wisdom.py."""

    @pytest.fixture
    def router(self):
        return __import__("gugu.wisdom.book_router", fromlist=["BookPerspectiveRouter"]).BookPerspectiveRouter()

    def test_available_perspectives(self, router):
        names = router.available_perspectives
        assert isinstance(names, list)
        # Should find some perspectives if books exist

    def test_select_perspectives_custom(self, router):
        """Custom perspective list should filter by loaded perspectives."""
        result = router.select_perspectives(
            scenario="entry",
            custom_perspectives=["nonexistent-perspective", "also-fake"],
        )
        assert result == []

    def test_select_perspectives_empty_custom(self, router):
        result = router.select_perspectives(
            scenario="entry",
            custom_perspectives=[],
        )
        assert isinstance(result, list)

    def test_select_perspectives_dynamic_no_volatility(self, router):
        """Without volatility, should fall back to static routing."""
        result = router.select_perspectives_dynamic(scenario="entry", direction="buy")
        assert isinstance(result, list)

    def test_select_perspectives_dynamic_high_volatility(self, router):
        """High volatility should change perspective selection."""
        result = router.select_perspectives_dynamic(
            scenario="entry", direction="buy", market_volatility=0.05
        )
        assert isinstance(result, list)

    def test_select_perspectives_dynamic_low_volatility(self, router):
        result = router.select_perspectives_dynamic(
            scenario="entry", direction="buy", market_volatility=0.005
        )
        assert isinstance(result, list)

    def test_select_perspectives_dynamic_with_signal(self, router):
        """Signal with ATR should be used to derive volatility."""
        result = router.select_perspectives_dynamic(
            scenario="entry",
            direction="buy",
            signal={"atr": 2.0, "price": 50.0},
        )
        assert isinstance(result, list)

    def test_select_perspectives_dynamic_custom(self, router):
        result = router.select_perspectives_dynamic(
            scenario="entry", custom_perspectives=["nonexistent"]
        )
        assert result == []

    def test_get_advice_empty_signal(self, router):
        result = router.get_advice({})
        assert "perspectives" in result
        assert "consensus" in result
        assert result["consensus"]["buy_pct"] >= 0

    def test_get_advice_buy_signal(self, router):
        result = router.get_advice({"direction": "buy", "confidence": 0.8})
        assert "perspectives" in result
        assert "consensus" in result

    def test_load_all_perspectives(self, router):
        """load_all_perspectives should return list of dicts with metadata."""
        all_perspectives = router.load_all_perspectives()
        assert isinstance(all_perspectives, list)
        if all_perspectives:
            p = all_perspectives[0]
            assert "name" in p
            assert "display_name" in p
            assert "category" in p
            assert "summary" in p
            assert "available" in p

    def test_get_perspective_summary_nonexistent(self, router):
        """Non-existent perspective returns empty string."""
        result = router.get_perspective_summary("does-not-exist")
        assert result == ""


class TestBookPerspectiveRouterCore:
    """Test _extract_core_wisdom and other static methods directly."""

    def test_extract_core_wisdom_beliefs_nevers_habits(self):
        """Extract beliefs section."""
        from gugu.wisdom.book_router import BookPerspectiveRouter
        router = BookPerspectiveRouter()
        content = (
            "## 核心理念\n"
            "1. first belief\n"
            "2. second belief\n"
            "3. third belief\n"
            "4. fourth belief (should be truncated)\n"
            "## 我绝不会做的事\n"
            "- never do this\n"
            "- never do that\n"
            "## 我看问题的方式\n"
            "1. first habit\n"
            "2. second habit\n"
        )
        result = router._extract_core_wisdom(content, "test-perspective")
        assert "核心理念" in result
        assert "first belief" in result
        assert "绝不会做" in result or "never do" in result
        assert "看问题的方式" in result or "habit" in result
        # Fourth belief should be truncated
        assert "...(更多信念详见完整SKILL.md)" in result

    def test_extract_core_wisdom_no_sections(self):
        """Content with no matching sections returns empty."""
        from gugu.wisdom.book_router import BookPerspectiveRouter
        router = BookPerspectiveRouter()
        content = "## Some random section\nrandom content\n"
        result = router._extract_core_wisdom(content, "test")
        assert result == ""

    def test_get_perspective_summary_with_intro(self):
        """Extract summary from '我是谁' intro section."""
        from gugu.wisdom.book_router import BookPerspectiveRouter
        content = (
            "## 我是谁\n"
            "I am a wise investor who follows value principles.\n"
            "My approach is based on Graham.\n"
            "## 核心理念\n"
            "other content\n"
        )
        router = BookPerspectiveRouter()
        router._perspectives["test"] = content
        result = router.get_perspective_summary("test")
        assert "wise investor" in result
        assert "Graham" in result


# ==============================================================================
# screener/report.py — Screener report formatting
# ==============================================================================

class TestScreenerReport:
    """format_screener_report and format_range_watchlist."""

    @patch("gugu.screener.report.datetime")
    def test_format_empty_results(self, mock_dt):
        from gugu.screener.report import format_screener_report

        mock_dt.now.return_value.strftime.return_value = "07-06 14:30"
        result = format_screener_report(results=[], total_scanned=5000)
        assert result["msg_type"] == "interactive"
        assert "无结果" in result["card"]["header"]["title"]["content"]
        assert result["card"]["header"]["template"] == "yellow"

    @patch("gugu.screener.report.datetime")
    def test_format_with_results(self, mock_dt):
        from gugu.screener.report import format_screener_report

        mock_dt.now.return_value.strftime.return_value = "07-06 14:30"

        # Create mock ScreeningResult objects
        MockResult = MagicMock()
        MockResult.passed = True
        MockResult.name = "贵州茅台"
        MockResult.symbol = "600519"
        MockResult.change_pct = 2.5
        MockResult.price = 1500.0
        MockResult.volume_ratio = 1.2
        MockResult.turnover_pct = 0.5
        MockResult.mcap_billion = 20000
        MockResult.volume_trend = "📈"
        MockResult.beats_index = True
        MockResult.late_session_high = False
        MockResult.risk_reward_ratio = 3.0
        MockResult.pass_count = 5
        MockResult.failed_conditions = []
        MockResult.entry_low = 1480.0
        MockResult.entry_high = 1490.0
        MockResult.stop_loss_price = 1450.0
        MockResult.take_profit_price = 1600.0
        MockResult.suggested_position = 0.1

        MockResult2 = MagicMock()
        MockResult2.passed = False
        MockResult2.name = "五粮液"
        MockResult2.symbol = "000858"
        MockResult2.change_pct = 1.0
        MockResult2.price = 150.0
        MockResult2.volume_ratio = 0.8
        MockResult2.turnover_pct = 0.3
        MockResult2.mcap_billion = 6000
        MockResult2.volume_trend = "📊"
        MockResult2.beats_index = False
        MockResult2.late_session_high = True
        MockResult2.risk_reward_ratio = 1.5
        MockResult2.pass_count = 3
        MockResult2.failed_conditions = ["量能不足"]
        MockResult2.entry_low = 148.0
        MockResult2.entry_high = 149.0
        MockResult2.stop_loss_price = 145.0
        MockResult2.take_profit_price = 160.0
        MockResult2.suggested_position = 0.05

        results = [MockResult, MockResult2]
        result = format_screener_report(results=results, total_scanned=5000)

        assert result["msg_type"] == "interactive"
        card = result["card"]
        assert "尾盘选股" in card["header"]["title"]["content"]
        assert card["header"]["template"] == "green"

    @patch("gugu.screener.report.datetime")
    def test_format_range_watchlist_empty(self, mock_dt):
        from gugu.screener.report import format_range_watchlist

        mock_dt.now.return_value.strftime.return_value = "07-06 11:00"
        result = format_range_watchlist(candidates=[], market_regime="range")
        assert result["msg_type"] == "interactive"
        assert "暂无接近买点的股票" in str(result)

    @patch("gugu.screener.report.datetime")
    def test_format_range_watchlist_with_candidates(self, mock_dt):
        from gugu.screener.report import format_range_watchlist

        mock_dt.now.return_value.strftime.return_value = "07-06 11:00"
        candidates = [
            {"symbol": "600519", "name": "贵州茅台", "price": 1500.0, "ma20": 1490.0, "distance_pct": 0.67},
            {"symbol": "000858", "name": "五粮液", "price": 150.0, "ma20": 155.0, "distance_pct": -3.33},
        ]
        result = format_range_watchlist(candidates=candidates, market_regime="range")
        assert result["msg_type"] == "interactive"
        card_str = str(result)
        assert "600519" in card_str or "贵州茅台" in card_str
        assert "000858" in card_str or "五粮液" in card_str


# ==============================================================================
# edge case: empty/none handling
# ==============================================================================

class TestEdgeCases:
    """Edge case handling for robust coverage."""

    def test_advisor_empty_signal(self):
        """Empty signal should not crash advise_fallback."""
        with (
            patch("gugu.wisdom.advisor.get_config") as mock_cfg,
            patch("gugu.wisdom.advisor.env") as mock_env,
        ):
            cfg = MagicMock()
            cfg.wisdom.skill_names = []
            cfg.wisdom.skill_dir = None
            cfg.wisdom.fallback_dirs = []
            cfg.wisdom.enable_multi_perspective = False
            cfg.risk.max_position_ratio = 0.30
            mock_cfg.return_value = cfg
            env_cfg = MagicMock()
            env_cfg.llm_api_key = ""
            env_cfg.llm_base_url = ""
            env_cfg.llm_model = ""
            mock_env.return_value = env_cfg
            advisor = WisdomAdvisor()
            advisor._llm_available = False

            # Empty signal should not crash
            result = advisor.advise({})
            assert "wisdom" in result
            assert "wisdom_decision" in result
