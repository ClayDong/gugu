"""资金流日报模块单元测试。"""

from __future__ import annotations

import math

import pytest

from gugu.notifier.flow_report import (
    _fmt,
    _fmt_flow,
    _normalize,
    _rotation_label,
    _safe_float,
    _yuan_to_yi,
)


# ── _safe_float ──────────────────────────────────────────────


class TestSafeFloat:
    """_safe_float 安全转 float，过滤 NaN / Inf。"""

    def test_safe_float_none(self):
        assert _safe_float(None) == 0.0

    def test_safe_float_none_custom_default(self):
        assert _safe_float(None, default=-1.0) == -1.0

    def test_safe_float_normal_int(self):
        assert _safe_float(42) == 42.0

    def test_safe_float_normal_float(self):
        assert _safe_float(3.14) == 3.14

    def test_safe_float_normal_str_number(self):
        assert _safe_float("123.45") == 123.45

    def test_safe_float_zero(self):
        assert _safe_float(0) == 0.0

    def test_safe_float_negative(self):
        assert _safe_float(-5.5) == -5.5

    def test_safe_float_nan(self):
        assert _safe_float(float("nan")) == 0.0

    def test_safe_float_nan_with_default(self):
        assert _safe_float(float("nan"), default=1.0) == 1.0

    def test_safe_float_inf(self):
        assert _safe_float(float("inf")) == 0.0

    def test_safe_float_neg_inf(self):
        assert _safe_float(float("-inf")) == 0.0

    def test_safe_float_inf_with_default(self):
        assert _safe_float(float("inf"), default=999.0) == 999.0

    def test_safe_float_nan_via_math(self):
        assert _safe_float(math.nan) == 0.0

    def test_safe_float_inf_via_math(self):
        assert _safe_float(math.inf) == 0.0

    def test_safe_float_value_error(self):
        """无法转为 float 的字符串。"""
        assert _safe_float("not_a_number") == 0.0

    def test_safe_float_type_error(self):
        """不可转换的类型。"""
        assert _safe_float({"a": 1}) == 0.0

    def test_safe_float_bool(self):
        """bool 是 int 子类，float(True) → 1.0，应正常处理。"""
        assert _safe_float(True) == 1.0
        assert _safe_float(False) == 0.0


# ── _normalize ───────────────────────────────────────────────


class TestNormalize:
    """_normalize 线性归一化到 [0, 1]。"""

    def test_normalize_mid(self):
        """正常中间值。"""
        assert _normalize(5, 0, 10) == 0.5

    def test_normalize_min(self):
        assert _normalize(0, 0, 10) == 0.0

    def test_normalize_max(self):
        assert _normalize(10, 0, 10) == 1.0

    def test_normalize_below_min(self):
        assert _normalize(-1, 0, 10) == 0.0

    def test_normalize_above_max(self):
        assert _normalize(20, 0, 10) == 1.0

    def test_normalize_max_eq_min(self):
        """max_v == min_v 返回 0.5。"""
        assert _normalize(5, 10, 10) == 0.5

    def test_normalize_max_eq_min_at_zero(self):
        assert _normalize(0, 0, 0) == 0.5

    def test_normalize_max_less_than_min(self):
        """max_v < min_v 返回 0.5。"""
        assert _normalize(5, 10, 5) == 0.5

    def test_normalize_negative_range(self):
        """负值范围归一化。"""
        assert _normalize(-5, -10, 0) == 0.5
        assert _normalize(-10, -10, 0) == 0.0
        assert _normalize(0, -10, 0) == 1.0

    def test_normalize_float_precision(self):
        result = _normalize(3, 0, 7)
        assert abs(result - 3 / 7) < 1e-10


# ── _yuan_to_yi ──────────────────────────────────────────────


