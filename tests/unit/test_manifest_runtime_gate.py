"""manifest 验证状态到运行时角色的映射测试。

2026-11-04 更新：生产准入新增 evidence 完整性校验（防自证循环）。
2026-11-05 更新：证据必须绑定策略代码指纹，策略改版后旧证据立即失效。
原有语义（分层回撤 / paper 观察 / enabled=false）全部保留，
并补充「手写 metrics 无法放行」「改版后旧证据无法放行」的负向测试。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from gugu.strategies.manifest import StrategyManifest
from gugu.validation.code_fingerprint import declared_params, strategy_fingerprint
from gugu.validation.evidence import build_evidence

# 夹具用一个真实存在的策略模块，这样「当前代码指纹」是可解析的真值。
DEMO_MODULE = "gugu.strategies.two_b_reversal"

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


def _fingerprint(key: str = "demo") -> str:
    """与 manifest 运行时同一套算法：模块源码 + 声明参数。"""
    value = strategy_fingerprint(DEMO_MODULE, params=declared_params(key))
    assert value, "测试夹具依赖的策略模块必须可解析"
    return value


def _manifest(
    tmp_path: Path,
    status: str,
    enabled: bool = True,
    metrics: dict[str, float] | None = None,
    *,
    with_evidence: bool = True,
    evidence_patch: dict | None = None,
    module: str | None = DEMO_MODULE,
) -> StrategyManifest:
    gate: dict = {"status": status}
    if metrics is not None:
        gate["metrics"] = dict(metrics)
    if with_evidence and metrics is not None:
        evidence = build_evidence(
            metrics,
            evidence_class="walk_forward_oos",
            code_fingerprint=_fingerprint(),
            generated_at="2026-11-04T00:00:00",
            source="unit-test",
        )
        if evidence_patch:
            evidence.update(evidence_patch)
        gate["gate_v2"] = evidence
    entry: dict = {"enabled": enabled, "validation_gate": gate}
    if module is not None:
        entry["module"] = module
    data = {"signal_strategies": {"demo": entry}}
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


class TestEvidenceBoundToCodeVersion:
    """证据必须绑定策略代码版本，否则改版后旧指标会继续冒充有效。

    现实来源：two_b_reversal 在 2026-09-04 改过退出逻辑，但 manifest 里
    2026-09-23 复核的 ``excess_5d=+2.18%`` 一直留在 metrics 里。用项目自己的
    四种标准口径（收盘/次开 × 有/无成本）重跑，最高只到 +0.31%。
    哈希只能证明数字没被改，证明不了数字还对应当前代码。
    """

    def test_fingerprint_mismatch_is_rejected(self, tmp_path):
        m = _manifest(
            tmp_path,
            "enabled",
            metrics=VALID_METRICS,
            evidence_patch={"code_fingerprint": "b" * 64},
        )
        assert m.list_runtime_enabled("track_live") == []

    def test_missing_fingerprint_is_rejected(self, tmp_path):
        m = _manifest(
            tmp_path,
            "enabled",
            metrics=VALID_METRICS,
            evidence_patch={"code_fingerprint": ""},
        )
        assert m.list_runtime_enabled("track_live") == []

    def test_unresolvable_module_blocks_production(self, tmp_path):
        """模块解析不到就不能校验代码版本，必须拒绝而不是跳过。"""
        m = _manifest(tmp_path, "enabled", metrics=VALID_METRICS, module="gugu.not_exist_xyz")
        assert m.list_runtime_enabled("track_live") == []

    def test_module_missing_from_manifest_blocks_production(self, tmp_path):
        m = _manifest(tmp_path, "enabled", metrics=VALID_METRICS, module=None)
        assert m.list_runtime_enabled("track_live") == []

    def test_current_code_fingerprint_is_exposed(self, tmp_path):
        """指纹必须可被外部审计复算，否则无法离线核对证据是否过期。"""
        m = _manifest(tmp_path, "enabled", metrics=VALID_METRICS)
        assert m.current_code_fingerprint("demo") == _fingerprint()
        assert m.current_code_fingerprint("missing") is None

    def test_paper_mode_ignores_code_fingerprint(self, tmp_path):
        """代码版本校验只约束生产准入；paper 仍按 status 放行。"""
        m = _manifest(
            tmp_path,
            "conditional_pass",
            metrics=VALID_METRICS,
            evidence_patch={"code_fingerprint": "b" * 64},
        )
        assert m.list_runtime_enabled("paper") == ["demo"]


# ── 策略关闭必须带证据，且不许被静默改回 ─────────────────


class TestRetiredStrategy:
    """two_b_reversal 于 2026-11-05 关闭。

    关闭依据不是"感觉不行"，而是两条独立证据同向：
    扣掉同池漂移后 3 日净 alpha -0.12%（t=-2.00，39 只中 16 只为正），
    以及 Walk-Forward 样本外 Sharpe -0.360、三窗口参数互不一致。

    钉这条测试的目的：`enabled: false` 是唯一挡住它重回 paper 的开关，
    而 manifest 是纯文本。若有人为了"多一个策略试试"把它翻回 true，
    测试会立刻失败，并被迫面对 disabled_reason。
    """

    def test_realtime_manifest_blocks_paper_and_production(self):
        m = StrategyManifest()
        assert m.is_runtime_enabled("two_b_reversal", "paper") is False
        assert m.is_runtime_enabled("two_b_reversal", "track_live") is False
        assert "two_b_reversal" not in m.list_runtime_enabled("paper")

    def test_manifest_records_evidence_for_the_retirement(self):
        info = StrategyManifest().get_strategy("two_b_reversal")
        assert info is not None
        assert info["enabled"] is False
        reason = str(info.get("disabled_reason") or "")
        # 数字必须写进 manifest，而不是只活在对话里
        assert "t=-2.00" in reason
        assert "0.360" in reason
        assert info.get("disabled_at") == "2026-11-05"

    def test_strategy_class_still_importable_for_backtest(self):
        """关闭的是运行时准入，不是代码。回测仍要能调到它，否则无法复验结论。"""
        from gugu.strategies.registry import get_strategy

        assert get_strategy("two_b_reversal").name == "two_b_reversal"
