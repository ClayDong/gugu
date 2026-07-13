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
    # P2-1: 紧凑格式不再显示换手/市值，改为显示止损/目标
    assert "止损" in line
    assert "目标" in line
    assert "✅ 量增" in line
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
    # P2-1: 紧凑格式最多显示前 2 个 failed_conditions
    assert "量比不足" in line
    assert "换手率不足" in line
    # 第 3 个及之后不再显示
    assert "量能递减" not in line
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
    # P2-1: 新格式用"首选"替代"完全通过"
    assert "首选" in content
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
    # P2-1: 新格式用"首选"替代"完全通过"
    assert "首选" in content
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


# ========== G. P0 性能修复：快照硬过滤 + 并发 + 熔断 ==========


def test_hard_filter_snapshot_filters_by_change_pct():
    """P0-2: 快照硬过滤 涨幅 -2% ~ +1% 生效。"""
    import pandas as pd
    from gugu.screener.terminal_screener import TerminalScreener

    spot = pd.DataFrame(
        [
            {
                "代码": "000001",
                "名称": "A",
                "最新价": 10.0,
                "涨跌幅": 0.5,
                "成交量": 1000,
                "量比": 0.8,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 通过
            {
                "代码": "000002",
                "名称": "B",
                "最新价": 10.0,
                "涨跌幅": 5.0,
                "成交量": 1000,
                "量比": 0.8,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 涨幅过高
            {
                "代码": "000003",
                "名称": "C",
                "最新价": 10.0,
                "涨跌幅": -3.0,
                "成交量": 1000,
                "量比": 0.8,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 跌幅过深
        ]
    )
    screener = TerminalScreener()
    result = screener._hard_filter_snapshot(spot)
    assert len(result) == 1
    assert result.iloc[0]["代码"] == "000001"


def test_hard_filter_snapshot_filters_by_volume_ratio():
    """P0-2: 量比 < 1.0 生效。"""
    import pandas as pd
    from gugu.screener.terminal_screener import TerminalScreener

    spot = pd.DataFrame(
        [
            {
                "代码": "000001",
                "名称": "A",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 通过
            {
                "代码": "000002",
                "名称": "B",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 1.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 量比过高（放量）
        ]
    )
    screener = TerminalScreener()
    result = screener._hard_filter_snapshot(spot)
    assert len(result) == 1
    assert result.iloc[0]["代码"] == "000001"


def test_hard_filter_snapshot_filters_by_turnover():
    """P0-2: 换手率 3%-15% 生效。"""
    import pandas as pd
    from gugu.screener.terminal_screener import TerminalScreener

    spot = pd.DataFrame(
        [
            {
                "代码": "000001",
                "名称": "A",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 通过
            {
                "代码": "000002",
                "名称": "B",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 1.0,
                "流通市值": 100e8,
            },  # 换手率过低
            {
                "代码": "000003",
                "名称": "C",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 20.0,
                "流通市值": 100e8,
            },  # 换手率过高
        ]
    )
    screener = TerminalScreener()
    result = screener._hard_filter_snapshot(spot)
    assert len(result) == 1
    assert result.iloc[0]["代码"] == "000001"


def test_hard_filter_snapshot_filters_by_market_cap():
    """P0-2: 流通市值 50亿-500亿 生效。"""
    import pandas as pd
    from gugu.screener.terminal_screener import TerminalScreener

    spot = pd.DataFrame(
        [
            {
                "代码": "000001",
                "名称": "A",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 通过（100亿）
            {
                "代码": "000002",
                "名称": "B",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 10e8,
            },  # 过小（10亿）
            {
                "代码": "000003",
                "名称": "C",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 800e8,
            },  # 过大（800亿）
        ]
    )
    screener = TerminalScreener()
    result = screener._hard_filter_snapshot(spot)
    assert len(result) == 1
    assert result.iloc[0]["代码"] == "000001"


def test_hard_filter_snapshot_excludes_st_and_bj():
    """P0-2: 排除 ST/退/N/C 和北交所。"""
    import pandas as pd
    from gugu.screener.terminal_screener import TerminalScreener

    spot = pd.DataFrame(
        [
            {
                "代码": "000001",
                "名称": "正常股",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 通过
            {
                "代码": "000002",
                "名称": "ST退市",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # ST
            {
                "代码": "430001",
                "名称": "北交所",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            },  # 北交所
        ]
    )
    screener = TerminalScreener()
    result = screener._hard_filter_snapshot(spot)
    assert len(result) == 1
    assert result.iloc[0]["代码"] == "000001"


def test_hard_filter_snapshot_normalizes_column_names():
    """P0-2: 列名标准化（兼容 stock_zh_a_spot_em 的英文列名）。"""
    import pandas as pd
    from gugu.screener.terminal_screener import TerminalScreener

    # 模拟 stock_zh_a_spot_em 的英文列名格式
    spot = pd.DataFrame(
        [
            {
                "code": "000001",
                "name": "测试",
                "trade": 10.0,
                "changepercent": 0.5,
                "volume": 1000,
                "volume_ratio": 0.8,
                "turnoverratio": 5.0,
                "circ_mv": 100e8,
            }
        ]
    )
    screener = TerminalScreener()
    result = screener._hard_filter_snapshot(spot)
    assert len(result) == 1
    # 标准化后应有中文列名
    assert "量比" in result.columns
    assert "换手率" in result.columns
    assert "流通市值" in result.columns


@pytest.mark.asyncio
async def test_scan_returns_empty_when_snapshot_fails():
    """P0-3: 快照获取失败时返回空列表，不抛异常。"""
    from unittest.mock import AsyncMock, patch
    from gugu.screener.terminal_screener import TerminalScreener

    screener = TerminalScreener()
    with patch.object(screener, "_fetch_market_snapshot", return_value=None):
        results = await screener.scan()
    assert results == []


@pytest.mark.asyncio
async def test_scan_returns_empty_when_no_candidates():
    """P0-3: 快照硬过滤后无候选时返回空列表。"""
    from unittest.mock import AsyncMock, patch
    import pandas as pd
    from gugu.screener.terminal_screener import TerminalScreener

    screener = TerminalScreener()
    # 所有股票涨幅都过高（被涨幅 -2%~+1% 过滤）
    empty_spot = pd.DataFrame(
        [
            {
                "代码": "000001",
                "名称": "A",
                "最新价": 10.0,
                "涨跌幅": 5.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            }
        ]
    )
    with patch.object(screener, "_fetch_market_snapshot", return_value=empty_spot):
        results = await screener.scan()
    assert results == []


@pytest.mark.asyncio
async def test_scan_total_timeout_returns_empty():
    """P0-3: 总超时触发时返回空列表（不抛异常）。"""
    from unittest.mock import patch
    import asyncio
    from gugu.screener.terminal_screener import TerminalScreener

    screener = TerminalScreener()

    # 让 _scan_internal 在被取消时正常返回空列表（模拟被超时中断后的行为）
    async def hang(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return []

    with patch.object(screener, "_scan_internal", side_effect=hang):
        # 临时把超时设小到 1 秒以加速测试
        import gugu.screener.terminal_screener as ts

        original = ts._TOTAL_TIMEOUT_SEC
        ts._TOTAL_TIMEOUT_SEC = 1
        try:
            results = await screener.scan()
            assert results == []
        finally:
            ts._TOTAL_TIMEOUT_SEC = original


@pytest.mark.asyncio
async def test_concurrent_check_circuit_breaker():
    """P0-4: 连续失败 N 次触发熔断，提前返回。"""
    from unittest.mock import patch
    import pandas as pd
    import time
    from gugu.screener.terminal_screener import TerminalScreener

    screener = TerminalScreener()

    # 构造 10 只候选股
    candidates = pd.DataFrame(
        [
            {
                "代码": f"00000{i}",
                "名称": f"S{i}",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            }
            for i in range(10)
        ]
    )

    # mock _check_stock 永远抛 TimeoutError
    async def always_timeout(*args, **kwargs):
        raise TimeoutError("mocked timeout")

    with patch.object(screener, "_check_stock", side_effect=always_timeout):
        results = await screener._concurrent_check(candidates, 0.0, time.time())

    # 触发熔断（_FAILURE_THRESHOLD=5），返回空列表
    assert results == []


@pytest.mark.asyncio
async def test_concurrent_check_returns_results_on_success():
    """P0-3: 正常场景下并发返回结果。"""
    from unittest.mock import patch
    import pandas as pd
    import time
    from gugu.screener.terminal_screener import TerminalScreener, ScreeningResult

    screener = TerminalScreener()

    candidates = pd.DataFrame(
        [
            {
                "代码": "000001",
                "名称": "A",
                "最新价": 10.0,
                "涨跌幅": 0.0,
                "成交量": 1000,
                "量比": 0.5,
                "换手率": 5.0,
                "流通市值": 100e8,
            },
            {
                "代码": "000002",
                "名称": "B",
                "最新价": 10.0,
                "涨跌幅": 0.5,
                "成交量": 1000,
                "量比": 0.6,
                "换手率": 6.0,
                "流通市值": 200e8,
            },
        ]
    )

    mock_result = ScreeningResult(
        symbol="000001",
        name="A",
        price=10.0,
        change_pct=0.0,
        volume_ratio=0.5,
        turnover_pct=5.0,
        mcap_billion=100.0,
        volume_trend="✅",
        beats_index=True,
        late_session_high=True,
        passed=True,
        failed_conditions=[],
    )

    async def success_check(*args, **kwargs):
        return mock_result

    with patch.object(screener, "_check_stock", side_effect=success_check):
        results = await screener._concurrent_check(candidates, 0.0, time.time())

    assert len(results) == 2
    assert all(isinstance(r, ScreeningResult) for r in results)


def test_p0_performance_constants():
    """P0: 性能参数常量值合理。"""
    from gugu.screener.terminal_screener import (
        _KLINE_CONCURRENCY,
        _TOTAL_TIMEOUT_SEC,
        _PER_STOCK_TIMEOUT,
        _FAILURE_THRESHOLD,
    )

    assert _KLINE_CONCURRENCY >= 1 and _KLINE_CONCURRENCY <= 10
    assert _TOTAL_TIMEOUT_SEC >= 600  # 至少 10 分钟
    assert _PER_STOCK_TIMEOUT >= 5
    assert _FAILURE_THRESHOLD >= 3 and _FAILURE_THRESHOLD <= 10