class TestYuanToYi:
    """_yuan_to_yi 元转亿。"""

    def test_yuan_to_yi_normal(self):
        assert _yuan_to_yi(100_000_000) == 1.0

    def test_yuan_to_yi_zero(self):
        assert _yuan_to_yi(0) == 0.0

    def test_yuan_to_yi_negative(self):
        assert _yuan_to_yi(-500_000_000) == -5.0

    def test_yuan_to_yi_small(self):
        assert _yuan_to_yi(1_234_567) == 0.01

    def test_yuan_to_yi_none(self):
        assert _yuan_to_yi(None) == 0.0

    def test_yuan_to_yi_rounding(self):
        """验证 round 行为：123_456_789 → 1.23。"""
        assert _yuan_to_yi(123_456_789) == 1.23
        assert _yuan_to_yi(123_999_999) == 1.24

    def test_yuan_to_yi_large(self):
        assert _yuan_to_yi(10_000_000_000) == 100.0


# ── _fmt_flow ────────────────────────────────────────────────


class TestFmtFlow:
    """_fmt_flow 格式化资金流金额（亿），带颜色（A 股惯例：流入红，流出绿）。"""

    def test_fmt_flow_positive(self):
        assert _fmt_flow(1.5) == "🔴 +1.5亿"

    def test_fmt_flow_positive_large(self):
        assert _fmt_flow(100.0) == "🔴 +100.0亿"

    def test_fmt_flow_negative(self):
        assert _fmt_flow(-2.3) == "🟢 -2.3亿"

    def test_fmt_flow_negative_large(self):
        assert _fmt_flow(-99.9) == "🟢 -99.9亿"

    def test_fmt_flow_zero(self):
        assert _fmt_flow(0) == "⚪ 0.0亿"

    def test_fmt_flow_zero_float(self):
        assert _fmt_flow(0.0) == "⚪ 0.0亿"

    def test_fmt_flow_none(self):
        """None 表示数据获取不到。"""
        assert _fmt_flow(None) == "获取不到"

    def test_fmt_flow_str_number(self):
        assert _fmt_flow("5.5") == "🔴 +5.5亿"

    def test_fmt_flow_negative_str(self):
        assert _fmt_flow("-3.0") == "🟢 -3.0亿"

    def test_fmt_flow_value_error(self):
        """不可转换的类型返回其 str 表示。"""
        assert _fmt_flow([1, 2]) == "[1, 2]"

    def test_fmt_flow_small_positive(self):
        assert _fmt_flow(0.01) == "🔴 +0.0亿"


# ── _rotation_label ──────────────────────────────────────────


class TestRotationLabel:
    """_rotation_label 轮动强度文字标签。"""

    def test_rotation_very_strong(self):
        """score >= 5。"""
        assert "轮动极强" in _rotation_label(5.0)
        assert "轮动极强" in _rotation_label(10.0)

    def test_rotation_strong(self):
        """3 <= score < 5。"""
        label = _rotation_label(3.0)
        assert "轮动较强" in label
        label = _rotation_label(4.99)
        assert "轮动较强" in label

    def test_rotation_normal(self):
        """1.5 <= score < 3。"""
        label = _rotation_label(1.5)
        assert "轮动正常" in label
        label = _rotation_label(2.99)
        assert "轮动正常" in label

    def test_rotation_weak(self):
        """0 < score < 1.5。"""
        label = _rotation_label(0.01)
        assert "轮动偏弱" in label
        label = _rotation_label(1.49)
        assert "轮动偏弱" in label

    def test_rotation_zero(self):
        """score == 0 返回市场普涨。"""
        assert "市场普涨" in _rotation_label(0.0)

    def test_rotation_negative(self):
        """负数也应归为市场普涨（score > 0 为 True，0 不成立，走 else）。"""
        assert "市场普涨" in _rotation_label(-1.0)

    def test_rotation_boundary_5(self):
        """边界：恰好 5.0 属于 >=5（轮动极强）。"""
        label = _rotation_label(5.0)
        assert "轮动极强" in label
        assert "轮动较强" not in label

    def test_rotation_boundary_3(self):
        """边界：恰好 3.0 属于 >=3（轮动较强）。"""
        label = _rotation_label(3.0)
        assert "轮动较强" in label
        assert "轮动正常" not in label

    def test_rotation_boundary_1_5(self):
        """边界：恰好 1.5 属于 >=1.5（轮动正常）。"""
        label = _rotation_label(1.5)
        assert "轮动正常" in label
        assert "轮动偏弱" not in label


