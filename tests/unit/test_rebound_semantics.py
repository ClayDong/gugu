"""断板反包研究层语义标签契约测试。"""

from __future__ import annotations

import json

import pytest

from gugu.research.rebound_semantics import (
    SemanticLabelError,
    parse_semantic_label,
    validate_semantic_label,
)


CUTOFF = "2026-08-17T15:00:00+08:00"


def _label(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "asof": "2026-08-17T14:30:00+08:00",
        "source_ids": ["kline:000001:2026-08-17"],
        "theme_state": "主线",
        "theme_strength": "强",
        "board_role": "核心",
        "intraday_acceptance": "中",
        "wash_quality": "有效",
        "uncertainty": 0.25,
        "evidence_spans": ["收盘前可见的板块强度与承接证据"],
        "model_version": "deepseek-test",
        "prompt_version": "rebound-semantic-v1",
    }
    payload.update(overrides)
    return payload


def test_valid_label_is_fixed_and_serializable() -> None:
    label = validate_semantic_label(_label(), data_cutoff=CUTOFF)
    assert label["theme_state"] == "主线"
    assert json.loads(json.dumps(label, ensure_ascii=False))["uncertainty"] == 0.25


def test_asof_after_cutoff_is_rejected() -> None:
    with pytest.raises(SemanticLabelError, match="未来信息泄漏"):
        validate_semantic_label(_label(asof="2026-08-17T15:00:01+08:00"), data_cutoff=CUTOFF)


def test_illegal_enum_is_rejected() -> None:
    with pytest.raises(SemanticLabelError, match="枚举值"):
        validate_semantic_label(_label(theme_strength="极强"), data_cutoff=CUTOFF)


def test_uncertainty_must_be_in_range() -> None:
    with pytest.raises(SemanticLabelError, match=r"\[0, 1\]"):
        validate_semantic_label(_label(uncertainty=1.1), data_cutoff=CUTOFF)


def test_missing_evidence_forces_unknown() -> None:
    payload = _label(evidence_spans=[], theme_state="主线")
    with pytest.raises(SemanticLabelError, match="缺少证据"):
        validate_semantic_label(payload, data_cutoff=CUTOFF)


def test_decision_fields_are_rejected() -> None:
    with pytest.raises(SemanticLabelError, match="交易决策字段"):
        validate_semantic_label(_label(buy="是"), data_cutoff=CUTOFF)


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(SemanticLabelError, match="未允许字段"):
        validate_semantic_label(_label(comment="应该买"), data_cutoff=CUTOFF)


def test_json_fence_is_supported_but_still_validated() -> None:
    content = "```json\n" + json.dumps(_label(), ensure_ascii=False) + "\n```"
    label = parse_semantic_label(content, data_cutoff=CUTOFF)
    assert label["prompt_version"] == "rebound-semantic-v1"


def test_repeated_parse_is_deterministic() -> None:
    content = json.dumps(_label(), ensure_ascii=False)
    first = parse_semantic_label(content, data_cutoff=CUTOFF)
    second = parse_semantic_label(content, data_cutoff=CUTOFF)
    assert first == second
