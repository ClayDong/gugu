"""策略注册表过滤测试（v0.9.7 P0/P1 修复）。

覆盖：
- hot_money 已从注册表移除（2026-08-01 样本外证伪）
- get_enabled_strategies() 在 track_live 模式排除 experimental 策略
- 指数代码白名单识别（000300 → sh）
"""

from __future__ import annotations

from unittest.mock import patch

from gugu.data.collectors.base import BaseCollector
from gugu.strategies.registry import _REGISTRY, list_strategies


class TestHotMoneyRemoved:
    """hot_money 幽灵策略清除验证。"""

    def test_hot_money_not_in_registry(self):
        """hot_money 不应再出现在注册表中。"""
        assert "hot_money" not in _REGISTRY
        assert "hot_money" not in list_strategies()

    def test_get_strategy_hot_money_raises(self):
        """直接获取 hot_money 应抛 KeyError（fail-fast，防止幽灵参与融合）。"""
        from gugu.strategies.registry import get_strategy

        try:
            get_strategy("hot_money")
            raised = False
        except KeyError:
            raised = True
        assert raised is True


class TestIndexCodeRecognition:
    """指数代码白名单识别。"""

    def test_hs300_is_sh_index(self):
        assert BaseCollector.is_index_code("000300") is True
        assert BaseCollector.symbol_with_prefix("000300") == "sh000300"

    def test_shenzhen_index_is_sz(self):
        assert BaseCollector.is_index_code("399001") is True
        assert BaseCollector.symbol_with_prefix("399001") == "sz399001"

    def test_stock_not_index(self):
        """平安银行(000001)/国城矿业(000688) 是深市股票，不在白名单。"""
        assert BaseCollector.is_index_code("000001") is False
        assert BaseCollector.is_index_code("000688") is False
        assert BaseCollector.symbol_with_prefix("000001") == "sz000001"
        assert BaseCollector.symbol_with_prefix("000688") == "sz000688"


class TestEnabledStrategiesFiltering:
    """get_enabled_strategies() 按 execution_mode 过滤 experimental（P1-1）。"""

    def test_track_live_uses_verified_allowlist(self):
        """track_live 使用 verified 白名单，实验策略仍不得加载。"""
        with patch("gugu.config.get_config") as mock_cfg:
            mock_cfg.return_value.execution.mode = "track_live"
            strategy_cfg = mock_cfg.return_value.strategy
            strategy_cfg.verified_strategies = ["box_breakout", "double_yin"]
            strategy_cfg.experimental_strategies = ["chenxuezhang", "linjudaye"]
            strategy_cfg.enabled_by_regime = {
                "sideways": ["box_breakout", "double_yin", "chenxuezhang"],
            }
            strategy_cfg.enabled = []
            mock_cfg.return_value.strategy.get_enabled_for_regime.return_value = [
                "box_breakout",
                "double_yin",
            ]

            from gugu.strategies.registry import get_enabled_strategies

            get_enabled_strategies.cache_clear()
            strategies = get_enabled_strategies()
            names = {s.name for s in strategies}
            assert {"box_breakout", "double_yin"} <= names
            assert "chenxuezhang" not in names
            assert "linjudaye" not in names
            get_enabled_strategies.cache_clear()