# ── _fmt ─────────────────────────────────────────────────────


class TestFmt:
    """_fmt 格式化数值，None 显示 '--'。"""

    def test_fmt_positive(self):
        assert _fmt(3.14) == "+3.14"

    def test_fmt_negative(self):
        assert _fmt(-5.5) == "-5.50"

    def test_fmt_zero(self):
        assert _fmt(0) == "0.00"

    def test_fmt_zero_float(self):
        assert _fmt(0.0) == "0.00"

    def test_fmt_none(self):
        assert _fmt(None) == "--"

    def test_fmt_with_suffix(self):
        assert _fmt(1.23, "%") == "+1.23%"

    def test_fmt_negative_with_suffix(self):
        assert _fmt(-2.5, "%") == "-2.50%"

    def test_fmt_zero_with_suffix(self):
        assert _fmt(0, "%") == "0.00%"

    def test_fmt_none_with_suffix(self):
        assert _fmt(None, "亿") == "--"

    def test_fmt_str_number(self):
        assert _fmt("42.0") == "+42.00"

    def test_fmt_negative_str(self):
        assert _fmt("-1.0") == "-1.00"

    def test_fmt_value_error(self):
        """不可转换的类型返回其 str 表示。"""
        assert _fmt([1, 2]) == "[1, 2]"

    def test_fmt_large_number(self):
        assert _fmt(1234567.89) == "+1234567.89"

    def test_fmt_small_positive(self):
        assert _fmt(0.001) == "+0.00"


# ── analyze_sectors ────────────────────────────────────────────


class TestAnalyzeSectors:
    """analyze_sectors 行业轮动分析测试。"""

    def test_empty_data(self):
        """空数据返回空结果。"""
        from gugu.notifier.flow_report import analyze_sectors

        result = analyze_sectors([])
        assert result["inflow_top"] == []
        assert result["outflow_top"] == []
        assert result["divergence"] == []
        assert result["rotation_score"] == 0

    def test_inflow_top(self):
        """应正确排序流入TOP。"""
        from gugu.notifier.flow_report import analyze_sectors

        data = [
            {"名称": "白酒", "主力净流入-净额": 500_000_000, "涨跌幅": 2.0},
            {"名称": "半导体", "主力净流入-净额": 300_000_000, "涨跌幅": 1.5},
            {"名称": "银行", "主力净流入-净额": -100_000_000, "涨跌幅": -0.5},
        ]
        result = analyze_sectors(data, top_n=2)
        assert len(result["inflow_top"]) == 2
        assert result["inflow_top"][0]["name"] == "白酒"
        assert result["inflow_top"][0]["main_inflow"] == 5.0  # 5亿

    def test_outflow_top(self):
        """应正确排序流出TOP。"""
        from gugu.notifier.flow_report import analyze_sectors

        data = [
            {"名称": "白酒", "主力净流入-净额": 500_000_000, "涨跌幅": 2.0},
            {"名称": "银行", "主力净流入-净额": -200_000_000, "涨跌幅": -1.0},
            {"名称": "地产", "主力净流入-净额": -100_000_000, "涨跌幅": -0.5},
        ]
        result = analyze_sectors(data, top_n=2)
        assert len(result["outflow_top"]) == 2
        assert result["outflow_top"][0]["name"] == "银行"

    def test_divergence_detection(self):
        """应检测资金流背离。"""
        from gugu.notifier.flow_report import analyze_sectors

        data = [
            # 底背离：主力流入 > 1亿 但 跌 > 1%
            {"名称": "白酒", "主力净流入-净额": 200_000_000, "涨跌幅": -2.0},
            # 顶背离：主力流出 > 1亿 但 涨 > 1%
            {"名称": "半导体", "主力净流入-净额": -200_000_000, "涨跌幅": 3.0},
        ]
        result = analyze_sectors(data)
        assert len(result["divergence"]) == 2
        types = [d["type"] for d in result["divergence"]]
        assert "底背离" in types
        assert "顶背离" in types

    def test_rotation_score(self):
        """应计算轮动强度。"""
        from gugu.notifier.flow_report import analyze_sectors

        data = [
            {"名称": "A", "主力净流入-净额": 500_000_000, "涨跌幅": 2.0},
            {"名称": "B", "主力净流入-净额": 400_000_000, "涨跌幅": 1.5},
            {"名称": "C", "主力净流入-净额": 300_000_000, "涨跌幅": 1.0},
            {"名称": "D", "主力净流入-净额": -200_000_000, "涨跌幅": -1.0},
            {"名称": "E", "主力净流入-净额": -100_000_000, "涨跌幅": -0.5},
        ]
        result = analyze_sectors(data)
        assert result["rotation_score"] > 0


