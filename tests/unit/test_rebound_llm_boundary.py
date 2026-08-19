"""LLM 语义抽取脚本的安全边界测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest

from gugu.research.rebound_semantics import SemanticLabelError


SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "extract_chenxuezhang_rules_llm.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("rebound_llm_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_api_key_fails_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with mock.patch.object(module.httpx, "Client") as client:
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            module._call_llm("test")
    client.assert_not_called()


def test_source_ids_are_bound_to_caller_evidence() -> None:
    module = _load_module()
    label = {
        "asof": "2026-08-17T14:30:00+08:00",
        "source_ids": ["forged-source"],
        "theme_state": "未知",
        "theme_strength": "未知",
        "board_role": "未知",
        "intraday_acceptance": "未知",
        "wash_quality": "未知",
        "uncertainty": 1.0,
        "evidence_spans": [],
        "model_version": "test",
        "prompt_version": "rebound-semantic-v1",
    }
    with mock.patch.object(module, "_call_llm", return_value=json.dumps(label)):
        with pytest.raises(SemanticLabelError, match="source_ids"):
            module.extract_semantic_label(
                "材料",
                data_cutoff="2026-08-17T15:00:00+08:00",
                source_ids=["caller-source"],
            )
