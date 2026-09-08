"""新手决策卡片：展示状态，不把实验信号伪装成交易指令。"""

from __future__ import annotations

from typing import Any


def format_beginner_signal_card(
    signal: dict[str, Any],
    market_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a conservative beginner-facing signal card.

    This is deliberately separate from the legacy formatter so existing Feishu
    contracts remain unchanged. It never upgrades validation status.
    """
    capability = signal.get("capability") or {}
    status = str(
        capability.get("validation_status") or signal.get("validation_status") or "unknown"
    ).lower()
    observation = bool(signal.get("observation_only"))
    blocked = bool(signal.get("gate_blocked") or signal.get("rejected"))
    if blocked:
        verdict = "禁止交易"
        template = "grey"
    elif observation or status != "verified":
        verdict = "仅观察"
        template = "yellow"
    else:
        verdict = "可模拟成交"
        template = "blue"

    env = market_context or signal.get("market_env") or {}
    reason = (
        signal.get("gate_reason") or signal.get("filter_reason") or signal.get("reason") or "未提供"
    )
    sample = capability.get("sample_count", signal.get("sample_count", "未知"))
    max_loss = signal.get("max_loss_estimate") or signal.get("risk_estimate") or "暂无法估算"
    strategy = signal.get("strategy") or ",".join(signal.get("strategies") or []) or "未提供"
    symbol = signal.get("symbol", "")

    lines = [
        f"**决策状态**：{verdict}",
        f"**股票**：{symbol}",
        f"**策略**：{strategy}",
        f"**验证等级**：{status or 'unknown'}（样本 n={sample}）",
        f"**市场环境**：{env.get('mode', env.get('regime', 'unknown'))}",
        f"**计划最大亏损**：{max_loss}",
        f"**不买/观察原因**：{reason}",
    ]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"新手决策卡 · {verdict}"},
                "template": template,
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        },
    }
