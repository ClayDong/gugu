"""生产准入证据完整性校验（防「自证循环」）。

问题：manifest 里的 metrics 是人手可编辑的数字，旧门禁只验证这些数字彼此
一致，等于「用被审计对象自己证明自己」。本模块要求准入证据由验证脚本生成，
带 metrics 的规范化哈希与来源信息；运行时校验哈希，任何篡改都会让准入失败。

用法（由 scripts/validate_strategy_gate.py 等验证脚本调用）：
    evidence = build_evidence(metrics, evidence_class="walk_forward_oos",
                              generated_at=..., source="walk_forward_offline.py")
    gate["gate_v2"] = evidence
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

GATE_EVIDENCE_VERSION = "evidence-v1"

ALLOWED_EVIDENCE_CLASSES = frozenset(
    {
        "walk_forward_oos",  # 滚动样本外检验
        "forward_paper",  # 前向模拟盘已实现盈亏
        "audited_manual",  # 人工截图核验 + 复核签字
    }
)


def canonical_metrics_hash(metrics: dict[str, Any]) -> str:
    """对 metrics 做规范化 JSON 哈希（键排序、去多余空白）。"""
    payload = json.dumps(
        metrics,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_evidence(
    metrics: dict[str, Any],
    *,
    evidence_class: str,
    generated_at: str | None = None,
    source: str = "validate_strategy_gate.py",
    passed: bool = True,
) -> dict[str, Any]:
    """由验证产物生成证据块。禁止手工构造 passed=True 而无 metrics 哈希。"""
    if evidence_class not in ALLOWED_EVIDENCE_CLASSES:
        raise ValueError(f"非法 evidence_class: {evidence_class}")
    if not isinstance(metrics, dict) or not metrics:
        raise ValueError("metrics 不能为空")
    return {
        "version": GATE_EVIDENCE_VERSION,
        "passed": bool(passed),
        "evidence_class": evidence_class,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "metrics_hash": canonical_metrics_hash(metrics),
    }


def validate_evidence(gate: dict[str, Any]) -> tuple[bool, str]:
    """校验 validation_gate 的证据块与 metrics 是否一致。

    Returns:
        (是否通过, 失败原因码)。失败原因码便于日志与测试断言。
    """
    evidence = gate.get("gate_v2")
    if not isinstance(evidence, dict):
        return False, "missing_gate_v2_evidence"
    if evidence.get("version") != GATE_EVIDENCE_VERSION:
        return False, "unsupported_evidence_version"
    if evidence.get("passed") is not True:
        return False, "evidence_not_passed"
    if evidence.get("evidence_class") not in ALLOWED_EVIDENCE_CLASSES:
        return False, "invalid_evidence_class"
    if not evidence.get("generated_at") or not evidence.get("source"):
        return False, "missing_provenance"

    metrics = gate.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        return False, "missing_metrics"
    expected = canonical_metrics_hash(metrics)
    actual = str(evidence.get("metrics_hash", ""))
    if not hmac.compare_digest(actual, expected):
        return False, "metrics_hash_mismatch"
    return True, ""
