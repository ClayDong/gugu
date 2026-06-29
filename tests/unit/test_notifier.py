"""通知模块单元测试。"""
from __future__ import annotations

from unittest import mock

import httpx
import pytest

from gugu.notifier.formatter import (
    format_backtest_report,
    format_daily_report,
    format_risk_alert,
    format_signal,
)


def test_format_signal_buy():
    """测试买入信号卡片格式。"""
    signal = {
        "symbol": "600519",
        "name": "贵州茅台",
        "direction": "buy",
        "strategy": "turtle",
        "reason": "突破上轨",
        "suggested_position": "20%",
        "price": 1500.0,
    }
    card = format_signal(signal)
    assert card["msg_type"] == "interactive"
    assert "header" in card["card"]


def test_format_signal_sell():
    """测试卖出信号卡片颜色为红色。"""
    signal = {
        "symbol": "600519",
        "direction": "sell",
        "strategy": "rsi_reversal",
        "reason": "超买卖出",
        "price": 1600.0,
    }
    card = format_signal(signal)
    assert card["card"]["header"]["template"] == "red"


def test_format_risk_alert_halt():
    """熔断告警颜色为红色。"""
    alert = {"level": "halt", "message": "日亏5%熔断", "suggestion": "减仓"}
    card = format_risk_alert(alert)
    assert card["card"]["header"]["template"] == "red"


def test_format_daily_report():
    """日报卡片格式。"""
    data = {
        "market_summary": {"total_value": 1_050_000, "cash": 500_000, "positions_count": 3},
        "sector_top": [{"sector": "白酒", "main_net": 1e9}],
        "signals": [],
        "portfolio_summary": {},
    }
    card = format_daily_report("close", data)
    assert card["msg_type"] == "interactive"


def test_format_backtest_report():
    """回测报告卡片。"""
    report = {
        "strategy": "turtle",
        "total_return": 0.15,
        "sharpe": 0.5,
        "max_drawdown": 0.05,
        "win_rate": 0.6,
        "trades_count": 10,
    }
    card = format_backtest_report(report)
    assert card["msg_type"] == "interactive"


def test_format_backtest_report_negative_return():
    """负收益回测报告为红色。"""
    report = {
        "strategy": "turtle",
        "total_return": -0.05,
        "sharpe": -0.5,
        "max_drawdown": 0.05,
        "win_rate": 0.4,
        "trades_count": 5,
    }
    card = format_backtest_report(report)
    assert card["card"]["header"]["template"] == "red"


def test_format_backtest_report_invalid_return():
    """异常 total_return fallback。"""
    report = {
        "strategy": "turtle",
        "total_return": "invalid",
        "sharpe": 0.5,
        "max_drawdown": 0.05,
        "win_rate": 0.6,
        "trades_count": 10,
    }
    card = format_backtest_report(report)
    assert card["msg_type"] == "interactive"


def test_format_daily_report_empty():
    """空日报。"""
    card = format_daily_report("close", {})
    assert card["msg_type"] == "interactive"


def test_format_system_error():
    """系统异常卡片。"""
    from gugu.notifier.formatter import format_system_error

    card = format_system_error(
        {"module": "data", "message": "采集失败", "suggestion": "重试"}
    )
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "red"


def test_format_signals_empty():
    """_format_signals 空输入。"""
    from gugu.notifier.formatter import _format_signals

    assert _format_signals([]) == "无"


def test_format_signals_list_of_dicts():
    """_format_signals 信号 dict 列表。"""
    from gugu.notifier.formatter import _format_signals

    signals = [
        {"direction": "buy", "symbol": "600519", "name": "茅台"},
        {"direction": "sell", "symbol": "000001", "name": "平安"},
    ]
    text = _format_signals(signals)
    assert "买入" in text
    assert "卖出" in text


