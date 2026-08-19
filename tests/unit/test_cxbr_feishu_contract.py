"""CXBR 飞书消息契约测试。"""

from gugu.notifier.formatter import format_cxbr_signal


def _text(card: dict) -> str:
    return "\n".join(
        element.get("text", {}).get("content", "")
        for element in card["card"]["elements"]
        if element.get("tag") == "div"
    )


def test_observe_is_explicitly_non_trading() -> None:
    card = format_cxbr_signal(
        {
            "action": "observe",
            "stage": "next_day_watch",
            "code": "600001",
            "asof": "2026-08-18",
            "reason": "已完成手板和洗盘，等待确认",
            "data_quality": "fresh_valid",
        }
    )
    assert "观察" in card["card"]["header"]["title"]["content"]
    assert "买入条件成立" not in card["card"]["header"]["title"]["content"]
    assert "不代表实盘成交" in _text(card) or "不代表实盘成交" in str(card)


def test_buy_contains_rule_stop_and_invalidation_fields() -> None:
    card = format_cxbr_signal(
        {
            "action": "buy",
            "stage": "entry",
            "code": "600001",
            "asof": "2026-08-18",
            "rule_version": "cxbr-v1",
            "reason": "买点 A 成立",
            "buy_point": "A",
            "price": 10.2,
            "strict_stop_price": 9.9,
            "conditional_stop_price": 9.8,
            "suggested_position": 0.2,
            "data_quality": "fresh_valid",
            "invalidation": "收盘有效跌破 MA10",
        }
    )
    text = _text(card)
    assert "cxbr-v1" in text
    assert "严格止损" in text
    assert "条件单提醒" in text
    assert "失效条件" in text
    assert "模拟盘" in str(card)
