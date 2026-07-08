"""十大视角分析系统单元测试。

测试范围：
1. StockDataContext — 数据模型构建
2. 10个评分函数 — 各视角独立评分
3. PerspectiveEngine — 核心引擎聚合
4. integrators — 上下文构建和信号集成
5. perspective_report — 飞书卡片生成

所有测试使用合成/模拟数据，不依赖网络或数据库。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.perspectives import (
    ALL_PERSPECTIVES,
    COMPOSITE_PASS_THRESHOLD,
    DEFAULT_WEIGHTS,
    MIN_PERSPECTIVE_SCORE,
    PASS_THRESHOLD,
    PerspectiveEngine,
    PerspectiveScore,
    StockDataContext,
    attach_perspective_to_signal,
    build_context_from_kline,
    build_context_from_signal,
    filter_by_perspective,
    SCORERS,
)
from gugu.analysis.perspectives.data_types import PerspectiveAnalysisResult


# =========================================================================
# 辅助工厂函数
# =========================================================================


def make_uptrend_kline(days: int = 120, base: float = 50.0) -> pd.DataFrame:
    """构造上涨趋势K线数据。"""
    np.random.seed(42)
    trend = np.linspace(0, 50, days)
    noise = np.random.randn(days) * 0.5
    close = base + trend + noise
    high = close + np.abs(np.random.randn(days)) * 0.3 + 0.1
    low = close - np.abs(np.random.randn(days)) * 0.3 - 0.1
    volume = np.random.randint(1_000_000, 5_000_000, size=days)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=days, freq="B"),
        "open": low + 0.1,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


def make_downtrend_kline(days: int = 120, base: float = 100.0) -> pd.DataFrame:
    """构造下跌趋势K线数据。"""
    np.random.seed(42)
    trend = np.linspace(0, -40, days)
    noise = np.random.randn(days) * 0.5
    close = base + trend + noise
    high = close + np.abs(np.random.randn(days)) * 0.3 + 0.1
    low = close - np.abs(np.random.randn(days)) * 0.3 - 0.1
    volume = np.random.randint(1_000_000, 5_000_000, size=days)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=days, freq="B"),
        "open": low + 0.1,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


def make_excellent_context() -> StockDataContext:
    """构建一个优秀公司的数据上下文（应该高分）。"""
    return StockDataContext(
        symbol="600519",
        name="贵州茅台",
        price=1800.0,
        change_pct=0.5,
        volume_ratio=0.8,
        turnover_pct=0.5,
        mcap_billion=22000,
        pe_static=30.0,
        pe_dynamic=28.0,
        pb=8.0,
        roe=30.0,
        revenue_growth=18.0,
        profit_growth=20.0,
        ma5=1790,
        ma10=1780,
        ma20=1770,
        ma60=1700,
        ma120=1650,
        ma250=1600,
        rsi14=55.0,
        macd_dif=5.0,
        macd_dea=3.0,
        macd_hist=4.0,
        bollinger_upper=1850,
        bollinger_lower=1700,
        atr14=25.0,
        high_52w=2000,
        low_52w=1500,
        distance_from_52w_high=-10.0,
        distance_from_52w_low=20.0,
        main_net_inflow=3.0,
        main_net_inflow_5d=15.0,
        market_stage="bull",
        hot_sector=True,
        sector_rotation_strength=0.7,
        debt_ratio=20.0,
        operating_cash_flow=600.0,
        free_cash_flow=400.0,
        dividend_yield=1.8,
        institutional_holding=80.0,
        insider_buying=False,
        insider_selling=False,
        industry="食品饮料",
        industry_pe_avg=35.0,
        is_monopoly=True,
        moat_type="brand",
    )


def make_terrible_context() -> StockDataContext:
    """构建一个劣质公司的数据上下文（应该低分）。"""
    return StockDataContext(
        symbol="000001",
        name="垃圾股",
        price=5.0,
        change_pct=-2.0,
        volume_ratio=2.5,
        turnover_pct=20.0,
        mcap_billion=30,
        pe_static=80.0,
        pe_dynamic=120.0,
        pb=12.0,
        roe=-5.0,
        revenue_growth=-30.0,
        profit_growth=-50.0,
        ma5=5.5,
        ma10=5.8,
        ma20=6.0,
        ma60=7.0,
        ma120=8.0,
        ma250=10.0,
        rsi14=80.0,
        macd_dif=-0.5,
        macd_dea=-0.3,
        macd_hist=-0.4,
        bollinger_upper=7.0,
        bollinger_lower=4.5,
        atr14=0.5,
        high_52w=15.0,
        low_52w=4.0,
        distance_from_52w_high=-66.7,
        distance_from_52w_low=25.0,
        main_net_inflow=-3.0,
        main_net_inflow_5d=-15.0,
        market_stage="bear",
        hot_sector=False,
        debt_ratio=85.0,
        operating_cash_flow=-10.0,
        free_cash_flow=-15.0,
        dividend_yield=0.0,
        institutional_holding=5.0,
        insider_buying=False,
        insider_selling=True,
        industry="纺织",
        industry_pe_avg=15.0,
        is_monopoly=False,
        moat_type="unknown",
    )


def make_empty_context() -> StockDataContext:
    """构建只有基本字段的上下文（测试降级和置信度）。"""
    return StockDataContext(
        symbol="000001",
        name="测试",
        price=100.0,
    )


# =========================================================================
# 测试 StockDataContext
# =========================================================================


class TestStockDataContext:
    """StockDataContext 数据模型测试。"""

    def test_creation(self):
        """测试基本创建。"""
        ctx = make_excellent_context()
        assert ctx.symbol == "600519"
        assert ctx.name == "贵州茅台"
        assert ctx.price == 1800.0

    def test_to_dict(self):
        """测试字典转换。"""
        ctx = make_excellent_context()
        d = ctx.to_dict()
        assert isinstance(d, dict)
        assert d["symbol"] == "600519"
        assert d["price"] == 1800.0
        assert "kline_data" not in d  # kline_data 不导出

    def test_empty_context(self):
        """测试空上下文。"""
        ctx = make_empty_context()
        assert ctx.symbol == "000001"
        assert ctx.ma20 == 0.0  # 默认值
        assert ctx.roe == 0.0

    def test_defaults(self):
        """测试默认值。"""
        ctx = StockDataContext(symbol="123456")
        assert ctx.pe_static == 0.0
        assert ctx.is_monopoly is False
        assert ctx.insider_buying is False


# =========================================================================
# 测试 PerspectiveScore
# =========================================================================


class TestPerspectiveScore:
    """PerspectiveScore 模型测试。"""

    def test_weighted_score(self):
        """测试加权分计算。"""
        score = PerspectiveScore(
            perspective="buffett",
            score=8.0,
            weight=0.15,
            confidence=0.9,
        )
        assert score.weighted_score == pytest.approx(8.0 * 0.15 * 0.9)

    def test_passed(self):
        """测试通过判断。"""
        s1 = PerspectiveScore(perspective="test", score=6.0, weight=0.1)
        s2 = PerspectiveScore(perspective="test", score=5.9, weight=0.1)
        assert s1.passed is True
        assert s2.passed is False

    def test_is_fatal(self):
        """测试致命缺陷判断。"""
        s1 = PerspectiveScore(perspective="test", score=3.9, weight=0.1)
        s2 = PerspectiveScore(perspective="test", score=4.0, weight=0.1)
        assert s1.is_fatal is True
        assert s2.is_fatal is False


# =========================================================================
# 测试 PerspectiveAnalysisResult
# =========================================================================


class TestPerspectiveAnalysisResult:
    """PerspectiveAnalysisResult 模型测试。"""

    def test_passed_all_good(self):
        """测试全部通过时的判定。"""
        scores = [
            PerspectiveScore(perspective=p, score=8.0, weight=0.1)
            for p in ALL_PERSPECTIVES
        ]
        result = PerspectiveAnalysisResult(
            symbol="600519",
            name="茅台",
            price=1800,
            scores=scores,
            composite_score=8.0,
            min_score=7.5,
            min_perspective="technical",
            passed_count=10,
            fatal_count=0,
        )
        assert result.passed is True

    def test_failed_fatal(self):
        """测试有致命缺陷时判定。"""
        scores = [
            PerspectiveScore(perspective=p, score=8.0, weight=0.1)
            for p in ALL_PERSPECTIVES
        ]
        # 设一个极低分
        scores[0] = PerspectiveScore(
            perspective=ALL_PERSPECTIVES[0],
            score=2.0,
            weight=0.1,
        )
        result = PerspectiveAnalysisResult(
            symbol="000001",
            name="垃圾",
            price=5,
            scores=scores,
            composite_score=7.5,
            min_score=2.0,
            min_perspective=ALL_PERSPECTIVES[0],
            passed_count=9,
            fatal_count=1,
        )
        assert result.passed is False

    def test_failed_low_composite(self):
        """测试综合分不足时判定。"""
        scores = [
            PerspectiveScore(perspective=p, score=5.0, weight=0.1)
            for p in ALL_PERSPECTIVES
        ]
        result = PerspectiveAnalysisResult(
            symbol="000001",
            name="测试",
            price=10,
            scores=scores,
            composite_score=5.0,
            min_score=5.0,
            min_perspective="buffett",
            passed_count=0,
            fatal_count=0,
        )
        assert result.passed is False

    def test_summary(self):
        """测试摘要生成。"""
        result = PerspectiveAnalysisResult(
            symbol="600519",
            name="茅台",
            price=1800,
            composite_score=8.5,
            min_score=7.0,
            min_perspective="technical",
            passed_count=10,
            fatal_count=0,
        )
        summary = result.summary
        assert "600519" in summary
        assert "8.5" in summary
        assert "通过" in summary

    def test_get_score(self):
        """测试视角分获取。"""
        scores = [
            PerspectiveScore(perspective=p, score=float(i), weight=0.1)
            for i, p in enumerate(ALL_PERSPECTIVES)
        ]
        result = PerspectiveAnalysisResult(
            symbol="test", name="test", price=10, scores=scores
        )
        s = result.get_score("buffett")
        assert s is not None
        assert s.perspective == "buffett"

    def test_get_score_missing(self):
        """测试获取不存在的视角。"""
        result = PerspectiveAnalysisResult(symbol="test", name="test", price=10)
        assert result.get_score("nonexistent") is None

    def test_to_dict(self):
        """测试字典转换。"""
        scores = [
            PerspectiveScore(perspective=p, score=7.0, weight=0.1)
            for p in ALL_PERSPECTIVES
        ]
        result = PerspectiveAnalysisResult(
            symbol="600519",
            name="茅台",
            price=1800,
            scores=scores,
            composite_score=7.0,
            min_score=7.0,
            min_perspective="buffett",
            passed_count=10,
        )
        d = result.to_dict()
        assert d["symbol"] == "600519"
        assert d["composite_score"] == 7.0
        assert d["passed"] is True
        assert len(d["scores"]) == 10

    def test_failed_low_pass_count(self):
        """测试通过视角数不足时判定（<7个通过）。"""
        scores = []
        for i, p in enumerate(ALL_PERSPECTIVES):
            # 只有6个通过（<7）
            score_val = 7.0 if i < 6 else 4.0
            scores.append(PerspectiveScore(
                perspective=p, score=score_val, weight=0.1
            ))
        result = PerspectiveAnalysisResult(
            symbol="test",
            name="test",
            price=10,
            scores=scores,
            composite_score=6.5,
            min_score=4.0,
            min_perspective=ALL_PERSPECTIVES[9],
            passed_count=6,
            fatal_count=0,
        )
        assert result.passed is False


# =========================================================================
# 测试评分函数
# =========================================================================


class TestScorers:
    """10个评分函数的通用测试。"""

    @pytest.mark.parametrize("perspective", ALL_PERSPECTIVES)
    def test_scorer_exists(self, perspective: str):
        """测试每个视角都有对应的评分函数。"""
        assert perspective in SCORERS

    @pytest.mark.parametrize("perspective", ALL_PERSPECTIVES)
    def test_scorer_returns_valid_score(self, perspective: str):
        """测试每个评分函数返回有效的 0-10 分数。"""
        ctx = make_excellent_context()
        scorer = SCORERS[perspective]
        result = scorer(ctx)
        assert isinstance(result, PerspectiveScore)
        assert 0.0 <= result.score <= 10.0
        assert 0.0 <= result.confidence <= 1.0
        assert result.perspective == perspective
        assert result.weight > 0

    @pytest.mark.parametrize("perspective", ALL_PERSPECTIVES)
    def test_scorer_handles_empty_context(self, perspective: str):
        """测试每个评分函数能处理空上下文（不抛异常）。"""
        ctx = make_empty_context()
        scorer = SCORERS[perspective]
        result = scorer(ctx)
        assert isinstance(result, PerspectiveScore)
        assert 0.0 <= result.score <= 10.0

    @pytest.mark.parametrize("perspective", ALL_PERSPECTIVES)
    def test_scorer_has_reasoning(self, perspective: str):
        """测试每个评分函数返回推理链。"""
        ctx = make_excellent_context()
        scorer = SCORERS[perspective]
        result = scorer(ctx)
        assert isinstance(result.reasoning, str)
        assert len(result.reasoning) > 0

    @pytest.mark.parametrize("perspective", ALL_PERSPECTIVES)
    def test_scorer_has_verdict(self, perspective: str):
        """测试每个评分函数返回判定。"""
        ctx = make_excellent_context()
        scorer = SCORERS[perspective]
        result = scorer(ctx)
        assert isinstance(result.verdict, str)
        assert len(result.verdict) > 0


class TestBuffettScorer:
    """巴菲特视角评分详细测试。"""

    def test_excellent_company(self):
        """优秀公司应得高分。"""
        from gugu.analysis.perspectives.scorer import score_buffett

        ctx = make_excellent_context()
        result = score_buffett(ctx)
        assert result.score >= 6.0
        assert "ROE" in " ".join(result.key_factors) or "护城河" in " ".join(result.key_factors)

    def test_terrible_company(self):
        """劣质公司应得低分。"""
        from gugu.analysis.perspectives.scorer import score_buffett

        ctx = make_terrible_context()
        result = score_buffett(ctx)
        assert result.score < 5.0

    def test_insider_selling_penalty(self):
        """内部人减持应扣分。"""
        from gugu.analysis.perspectives.scorer import score_buffett

        ctx = make_excellent_context()
        ctx.insider_selling = True
        result_with_selling = score_buffett(ctx)

        ctx2 = make_excellent_context()
        ctx2.insider_selling = False
        result_without = score_buffett(ctx2)

        assert result_with_selling.score < result_without.score

    def test_high_pe_penalty(self):
        """高PE应扣分（相比正常PE）。"""
        from gugu.analysis.perspectives.scorer import score_buffett

        ctx_normal = make_excellent_context()
        result_normal = score_buffett(ctx_normal)

        ctx_high_pe = make_excellent_context()
        ctx_high_pe.pe_dynamic = 80.0
        ctx_high_pe.profit_growth = 10.0  # PEG=8 极差
        result_high_pe = score_buffett(ctx_high_pe)

        # 高PE应比正常PE扣分
        assert result_high_pe.score < result_normal.score
        # 且PE应出现在风险标志中
        assert any("PE" in r for r in result_high_pe.risk_flags)


class TestLynchScorer:
    """林奇视角评分详细测试。"""

    def test_peg_scoring(self):
        """PEG 评估应正确反映成长性价比。"""
        from gugu.analysis.perspectives.scorer import score_lynch

        # PEG < 1 应得高分
        ctx = make_excellent_context()
        ctx.pe_dynamic = 15.0
        ctx.profit_growth = 20.0
        result_good = score_lynch(ctx)
        assert result_good.score >= 6.0

        # PEG > 3 应得低分
        ctx2 = make_excellent_context()
        ctx2.pe_dynamic = 80.0
        ctx2.profit_growth = 20.0
        result_bad = score_lynch(ctx2)
        assert result_bad.score < result_good.score


class TestSorosScorer:
    """索罗斯视角评分详细测试。"""

    def test_bull_market_bonus(self):
        """牛市环境应加分。"""
        from gugu.analysis.perspectives.scorer import score_soros

        ctx = make_excellent_context()
        ctx.market_stage = "bull"
        result_bull = score_soros(ctx)

        ctx2 = make_excellent_context()
        ctx2.market_stage = "bear"
        result_bear = score_soros(ctx2)

        assert result_bull.score > result_bear.score

    def test_volume_confirm(self):
        """量价配合应加分。"""
        from gugu.analysis.perspectives.scorer import score_soros

        ctx = make_excellent_context()
        ctx.change_pct = 3.0
        ctx.volume_ratio = 2.0
        result = score_soros(ctx)
        assert result.score >= 5.0


class TestEngine:
    """PerspectiveEngine 核心引擎测试。"""

    def test_analyze_returns_result(self):
        """测试分析返回完整结果。"""
        engine = PerspectiveEngine()
        ctx = make_excellent_context()
        result = engine.analyze(ctx)
        assert isinstance(result, PerspectiveAnalysisResult)
        assert result.symbol == "600519"
        assert len(result.scores) == 10

    def test_analyze_excellent_high_score(self):
        """优秀公司综合分应较高。"""
        engine = PerspectiveEngine()
        ctx = make_excellent_context()
        result = engine.analyze(ctx)
        assert result.composite_score >= 6.0
        assert result.passed_count >= 7

    def test_analyze_terrible_low_score(self):
        """劣质公司综合分应较低。"""
        engine = PerspectiveEngine()
        ctx = make_terrible_context()
        result = engine.analyze(ctx)
        assert result.composite_score < 6.0

    def test_analyze_empty_context(self):
        """空上下文不应崩溃。"""
        engine = PerspectiveEngine()
        ctx = make_empty_context()
        result = engine.analyze(ctx)
        assert isinstance(result, PerspectiveAnalysisResult)
        assert result.composite_score > 0  # 中性分

    def test_analyze_custom_perspectives(self):
        """测试自定义视角列表。"""
        engine = PerspectiveEngine(perspectives=["buffett", "lynch"])
        ctx = make_excellent_context()
        result = engine.analyze(ctx)
        assert len(result.scores) == 2

    def test_analyze_batch(self):
        """测试批量分析。"""
        engine = PerspectiveEngine()
        contexts = [make_excellent_context(), make_terrible_context()]
        results = engine.analyze_batch(contexts)
        assert len(results) == 2
        # 优秀公司分应高于劣质公司
        assert results[0].composite_score > results[1].composite_score

    def test_min_score_tracking(self):
        """测试最低分追踪（木桶效应）。"""
        engine = PerspectiveEngine()
        ctx = make_excellent_context()
        result = engine.analyze(ctx)
        assert result.min_score == min(s.score for s in result.scores)
        assert result.min_perspective != ""

    def test_composite_score_range(self):
        """测试综合分在合理范围。"""
        engine = PerspectiveEngine()
        for ctx in [make_excellent_context(), make_terrible_context(), make_empty_context()]:
            result = engine.analyze(ctx)
            assert 0.0 <= result.composite_score <= 10.0

    def test_prices_calculated(self):
        """测试价格计算。"""
        engine = PerspectiveEngine()
        ctx = make_excellent_context()
        result = engine.analyze(ctx)
        assert result.entry_price is not None
        assert result.stop_loss_price is not None
        assert result.target_price is not None
        assert result.stop_loss_price < result.entry_price < result.target_price

    def test_suggestion_generated(self):
        """测试操作建议生成。"""
        engine = PerspectiveEngine()
        ctx = make_excellent_context()
        result = engine.analyze(ctx)
        assert len(result.suggestion) > 0

    def test_overall_reasoning(self):
        """测试综合推理链。"""
        engine = PerspectiveEngine()
        ctx = make_excellent_context()
        result = engine.analyze(ctx)
        assert "综合" in result.overall_reasoning
        assert "最强" in result.overall_reasoning
        assert "最弱" in result.overall_reasoning


class TestIntegrators:
    """集成器测试。"""

    def test_build_context_from_signal(self):
        """测试从信号字典构建上下文。"""
        signal = {
            "symbol": "600519",
            "name": "茅台",
            "price": 1800,
            "pe_static": 30,
            "roe": 30,
            "ma20": 1770,
            "ma60": 1700,
        }
        ctx = build_context_from_signal(signal)
        assert ctx.symbol == "600519"
        assert ctx.price == 1800
        assert ctx.pe_static == 30

    def test_build_context_from_kline_uptrend(self):
        """测试从K线构建上下文（上涨趋势）。"""
        kline = make_uptrend_kline()
        ctx = build_context_from_kline("600519", "茅台", kline)
        assert ctx.symbol == "600519"
        assert ctx.price > 0
        assert ctx.ma20 > 0
        assert ctx.ma60 > 0
        assert ctx.rsi14 > 0
        assert ctx.macd_dif != 0

    def test_build_context_from_kline_downtrend(self):
        """测试从K线构建上下文（下跌趋势）。"""
        kline = make_downtrend_kline()
        ctx = build_context_from_kline("000001", "垃圾", kline)
        assert ctx.price > 0
        # 下跌趋势中 MA20 应小于 MA60
        assert ctx.ma20 < ctx.ma60

    def test_build_context_from_empty_kline(self):
        """测试空K线处理。"""
        ctx = build_context_from_kline("000001", "test", pd.DataFrame())
        assert ctx.symbol == "000001"
        assert ctx.price == 0

    def test_build_context_from_none_kline(self):
        """测试None K线处理。"""
        ctx = build_context_from_kline("000001", "test", None)
        assert ctx.symbol == "000001"

    def test_attach_perspective_to_signal(self):
        """测试视角结果附加到信号。"""
        signal = {"symbol": "600519", "name": "茅台", "price": 1800}
        result = PerspectiveAnalysisResult(
            symbol="600519",
            name="茅台",
            price=1800,
            composite_score=8.5,
            overall_verdict="推荐买入",
            min_score=7.0,
            min_perspective="technical",
            passed_count=10,
            suggestion="建议买入",
            entry_price=1770,
            stop_loss_price=1716,
            target_price=2100,
        )
        enhanced = attach_perspective_to_signal(signal, result)
        assert enhanced["perspective_score"] == 8.5
        assert enhanced["perspective_passed"] is True
        assert enhanced["perspective_entry"] == 1770
        assert enhanced["perspective_stop_loss"] == 1716
        assert enhanced["perspective_target"] == 2100

    def test_filter_by_perspective(self):
        """测试视角过滤。"""
        signals = [
            {"symbol": "A", "perspective_score": 8.0, "perspective_passed": True},
            {"symbol": "B", "perspective_score": 4.0, "perspective_passed": False},
            {"symbol": "C", "perspective_score": 7.0, "perspective_passed": True},
        ]
        filtered = filter_by_perspective(signals, min_score=6.0)
        assert len(filtered) == 2
        symbols = [s["symbol"] for s in filtered]
        assert "B" not in symbols

    def test_filter_by_perspective_require_pass(self):
        """测试严格模式过滤。"""
        signals = [
            {"symbol": "A", "perspective_score": 8.0, "perspective_passed": True},
            {"symbol": "B", "perspective_score": 7.0, "perspective_passed": False},
        ]
        filtered = filter_by_perspective(signals, min_score=6.0, require_pass=True)
        assert len(filtered) == 1
        assert filtered[0]["symbol"] == "A"

    def test_filter_preserves_no_score(self):
        """测试无评分信号保留（向后兼容）。"""
        signals = [
            {"symbol": "A", "perspective_score": 8.0},
            {"symbol": "B"},  # 无视角评分
        ]
        filtered = filter_by_perspective(signals, min_score=6.0)
        assert len(filtered) == 2  # B 保留


class TestPerspectiveReport:
    """飞书报告生成器测试。"""

    def test_build_card(self):
        """测试单股报告卡片生成。"""
        from gugu.notifier.perspective_report import build_perspective_card

        result = PerspectiveAnalysisResult(
            symbol="600519",
            name="茅台",
            price=1800,
            composite_score=8.5,
            overall_verdict="推荐买入",
            min_score=7.0,
            min_perspective="technical",
            passed_count=10,
            fatal_count=0,
            suggestion="建议买入",
            entry_price=1770,
            stop_loss_price=1716,
            target_price=2100,
            scores=[
                PerspectiveScore(
                    perspective="buffett",
                    score=8.0,
                    weight=0.15,
                    verdict="巴菲特:推荐",
                ),
            ],
            top_strengths=["ROE优秀", "护城河强"],
            top_risks=["PE偏高"],
        )
        card = build_perspective_card(result)
        assert "header" in card
        assert "elements" in card
        assert card["header"]["template"] == "green"

    def test_build_card_red_for_bad(self):
        """测试低分卡片为红色。"""
        from gugu.notifier.perspective_report import build_perspective_card

        result = PerspectiveAnalysisResult(
            symbol="000001",
            name="垃圾",
            price=5,
            composite_score=3.0,
            overall_verdict="否决",
            min_score=2.0,
            min_perspective="buffett",
            passed_count=2,
            fatal_count=2,
            scores=[],
        )
        card = build_perspective_card(result)
        assert card["header"]["template"] == "red"

    def test_build_batch_summary(self):
        """测试批量摘要卡片。"""
        from gugu.notifier.perspective_report import build_batch_summary_card

        results = [
            PerspectiveAnalysisResult(
                symbol="600519", name="茅台", price=1800,
                composite_score=8.5,
            ),
            PerspectiveAnalysisResult(
                symbol="000001", name="垃圾", price=5,
                composite_score=3.0,
            ),
        ]
        card = build_batch_summary_card(results)
        assert "header" in card
        assert "elements" in card

    def test_build_batch_summary_empty(self):
        """测试空结果批量摘要。"""
        from gugu.notifier.perspective_report import build_batch_summary_card

        card = build_batch_summary_card([])
        assert "elements" in card


class TestConfigIntegration:
    """配置集成测试。"""

    def test_default_weights_sum(self):
        """测试默认权重总和为1.0。"""
        total = sum(DEFAULT_WEIGHTS.values())
        assert abs(total - 1.0) < 0.01

    def test_all_perspectives_have_scorer(self):
        """测试所有视角都有评分函数。"""
        for p in ALL_PERSPECTIVES:
            assert p in SCORERS, f"视角 {p} 缺少评分函数"

    def test_all_perspectives_have_weight(self):
        """测试所有视角都有权重。"""
        for p in ALL_PERSPECTIVES:
            assert p in DEFAULT_WEIGHTS, f"视角 {p} 缺少权重"

    def test_thresholds_consistent(self):
        """测试阈值一致性。"""
        assert MIN_PERSPECTIVE_SCORE < PASS_THRESHOLD < COMPOSITE_PASS_THRESHOLD


class TestEdgeCases:
    """边界情况测试。"""

    def test_extreme_pe(self):
        """极端PE值不崩溃。"""
        from gugu.analysis.perspectives.scorer import score_buffett

        ctx = make_excellent_context()
        ctx.pe_static = 9999.0
        ctx.pe_dynamic = 9999.0
        result = score_buffett(ctx)
        assert 0.0 <= result.score <= 10.0

    def test_negative_values(self):
        """负值指标不崩溃。"""
        from gugu.analysis.perspectives.scorer import score_munger

        ctx = make_excellent_context()
        ctx.profit_growth = -100.0
        ctx.revenue_growth = -50.0
        result = score_munger(ctx)
        assert 0.0 <= result.score <= 10.0

    def test_zero_price(self):
        """零价格不崩溃。"""
        engine = PerspectiveEngine()
        ctx = make_excellent_context()
        ctx.price = 0
        result = engine.analyze(ctx)
        assert result.entry_price is None or result.entry_price == 0

    def test_nan_values(self):
        """NaN值不崩溃。"""
        from gugu.analysis.perspectives.scorer import score_technical

        ctx = make_excellent_context()
        ctx.rsi14 = float("nan")
        result = score_technical(ctx)
        assert 0.0 <= result.score <= 10.0

    def test_very_large_mcap(self):
        """极大市值不崩溃。"""
        from gugu.analysis.perspectives.scorer import score_lynch

        ctx = make_excellent_context()
        ctx.mcap_billion = 100000.0
        result = score_lynch(ctx)
        assert 0.0 <= result.score <= 10.0


# =========================================================================
# 新增：各视角详细评分逻辑测试
# =========================================================================


class TestMungerScorerDetail:
    """芒格视角评分详细测试。"""

    def test_high_profit_growth(self):
        """高净利增长应加分。"""
        from gugu.analysis.perspectives.scorer import score_munger
        ctx = make_excellent_context()
        ctx.profit_growth = 50.0
        result = score_munger(ctx)
        assert result.score >= 7.0

    def test_negative_cash_flow_penalty(self):
        """负经营现金流应扣分。"""
        from gugu.analysis.perspectives.scorer import score_munger
        ctx = make_excellent_context()
        ctx.operating_cash_flow = -100.0
        result = score_munger(ctx)
        assert "现金流" in " ".join(result.risk_flags)

    def test_high_peg_penalty(self):
        """高PEG应扣分。"""
        from gugu.analysis.perspectives.scorer import score_munger
        ctx = make_excellent_context()
        ctx.pe_dynamic = 100.0
        ctx.profit_growth = 10.0  # PEG=10
        result = score_munger(ctx)
        assert "PEG" in " ".join(result.risk_flags)


class TestLivermoreScorerDetail:
    """利弗莫尔视角评分详细测试。"""

    def test_near_52w_high_bonus(self):
        """接近52周高点应加分（突破新高）。"""
        from gugu.analysis.perspectives.scorer import score_livermore
        ctx = make_excellent_context()
        ctx.distance_from_52w_high = -2.0  # 距高点仅2%
        result = score_livermore(ctx)
        assert "突破新高" in " ".join(result.key_factors)

    def test_strong_downtrend_penalty(self):
        """强下降趋势应扣分。"""
        from gugu.analysis.perspectives.scorer import score_livermore
        ctx = make_excellent_context()
        # 构造空头排列
        ctx.price = 50
        ctx.ma5 = 55
        ctx.ma10 = 60
        ctx.ma20 = 65
        ctx.ma60 = 70
        result = score_livermore(ctx)
        assert result.score < 5.0

    def test_volume_breakout_bonus(self):
        """放量突破应加分。"""
        from gugu.analysis.perspectives.scorer import score_livermore
        ctx = make_excellent_context()
        ctx.volume_ratio = 2.0
        ctx.change_pct = 3.0
        result = score_livermore(ctx)
        assert "放量突破" in " ".join(result.key_factors)


class TestQiuGuoLuScorerDetail:
    """邱国鹭视角评分详细测试。"""

    def test_monopoly_bonus(self):
        """行业龙头应加分。"""
        from gugu.analysis.perspectives.scorer import score_qiu_guo_lu
        ctx = make_excellent_context()
        ctx.is_monopoly = True
        result = score_qiu_guo_lu(ctx)
        assert "龙头" in " ".join(result.key_factors)

    def test_pe_below_industry(self):
        """PE低于行业均值应加分。"""
        from gugu.analysis.perspectives.scorer import score_qiu_guo_lu
        ctx = make_excellent_context()
        ctx.pe_static = 20.0
        ctx.industry_pe_avg = 40.0
        result = score_qiu_guo_lu(ctx)
        assert "相对低估" in " ".join(result.key_factors)

    def test_pe_above_industry_penalty(self):
        """PE高于行业均值应扣分。"""
        from gugu.analysis.perspectives.scorer import score_qiu_guo_lu
        ctx = make_excellent_context()
        ctx.pe_static = 60.0
        ctx.industry_pe_avg = 30.0
        result = score_qiu_guo_lu(ctx)
        assert "相对高估" in " ".join(result.risk_flags)


class TestChenJiangTingScorerDetail:
    """陈江挺视角评分详细测试。"""

    def test_reasonable_stop_distance(self):
        """合理止损距离应加分。"""
        from gugu.analysis.perspectives.scorer import score_chen_jiang_ting
        ctx = make_excellent_context()
        ctx.price = 100
        ctx.ma20 = 95
        result = score_chen_jiang_ting(ctx)
        assert result.score >= 5.0

    def test_high_turnover_penalty(self):
        """高换手率应扣分。"""
        from gugu.analysis.perspectives.scorer import score_chen_jiang_ting
        ctx = make_excellent_context()
        ctx.turnover_pct = 20.0
        result = score_chen_jiang_ting(ctx)
        assert "换手率" in " ".join(result.risk_flags)


class TestTechnicalScorerDetail:
    """技术分析视角评分详细测试。"""

    def test_rsi_oversold_bonus(self):
        """RSI超卖应加分。"""
        from gugu.analysis.perspectives.scorer import score_technical
        ctx = make_excellent_context()
        ctx.rsi14 = 30.0
        result = score_technical(ctx)
        assert "超卖" in " ".join(result.key_factors)

    def test_rsi_overbought_penalty(self):
        """RSI超买应扣分。"""
        from gugu.analysis.perspectives.scorer import score_technical
        ctx = make_excellent_context()
        ctx.rsi14 = 85.0
        result = score_technical(ctx)
        assert "超买" in " ".join(result.risk_flags)

    def test_macd_bullish(self):
        """MACD多头应加分。"""
        from gugu.analysis.perspectives.scorer import score_technical
        ctx = make_excellent_context()
        ctx.macd_hist = 2.0
        ctx.macd_dif = 5.0
        ctx.macd_dea = 3.0
        result = score_technical(ctx)
        assert "MACD" in " ".join(result.key_factors)

    def test_macd_bearish(self):
        """MACD空头应扣分。"""
        from gugu.analysis.perspectives.scorer import score_technical
        ctx = make_excellent_context()
        ctx.macd_hist = -2.0
        ctx.macd_dif = -5.0
        ctx.macd_dea = -3.0
        result = score_technical(ctx)
        assert "MACD" in " ".join(result.risk_flags)


class TestInvestingHabitsScorerDetail:
    """投资习惯视角评分详细测试。"""

    def test_insider_selling_penalty(self):
        """内部人减持应扣分。"""
        from gugu.analysis.perspectives.scorer import score_investing_habits
        ctx = make_excellent_context()
        ctx.insider_selling = True
        result = score_investing_habits(ctx)
        assert "内部人减持" in " ".join(result.risk_flags)

    def test_high_volatility_penalty(self):
        """高波动率应扣分。"""
        from gugu.analysis.perspectives.scorer import score_investing_habits
        ctx = make_excellent_context()
        ctx.atr14 = 20.0
        ctx.price = 100.0  # ATR=20%
        result = score_investing_habits(ctx)
        assert "波动" in " ".join(result.risk_flags)


class TestDashixiongScorerDetail:
    """大师兄视角评分详细测试。"""

    def test_bull_market_bonus(self):
        """牛市应加分。"""
        from gugu.analysis.perspectives.scorer import score_dashixiong
        ctx = make_excellent_context()
        ctx.market_stage = "bull"
        result = score_dashixiong(ctx)
        assert "牛市" in " ".join(result.key_factors)

    def test_crash_market_penalty(self):
        """崩盘应大幅扣分。"""
        from gugu.analysis.perspectives.scorer import score_dashixiong
        ctx = make_excellent_context()
        ctx.market_stage = "crash"
        result = score_dashixiong(ctx)
        assert "崩盘" in " ".join(result.risk_flags)

    def test_high_usd_cny_penalty(self):
        """人民币贬值应扣分。"""
        from gugu.analysis.perspectives.scorer import score_dashixiong
        ctx = make_excellent_context()
        ctx.usd_cny = 7.5
        result = score_dashixiong(ctx)
        assert "贬值" in " ".join(result.risk_flags)

    def test_high_us10y_penalty(self):
        """高美债收益率应扣分。"""
        from gugu.analysis.perspectives.scorer import score_dashixiong
        ctx = make_excellent_context()
        ctx.us10y_yield = 5.0
        result = score_dashixiong(ctx)
        assert "高利率" in " ".join(result.risk_flags)


class TestSorosScorerDetail:
    """索罗斯视角评分详细测试。"""

    def test_main_inflow_bonus(self):
        """主力流入应加分。"""
        from gugu.analysis.perspectives.scorer import score_soros
        ctx = make_excellent_context()
        ctx.main_net_inflow_5d = 20.0
        result = score_soros(ctx)
        assert "主力流入" in " ".join(result.key_factors)

    def test_main_outflow_penalty(self):
        """主力流出应扣分。"""
        from gugu.analysis.perspectives.scorer import score_soros
        ctx = make_excellent_context()
        ctx.main_net_inflow_5d = -20.0
        result = score_soros(ctx)
        assert "主力流出" in " ".join(result.risk_flags)

    def test_hot_sector_bonus(self):
        """热门板块应加分。"""
        from gugu.analysis.perspectives.scorer import score_soros
        ctx = make_excellent_context()
        ctx.hot_sector = True
        result = score_soros(ctx)
        assert "热门板块" in " ".join(result.key_factors)


class TestBuffettScorerDetail:
    """巴菲特视角评分详细测试。"""

    def test_low_pe_bonus(self):
        """低PE应加分。"""
        from gugu.analysis.perspectives.scorer import score_buffett
        ctx = make_excellent_context()
        ctx.pe_dynamic = 10.0
        result = score_buffett(ctx)
        assert "安全边际" in " ".join(result.key_factors)

    def test_high_debt_penalty(self):
        """高负债率应扣分。"""
        from gugu.analysis.perspectives.scorer import score_buffett
        ctx = make_excellent_context()
        ctx.debt_ratio = 80.0
        result = score_buffett(ctx)
        assert "负债率" in " ".join(result.risk_flags)

    def test_dividend_bonus(self):
        """高股息率应加分。"""
        from gugu.analysis.perspectives.scorer import score_buffett
        ctx = make_excellent_context()
        ctx.dividend_yield = 4.0
        result = score_buffett(ctx)
        assert "股息率" in " ".join(result.key_factors)


class TestLynchScorerDetail:
    """林奇视角评分详细测试。"""

    def test_small_mcap_bonus(self):
        """中小市值应加分（Tenbagger潜力）。"""
        from gugu.analysis.perspectives.scorer import score_lynch
        ctx = make_excellent_context()
        ctx.mcap_billion = 50.0
        result = score_lynch(ctx)
        assert "中小市值" in " ".join(result.key_factors)

    def test_low_institutional_holding_bonus(self):
        """低机构持仓应加分（潜在黑马）。"""
        from gugu.analysis.perspectives.scorer import score_lynch
        ctx = make_excellent_context()
        ctx.institutional_holding = 10.0
        result = score_lynch(ctx)
        assert "潜在黑马" in " ".join(result.key_factors)

    def test_negative_growth_penalty(self):
        """负增长应扣分。"""
        from gugu.analysis.perspectives.scorer import score_lynch
        ctx = make_excellent_context()
        ctx.profit_growth = -30.0
        result = score_lynch(ctx)
        assert "增长衰退" in " ".join(result.risk_flags)