def test_format_signal_uses_strategies_fallback():
    """signal 中 strategy 缺失时，使用 strategies 列表拼接。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "strategies": ["dual_ma", "bollinger"],
        "suggested_position_ratio": 0.24,
        "price": 1500.0,
    }
    card = format_signal(signal)
    # elements: [div, hr, div, hr, note] → 第 2 个 div 是 index 2
    content = card["card"]["elements"][2]["text"]["content"]
    assert "dual_ma,bollinger" in content
    assert "24%" in content


def test_feishu_notifier_skip_when_unconfigured():
    """未配置飞书时发送直接返回 False 且不抛异常。"""
    from gugu.notifier.feishu import FeishuNotifier

    with mock.patch("gugu.notifier.feishu.env") as mock_env:
        mock_env.return_value.feishu_app_id = ""
        notifier = FeishuNotifier()
        result = notifier._is_configured()
        assert result is False


@pytest.mark.asyncio
async def test_feishu_notifier_token_and_send():
    """Mock 飞书 token 与消息发送完整路径。"""
    from gugu.notifier.feishu import FeishuNotifier

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "fake-token", "expire": 7200},
            )
        return httpx.Response(200, json={"code": 0, "msg": "ok"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with mock.patch("gugu.notifier.feishu.env") as mock_env:
        mock_env.return_value.feishu_app_id = "app-id"
        mock_env.return_value.feishu_app_secret = "secret"
        mock_env.return_value.feishu_chat_id = "chat-id"
        notifier = FeishuNotifier()
        notifier._client = mock_client
        ok = await notifier.notify_signal({"symbol": "600519", "direction": "buy", "price": 1.0})
        assert ok is True
        assert len(requests) == 2  # token + message

    await mock_client.aclose()


def test_format_signal_with_wisdom():
    """信号卡片应展示 wisdom 建议内容。"""
    signal = {
        "symbol": "600519",
        "name": "贵州茅台",
        "direction": "buy",
        "strategy": "turtle",
        "reason": "突破上轨",
        "suggested_position": "20%",
        "price": 1500.0,
        "wisdom": {
            "entry_check": "# 入场建议\n- 等待回调确认",
            "stop_loss": "# 止损建议\n- 跌破 20 日均线止损",
            "position_sizing": "# 仓位建议\n- 单股不超过 30%",
            "psychology_check": "# 心态检查\n- 保持冷静",
        },
    }
    card = format_signal(signal)
    # 应有 3 个 section（股票/策略 + wisdom），即 5 个 elements（3 div + 2 hr）
    elements = card["card"]["elements"]
    # 找到包含 wisdom 的 section
    wisdom_section = None
    for elem in elements:
        if elem.get("tag") == "div":
            content = elem.get("text", {}).get("content", "")
            if "交易智慧参考" in content:
                wisdom_section = content
                break
    assert wisdom_section is not None
    assert "入场建议" in wisdom_section
    assert "止损建议" in wisdom_section
    assert "仓位建议" in wisdom_section


def test_format_signal_without_wisdom():
    """无 wisdom 字段时不应展示交易智慧参考 section。"""
    signal = {
        "symbol": "600519",
        "name": "贵州茅台",
        "direction": "buy",
        "strategy": "turtle",
        "reason": "突破上轨",
        "price": 1500.0,
    }
    card = format_signal(signal)
    elements = card["card"]["elements"]
    for elem in elements:
        if elem.get("tag") == "div":
            content = elem.get("text", {}).get("content", "")
            assert "交易智慧参考" not in content


def test_format_signal_empty_wisdom():
    """wisdom 字段为空字典时不应展示交易智慧参考 section。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "wisdom": {},
    }
    card = format_signal(signal)
    elements = card["card"]["elements"]
    for elem in elements:
        if elem.get("tag") == "div":
            content = elem.get("text", {}).get("content", "")
            assert "交易智慧参考" not in content


