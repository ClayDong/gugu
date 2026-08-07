"""v0.9.11 防线2：策略注册表一致性自检测试。

覆盖 verify_registry_consistency：
- 配置启用但代码未注册 → 告警
- 代码注册但配置未引用 → 告警（幽灵策略残留）
- 一致时返回空列表
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gugu.strategies.registry import verify_registry_consistency


class TestVerifyRegistryConsistency:
    def test_consistent_returns_empty(self):
        """配置与注册表一致 → 无 fatal 无 warning。"""
        with (
            patch(
                "gugu.strategies.registry._REGISTRY", {"box_breakout": object, "double_yin": object}
            ),
            patch("gugu.config.get_config") as mock_get,
        ):
            cfg = MagicMock()
            cfg.strategy.get_all_enabled_unique.return_value = ["box_breakout", "double_yin"]
            cfg.strategy.experimental_strategies = []
            mock_get.return_value = cfg

            fatal, warning = verify_registry_consistency()
            assert fatal == []
            assert warning == []

    def test_configured_but_not_registered(self):
        """配置启用但代码未注册 → fatal（拒绝启动）。"""
        with (
            patch("gugu.strategies.registry._REGISTRY", {"box_breakout": object}),
            patch("gugu.config.get_config") as mock_get,
        ):
            cfg = MagicMock()
            cfg.strategy.get_all_enabled_unique.return_value = ["box_breakout", "ghost_strategy"]
            cfg.strategy.experimental_strategies = []
            mock_get.return_value = cfg

            fatal, warning = verify_registry_consistency()
            assert any("配置启用但代码未注册: ghost_strategy" in i for i in fatal)

    def test_registered_but_not_configured(self):
        """代码注册但配置未引用 → warning（死代码残留，不阻塞启动）。"""
        with (
            patch(
                "gugu.strategies.registry._REGISTRY",
                {"box_breakout": object, "ghost_money": object},
            ),
            patch("gugu.config.get_config") as mock_get,
        ):
            cfg = MagicMock()
            cfg.strategy.get_all_enabled_unique.return_value = ["box_breakout"]
            cfg.strategy.experimental_strategies = []
            mock_get.return_value = cfg

            fatal, warning = verify_registry_consistency()
            assert fatal == []
            assert any("ghost_money" in i and "死代码残留" in i for i in warning)

    def test_experimental_not_flagged(self):
        """experimental 策略（paper 模式参与融合）不算死代码。"""
        with (
            patch(
                "gugu.strategies.registry._REGISTRY",
                {"box_breakout": object, "chenxuezhang": object},
            ),
            patch("gugu.config.get_config") as mock_get,
        ):
            cfg = MagicMock()
            cfg.strategy.get_all_enabled_unique.return_value = ["box_breakout"]
            cfg.strategy.experimental_strategies = ["chenxuezhang"]
            mock_get.return_value = cfg

            fatal, warning = verify_registry_consistency()
            assert not any("chenxuezhang" in i for i in warning)