class TestAnalyzeKeyIndustries:
    """重点行业资金流提取测试。"""

    def test_match_key_industries(self):
        """应正确匹配重点行业关键词。"""
        from gugu.notifier.flow_report import analyze_key_industries

        data = [
            {"行业名称": "半导体", "主力净流入-净额": 500_000_000, "涨跌幅": 2.0},
            {"行业名称": "白酒", "主力净流入-净额": 300_000_000, "涨跌幅": 1.0},
            {"行业名称": "银行", "主力净流入-净额": 100_000_000, "涨跌幅": 0.5},
        ]
        result = analyze_key_industries(data)
        categories = [r["category"] for r in result]
        assert "半导体/芯片" in categories
        assert "消费" in categories
        assert "金融" in categories

    def test_empty_data(self):
        """空数据返回空列表。"""
        from gugu.notifier.flow_report import analyze_key_industries

        assert analyze_key_industries(None) == []
        assert analyze_key_industries([]) == []

    def test_no_match_shows_nodata(self):
        """未匹配的行业应显示暂无数据。"""
        from gugu.notifier.flow_report import analyze_key_industries

        data = [{"行业名称": "未知行业", "主力净流入-净额": 0, "涨跌幅": 0}]
        result = analyze_key_industries(data)
        # 至少有一个类别显示暂无数据
        nodata = [r for r in result if r["name"] == "暂无数据"]
        assert len(nodata) > 0


class TestAnalyzeConcepts:
    """概念热点分析测试。"""

    def test_top_concepts(self):
        """应返回流入TOP概念。"""
        from gugu.notifier.flow_report import analyze_concepts

        data = [
            {"概念名称": "AI", "主力净流入-净额": 500_000_000},
            {"概念名称": "新能源", "主力净流入-净额": 300_000_000},
            {"概念名称": "军工", "主力净流入-净额": 100_000_000},
        ]
        result = analyze_concepts(data, top_n=2)
        assert len(result) == 2
        assert result[0]["name"] == "AI"

    def test_empty_data(self):
        """空数据返回空列表。"""
        from gugu.notifier.flow_report import analyze_concepts

        assert analyze_concepts([]) == []


class TestAnalyzeStrongStocks:
    """强资金流入个股分析测试。"""

    def test_strong_stocks_filter(self):
        """应过滤主力净占比 > 5% 的个股。"""
        from gugu.notifier.flow_report import analyze_strong_stocks

        data = [
            {
                "代码": "600519",
                "名称": "茅台",
                "主力净流入-净占比": 8.0,
                "主力净流入-净额": 500_000_000,
                "涨跌幅": 2.0,
            },
            {
                "代码": "000001",
                "名称": "平安",
                "主力净流入-净占比": 3.0,
                "主力净流入-净额": 100_000_000,
                "涨跌幅": 0.5,
            },
        ]
        result = analyze_strong_stocks(data, top_n=5)
        assert len(result) == 1
        assert result[0]["code"] == "600519"

    def test_empty_data(self):
        """空数据返回空列表。"""
        from gugu.notifier.flow_report import analyze_strong_stocks

        assert analyze_strong_stocks([]) == []