def test_format_signal_with_wisdom_decision():
    """信号卡片应展示智慧决策（止损价/仓位调整/入场过滤）。"""
    signal = {
        "symbol": "600519",
        "name": "贵州茅台",
        "direction": "buy",
        "strategy": "turtle",
        "reason": "突破上轨",
        "suggested_position_ratio": 0.05,
        "price": 1500.0,
        "wisdom_decision": {
            "entry_filtered": False,
            "adjusted_position_ratio": 0.05,
            "position_strategy": "trial",
            "stop_loss_price": 1380.0,
            "stop_loss_pct": 0.08,
        },
        "wisdom": {"entry_check": "入场建议"},
    }
    card = format_signal(signal)
    elements = card["card"]["elements"]
    # 找到包含智慧决策的 section
    decision_section = None
    for elem in elements:
        if elem.get("tag") == "div":
            content = elem.get("text", {}).get("content", "")
            if "智慧决策" in content:
                decision_section = content
                break
    assert decision_section is not None
    assert "仓位调整" in decision_section
    assert "试仓" in decision_section
    assert "止损预设" in decision_section
    assert "1380" in decision_section


def test_format_signal_entry_filtered():
    """入场过滤的信号应用黄色卡片并标注已过滤。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "wisdom_decision": {
            "entry_filtered": True,
            "filter_reason": "置信度 0.40 低于入场阈值 0.6",
        },
    }
    card = format_signal(signal)
    assert card["card"]["header"]["template"] == "yellow"
    assert "已过滤" in card["card"]["header"]["title"]["content"]


# ========== A-06 飞书决策摘要 ==========


def test_format_decision_chain_empty():
    """空的 decision_chain 返回空字符串。"""
    from gugu.notifier.formatter import _format_decision_chain
    assert _format_decision_chain([]) == ""


def test_format_decision_chain_all_passed():
    """完整决策链路，全部通过。"""
    from gugu.notifier.formatter import _format_decision_chain

    chain = [
        {"step": 0, "name": "四阶段判断", "result": "normal_up", "passed": True},
        {"step": 0.5, "name": "危险信号检测", "result": "none", "passed": True},
        {"step": 1, "name": "基本面过滤", "result": "pass", "passed": True, "reasons": []},
        {"step": 2.5, "name": "向下摊平检查", "result": "allowed", "passed": True},
    ]
    text = _format_decision_chain(chain)
    for name in ("四阶段判断", "危险信号检测", "基本面过滤", "向下摊平检查"):
        assert name in text
    assert "✅" in text
    assert "❌" not in text


def test_format_decision_chain_with_filter():
    """某层过滤时显示 ❌ 和原因。"""
    from gugu.notifier.formatter import _format_decision_chain

    chain = [
        {"step": 0, "name": "四阶段判断", "result": "normal_up", "passed": True},
        {"step": 1, "name": "基本面过滤", "result": "fail", "passed": False,
         "reasons": ["PE异常高"]},
    ]
    text = _format_decision_chain(chain)
    assert "❌" in text
    assert "基本面过滤" in text
    assert "fail" in text


def test_format_signal_with_decision_chain():
    """信号卡片含 decision_chain 时展示决策链路 section。"""
    signal = {
        "symbol": "600519",
        "name": "贵州茅台",
        "direction": "buy",
        "strategy": "turtle",
        "reason": "突破上轨",
        "price": 1500.0,
        "decision_chain": [
            {"step": 0, "name": "四阶段判断", "result": "normal_up", "passed": True},
            {"step": 5, "name": "Wisdom决策", "result": "buy", "passed": True,
             "reason": "趋势良好"},
        ],
    }
    card = format_signal(signal)
    content = str(card)
    assert "决策链路" in content
    assert "四阶段判断" in content
    assert "Wisdom决策" in content


# ========== A-04 修复验证: _resolve_name 多层 fallback ==========


def test_resolve_name_normal():
    """正常 name 应直接返回。"""
    from gugu.notifier.formatter import _resolve_name

    signal = {"symbol": "600519", "name": "贵州茅台"}
    assert _resolve_name(signal) == "贵州茅台"


def test_resolve_name_missing():
    """name 缺失时从内置映射查找。"""
    from gugu.notifier.formatter import _resolve_name

    signal = {"symbol": "600519"}
    assert _resolve_name(signal) == "贵州茅台"


def test_resolve_name_empty():
    """name 为空字符串时从内置映射查找。"""
    from gugu.notifier.formatter import _resolve_name

    signal = {"symbol": "600519", "name": ""}
    assert _resolve_name(signal) == "贵州茅台"


def test_resolve_name_equal_to_symbol():
    """name 等于代码本身时（API 脏数据），从内置映射查找。"""
    from gugu.notifier.formatter import _resolve_name

    signal = {"symbol": "600519", "name": "600519"}
    assert _resolve_name(signal) == "贵州茅台"


def test_resolve_name_unknown_symbol():
    """不在内置映射表的股票回退为代码。"""
    from gugu.notifier.formatter import _resolve_name

    signal = {"symbol": "000001"}
    assert _resolve_name(signal) == "000001"


def test_format_signal_missing_name():
    """A-04: name 缺失时应从内置映射补全，不显示"未知"或"600519(600519)"。"""
    signal = {
        "symbol": "600519",
        "direction": "buy",
        "price": 1.0,
    }
    card = format_signal(signal)
    title = card["card"]["header"]["title"]["content"]
    assert "贵州茅台" in title
    assert "未知" not in title
    assert "600519(600519)" not in title


def test_format_signal_name_equal_code():
    """A-04: name 等于代码时（API 脏数据），从内置映射补全。"""
    signal = {
        "symbol": "600519",
        "name": "600519",
        "direction": "buy",
        "price": 1.0,
    }
    card = format_signal(signal)
    title = card["card"]["header"]["title"]["content"]
    assert "贵州茅台" in title
    assert "600519(600519)" not in title


def test_format_signal_empty_name():
    """A-04: name 为空字符串时从内置映射补全。"""
    signal = {
        "symbol": "600519",
        "name": "",
        "direction": "buy",
        "price": 1.0,
    }
    card = format_signal(signal)
    title = card["card"]["header"]["title"]["content"]
    assert "贵州茅台" in title
    assert "未知" not in title


def test_format_signals_missing_name():
    """A-04: _format_signals 中 name 缺失时从内置映射补全。"""
    from gugu.notifier.formatter import _format_signals

    signals = [
        {"direction": "buy", "symbol": "600519"},
        {"direction": "sell", "symbol": "000858"},
    ]
    text = _format_signals(signals)
    assert "贵州茅台" in text
    assert "五粮液" in text
    assert "600519(600519)" not in text


def test_format_daily_report_portfolio_missing_name():
    """A-04: 日报持仓中 name 缺失时从内置映射补全。"""
    portfolio = {
        "600519": {"quantity": 100, "profit": 5000, "market_value": 150000},
        "000858": {"quantity": 200, "name": "", "profit": -2000, "market_value": 300000},
    }
    data = {
        "market_summary": {"total_value": 1_000_000, "cash": 500_000, "positions_count": 2},
        "signals": [],
        "portfolio_summary": portfolio,
    }
    card = format_daily_report("close", data)
    content = str(card)
    assert "贵州茅台" in content
    assert "五粮液" in content


def test_format_daily_report_close_enhanced():
    """A-06: 收盘日报含市场状态、风控、移动止损信息。"""
    data = {
        "market_summary": {"total_value": 1_000_000, "cash": 500_000, "positions_count": 2},
        "signals": [],
        "portfolio_summary": {},
        "regime": {"regime": "sideways", "total_limit": 0.4, "reason": "横盘震荡"},
        "risk": {"halted": False, "daily_loss_pct": 0.01},
        "trailing_stops": [
            {"symbol": "600519", "current_stop": 1420.0, "highest": 1500.0, "signal": "hold"},
        ],
    }
    card = format_daily_report("close", data)
    content = str(card)
    assert "市场状态" in content
    assert "风控状态" in content
    assert "移动止损" in content
    assert "600519" in content
    assert "宕" not in content  # 不应看到熔断
