"""manifest 验证状态到运行时角色的映射测试。"""

from __future__ import annotations

from gugu.strategies.manifest import StrategyManifest


def _manifest(
    tmp_path, status: str, enabled: bool = True, metrics: dict[str, float] | None = None
) -> StrategyManifest:
    path = tmp_path / "manifest.yaml"
    metrics_yaml = ""
    if metrics is not None:
        metrics_yaml = "      metrics:\n" + "".join(
            f"        {key}: {value}\n" for key, value in metrics.items()
        )
    path.write_text(
        "signal_strategies:\n"
        f"  demo:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    validation_gate:\n"
        f"      status: {status}\n"
        f"{metrics_yaml}",
        encoding="utf-8",
    )
    return StrategyManifest(path)


class TestManifestRuntimeGate:
    def test_production_requires_complete_gate_v1_evidence(self, tmp_path):
        valid_metrics = {
            "total_trades": 120,
            "avg_return": 0.03,
            "median_return": 0.02,
            "avg_sharpe": 0.2,
            "max_drawdown": 0.1,
            "profitable_ratio": 0.6,
            "param_stability": 0.9,
            "paper_signals": 40,
            "paper_return": 0.01,
        }
        assert _manifest(tmp_path, "enabled", metrics=valid_metrics).list_runtime_enabled(
            "track_live"
        ) == ["demo"]
        assert (
            _manifest(tmp_path, "conditional_pass", metrics=valid_metrics).list_runtime_enabled(
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
