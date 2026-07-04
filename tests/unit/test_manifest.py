"""策略注册中心测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from gugu.strategies.manifest import StrategyManifest, get_manifest

# 策略清单路径
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "config" / "strategy_manifest.yaml"


@pytest.fixture(scope="module")
def manifest() -> StrategyManifest:
    """加载策略清单（全局一次）。"""
    if not _MANIFEST_PATH.exists():
        pytest.skip(f"策略清单文件不存在: {_MANIFEST_PATH}")
    return StrategyManifest(manifest_path=_MANIFEST_PATH)


class TestManifestLoading:
    """测试策略清单加载。"""

    def test_manifest_loads_successfully(self, manifest):
        """策略清单应成功加载。"""
        data = manifest.list_all()
        assert data is not None
        assert len(data) > 0

    def test_contains_expected_categories(self, manifest):
        """应包含预期的策略类别。"""
        data = manifest.list_all()
        expected = {"signal_strategies", "risk_strategies", "skill_strategies", "dynamic_strategies"}
        actual = {k for k in data if isinstance(data[k], dict)}
        assert expected.issubset(actual), f"缺失类别: {expected - actual}"

    def test_manifest_yaml_is_valid(self):
        """YAML 文件格式应合法。"""
        content = _MANIFEST_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        assert data is not None
        assert isinstance(data, dict)

    def test_enabled_strategies(self, manifest):
        """至少有一些策略是启用的。"""
        enabled = manifest.list_enabled()
        assert len(enabled) >= 5, f"启用策略太少: {len(enabled)}"

    def test_disabled_strategies(self, manifest):
        """应有禁用策略（不全是启用状态）。"""
        disabled = manifest.list_disabled()
        assert len(disabled) >= 1


class TestManifestQueries:
    """测试策略查询功能。"""

    def test_list_by_category(self, manifest):
        """按类别查询应返回正确类型的数据。"""
        signal = manifest.list_by_category("signal_strategies")
        assert isinstance(signal, dict)
        assert len(signal) > 0

        risk = manifest.list_by_category("risk_strategies")
        assert isinstance(risk, dict)
        assert len(risk) > 0

        skill = manifest.list_by_category("skill_strategies")
        assert isinstance(skill, dict)
        assert len(skill) > 0

    def test_get_strategy_exists(self, manifest):
        """查询存在的策略应返回其信息。"""
        info = manifest.get_strategy("turtle")
        assert info is not None
        assert info.get("name") == "海龟交易法则"
        assert info.get("enabled") is True

    def test_get_strategy_not_exists(self, manifest):
        """查询不存在的策略应返回 None。"""
        info = manifest.get_strategy("nonexistent_strategy")
        assert info is None

    def test_list_by_source(self, manifest):
        """按出处关键词查询应返回匹配的策略。"""
        results = manifest.list_by_source("炒股的智慧")
        assert len(results) >= 3, f"查到 {len(results)} 个，预期 >= 3"

    def test_get_params(self, manifest):
        """获取策略参数。"""
        params = manifest.get_params("trailing_stop")
        assert isinstance(params, dict)
        assert "initial_stop_pct" in params
        assert params["initial_stop_pct"] == 0.08

    def test_get_params_not_exists(self, manifest):
        """获取不存在的策略参数应返回空 dict。"""
        params = manifest.get_params("nonexistent")
        assert params == {}


class TestManifestStats:
    """测试策略统计。"""

    def test_stats_structure(self, manifest):
        """统计信息应包含必要字段。"""
        stats = manifest.stats()
        assert "total" in stats
        assert "enabled" in stats
        assert "disabled" in stats
        assert "by_category" in stats
        assert stats["total"] > 0

    def test_stats_total_equals_enabled_plus_disabled(self, manifest):
        """总数应等于启用+禁用。"""
        stats = manifest.stats()
        assert stats["total"] == stats["enabled"] + stats["disabled"]

    def test_stats_by_category_counts_match(self, manifest):
        """按类别的计数应与实际条目数一致。"""
        stats = manifest.stats()
        for category, count in stats["by_category"].items():
            actual = len(manifest.list_by_category(category))
            assert count == actual, f"{category}: {count} != {actual}"


class TestManifestFormatting:
    """测试策略看板格式化。"""

    def test_format_dashboard(self, manifest):
        """看板格式应包含核心信息。"""
        dashboard = manifest.format_dashboard()
        assert "策略注册中心" in dashboard
        assert "🟢" in dashboard
        assert "⚪" in dashboard
        assert "signal_strategies" not in dashboard  # 应使用中文类别名

    def test_format_compact(self, manifest):
        """精简版看板应包含策略名称。"""
        compact = manifest.format_compact()
        assert "策略注册中心" in compact
        assert "风控" in compact
        assert "信号" in compact
        # 至少包含一个策略名称
        assert "移动止损" in compact or "海龟" in compact

    def test_dashboard_contains_sources(self, manifest):
        """看板应包含出处信息。"""
        dashboard = manifest.format_dashboard()
        assert "炒股的智慧" in dashboard


class TestManifestEdgeCases:
    """测试边界情况。"""

    def test_non_existent_file(self, tmp_path):
        """不存在的文件应静默处理。"""
        fake_path = tmp_path / "nonexistent.yaml"
        m = StrategyManifest(manifest_path=fake_path)
        assert m.list_all() == {}
        assert m.list_enabled() == []
        assert m.stats()["total"] == 0

    def test_get_manifest_singleton(self):
        """get_manifest 应返回单例。"""
        m1 = get_manifest()
        m2 = get_manifest()
        assert m1 is m2

    def test_signal_strategy_details(self, manifest):
        """信号策略应包含 module/class 字段。"""
        for key in ["turtle", "dual_ma", "bollinger", "rsi_reversal"]:
            info = manifest.get_strategy(key)
            assert info is not None, f"缺失策略: {key}"
            assert "module" in info, f"{key} 缺少 module"
            assert "class" in info, f"{key} 缺少 class"

    def test_risk_strategy_params(self, manifest):
        """风控策略应有参数或参数入口说明。"""
        for key in ["trailing_stop", "danger_signal", "stage_detector"]:
            info = manifest.get_strategy(key)
            assert info is not None, f"缺失策略: {key}"
            has_params = "params" in info or "params_entry" in info
            assert has_params, f"{key} 缺少 params 和 params_entry"


class TestManifestSettingsConsistency:
    """P1-8: 配置权威源一致性校验。

    manifest.yaml 与 settings.yaml 是两个独立配置源：
    - settings.yaml: strategy.enabled 列表 = 实际启用的策略
    - manifest.yaml: 每个策略的 enabled 字段 = 元数据描述

    两者必须保持一致，否则会出现：
    - settings 启用了 manifest 中没有的策略 → 看板缺失该策略
    - manifest enabled=true 但 settings 没启用 → 看板显示与实际不符
    """

    def test_settings_enabled_strategies_exist_in_manifest(self, manifest):
        """settings.yaml 启用的策略必须在 manifest 中有元数据。"""
        from gugu.config import get_config

        settings_enabled = set(get_config().strategy.enabled)
        manifest_keys = set()
        for category_strategies in manifest.list_all().values():
            if isinstance(category_strategies, dict):
                manifest_keys.update(category_strategies.keys())

        missing_in_manifest = settings_enabled - manifest_keys
        assert not missing_in_manifest, (
            f"settings.yaml 启用的策略在 manifest 中缺失: {missing_in_manifest}. "
            f"请在 config/strategy_manifest.yaml 中添加对应条目。"
        )

    def test_manifest_enabled_signal_strategies_match_settings(self, manifest):
        """manifest 中 signal_strategies 的 enabled=true 集合应与 settings.yaml 一致。"""
        from gugu.config import get_config

        settings_enabled = set(get_config().strategy.enabled)
        manifest_signal = manifest.list_by_category("signal_strategies")
        manifest_enabled_signal = {
            key for key, info in manifest_signal.items()
            if isinstance(info, dict) and info.get("enabled", False)
        }

        # settings 启用的 signal 策略应在 manifest 中 enabled=true
        not_enabled_in_manifest = settings_enabled - manifest_enabled_signal
        # 允许 settings 启用非 signal 类别的策略（如 risk_strategies）
        # 所以只检查 signal 类别中的策略
        signal_only_in_settings = not_enabled_in_manifest & set(manifest_signal.keys())
        assert not signal_only_in_settings, (
            f"以下 signal 策略在 settings.yaml 启用但在 manifest 中 enabled=false/缺失: "
            f"{signal_only_in_settings}"
        )