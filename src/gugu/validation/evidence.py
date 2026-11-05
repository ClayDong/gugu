"""生产准入证据完整性校验（防「自证循环」）。

问题：manifest 里的 metrics 是人手可编辑的数字，旧门禁只验证这些数字彼此
一致，等于「用被审计对象自己证明自己」。本模块要求准入证据由验证脚本生成，
带 metrics 的规范化哈希与来源信息；运行时校验哈希，任何篡改都会让准入失败。

2026-11-05 补强（evidence-v2）：哈希只能证明「数字没被改过」，证明不了
「数字还对应现在这份代码」。策略 2026-09-04 改过退出逻辑之后，manifest 里
``excess_5d=+2.18%`` 用四种标准口径都复现不出（最高 +0.31%），但旧证据
依然算有效。v2 因此强制携带 ``code_fingerprint``（模块源码 + 声明参数），
运行时用当前代码重算比对，代码一变旧证据即失效——失败方向是拒绝准入。

用法（由 scripts/validate_strategy_gate.py 等验证脚本调用）：
    from gugu.validation.code_fingerprint import strategy_fingerprint

    evidence = build_evidence(
        metrics,
        evidence_class="walk_forward_oos",
        code_fingerprint=strategy_fingerprint(module, params=params),
        source="walk_forward_offline.py",
    )
    gate["gate_v2"] = evidence
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

GATE_EVIDENCE_VERSION = "evidence-v2"

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
    code_fingerprint: str,
    generated_at: str | None = None,
    source: str = "validate_strategy_gate.py",
    passed: bool = True,
) -> dict[str, Any]:
    """由验证产物生成证据块。禁止手工构造 passed=True 而无 metrics 哈希/代码指纹。

    ``code_fingerprint`` 必须由 :func:`gugu.validation.code_fingerprint.strategy_fingerprint`
    从真实策略模块算得；空值直接拒绝，不提供「不知道代码是哪版」的放行路径。
    """
    if evidence_class not in ALLOWED_EVIDENCE_CLASSES:
        raise ValueError(f"非法 evidence_class: {evidence_class}")
    if not isinstance(metrics, dict) or not metrics:
        raise ValueError("metrics 不能为空")
    fingerprint = str(code_fingerprint or "").strip()
    if not fingerprint:
        raise ValueError("code_fingerprint 不能为空：证据必须绑定策略代码版本")
    return {
        "version": GATE_EVIDENCE_VERSION,
        "passed": bool(passed),
        "evidence_class": evidence_class,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "metrics_hash": canonical_metrics_hash(metrics),
        "code_fingerprint": fingerprint,
    }


def validate_evidence(
    gate: dict[str, Any],
    *,
    expected_code_fingerprint: str | None = None,
) -> tuple[bool, str]:
    """校验 validation_gate 的证据块与 metrics、代码版本是否一致。

    Args:
        gate: ``validation_gate`` 字典。
        expected_code_fingerprint: 当前代码算出的指纹。传入则比对，不一致判
            ``stale_strategy_code``；不传只校验指纹存在（供离线审计使用）。
            运行时准入必须传，否则等于放弃代码版本校验。

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
    recorded_fingerprint = str(evidence.get("code_fingerprint") or "").strip()
    if not recorded_fingerprint:
        return False, "missing_code_fingerprint"

    metrics = gate.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        return False, "missing_metrics"
    expected = canonical_metrics_hash(metrics)
    actual = str(evidence.get("metrics_hash", ""))
    if not hmac.compare_digest(actual, expected):
        return False, "metrics_hash_mismatch"

    if expected_code_fingerprint is not None:
        current = str(expected_code_fingerprint or "").strip()
        if not current:
            return False, "missing_code_fingerprint"
        if not hmac.compare_digest(recorded_fingerprint, current):
            return False, "stale_strategy_code"
    return True, ""
