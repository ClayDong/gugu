from gugu.notifier.beginner_card import format_beginner_signal_card


def test_unverified_observation_card_is_not_actionable():
    card = format_beginner_signal_card(
        {
            "symbol": "600519",
            "strategy": "two_b_reversal",
            "validation_status": "experimental",
            "observation_only": True,
            "confidence": 0.8,
            "gate_reason": "未通过 Gate",
        },
        {"mode": "ice_point_buy"},
    )
    text = card["card"]["elements"][0]["text"]["content"]
    assert "仅观察" in text
    assert "已验证" not in text
    assert "可执行买入" not in text


def test_gate_blocked_card_says_forbidden():
    card = format_beginner_signal_card(
        {"symbol": "920001", "strategy": "dragon_head", "rejected": True},
    )
    assert "禁止交易" in card["card"]["header"]["title"]["content"]
