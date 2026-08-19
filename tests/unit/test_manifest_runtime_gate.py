"""manifest 验证状态到运行时角色的映射测试。"""

from __future__ import annotations

from gugu.strategies.manifest import StrategyManifest


def _manifest(tmp_path, status: str, enabled: bool = True) -> StrategyManifest:
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "signal_strategies:\n"
        f"  demo:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    validation_gate:\n"
        f"      status: {status}\n",
        encoding="utf-8",
    )
    return StrategyManifest(path)


class TestManifestRuntimeGate:
    def test_enabled_status_is_required_for_track_live(self, tmp_path):
        assert _manifest(tmp_path, "enabled").list_runtime_enabled("track_live") == ["demo"]
        assert _manifest(tmp_path, "conditional_pass").list_runtime_enabled("track_live") == []

    def test_paper_allows_conditional_but_not_review_states(self, tmp_path):
        assert _manifest(tmp_path, "conditional_pass").list_runtime_enabled("paper") == ["demo"]
        for status in ("pending", "in_review", "backtest_failed", "failed"):
            assert _manifest(tmp_path, status).list_runtime_enabled("paper") == []

    def test_disabled_metadata_never_runs(self, tmp_path):
        assert _manifest(tmp_path, "enabled", enabled=False).list_runtime_enabled("paper") == []