class TestAnalyzeMarketOverview:
    """市场全局分析测试。"""

    def test_main_buy_retail_sell(self):
        """主力买入散户卖出。"""
        from gugu.notifier.flow_report import analyze_market_overview

        market = {"main_net_inflow": 50, "small_inflow": -30}
        result = analyze_market_overview(market, None)
        assert "主力买入" in result["main_vs_retail"]

    def test_main_sell_retail_buy(self):
        """主力卖出散户接盘。"""
        from gugu.notifier.flow_report import analyze_market_overview

        market = {"main_net_inflow": -50, "small_inflow": 30}
        result = analyze_market_overview(market, None)
        assert "主力卖出" in result["main_vs_retail"]

    def test_north_bound_analysis(self):
        """北向资金分析。"""
        from gugu.notifier.flow_report import analyze_market_overview

        north = {"total": 50}
        result = analyze_market_overview(None, north)
        assert result["north_analysis"]["direction"] == "流入"
        assert result["north_analysis"]["significant"] == "显著"

    def test_north_bound_none(self):
        """北向资金获取不到。"""
        from gugu.notifier.flow_report import analyze_market_overview

        north = {"total": None}
        result = analyze_market_overview(None, north)
        assert result["north_analysis"]["direction"] == "获取不到"


class TestBuildCards:
    """卡片构建测试。"""

    def test_build_morning_card(self):
        """盘前复盘卡片应包含关键信息。"""
        from gugu.notifier.flow_report import build_morning_card

        data = {
            "market": {
                "date": "2026-07-07",
                "sh_change": 0.5,
                "sz_change": 0.3,
                "main_net_inflow": 30,
            },
            "north_bound": {"total": 20},
            "market_overview": {"main_vs_retail": "主力买入，散户卖出"},
            "sectors": {
                "inflow_top": [{"name": "白酒", "main_inflow": 5.0, "price_change": 2.0}],
                "outflow_top": [{"name": "银行", "main_inflow": -3.0, "price_change": -1.0}],
                "rotation_score": 2.5,
                "divergence": [],
            },
            "key_industries": [
                {"category": "消费", "name": "白酒", "main_inflow": 5.0, "price_change": 2.0}
            ],
            "concepts": [{"name": "AI", "main_inflow": 10.0}],
            "strong_stocks": [
                {
                    "name": "茅台",
                    "code": "600519",
                    "main_ratio": 8.0,
                    "main_inflow": 5.0,
                    "price_change": 2.0,
                }
            ],
            "timestamp": "2026-07-07 08:00",
        }
        card = build_morning_card(data)
        assert "msg_type" in card
        assert "card" in card
        assert "header" in card["card"]
        assert "elements" in card["card"]
        assert "资金流复盘" in card["card"]["header"]["title"]["content"]

    def test_build_morning_card_unavailable(self):
        """数据源不可用时应发送告警卡片。"""
        from gugu.notifier.flow_report import build_morning_card

        card = build_morning_card({"sources_unavailable": True})
        assert "card" in card
        assert "数据源不可用" in card["card"]["header"]["title"]["content"]
        assert card["card"]["header"]["template"] == "red"  # 红色告警

    def test_build_close_card(self):
        """收盘日报卡片应包含关键信息。"""
        from gugu.notifier.flow_report import build_close_card

        data = {
            "market": {
                "date": "2026-07-07",
                "sh_change": 0.5,
                "sz_change": 0.3,
                "main_net_inflow": 30,
            },
            "north_bound": {"total": 20},
            "market_overview": {"main_vs_retail": "主力买入，散户卖出"},
            "sectors": {
                "inflow_top": [{"name": "白酒", "main_inflow": 5.0, "price_change": 2.0}],
                "outflow_top": [{"name": "银行", "main_inflow": -3.0, "price_change": -1.0}],
                "rotation_score": 2.5,
                "divergence": [
                    {"name": "白酒", "type": "底背离", "detail": "主力+5亿但跌2%", "action": "观察"}
                ],
            },
            "key_industries": [],
            "concepts": [{"name": "AI", "main_inflow": 10.0}],
            "strong_stocks": [
                {
                    "name": "茅台",
                    "code": "600519",
                    "main_ratio": 8.0,
                    "main_inflow": 5.0,
                    "price_change": 2.0,
                }
            ],
            "timestamp": "2026-07-07 15:10",
        }
        card = build_close_card(data)
        assert "card" in card
        assert "header" in card["card"]
        assert "今日资金流日报" in card["card"]["header"]["title"]["content"]

    def test_build_afternoon_card(self):
        """盘后决策报告卡片应包含决策指导。"""
        from gugu.notifier.flow_report import build_afternoon_card

        data = {
            "market": {
                "date": "2026-07-07",
                "sh_change": 0.5,
                "sz_change": 0.3,
                "main_net_inflow": 30,
            },
            "north_bound": {"total": 20},
            "market_overview": {"main_vs_retail": "主力买入，散户卖出"},
            "sectors": {
                "inflow_top": [{"name": "白酒", "main_inflow": 5.0, "price_change": 2.0}],
                "rotation_score": 2.5,
                "divergence": [],
            },
            "key_industries": [],
            "strong_stocks": [
                {
                    "name": "茅台",
                    "code": "600519",
                    "main_ratio": 8.0,
                    "main_inflow": 5.0,
                    "price_change": 2.0,
                }
            ],
            "timestamp": "2026-07-07 14:30",
        }
        card = build_afternoon_card(data)
        assert "card" in card
        assert "header" in card["card"]
        assert "盘后资金流决策报告" in card["card"]["header"]["title"]["content"]

    def test_build_unavailable_card(self):
        """不可用状态卡片。"""
        from gugu.notifier.flow_report import build_unavailable_card

        card = build_unavailable_card("资金流复盘")
        assert "card" in card
        assert (
            "数据暂不可用" in card["card"]["header"]["title"]["content"]
            or "数据源不可用" in card["card"]["header"]["title"]["content"]
        )


