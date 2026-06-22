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


def test_format_signals_string():
    """_format_signals 字符串输入。"""
    from gugu.notifier.formatter import _format_signals

    assert _format_signals("hold") == "hold"


def test_format_signals_empty():
    """_format_signals 空输入。"""
    from gugu.notifier.formatter import _format_signals

    assert _format_signals([]) == "无"
    assert _format_signals(None) == "无"


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
