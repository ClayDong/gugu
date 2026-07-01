"""尾盘选股器单元测试。"""

from __future__ import annotations

from datetime import datetime
from unittest import mock

import pytest

from gugu.screener.terminal_screener import ScreeningResult, _symbol_prefix
from gugu.screener.report import (
    _build_card,
    _format_stock_line,
    _now_str,
    format_screener_report,
)


# ========== A. ScreeningResult dataclass ==========


def test_screening_result_construction():
    """构造 ScreeningResult 实例，所有字段完整。"""
    r = ScreeningResult(
        symbol="600519",
        name="贵州茅台",
        price=1500.0,
        change_pct=3.5,
        volume_ratio=2.1,
        turnover_pct=6.5,
        mcap_billion=100.0,
        volume_trend="✅ 量增",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )
    assert r.symbol == "600519"
    assert r.name == "贵州茅台"
    assert r.price == 1500.0
    assert r.change_pct == 3.5
    assert r.volume_ratio == 2.1
    assert r.turnover_pct == 6.5
    assert r.mcap_billion == 100.0
    assert r.volume_trend == "✅ 量增"
    assert r.beats_index is True
    assert r.late_session_high is True
    assert r.passed is True
    assert r.failed_conditions == []


def test_screening_result_pass_count_all_passed():
    """全部通过时 pass_count = 8。"""
    r = ScreeningResult(
        symbol="600519",
        name="贵州茅台",
        price=1500.0,
        change_pct=3.5,
        volume_ratio=2.1,
        turnover_pct=6.5,
        mcap_billion=100.0,
        volume_trend="✅ 量增",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )
    assert r.pass_count == 8


def test_screening_result_pass_count_partial():
    """部分未通过时 pass_count 正确计算。"""
    r = ScreeningResult(
        symbol="000001",
        name="平安银行",
        price=10.0,
        change_pct=3.0,
        volume_ratio=1.2,
        turnover_pct=4.0,
        mcap_billion=200.0,
        volume_trend="❌",
        beats_index=False,
        late_session_high=False,
        passed=False,
        failed_conditions=["量比不足", "换手率不足", "量能递减"],
    )
    assert r.pass_count == 5
    assert r.passed is False


def test_screening_result_pass_count_all_failed():
    """全部未通过时 pass_count = 0。"""
    r = ScreeningResult(
        symbol="000001",
        name="平安银行",
        price=10.0,
        change_pct=0.0,
        volume_ratio=0.5,
        turnover_pct=2.0,
        mcap_billion=300.0,
        volume_trend="❌",
        beats_index=False,
        late_session_high=False,
        passed=False,
        failed_conditions=["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"],
    )
    assert r.pass_count == 0


# ========== B. _symbol_prefix ==========


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600519", "sh600519"),  # 沪市
        ("688001", "sh688001"),  # 科创板沪
        ("000001", "sz000001"),  # 深市
        ("300001", "sz300001"),  # 创业板
        ("430001", "bj430001"),  # 北交所
        ("xxxxx", "0xxxxx"),  # 未知前缀，zfill(6) 后原样返回
        (" 600519 ", "sh600519"),  # 带空格自动 strip
    ],
)
def test_symbol_prefix(code, expected):
    assert _symbol_prefix(code) == expected


# ========== C. _build_card ==========


def test_build_card_basic_structure():
    """验证 _build_card 输出 dict 的基本结构。"""
    card = _build_card("标题", "green", sections=["内容1", "内容2"], note="底部注释")
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["title"]["content"] == "标题"
    assert card["card"]["header"]["template"] == "green"
    assert len(card["card"]["elements"]) == 5  # div + hr + div + hr + note


def test_build_card_empty_sections():
    """空 sections 时 elements 只包含 note。"""
    card = _build_card("空", "red", sections=[], note="只有注释")
    assert card["msg_type"] == "interactive"
    assert len(card["card"]["elements"]) == 1
    assert card["card"]["elements"][0]["tag"] == "note"


def test_build_card_multiple_sections_with_note():
    """多 sections + note 验证 elements 顺序和数量。"""
    card = _build_card("多段", "blue", sections=["A", "B", "C"], note="尾部")
    elements = card["card"]["elements"]
    # A → hr → B → hr → C → hr → note => 7 elements
    assert len(elements) == 7
    # 检查首尾
    assert elements[0]["tag"] == "div"
    assert elements[0]["text"]["content"] == "A"
    assert elements[-1]["tag"] == "note"
    # 检查 hr
    assert elements[1]["tag"] == "hr"
    assert elements[3]["tag"] == "hr"
    assert elements[5]["tag"] == "hr"


# ========== D. _format_stock_line ==========


def test_format_stock_line_all_passed():
    """完全 passed 的股票，无红条件标。"""
    r = ScreeningResult(
        symbol="600519",
        name="贵州茅台",
        price=1520.50,
        change_pct=3.56,
        volume_ratio=2.14,
        turnover_pct=6.5,
        mcap_billion=100.0,
        volume_trend="✅ 量增",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )
    line = _format_stock_line(r)
    assert "贵州茅台" in line
    assert "600519" in line
    assert "+3.6%" in line or "+3.6" in line  # 四舍五入
    assert "¥1520.50" in line
    assert "量比2.1" in line
    assert "换手6.5%" in line
    assert "市值100亿" in line
    assert "✅ 量增" in line
    assert "📈" in line
    assert "🔝" in line
    assert "⚠️" not in line


