"""冲突裁决器单元测试。

覆盖：多维权重计算、加权融合、冲突检测、冷启动、降级、历史追踪。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gugu.wisdom.conflict_resolver import (
    COLD_START_MIN,
    CONFLICT_DISPERSION_THRESHOLD,
    ConflictResolver,
    PerspectiveOpinion,
    Resolution,
    record_opinion,
)


class TestScenarioFitness:
    """场景适配度测试。"""

    def test_first_category_gets_highest_weight(self):
        resolver = ConflictResolver()
        # entry 场景映射: ["trading_system", "technical_analysis", "value_investing"]
        assert resolver._scenario_fitness("trading_system", "entry") == 1.0
        assert resolver._scenario_fitness("technical_analysis", "entry") == 0.8
        assert resolver._scenario_fitness("value_investing", "entry") == 0.6

    def test_unmapped_category(self):
        resolver = ConflictResolver()
        # macro_economics 不在 entry 映射中
        assert resolver._scenario_fitness("macro_economics", "entry") == 0.5

    def test_unknown_scenario(self):
        resolver = ConflictResolver()
        # 未知场景 fallback 到 trading_system
        assert resolver._scenario_fitness("trading_system", "unknown_scenario") == 1.0


class TestTemporalFitness:
    """时效性权重测试。"""

    def test_high_volatility_favors_technical(self):
        resolver = ConflictResolver()
        assert resolver._temporal_fitness("technical_analysis", 0.05) == 1.2
        assert resolver._temporal_fitness("trading_system", 0.05) == 1.2
        assert resolver._temporal_fitness("value_investing", 0.05) == 0.8

    def test_low_volatility_favors_value(self):
        resolver = ConflictResolver()
        assert resolver._temporal_fitness("value_investing", 0.005) == 1.2
        assert resolver._temporal_fitness("macro_economics", 0.005) == 1.2
        assert resolver._temporal_fitness("technical_analysis", 0.005) == 0.8

    def test_normal_volatility_neutral(self):
        resolver = ConflictResolver()
        assert resolver._temporal_fitness("value_investing", 0.02) == 1.0
        assert resolver._temporal_fitness("technical_analysis", 0.02) == 1.0

    def test_none_volatility(self):
        resolver = ConflictResolver()
        assert resolver._temporal_fitness("value_investing", None) == 1.0


class TestHistoricalWinrate:
    """历史胜率测试。"""

    def test_cold_start_returns_none(self, tmp_path):
        with patch("gugu.wisdom.conflict_resolver.TRACK_FILE", tmp_path / "track.jsonl"):
            resolver = ConflictResolver()
            resolver._winrate_cache = {}
            # 无记录文件
            assert resolver._historical_winrate("jesse-livermore-perspective") is None

    def test_insufficient_records_returns_none(self, tmp_path):
        track_file = tmp_path / "track.jsonl"
        with track_file.open("w") as f:
            for _ in range(COLD_START_MIN - 1):
                f.write(json.dumps({"perspective": "test-p", "correct": True}) + "\n")
        with patch("gugu.wisdom.conflict_resolver.TRACK_FILE", track_file):
            resolver = ConflictResolver()
            resolver._winrate_cache = {}
            assert resolver._historical_winrate("test-p") is None

    def test_sufficient_records_returns_weight(self, tmp_path):
        track_file = tmp_path / "track.jsonl"
        # 5 记录，4 正确 → 胜率 0.8 → 权重 1.3
        records = [
            {"perspective": "test-p", "correct": True},
            {"perspective": "test-p", "correct": True},
            {"perspective": "test-p", "correct": True},
            {"perspective": "test-p", "correct": True},
            {"perspective": "test-p", "correct": False},
        ]
        with track_file.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        with patch("gugu.wisdom.conflict_resolver.TRACK_FILE", track_file):
            resolver = ConflictResolver()
            resolver._winrate_cache = {}
            winrate = resolver._historical_winrate("test-p")
            assert winrate is not None
            assert 0.5 <= winrate <= 1.5
            # 4/5 = 0.8 → 0.5 + 0.8 = 1.3
            assert winrate == 1.3


class TestFuse:
    """加权融合测试。"""

    def test_unanimous_action_no_conflict(self):
        resolver = ConflictResolver()
        opinions = [
            PerspectiveOpinion("p1", "trading_system", "buy", 0.1, 10.0, 0.8, "r1"),
            PerspectiveOpinion("p2", "technical_analysis", "buy", 0.15, 10.5, 0.7, "r2"),
            PerspectiveOpinion("p3", "value_investing", "buy", 0.08, 9.8, 0.9, "r3"),
        ]
        weights = {"p1": 0.4, "p2": 0.35, "p3": 0.25}
        resolution = resolver._fuse(opinions, weights)
        assert resolution.final_action == "buy"
        assert not resolution.is_conflict
        assert resolution.dispersion == 0.0
        assert len(resolution.dissenting_views) == 0

    def test_conflict_detected(self):
        resolver = ConflictResolver()
        opinions = [
            PerspectiveOpinion("p1", "trading_system", "buy", 0.1, 10.0, 0.8, "r1"),
            PerspectiveOpinion("p2", "value_investing", "filter", 0.0, None, 0.6, "r2"),
            PerspectiveOpinion("p3", "investment_philosophy", "sell", 0.0, None, 0.5, "r3"),
        ]
        weights = {"p1": 0.4, "p2": 0.35, "p3": 0.25}
        resolution = resolver._fuse(opinions, weights)
        # buy 权重 0.4，total=1.0，dispersion = 1 - 0.4 = 0.6 > 0.3
        assert resolution.is_conflict
        assert resolution.dispersion > CONFLICT_DISPERSION_THRESHOLD
        assert len(resolution.dissenting_views) == 2

    def test_weighted_majority_action(self):
        resolver = ConflictResolver()
        opinions = [
            PerspectiveOpinion("p1", "trading_system", "buy", 0.1, 10.0, 0.8, "r1"),
            PerspectiveOpinion("p2", "technical_analysis", "buy", 0.1, 10.0, 0.7, "r2"),
            PerspectiveOpinion("p3", "value_investing", "hold", 0.0, None, 0.5, "r3"),
        ]
        # buy 权重 0.7, hold 权重 0.3
        weights = {"p1": 0.4, "p2": 0.3, "p3": 0.3}
        resolution = resolver._fuse(opinions, weights)
        assert resolution.final_action == "buy"

    def test_stop_loss_takes_strictest(self):
        resolver = ConflictResolver()
        opinions = [
            PerspectiveOpinion("p1", "trading_system", "buy", 0.1, 10.5, 0.8, "r1"),
            PerspectiveOpinion("p2", "technical_analysis", "buy", 0.1, 9.8, 0.7, "r2"),
            PerspectiveOpinion("p3", "value_investing", "buy", 0.1, 10.2, 0.9, "r3"),
        ]
        weights = {"p1": 0.4, "p2": 0.35, "p3": 0.25}
        resolution = resolver._fuse(opinions, weights)
        # 最严格 = 最低价 = 9.8
        assert resolution.final_stop_loss_price == 9.8

    def test_position_weighted_average(self):
        resolver = ConflictResolver()
        opinions = [
            PerspectiveOpinion("p1", "trading_system", "buy", 0.10, 10.0, 0.8, "r1"),
            PerspectiveOpinion("p2", "technical_analysis", "buy", 0.20, 10.0, 0.7, "r2"),
        ]
        weights = {"p1": 0.5, "p2": 0.5}
        resolution = resolver._fuse(opinions, weights)
        # (0.10 * 0.5 + 0.20 * 0.5) = 0.15
        assert abs(resolution.final_position_ratio - 0.15) < 0.001

    def test_position_capped_at_30pct(self):
        resolver = ConflictResolver()
        opinions = [
            PerspectiveOpinion("p1", "trading_system", "buy", 0.4, 10.0, 0.8, "r1"),
            PerspectiveOpinion("p2", "technical_analysis", "buy", 0.5, 10.0, 0.7, "r2"),
        ]
        weights = {"p1": 0.5, "p2": 0.5}
        resolution = resolver._fuse(opinions, weights)
        assert resolution.final_position_ratio <= 0.3

    def test_empty_opinions(self):
        resolver = ConflictResolver()
        resolution = resolver._fuse([], {})
        assert resolution.final_action == "hold"
        assert resolution.final_position_ratio == 0.0


class TestComputeWeights:
    """权重计算集成测试。"""

    def test_weights_normalized(self):
        resolver = ConflictResolver()
        resolver._winrate_cache = {}
        opinions = [
            PerspectiveOpinion("p1", "trading_system", "buy", 0.1, 10.0, 0.8, "r1"),
            PerspectiveOpinion("p2", "technical_analysis", "buy", 0.1, 10.0, 0.7, "r2"),
        ]
        from gugu.models.user_profile import UserProfile

        profile = UserProfile()
        with patch("gugu.wisdom.conflict_resolver.TRACK_FILE", Path("/nonexistent")):
            weights = resolver._compute_weights(opinions, "entry", profile, 0.02)
        # 权重之和应≈1.0
        assert abs(sum(weights.values()) - 1.0) < 0.01
        # entry 场景中 trading_system 是第1类，应比 technical_analysis（第2类）权重高
        assert weights["p1"] >= weights["p2"]


class TestResolveConflict:
    """完整流程测试。"""

    @pytest.mark.asyncio
    async def test_llm_unavailable_uses_fallback(self):
        resolver = ConflictResolver()
        resolver._llm_available = False
        # mock router
        resolver._router = MagicMock()
        resolver._router.select_perspectives_dynamic.return_value = [
            "jesse-livermore-perspective",
            "chen-jiang-ting-perspective",
        ]
        resolver._router._perspectives = {}
        resolver._router._extract_core_wisdom.return_value = ""

        signal = {"symbol": "600519", "name": "茅台", "direction": "buy", "price": 1500.0}
        with patch("gugu.wisdom.conflict_resolver.TRACK_FILE", Path("/nonexistent")):
            resolution = await resolver.resolve_conflict("entry", signal, direction="buy")

        assert resolution is not None
        assert len(resolution.opinions) == 2
        # 降级时所有视角与信号方向一致
        assert all(o.action == "buy" for o in resolution.opinions)

    @pytest.mark.asyncio
    async def test_llm_success(self):
        resolver = ConflictResolver()
        resolver._llm_available = True
        resolver._llm_cfg = MagicMock(
            llm_base_url="http://test", llm_api_key="key", llm_model="m"
        )
        # mock router
        resolver._router = MagicMock()
        resolver._router.select_perspectives_dynamic.return_value = [
            "jesse-livermore-perspective",
        ]
        resolver._router._perspectives = {"jesse-livermore-perspective": "content"}
        resolver._router._extract_core_wisdom.return_value = "extracted wisdom"

        # mock LLM 响应
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "opinions": [
                                    {
                                        "perspective": "jesse-livermore-perspective",
                                        "action": "buy",
                                        "position_ratio": 0.1,
                                        "stop_loss_price": 1450.0,
                                        "confidence": 0.8,
                                        "reason": "关键点确认",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        resolver._async_client = mock_client

        signal = {"symbol": "600519", "name": "茅台", "direction": "buy", "price": 1500.0}
        with patch("gugu.wisdom.conflict_resolver.TRACK_FILE", Path("/nonexistent")):
            resolution = await resolver.resolve_conflict("entry", signal, direction="buy")

        assert resolution is not None
        assert len(resolution.opinions) == 1
        assert resolution.opinions[0].action == "buy"
        assert resolution.final_action == "buy"

    @pytest.mark.asyncio
    async def test_no_perspectives_returns_none(self):
        resolver = ConflictResolver()
        resolver._router = MagicMock()
        resolver._router.select_perspectives_dynamic.return_value = []

        result = await resolver.resolve_conflict("entry", {}, direction="buy")
        assert result is None


class TestRecordOpinion:
    """历史追踪记录测试。"""

    def test_record_appended(self, tmp_path):
        track_file = tmp_path / "track.jsonl"
        with patch("gugu.wisdom.conflict_resolver.TRACK_FILE", track_file):
            record_opinion("test-p", "buy", "600519", True, "entry")
            record_opinion("test-p", "buy", "600519", False, "entry")

        lines = track_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        r1 = json.loads(lines[0])
        assert r1["perspective"] == "test-p"
        assert r1["action"] == "buy"
        assert r1["correct"] is True


class TestResolutionDataclass:
    """Resolution 数据类测试。"""

    def test_to_dict(self):
        resolution = Resolution(
            final_action="buy",
            final_position_ratio=0.15,
            final_stop_loss_price=1450.0,
            is_conflict=False,
            dispersion=0.2,
            opinions=[
                PerspectiveOpinion("p1", "trading_system", "buy", 0.1, 1450.0, 0.8, "r1")
            ],
            weights={"p1": 1.0},
            action_scores={"buy": 1.0},
            dissenting_views=[],
        )
        d = resolution.to_dict()
        assert d["final_action"] == "buy"
        assert d["final_position_ratio"] == 0.15
        assert len(d["opinions"]) == 1
        assert d["weights"]["p1"] == 1.0
