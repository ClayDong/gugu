"""多重检验校正：防止「试了 30 组参数挑出最好一组」被当成 alpha。

2026-11-04 评审：项目存在大量网格搜索 / 事件研究，但没有任何
Bonferroni / FDR 校正产物。显著结论必须报告校正后的判定。
"""

from __future__ import annotations

from typing import Any


def _validate(p_values: list[float], alpha: float) -> None:
    if not 0 < alpha < 1:
        raise ValueError("alpha 必须位于 (0, 1)")
    if any(not 0 <= p <= 1 for p in p_values):
        raise ValueError("p 值必须位于 [0, 1]")


def bonferroni(p_values: list[float], alpha: float = 0.05) -> dict[str, Any]:
    """Bonferroni 校正：阈值收紧为 alpha / n。"""
    _validate(p_values, alpha)
    n = len(p_values)
    adjusted = alpha / n if n else alpha
    rejected = [bool(p <= adjusted) for p in p_values]
    return {
        "method": "bonferroni",
        "n_tests": n,
        "alpha": alpha,
        "adjusted_alpha": adjusted,
        "rejected": rejected,
        "n_significant": sum(rejected),
    }


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> dict[str, Any]:
    """BH-FDR 校正：控制错误发现率，比 Bonferroni 宽松但仍有约束。"""
    _validate(p_values, alpha)
    n = len(p_values)
    if n == 0:
        return {
            "method": "benjamini_hochberg",
            "n_tests": 0,
            "alpha": alpha,
            "rejected": [],
            "n_significant": 0,
        }
    ordered = sorted(range(n), key=lambda i: p_values[i])
    threshold = 0.0
    best_k = 0
    for k, idx in enumerate(ordered, start=1):
        bound = alpha * k / n
        if p_values[idx] <= bound:
            best_k = k
            threshold = bound
    rejected = [False] * n
    for idx in ordered[:best_k]:
        rejected[idx] = True
    return {
        "method": "benjamini_hochberg",
        "n_tests": n,
        "alpha": alpha,
        "fdr_threshold": threshold,
        "rejected": rejected,
        "n_significant": best_k,
    }


def report_correction(p_values: list[float], alpha: float = 0.05) -> dict[str, Any]:
    """同时给出两种校正结果，供验证脚本写入证据报告。"""
    naive = sum(1 for p in p_values if p <= alpha)
    return {
        "n_tests": len(p_values),
        "naive_significant": naive,
        "bonferroni": bonferroni(p_values, alpha),
        "benjamini_hochberg": benjamini_hochberg(p_values, alpha),
    }