def test_format_stock_line_partial_failed():
    """部分 failed_conditions 的股票显示红条件标。"""
    r = ScreeningResult(
        symbol="000001",
        name="平安银行",
        price=10.0,
        change_pct=3.0,
        volume_ratio=0.9,
        turnover_pct=4.0,
        mcap_billion=300.0,
        volume_trend="❌",
        beats_index=False,
        late_session_high=False,
        passed=False,
        failed_conditions=["量比不足", "换手率不足", "量能递减", "未跑赢大盘", "尾盘未创新高"],
    )
    line = _format_stock_line(r)
    assert "平安银行" in line
    assert "⚠️" in line
    assert "量比不足" in line
    assert "换手率不足" in line
    assert "量能递减" in line
    # 最多显示前 3 个 failed_conditions
    assert "未跑赢大盘" not in line
    assert "尾盘未创新高" not in line
    assert "📈" not in line
    assert "🔝" not in line


def test_format_stock_line_different_volume_trends():
    """不同的 volume_trend 值展示。"""
    r1 = ScreeningResult(
        symbol="600001",
        name="A",
        price=10.0,
        change_pct=3.0,
        volume_ratio=1.5,
        turnover_pct=6.0,
        mcap_billion=80.0,
        volume_trend="✅ 量增",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )
    r2 = ScreeningResult(
        symbol="600002",
        name="B",
        price=10.0,
        change_pct=3.0,
        volume_ratio=1.5,
        turnover_pct=6.0,
        mcap_billion=80.0,
        volume_trend="✅ 高位",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )
    r3 = ScreeningResult(
        symbol="600003",
        name="C",
        price=10.0,
        change_pct=3.0,
        volume_ratio=1.5,
        turnover_pct=6.0,
        mcap_billion=80.0,
        volume_trend="❌",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )
    assert "✅ 量增" in _format_stock_line(r1)
    assert "✅ 高位" in _format_stock_line(r2)
    assert "❌" in _format_stock_line(r3)


# ========== E. format_screener_report ==========


def test_format_screener_report_empty():
    """空结果列表。"""
    card = format_screener_report([], total_scanned=5000)
    assert card["card"]["header"]["title"]["content"] == "尾盘选股 · 无结果"
    assert card["card"]["header"]["template"] == "yellow"
    content = card["card"]["elements"][0]["text"]["content"]
    assert "5000" in content
    assert "未找到符合条件的标的" in content


def test_format_screener_report_all_passed():
    """全部 passed。"""
    r = ScreeningResult(
        symbol="600519",
        name="贵州茅台",
        price=1500.0,
        change_pct=3.5,
        volume_ratio=2.0,
        turnover_pct=6.0,
        mcap_billion=100.0,
        volume_trend="✅ 量增",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )
    card = format_screener_report([r], total_scanned=5000)
    assert card["card"]["header"]["template"] == "green"
    content = str(card)
    assert "完全通过（1 只）" in content
    assert "扫描 5000 只" in content
    assert "贵州茅台" in content


def test_format_screener_report_mixed():
    """混合 passed + failed。"""
    passed_r = ScreeningResult(
        symbol="600519",
        name="贵州茅台",
        price=1500.0,
        change_pct=3.5,
        volume_ratio=2.0,
        turnover_pct=6.0,
        mcap_billion=100.0,
        volume_trend="✅ 量增",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )
    failed_r = ScreeningResult(
        symbol="000001",
        name="平安银行",
        price=10.0,
        change_pct=3.0,
        volume_ratio=0.9,
        turnover_pct=4.0,
        mcap_billion=300.0,
        volume_trend="❌",
        beats_index=False,
        late_session_high=False,
        passed=False,
        failed_conditions=["量比不足", "换手率不足"],
    )
    card = format_screener_report([passed_r, failed_r], total_scanned=5000)
    content = str(card)
    assert "完全通过（1 只）" in content
    assert "部分通过（1 只）" in content
    assert "贵州茅台" in content
    assert "平安银行" in content
    assert "扫描 5000 只" in content
    assert "深度检查 2 只" in content
    assert "通过 1 只" in content


def test_format_screener_report_partial_only():
    """只有部分通过的股票，卡片应为黄色。"""
    r = ScreeningResult(
        symbol="000001",
        name="平安银行",
        price=10.0,
        change_pct=3.0,
        volume_ratio=0.9,
        turnover_pct=4.0,
        mcap_billion=300.0,
        volume_trend="❌",
        beats_index=False,
        late_session_high=False,
        passed=False,
        failed_conditions=["量比不足"],
    )
    card = format_screener_report([r], total_scanned=3000)
    assert card["card"]["header"]["template"] == "yellow"
    content = str(card)
    assert "部分通过（1 只）" in content
    assert "扫描 3000 只" in content


# ========== F. _now_str ==========


@mock.patch("gugu.screener.report.datetime")
def test_now_str(mock_datetime):
    """mock datetime 固定时间，验证输出格式。"""
    mock_datetime.now.return_value = datetime(2026, 3, 15, 14, 30, 0)
    mock_datetime.strftime = datetime.strftime
    result = _now_str()
    assert result == "03-15 14:30"