"""配置一致性 CI 测试。

校验 strategy_manifest.yaml 与 settings.yaml 之间的策略启用状态一致性，
以及所有信号策略在 _REGISTRY 中正确注册。

一致性校验范围说明：
仅针对 signal_strategies 类别进行 manifest ↔ settings 一致性校验。
- settings.yaml 的 enabled_by_regime / enabled 列表仅控制信号策略启用
- risk_strategies（gugu.analysis.*）由 SignalPipeline 直接实例化，不受 settings 控制
- skill_strategies（wisdom skills）由 wisdom 模块管理，无 Python 策略类
- dynamic_strategies（LLM 生成器）不参与运行时策略调度
以上三类不在 _REGISTRY 中注册，也不纳入一致性校验。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gugu.config import get_config
from gugu.strategies.registry import _REGISTRY

# 策略清单文件路径（从测试文件位置推算项目根目录，与 test_manifest.py 一致）
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "config" / "strategy_manifest.yaml"


@pytest.fixture(scope="module")
def manifest_data() -> dict:
    """加载 strategy_manifest.yaml 原始数据（yaml.safe_load，模块级共享）。"""
    if not _MANIFEST_PATH.exists():
        pytest.skip(f"策略清单文件不存在: {_MANIFEST_PATH}")
    with _MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), "strategy_manifest.yaml 顶层结构应为 dict"
    return data


@pytest.fixture(scope="module")
def signal_strategies(manifest_data: dict) -> dict:
    """提取 signal_strategies 类别的策略。

    signal_strategies 是唯一受 settings.yaml enabled_by_regime / enabled 控制的类别，
    也是唯一要求在 _REGISTRY 中注册的类别。
    """
    strategies = manifest_data.get("signal_strategies", {})
    assert isinstance(strategies, dict), "signal_strategies 应为 dict"
    return strategies


@pytest.fixture(scope="module")
def settings_enabled_set() -> set[str]:
    """获取 settings.yaml 中所有启用的信号策略名称集合。

    v0.7.5 P0-1修复：包含 verified_strategies + experimental_strategies。
    合并 enabled_by_regime 各 regime 策略并集 + enabled 列表 + verified + experimental。
    """
    cfg = get_config().strategy
    enabled_set: set[str] = set()
    for regime_strategies in cfg.enabled_by_regime.values():
        enabled_set.update(regime_strategies)
    enabled_set.update(cfg.enabled)
    # v0.7.5 P0-1修复：包含 verified + experimental
    if cfg.verified_strategies:
        enabled_set.update(cfg.verified_strategies)
    if cfg.experimental_strategies:
        enabled_set.update(cfg.experimental_strategies)
    return enabled_set


@pytest.fixture(scope="module")
def registry_keys() -> set[str]:
    """获取 _REGISTRY 中已注册的策略名称集合。"""
    return set(_REGISTRY.keys())


class TestWatchlistMonitoringConfig:
    """自选监控列表配置测试。"""

    def test_default_watchlist_matches_current_targets(self):
        """默认监控列表包含基础自选股和用户真实持仓。"""
        from gugu.config import get_config, refresh_config

        get_config.cache_clear()
        cfg = refresh_config()
        # P1-5 优化：扩大自选股池到 18 只（13 只自选 + 5 只 ETF）
        expected = {
            "600519",
            "000858",
            "600276",
            "300760",
            "300750",
            "002594",
            "002472",
            "603501",
            "002371",
            "601012",
            "600438",
            "300033",
            "002230",
            "562500",
            "510500",
            "588000",
            "510310",
            "512480",
        }
        assert set(cfg.watchlist) == expected
        assert len(cfg.watchlist) == 18

    def test_intraday_custom_watchlist_matches_default_watchlist(self):
        """盘中实时盯盘范围与默认自选列表保持一致。"""
        from gugu.config import refresh_config

        cfg = refresh_config()
        assert cfg.intraday_monitor.custom_watchlist == cfg.watchlist

    def test_execution_mode_is_safe_for_simulation(self):
        """执行模式必须是安全模式（非 live），保护用户资金。

        允许的模式：
        - paper: 模拟盘交易
        - signal_only: 仅信号通知不下单
        - track_live: 实盘持仓跟踪（只读用户真实持仓，不自动交易）
        禁止: live（实盘交易需显式启用券商接口）
        """
        from gugu.config import refresh_config

        cfg = refresh_config()
        assert cfg.execution.mode in ("paper", "signal_only", "track_live"), (
            f"执行模式 {cfg.execution.mode} 不是安全模式，"
            f"生产环境禁止使用 live 模式（除非显式启用券商接口）"
        )


class TestManifestSettingsConsistency:
    """校验 strategy_manifest.yaml 与 settings.yaml 的启用状态一致性。

    两个独立配置源必须保持一致：
    - settings.yaml: enabled / enabled_by_regime = 实际启用的信号策略
    - manifest.yaml: 每个策略的 enabled 字段 = 元数据描述
    不一致会导致看板显示与实际运行不符。
    """

    def test_manifest_enabled_appear_in_settings(
        self, signal_strategies: dict, settings_enabled_set: set[str]
    ):
        """2a: manifest 中 enabled=true 的信号策略，必须在 settings.yaml 启用列表中出现。

        启用列表 = enabled_by_regime 各 regime 并集 ∪ enabled 列表。
        """
        manifest_enabled = {
            key
            for key, info in signal_strategies.items()
            if isinstance(info, dict) and info.get("enabled", False)
        }
        missing_in_settings = manifest_enabled - settings_enabled_set
        assert not missing_in_settings, (
            f"以下策略在 manifest 中 enabled=true 但未在 settings.yaml 启用列表中: "
            f"{sorted(missing_in_settings)}。"
            f"settings 启用集合: {sorted(settings_enabled_set)}"
        )

    def test_manifest_disabled_not_in_settings(
        self, signal_strategies: dict, settings_enabled_set: set[str]
    ):
        """2b: manifest 中 enabled=false 的信号策略，不应出现在 settings.yaml 任何启用列表中。"""
        manifest_disabled = {
            key
            for key, info in signal_strategies.items()
            if isinstance(info, dict) and not info.get("enabled", False)
        }
        wrongly_enabled = manifest_disabled & settings_enabled_set
        assert not wrongly_enabled, (
            f"以下策略在 manifest 中 enabled=false 但在 settings.yaml 启用列表中出现: "
            f"{sorted(wrongly_enabled)}。"
            f"请在 manifest 中设为 enabled=true 或从 settings.yaml 移除。"
        )

    def test_settings_enabled_by_regime_exists_and_enabled_in_manifest(
        self, signal_strategies: dict
    ):
        """2c: settings.yaml 中 enabled_by_regime 的所有策略，必须在 manifest 中存在且 enabled=true。"""
        cfg = get_config().strategy
        # 收集 enabled_by_regime 中所有出现的策略
        regime_strategies: set[str] = set()
        for strategies in cfg.enabled_by_regime.values():
            regime_strategies.update(strategies)

        # 检查 manifest 中存在
        missing_in_manifest = regime_strategies - set(signal_strategies.keys())
        assert not missing_in_manifest, (
            f"以下策略在 settings.yaml enabled_by_regime 中但 manifest signal_strategies 缺失: "
            f"{sorted(missing_in_manifest)}"
        )

        # 检查 manifest 中 enabled=true
        not_enabled_in_manifest = {
            key
            for key in regime_strategies
            if key in signal_strategies
            and isinstance(signal_strategies[key], dict)
            and not signal_strategies[key].get("enabled", False)
        }
        assert not not_enabled_in_manifest, (
            f"以下策略在 settings.yaml enabled_by_regime 启用但 manifest 中 enabled=false: "
            f"{sorted(not_enabled_in_manifest)}"
        )


class TestStrategyRegistryRegistration:
    """校验策略在 _REGISTRY 中正确注册。"""

    def test_manifest_signal_strategies_in_registry(
        self, signal_strategies: dict, registry_keys: set[str]
    ):
        """3: strategy_manifest.yaml 中 signal_strategies 的所有策略必须在 _REGISTRY 中注册。

        _REGISTRY 仅管理信号生成策略（gugu.strategies.*）。
        risk_strategies（gugu.analysis.*）与 skill_strategies（wisdom skills）
        由各自模块直接实例化，不在 _REGISTRY 范围内。
        """
        manifest_signal_keys = set(signal_strategies.keys())
        unregistered = manifest_signal_keys - registry_keys
        assert not unregistered, (
            f"以下 signal 策略在 manifest 中声明但未在 _REGISTRY 注册: "
            f"{sorted(unregistered)}。"
            f"请在 src/gugu/strategies/registry.py 的 _REGISTRY 中添加。"
        )

    def test_settings_enabled_strategies_in_registry(
        self, settings_enabled_set: set[str], registry_keys: set[str]
    ):
        """4: settings.yaml 中所有启用的策略名称必须在 _REGISTRY 中注册。"""
        unregistered = settings_enabled_set - registry_keys
        assert not unregistered, (
            f"以下策略在 settings.yaml 中启用但未在 _REGISTRY 注册: "
            f"{sorted(unregistered)}。"
            f"请在 src/gugu/strategies/registry.py 的 _REGISTRY 中添加。"
        )
