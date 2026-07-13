"""筹码画像评分单元测试。

覆盖场景：
- 避雷针 → 0.0
- 潜伏者 → 高分
- 数据缺失 → 0.5（中性）
- 中性场景 → 中等分数
- 散户化 → 低分
- 流通市值分档评分
"""

from __future__ import annotations

import pytest

from gugu.analysis.stock_ranker import StockRanker


def _make_hc_result(
    label: str = "中性",
    holder_num_pct: float = 0.0,
    avg_hold_change: float = 0.0,
    org_hold_pct: float = 0.0,
    data_missing: bool = False,
) -> dict:
    """构造 HolderConcentrationFilter.check() 的返回结果。"""
    return {
        "pass": True,
        "label": label,
        "holder_num_pct": holder_num_pct,
        "avg_hold_change": avg_hold_change,
        "org_hold_pct": org_hold_pct,
        "data_missing": data_missing,
        "holder_num": 50000.0,
        "end_date": "2026-06-30",
        "reasons": [],
    }


class TestChipScoreLightningRod:
    """避雷针场景 → chip_score = 0.0。"""

    def test_lightning_rod_returns_zero(self):
        """避雷针（户数激增>30%）应返回 0.0。"""
        ranker = StockRanker()
        hc = _make_hc_result(label="避雷针", holder_num_pct=35.0)
        score = ranker._compute_chip_score(hc, circ_mv=100.0)
        assert score == 0.0

    def test_lightning_rod_with_high_org_pct_still_zero(self):
        """即使机构占比高，避雷针仍返回 0.0。"""
        ranker = StockRanker()
        hc = _make_hc_result(label="避雷针", holder_num_pct=50.0, org_hold_pct=60.0)
        score = ranker._compute_chip_score(hc, circ_mv=100.0)
        assert score == 0.0


class TestChipScoreDataMissing:
    """数据缺失 → chip_score = 0.5（中性）。"""

    def test_data_missing_returns_neutral(self):
        """数据缺失应返回中性分 0.5。"""
        ranker = StockRanker()
        hc = _make_hc_result(data_missing=True)
        score = ranker._compute_chip_score(hc, circ_mv=100.0)
        assert score == 0.5


class TestChipScoreLurker:
    """潜伏者场景 → 高分。"""

    def test_lurker_full_score(self):
        """潜伏者（户数连降>20% + 户均+20% + 机构50% + 流通市值100亿）应得高分。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="潜伏者",
            holder_num_pct=-25.0,  # 户数下降>20% → 0.4
            avg_hold_change=20.0,  # 户均+20% → 0.3
            org_hold_pct=50.0,  # 机构>40% → 0.2
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)  # 50-500亿 → 0.1
        assert score == pytest.approx(1.0, abs=0.01)

    def test_lurker_partial_score(self):
        """潜伏者（户数-10% + 户均+10% + 机构25% + 流通市值100亿）应得中等高分。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="潜伏者",
            holder_num_pct=-10.0,  # 单季下降>5% → 0.3
            avg_hold_change=10.0,  # 上升5-15% → 0.2
            org_hold_pct=25.0,  # 20-40% → 0.15
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)  # 50-500亿 → 0.1
        # 0.3 + 0.2 + 0.15 + 0.1 = 0.75
        assert score == pytest.approx(0.75, abs=0.01)


class TestChipScoreNeutral:
    """中性场景 → 中等分数。"""

    def test_neutral_stable(self):
        """中性稳定（户数-3% + 户均+2% + 机构15% + 100亿）应得中等分。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=-3.0,  # ±5% → 0.2
            avg_hold_change=2.0,  # ±5% → 0.15
            org_hold_pct=15.0,  # 10-20% → 0.1
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)  # 50-500亿 → 0.1
        # 0.2 + 0.15 + 0.1 + 0.1 = 0.55
        assert score == pytest.approx(0.55, abs=0.01)


class TestChipScoreRetailDominated:
    """散户化场景 → 低分。"""

    def test_retail_dominated_low_score(self):
        """散户化（户数+25% + 户均-25% + 机构5% + 100亿）应得极低分。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=25.0,  # 连续上升>20% → 0.0
            avg_hold_change=-25.0,  # 下降>20% → 0.0
            org_hold_pct=5.0,  # <10% → 0.05
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)  # 50-500亿 → 0.1
        # 0.0 + 0.0 + 0.05 + 0.1 = 0.15
        assert score == pytest.approx(0.15, abs=0.01)


class TestChipScoreMarketCap:
    """流通市值分档评分。"""

    def test_small_cap_low_score(self):
        """小盘股（30亿）流通市值评分 0.03。"""
        ranker = StockRanker()
        hc = _make_hc_result(data_missing=True)  # 中性0.5 + 市值调整
        score = ranker._compute_chip_score(hc, circ_mv=30.0)
        assert score == 0.5  # 数据缺失直接返回0.5，不看市值

    def test_mid_cap_full_score(self):
        """中盘股（100亿）流通市值评分 0.1。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=0.0,
            avg_hold_change=0.0,
            org_hold_pct=0.0,  # 数据缺失 → 0.1
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)
        # 0.2 + 0.15 + 0.1 + 0.1 = 0.55
        assert score == pytest.approx(0.55, abs=0.01)

    def test_large_cap_half_score(self):
        """大盘股（1000亿）流通市值评分 0.05。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=0.0,
            avg_hold_change=0.0,
            org_hold_pct=0.0,
        )
        score = ranker._compute_chip_score(hc, circ_mv=1000.0)
        # 0.2 + 0.15 + 0.1 + 0.05 = 0.5
        assert score == pytest.approx(0.5, abs=0.01)

    def test_mega_cap_minimal_score(self):
        """超大盘（3000亿）流通市值评分 0.03。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=0.0,
            avg_hold_change=0.0,
            org_hold_pct=0.0,
        )
        score = ranker._compute_chip_score(hc, circ_mv=3000.0)
        # 0.2 + 0.15 + 0.1 + 0.03 = 0.48
        assert score == pytest.approx(0.48, abs=0.01)


class TestChipScoreRange:
    """评分范围验证。"""

    def test_score_always_in_0_1(self):
        """所有场景评分应在 [0, 1] 区间。"""
        ranker = StockRanker()
        scenarios = [
            _make_hc_result(label="避雷针", holder_num_pct=100.0),
            _make_hc_result(data_missing=True),
            _make_hc_result(
                label="潜伏者", holder_num_pct=-30.0, avg_hold_change=30.0, org_hold_pct=80.0
            ),
            _make_hc_result(
                label="中性", holder_num_pct=0.0, avg_hold_change=0.0, org_hold_pct=0.0
            ),
            _make_hc_result(
                label="中性", holder_num_pct=50.0, avg_hold_change=-30.0, org_hold_pct=0.0
            ),
        ]
        for hc in scenarios:
            score = ranker._compute_chip_score(hc, circ_mv=100.0)
            assert 0.0 <= score <= 1.0, f"评分越界: {score}"
