"""单元测试：行业景气度评分器（P1-6）。

覆盖 IndustryProsperityChecker 的评分算法、匹配策略、阈值判断和数据缺失处理。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from gugu.filters.fundamental import IndustryProsperityChecker


# ============================================================
# 辅助函数
# ============================================================


def _make_board_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """生成模拟的 akshare 行业板块 DataFrame。"""
    return pd.DataFrame(rows)


def _make_hot_board() -> pd.DataFrame:
    """生成强势行业板块（高景气度）。"""
    return _make_board_df(
        [
            {
                "板块名称": "电子",
                "板块涨跌幅": 3.5,
                "主力净流入": 15e8,  # 15 亿
                "换手率": 3.0,
                "量比": 1.8,
            },
        ]
    )


def _make_cold_board() -> pd.DataFrame:
    """生成衰退行业板块（低景气度）。"""
    return _make_board_df(
        [
            {
                "板块名称": "房地产",
                "板块涨跌幅": -4.2,
                "主力净流入": -12e8,  # -12 亿
                "换手率": 0.5,
                "量比": 0.3,
            },
        ]
    )


def _make_neutral_board() -> pd.DataFrame:
    """生成中等景气度板块。"""
    return _make_board_df(
        [
            {
                "板块名称": "银行",
                "板块涨跌幅": 0.3,
                "主力净流入": 0.5e8,  # 0.5 亿
                "换手率": 1.0,
                "量比": 0.9,
            },
        ]
    )


# ============================================================
# TestIndustryProsperityChecker 评分算法
# ============================================================


class TestIndustryProsperityScoring:
    """评分算法单元测试。"""

    def test_linear_map_midpoint(self) -> None:
        """value=0 时（中点）应得 max_score/2。"""
        score = IndustryProsperityChecker._linear_map(0.0, 5.0, 40.0)
        assert score == pytest.approx(20.0, abs=0.01)

    def test_linear_map_positive_max(self) -> None:
        """value=+range 时应得 max_score。"""
        score = IndustryProsperityChecker._linear_map(5.0, 5.0, 40.0)
        assert score == pytest.approx(40.0, abs=0.01)

    def test_linear_map_negative_min(self) -> None:
        """value=-range 时应得 0。"""
        score = IndustryProsperityChecker._linear_map(-5.0, 5.0, 40.0)
        assert score == pytest.approx(0.0, abs=0.01)

    def test_linear_map_clamp_high(self) -> None:
        """value 超过 range 应截断到 max_score。"""
        score = IndustryProsperityChecker._linear_map(100.0, 5.0, 40.0)
        assert score == pytest.approx(40.0, abs=0.01)

    def test_linear_map_clamp_low(self) -> None:
        """value 低于 -range 应截断到 0。"""
        score = IndustryProsperityChecker._linear_map(-100.0, 5.0, 40.0)
        assert score == pytest.approx(0.0, abs=0.01)


# ============================================================
# TestIndustryProsperityChecker 匹配策略
# ============================================================


class TestIndustryProsperityMatching:
    """行业匹配策略测试。"""

    def test_exact_match(self) -> None:
        """精确匹配「板块名称」。"""
        df = _make_board_df(
            [
                {
                    "板块名称": "半导体",
                    "板块涨跌幅": 2.0,
                    "主力净流入": 5e8,
                    "换手率": 2.0,
                    "量比": 1.5,
                },
            ]
        )
        row = IndustryProsperityChecker._match_industry_row(df, "半导体")
        assert row is not None
        assert row["板块名称"] == "半导体"

    def test_contains_match(self) -> None:
        """包含匹配：「半导体」匹配「半导体及元件」。"""
        df = _make_board_df(
            [
                {
                    "板块名称": "半导体及元件",
                    "板块涨跌幅": 2.0,
                    "主力净流入": 5e8,
                    "换手率": 2.0,
                    "量比": 1.5,
                },
            ]
        )
        row = IndustryProsperityChecker._match_industry_row(df, "半导体")
        assert row is not None
        assert "半导体" in row["板块名称"]

    def test_no_match(self) -> None:
        """无匹配行业返回 None。"""
        df = _make_board_df(
            [
                {
                    "板块名称": "银行",
                    "板块涨跌幅": 0.5,
                    "主力净流入": 1e8,
                    "换手率": 1.0,
                    "量比": 0.8,
                },
            ]
        )
        row = IndustryProsperityChecker._match_industry_row(df, "航天")
        assert row is None

    def test_missing_column(self) -> None:
        """缺少「板块名称」列返回 None。"""
        df = pd.DataFrame({"name": ["银行"], "pct": [0.5]})
        row = IndustryProsperityChecker._match_industry_row(df, "银行")
        assert row is None


# ============================================================
# TestIndustryProsperityChecker 完整流程
# ============================================================


class TestIndustryProsperityScore:
    """完整评分流程测试。"""

    @patch("gugu.filters.fundamental.ak.stock_board_industry_name_em")
    def test_high_prosperity_industry(self, mock_board: MagicMock) -> None:
        """高景气度行业得分 >= 50。"""
        mock_board.return_value = _make_hot_board()
        checker = IndustryProsperityChecker()
        result = checker.score("电子")

        assert result["score"] is not None
        assert result["score"] >= 50, f"强势行业得分应 >= 50, got {result['score']}"
        assert "良好" in result["reason"]
        assert result["pct_change"] == 3.5
        assert result["main_net_billion"] == 15.0

    @patch("gugu.filters.fundamental.ak.stock_board_industry_name_em")
    def test_low_prosperity_industry(self, mock_board: MagicMock) -> None:
        """低景气度行业得分 < 30。"""
        mock_board.return_value = _make_cold_board()
        checker = IndustryProsperityChecker()
        result = checker.score("房地产")

        assert result["score"] is not None
        assert result["score"] < 30, f"衰退行业得分应 < 30, got {result['score']}"
        assert "偏低" in result["reason"] or "极低" in result["reason"]
        assert result["pct_change"] == -4.2
        assert result["main_net_billion"] == -12.0

    @patch("gugu.filters.fundamental.ak.stock_board_industry_name_em")
    def test_neutral_prosperity_industry(self, mock_board: MagicMock) -> None:
        """中等景气度行业得分在 30-50 之间。"""
        mock_board.return_value = _make_neutral_board()
        checker = IndustryProsperityChecker()
        result = checker.score("银行")

        assert result["score"] is not None
        assert 25 <= result["score"] <= 55, f"中等景气度得分应在 25-55, got {result['score']}"
        assert "良好" in result["reason"] or "偏低" in result["reason"]

    @patch("gugu.filters.fundamental.ak.stock_board_industry_name_em")
    def test_extreme_low_prosperity_hard_fail(self, mock_board: MagicMock) -> None:
        """极端低景气度（< 10）应触发硬拦截判定。"""
        mock_board.return_value = _make_board_df(
            [
                {
                    "板块名称": "衰退行业",
                    "板块涨跌幅": -5.0,  # 满分下限
                    "主力净流入": -10e8,  # 满分下限
                    "换手率": 0.0,
                    "量比": 0.0,
                },
            ]
        )
        checker = IndustryProsperityChecker()
        result = checker.score("衰退行业")

        assert result["score"] is not None
        assert result["score"] < checker._HARD_FAIL_THRESHOLD, (
            f"极端衰退行业得分应 < {checker._HARD_FAIL_THRESHOLD}, got {result['score']}"
        )
        assert "极低" in result["reason"]
        assert "行业衰退" in result["reason"]


# ============================================================
# TestIndustryProsperityChecker 数据缺失处理
# ============================================================


class TestIndustryProsperityMissingData:
    """数据缺失场景测试。"""

    def test_empty_industry_name(self) -> None:
        """行业名称为空 → 返回空结果。"""
        checker = IndustryProsperityChecker()
        result = checker.score("")
        assert result["score"] is None
        assert "为空" in result["reason"]

    @patch("gugu.filters.fundamental.ak.stock_board_industry_name_em")
    def test_api_failure(self, mock_board: MagicMock) -> None:
        """API 异常 → 返回空结果，不抛异常。"""
        mock_board.side_effect = Exception("API error")
        checker = IndustryProsperityChecker()
        result = checker.score("银行")

        assert result["score"] is None
        assert "获取失败" in result["reason"]

    @patch("gugu.filters.fundamental.ak.stock_board_industry_name_em")
    def test_empty_dataframe(self, mock_board: MagicMock) -> None:
        """返回空 DataFrame → 返回空结果。"""
        mock_board.return_value = pd.DataFrame()
        checker = IndustryProsperityChecker()
        result = checker.score("银行")

        assert result["score"] is None
        assert "获取失败" in result["reason"]

    @patch("gugu.filters.fundamental.ak.stock_board_industry_name_em")
    def test_industry_not_in_board(self, mock_board: MagicMock) -> None:
        """行业不在板块数据中 → 返回空结果。"""
        mock_board.return_value = _make_board_df(
            [
                {
                    "板块名称": "银行",
                    "板块涨跌幅": 0.5,
                    "主力净流入": 1e8,
                    "换手率": 1.0,
                    "量比": 0.8,
                },
            ]
        )
        checker = IndustryProsperityChecker()
        result = checker.score("航天")

        assert result["score"] is None
        assert "未匹配" in result["reason"]


# ============================================================
# TestIndustryProsperityChecker 缓存
# ============================================================


class TestIndustryProsperityCache:
    """日内缓存测试。"""

    @patch("gugu.filters.fundamental.ak.stock_board_industry_name_em")
    def test_cache_reuse_same_day(self, mock_board: MagicMock) -> None:
        """同一日内多次调用复用缓存，只请求一次 API。"""
        mock_board.return_value = _make_hot_board()
        checker = IndustryProsperityChecker()

        # 第一次调用
        checker.score("电子")
        # 第二次调用（应命中缓存）
        checker.score("电子")

        # API 只应被调用一次
        assert mock_board.call_count == 1, (
            f"缓存命中时 API 应只调用 1 次，实际 {mock_board.call_count} 次"
        )
