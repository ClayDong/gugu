"""通知模块单元测试。"""
from __future__ import annotations

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