class TestFormatDecisionGuidance:
    """决策指导格式化测试。"""

    def test_strong_market(self):
        """主力大幅流入应提示偏强。"""
        from gugu.notifier.flow_report import _format_decision_guidance

        data = {
            "market": {"main_net_inflow": 50},
            "sectors": {"divergence": []},
            "strong_stocks": [],
        }
        text = _format_decision_guidance(data)
        assert "偏强" in text

    def test_weak_market(self):
        """主力大幅流出应提示偏弱。"""
        from gugu.notifier.flow_report import _format_decision_guidance

        data = {
            "market": {"main_net_inflow": -50},
            "sectors": {"divergence": []},
            "strong_stocks": [],
        }
        text = _format_decision_guidance(data)
        assert "偏弱" in text

    def test_divergence_warning(self):
        """背离预警应显示。"""
        from gugu.notifier.flow_report import _format_decision_guidance

        data = {
            "market": {"main_net_inflow": 10},
            "sectors": {
                "divergence": [
                    {
                        "name": "白酒",
                        "type": "顶背离",
                        "detail": "主力-5亿但涨3%",
                        "action": "警惕出货",
                    },
                ]
            },
            "strong_stocks": [],
        }
        text = _format_decision_guidance(data)
        assert "顶背离" in text

    def test_strong_stock_highlight(self):
        """强流入个股应高亮。"""
        from gugu.notifier.flow_report import _format_decision_guidance

        data = {
            "market": {"main_net_inflow": 10},
            "sectors": {"divergence": []},
            "strong_stocks": [{"name": "茅台", "code": "600519", "main_ratio": 8.0}],
        }
        text = _format_decision_guidance(data)
        assert "茅台" in text
