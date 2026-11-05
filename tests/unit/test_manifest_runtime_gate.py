"""manifest 验证状态到运行时角色的映射测试。

2026-11-04 更新：生产准入新增 evidence 完整性校验（防自证循环）。
原有语义（分层回撤 / paper 观察 / enabled=false）全部保留，
并补充「手写 metrics 无法放行」的负向测试。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from gugu.strategies.manifest import StrategyManifest
from gugu.validation.evidence import build_evidence

VALID_METRICS = {
    "total_trades": 120,
    "avg_return": 0.03,
    "median_return": 0.02,
    "avg_sharpe": 0.2,
    "max_drawdown": 0.1,
    "profitable_ratio": 0.6,
    "profit_factor": 2.0,
    "param_stability": 0.9,
    "paper_signals": 40,
    "paper_return": 0.01,
}


def _manifest(
    tmp_path: Path,
    status: str,
    enabled: bool = True,
    metrics: dict[str, float] | None = None,
    *,
    with_evidence: bool = True,
    evidence_patch: dict | None = None,
) -> StrategyManifest:
    gate: dict = {"status": status}
    if metrics is not None:
        gate["metrics"] = dict(metrics)
    if with_evidence and metrics is not None:
        evidence = build_evidence(
            metrics,
            evidence_class="walk_forward_oos",
            generated_at="2026-11-04T00:00:00",
            source="unit-test",
        )
        if evidence_patch:
            evidence.update(evidence_patch)
        gate["gate_v2"] = evidence
    data = {"signal_strategies": {"demo": {"enabled": enabled, "validation_gate": gate}}}
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return StrategyManifest(path)


class TestManifestRuntimeGate:
    def test_production_requires_complete_gate_v2_evidence(self, tmp_path):
        assert _manifest(tmp_path, "enabled", metrics=VALID_METRICS).list_runtime_enabled(
            "track_live"
        ) == ["demo"]
        assert (
            _manifest(tmp_path, "conditional_pass", metrics=VALID_METRICS).list_runtime_enabled(
                "track_live"
            )
            == []
        )
        assert _manifest(tmp_path, "enabled").list_runtime_enabled("track_live") == []

    def test_paper_allows_conditional_but_not_review_states(self, tmp_path):
        assert _manifest(tmp_path, "conditional_pass").list_runtime_enabled("paper") == ["demo"]
        for status in ("pending", "in_review", "backtest_failed", "failed"):
            assert _manifest(tmp_path, status).list_runtime_enabled("paper") == []

    def test_disabled_metadata_never_runs(self, tmp_path):
        """即使 production 模式，enabled=false 仍被拒绝。"""
        assert (
            _manifest(tmp_path, "enabled", enabled=False).list_runtime_enabled("track_live") == []
        )
        assert _manifest(tmp_path, "enabled", enabled=False).list_runtime_enabled("paper") == []

    def test_production_layered_drawdown_with_category(self, tmp_path):
        """gate-v2 分层回撤：短线策略回撤 18%（<25% 旧阈值、≥15% 新阈值）传入 category 时
        应被拦截；不传 category 时退化为默认 25% 阈值（仍通过）。
        """
        metrics = {**VALID_METRICS, "max_drawdown": 0.18}
        assert _manifest(tmp_path, "enabled", metrics=metrics).list_runtime_enabled(
            "track_live"
        ) == ["demo"]

        m = _manifest(tmp_path, "enabled", metrics=metrics)
        m._data["signal_strategies"]["demo"]["validation_gate"]["gate_category"] = "trend"
        assert m.list_runtime_enabled("track_live") == []

        m2 = _manifest(tmp_path, "enabled", metrics=metrics)
        m2._data["signal_strategies"]["demo"]["validation_gate"]["gate_category"] = "main_force"
        assert m2.list_runtime_enabled("track_live") == ["demo"]


class TestEvidenceCannotBeSelfAttested:
    """篡改两个 YAML 也不得把未验证策略送进生产。"""

    def test_handwritten_metrics_without_evidence_is_rejected(self, tmp_path):
        assert (
            _manifest(
                tmp_path, "enabled", metrics=VALID_METRICS, with_evidence=False
            ).list_runtime_enabled("track_live")
            == []
        )

    def test_tampered_metrics_invalidates_hash(self, tmp_path):
        m = _manifest(tmp_path, "enabled", metrics=VALID_METRICS)
        m._data["signal_strategies"]["demo"]["validation_gate"]["metrics"]["avg_return"] = 0.99
        assert m.list_runtime_enabled("track_live") == []

    def test_wrong_hash_is_rejected(self, tmp_path):
        m = _manifest(
            tmp_path,
            "enabled",
            metrics=VALID_METRICS,
            evidence_patch={"metrics_hash": "deadbeef"},
        )
        assert m.list_runtime_enabled("track_live") == []

    def test_forged_evidence_class_is_rejected(self, tmp_path):
        m = _manifest(
            tmp_path,
            "enabled",
            metrics=VALID_METRICS,
            evidence_patch={"evidence_class": "trust_me"},
        )
        assert m.list_runtime_enabled("track_live") == []

    def test_passed_false_is_rejected(self, tmp_path):
        m = _manifest(
            tmp_path,
            "enabled",
            metrics=VALID_METRICS,
            evidence_patch={"passed": False},
        )
        assert m.list_runtime_enabled("track_live") == []

    def test_missing_provenance_is_rejected(self, tmp_path):
        m = _manifest(
            tmp_path,
            "enabled",
            metrics=VALID_METRICS,
            evidence_patch={"source": ""},
        )
        assert m.list_runtime_enabled("track_live") == []

    def test_paper_mode_does_not_require_evidence(self, tmp_path):
        """模拟盘必须继续积累验证数据，不能因证据门禁变成 0 成交。"""
        assert _manifest(
            tmp_path, "conditional_pass", metrics=VALID_METRICS, with_evidence=False
        ).list_runtime_enabled("paper") == ["demo"]
