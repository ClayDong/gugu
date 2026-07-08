"""盘中异动告警模块测试。"""
from __future__ import annotations

from gugu.engine.intraday_monitor import AlertItem
from gugu.notifier.intraday_alert import (
    build_intraday_alert_card,
    format_intraday_alert_text,
)


def _make_alert(
    symbol: str = "600519",
    name: str = "贵州茅台",
    alert_type: str = "super_large_inflow",
    severity: str = "warning",
    direction: str = "buy",
    value: float = 5000.0,
    message: str = "主力净流入 5000 万",
    timestamp: str = "2026-07-07 10:30:00",
) -> AlertItem:
    return AlertItem(
        alert_type=alert_type,
        symbol=symbol,
        name=name,
        severity=severity,
        direction=direction,
        value=value,
        message=message,
        timestamp=timestamp,
    )


class TestBuildIntradayAlertCard:
    """build_intraday_alert_card 函数测试。"""

    def test_empty_alerts_returns_grey_card(self) -> None:
        """空告警列表返回灰色"无异动"卡片。"""
        card = build_intraday_alert_card([])
        assert card["header"]["template"] == "grey"
        assert "无异动" in card["elements"][0]["text"]["content"]

    def test_critical_alert_uses_red_template(self) -> None:
        """严重告警使用红色模板。"""
        alerts = [_make_alert(severity="critical", message="暴跌警告")]
        card = build_intraday_alert_card(alerts)
        assert card["header"]["template"] == "red"
        assert "严重" in card["header"]["title"]["content"]

    def test_warning_alert_uses_orange_template(self) -> None:
        """警告告警使用橙色模板。"""
        alerts = [_make_alert(severity="warning", message="资金流出")]
        card = build_intraday_alert_card(alerts)
        assert card["header"]["template"] == "orange"

    def test_info_alert_uses_blue_template(self) -> None:
        """信息级告警使用蓝色模板。"""
        alerts = [_make_alert(severity="info", message="温和流入")]
        card = build_intraday_alert_card(alerts)
        assert card["header"]["template"] == "blue"

    def test_mixed_severity_groups_correctly(self) -> None:
        """混合严重度按 critical/warning/info 分组。"""
        alerts = [
            _make_alert(severity="critical", message="暴跌"),
            _make_alert(severity="warning", message="流出"),
            _make_alert(severity="info", message="流入"),
        ]
        card = build_intraday_alert_card(alerts)
        elements = card["elements"]
        # 应包含 3 个分组 div + 1 个建议 div + 1 个 note
        assert len(elements) >= 4
        # 第一组是严重告警
        assert "暴跌" in elements[0]["text"]["content"]

    def test_buy_dominant_suggestion(self) -> None:
        """买入方向占多时建议关注买入机会。"""
        alerts = [
            _make_alert(direction="buy", message="流入1"),
            _make_alert(direction="buy", message="流入2"),
            _make_alert(direction="sell", message="流出1"),
        ]
        card = build_intraday_alert_card(alerts)
        suggestion_text = card["elements"][-2]["text"]["content"]
        assert "买入机会" in suggestion_text

    def test_sell_dominant_suggestion(self) -> None:
        """卖出方向占多时建议警惕减仓。"""
        alerts = [
            _make_alert(direction="sell", message="流出1"),
            _make_alert(direction="sell", message="流出2"),
            _make_alert(direction="buy", message="流入1"),
        ]
        card = build_intraday_alert_card(alerts)
        suggestion_text = card["elements"][-2]["text"]["content"]
        assert "减仓" in suggestion_text

    def test_balanced_suggestion(self) -> None:
        """买卖均衡时显示均衡文案。"""
        alerts = [
            _make_alert(direction="buy", message="流入1"),
            _make_alert(direction="sell", message="流出1"),
        ]
        card = build_intraday_alert_card(alerts)
        suggestion_text = card["elements"][-2]["text"]["content"]
        assert "均衡" in suggestion_text

    def test_card_has_timestamp_note(self) -> None:
        """卡片末尾包含时间戳 note。"""
        alerts = [_make_alert(timestamp="2026-07-07 14:00:00")]
        card = build_intraday_alert_card(alerts)
        note = card["elements"][-1]
        assert note["tag"] == "note"
        assert "2026-07-07 14:00:00" in note["elements"][0]["content"]


class TestFormatIntradayAlertText:
    """format_intraday_alert_text 函数测试。"""

    def test_empty_alerts(self) -> None:
        """空列表返回无异动文案。"""
        text = format_intraday_alert_text([])
        assert "异动" in text

    def test_with_alerts(self) -> None:
        """有告警时返回包含消息的文本。"""
        alerts = [
            _make_alert(message="主力流入 5000 万"),
            _make_alert(message="大宗交易 3000 万"),
        ]
        text = format_intraday_alert_text(alerts)
        assert "2 条" in text
        assert "主力流入 5000 万" in text
        assert "大宗交易 3000 万" in text
