"""断板反包研究层语义标签。

该模块只负责校验研究标签，不产生买卖信号、仓位或订单。
LLM 的输出必须先通过 :func:`parse_semantic_label`，才能进入研究数据集。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Final, Literal, TypedDict

ThemeState = Literal["主线", "支线", "孤立", "未知"]
ThemeStrength = Literal["强", "中", "弱", "未知"]
BoardRole = Literal["核心", "中军", "跟风", "未知"]
IntradayAcceptance = Literal["强", "中", "弱", "未知"]
WashQuality = Literal["有效", "存疑", "无效", "未知"]

SEMANTIC_FIELDS: Final = (
    "theme_state",
    "theme_strength",
    "board_role",
    "intraday_acceptance",
    "wash_quality",
)
REQUIRED_FIELDS: Final = frozenset(
    {
        "asof",
        "source_ids",
        *SEMANTIC_FIELDS,
        "uncertainty",
        "evidence_spans",
        "model_version",
        "prompt_version",
    }
)
FORBIDDEN_DECISION_FIELDS: Final = frozenset(
    {
        "buy",
        "sell",
        "signal",
        "position",
        "position_size",
        "order",
        "entry",
        "exit",
        "take_profit",
        "stop_loss",
    }
)


class ReboundSemanticLabel(TypedDict):
    """可序列化的研究标签固定结构。"""

    asof: str
    source_ids: list[str]
    theme_state: ThemeState
    theme_strength: ThemeStrength
    board_role: BoardRole
    intraday_acceptance: IntradayAcceptance
    wash_quality: WashQuality
    uncertainty: float
    evidence_spans: list[str]
    model_version: str
    prompt_version: str


class SemanticLabelError(ValueError):
    """语义标签不符合研究层契约。"""


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SemanticLabelError(f"{field_name} 必须是非空 ISO-8601 字符串")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SemanticLabelError(f"{field_name} 不是有效 ISO-8601 时间") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _normalise_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _reject_decision_fields(payload: dict[object, object]) -> None:
    forbidden = {
        key
        for key in payload
        if _normalise_key(key) in {_normalise_key(item) for item in FORBIDDEN_DECISION_FIELDS}
    }
    if forbidden:
        names = ", ".join(sorted(str(item) for item in forbidden))
        raise SemanticLabelError(f"语义标签不得包含交易决策字段: {names}")


def validate_semantic_label(
    payload: object,
    *,
    data_cutoff: str,
) -> ReboundSemanticLabel:
    """校验固定 JSON 标签，并保证标签不使用截止时点之后的信息。"""
    if not isinstance(payload, dict):
        raise SemanticLabelError("标签根节点必须是 JSON 对象")

    _reject_decision_fields(payload)
    keys = set(payload)
    missing = REQUIRED_FIELDS - keys
    extra = keys - REQUIRED_FIELDS
    if missing:
        raise SemanticLabelError(f"缺少必填字段: {', '.join(sorted(missing))}")
    if extra:
        raise SemanticLabelError(
            f"存在未允许字段: {', '.join(sorted(str(item) for item in extra))}"
        )

    asof = _parse_datetime(payload["asof"], "asof")
    cutoff = _parse_datetime(data_cutoff, "data_cutoff")
    if asof > cutoff:
        raise SemanticLabelError("asof 晚于输入数据截止时间，疑似未来信息泄漏")

    if not isinstance(payload["source_ids"], list) or not all(
        isinstance(item, str) and item.strip() for item in payload["source_ids"]
    ):
        raise SemanticLabelError("source_ids 必须是非空字符串列表")
    if not payload["source_ids"]:
        raise SemanticLabelError("source_ids 不能为空")

    if not isinstance(payload["evidence_spans"], list) or not all(
        isinstance(item, str) and item.strip() for item in payload["evidence_spans"]
    ):
        raise SemanticLabelError("evidence_spans 必须是字符串列表")

    enum_values = {
        "theme_state": {"主线", "支线", "孤立", "未知"},
        "theme_strength": {"强", "中", "弱", "未知"},
        "board_role": {"核心", "中军", "跟风", "未知"},
        "intraday_acceptance": {"强", "中", "弱", "未知"},
        "wash_quality": {"有效", "存疑", "无效", "未知"},
    }
    for field, allowed in enum_values.items():
        if payload[field] not in allowed:
            raise SemanticLabelError(f"{field} 不是允许的枚举值")

    uncertainty = payload["uncertainty"]
    if isinstance(uncertainty, bool) or not isinstance(uncertainty, (int, float)):
        raise SemanticLabelError("uncertainty 必须是 0 到 1 的数字")
    if not 0 <= float(uncertainty) <= 1:
        raise SemanticLabelError("uncertainty 必须位于 [0, 1]")

    for field in ("model_version", "prompt_version"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise SemanticLabelError(f"{field} 必须是非空字符串")

    if not payload["evidence_spans"]:
        non_unknown = [field for field in SEMANTIC_FIELDS if payload[field] != "未知"]
        if non_unknown:
            raise SemanticLabelError("缺少证据时，语义字段只能输出‘未知’")

    return {
        "asof": str(payload["asof"]),
        "source_ids": list(payload["source_ids"]),
        "theme_state": payload["theme_state"],
        "theme_strength": payload["theme_strength"],
        "board_role": payload["board_role"],
        "intraday_acceptance": payload["intraday_acceptance"],
        "wash_quality": payload["wash_quality"],
        "uncertainty": float(uncertainty),
        "evidence_spans": list(payload["evidence_spans"]),
        "model_version": str(payload["model_version"]),
        "prompt_version": str(payload["prompt_version"]),
    }


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def parse_semantic_label(content: str, *, data_cutoff: str) -> ReboundSemanticLabel:
    """解析 LLM JSON 文本并执行全部研究层校验。"""
    if not isinstance(content, str) or not content.strip():
        raise SemanticLabelError("LLM 输出为空")
    try:
        payload = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError as exc:
        raise SemanticLabelError("LLM 输出不是有效 JSON") from exc
    return validate_semantic_label(payload, data_cutoff=data_cutoff)
