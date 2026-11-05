"""验证证据模块的哈希与校验契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gugu.validation.evidence import (
    ALLOWED_EVIDENCE_CLASSES,
    GATE_EVIDENCE_VERSION,
    build_evidence,
    canonical_metrics_hash,
    validate_evidence,
)

METRICS = {"total_trades": 42, "avg_return": 0.012, "max_drawdown": 0.08}

# 纯证据契约测试里用一个固定指纹，不依赖任何策略模块是否存在。
FP = "a" * 64


def test_hash_is_order_independent() -> None:
    assert canonical_metrics_hash(METRICS) == canonical_metrics_hash(
        {"max_drawdown": 0.08, "avg_return": 0.012, "total_trades": 42}
    )


def test_hash_changes_when_metrics_change() -> None:
    assert canonical_metrics_hash(METRICS) != canonical_metrics_hash(
        {**METRICS, "avg_return": 0.99}
    )


def test_build_evidence_contains_provenance() -> None:
    ev = build_evidence(METRICS, evidence_class="walk_forward_oos", code_fingerprint=FP)
    assert ev["version"] == GATE_EVIDENCE_VERSION
    assert ev["metrics_hash"] == canonical_metrics_hash(METRICS)
    assert ev["code_fingerprint"] == FP
    assert ev["generated_at"] and ev["source"]


def test_build_evidence_requires_code_fingerprint() -> None:
    """没有代码指纹就没有证据——不提供「不知道是哪版代码」的放行路径。"""
    with pytest.raises(ValueError, match="code_fingerprint"):
        build_evidence(METRICS, evidence_class="walk_forward_oos", code_fingerprint="")
    with pytest.raises(ValueError, match="code_fingerprint"):
        build_evidence(METRICS, evidence_class="walk_forward_oos", code_fingerprint="   ")


def test_valid_evidence_passes() -> None:
    gate = {
        "status": "enabled",
        "metrics": METRICS,
        "gate_v2": build_evidence(METRICS, evidence_class="forward_paper", code_fingerprint=FP),
    }
    assert validate_evidence(gate) == (True, "")
    assert validate_evidence(gate, expected_code_fingerprint=FP) == (True, "")


def test_stale_code_fingerprint_is_rejected() -> None:
    """策略改版后代码指纹变了，旧证据必须失效。"""
    gate = {
        "status": "enabled",
        "metrics": METRICS,
        "gate_v2": build_evidence(METRICS, evidence_class="forward_paper", code_fingerprint=FP),
    }
    passed, reason = validate_evidence(gate, expected_code_fingerprint="b" * 64)
    assert (passed, reason) == (False, "stale_strategy_code")


def test_empty_expected_fingerprint_is_rejected() -> None:
    """调用方拿不到当前指纹时不能静默跳过比对。"""
    gate = {
        "status": "enabled",
        "metrics": METRICS,
        "gate_v2": build_evidence(METRICS, evidence_class="forward_paper", code_fingerprint=FP),
    }
    passed, reason = validate_evidence(gate, expected_code_fingerprint="")
    assert (passed, reason) == (False, "missing_code_fingerprint")


def test_invalid_evidence_class_raises() -> None:
    with pytest.raises(ValueError):
        build_evidence(METRICS, evidence_class="made_up", code_fingerprint=FP)


def test_empty_metrics_raises() -> None:
    with pytest.raises(ValueError):
        build_evidence({}, evidence_class="forward_paper", code_fingerprint=FP)


def test_missing_evidence_block_is_rejected() -> None:
    assert validate_evidence({"status": "enabled", "metrics": METRICS})[1] == (
        "missing_gate_v2_evidence"
    )


@pytest.mark.parametrize(
    ("patch", "expected_reason"),
    [
        ({"version": "evidence-v0"}, "unsupported_evidence_version"),
        ({"version": "evidence-v1"}, "unsupported_evidence_version"),
        ({"passed": False}, "evidence_not_passed"),
        ({"evidence_class": "bogus"}, "invalid_evidence_class"),
        ({"source": ""}, "missing_provenance"),
        ({"code_fingerprint": ""}, "missing_code_fingerprint"),
        ({"metrics_hash": "0" * 64}, "metrics_hash_mismatch"),
    ],
)
def test_validation_rejects_bad_evidence(patch, expected_reason) -> None:
    evidence = build_evidence(METRICS, evidence_class="walk_forward_oos", code_fingerprint=FP)
    evidence.update(patch)
    gate = {"status": "enabled", "metrics": METRICS, "gate_v2": evidence}
    assert validate_evidence(gate)[1] == expected_reason


def test_allowed_classes_are_explicit() -> None:
    assert ALLOWED_EVIDENCE_CLASSES == {
        "walk_forward_oos",
        "forward_paper",
        "audited_manual",
    }


def test_validate_strategy_gate_emits_matching_evidence() -> None:
    """验证脚本产出的 evidence 必须能被运行时校验通过（闭环）。"""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "validate_strategy_gate.py"
    spec = importlib.util.spec_from_file_location("vsg_under_test", path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    metrics = {
        "total_trades": 120,
        "avg_return": 0.03,
        "median_return": 0.01,
        "avg_sharpe": 0.35,
        "max_drawdown": 0.12,
        "profitable_ratio": 0.56,
        "profit_factor": 2.0,
        "param_stability": 0.8,
        "paper_signals": 35,
        "paper_return": 0.02,
    }
    report = script.evaluate_gate("box_breakout", metrics)
    assert report["passed"] is True
    assert "evidence_error" not in report
    evidence = report["evidence"]
    gate = {"status": "enabled", "metrics": metrics, "gate_v2": evidence}

    assert validate_evidence(gate) == (True, "")
    # 闭环的真正意义：运行时用当前代码重算的指纹，必须与脚本写入的一致。
    expected = script._code_fingerprint("box_breakout")
    assert expected is not None
    assert validate_evidence(gate, expected_code_fingerprint=expected) == (True, "")
