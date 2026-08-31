"""策略层统一导出。

保留生产策略 (verified) 和实验策略 (experimental) 的导出。
僵尸策略已归档到 _archive/strategies/，不移除引用以保持向后兼容，
但通过条件 import 避免模块级加载报错。
"""

import logging

from gugu.strategies.base import Strategy, StrategyConfig
from gugu.strategies.breakout import BoxBreakoutStrategy, DualThrustStrategy
from gugu.strategies.double_yin import DoubleYinMoneyBagStrategy
from gugu.strategies.mean_revert import BollingerStrategy, KDJStrategy, RSIStrategy
from gugu.strategies.price_volume import PriceVolumeSignalStrategy
from gugu.strategies.regime_cycle import RegimeCycleFourStrategy
from gugu.strategies.registry import (
    get_enabled_strategies,
    get_enabled_strategies_for_regime,
    get_strategy,
    list_strategies,
)

logger = logging.getLogger(__name__)

# 兼容导入：已归档策略可选加载，不阻塞模块初始化
_ARCHIVED_STRATEGIES = {
    "BreakoutReboundStrategy": ("gugu.strategies.breakout_rebound", "BreakoutReboundStrategy"),
    "CloudTrendStrategy": ("gugu.strategies.cloud_trend", "CloudTrendStrategy"),
    "DragonTigerShortStrategy": ("gugu.strategies.dragon_tiger_short", "DragonTigerShortStrategy"),
    "IndustryCapitalMomentumStrategy": (
        "gugu.strategies.industry_capital_momentum",
        "IndustryCapitalMomentumStrategy",
    ),
    "NorthForceLongStrategy": ("gugu.strategies.north_force_long", "NorthForceLongStrategy"),
    "DualMAStrategy": ("gugu.strategies.trend", "DualMAStrategy"),
    "MACDStrategy": ("gugu.strategies.trend", "MACDStrategy"),
    "TurtleStrategy": ("gugu.strategies.trend", "TurtleStrategy"),
}


def __getattr__(name):
    """延迟加载已归档策略，避免模块级 ImportError。"""
    if name in _ARCHIVED_STRATEGIES:
        module_path, class_name = _ARCHIVED_STRATEGIES[name]
        try:
            import importlib

            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            # 缓存到模块命名空间
            globals()[name] = cls
            return cls
        except ModuleNotFoundError:
            logger.warning("归档策略 %s 不可用（已移至 _archive/strategies/）", name)
            return None
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BollingerStrategy",
    "BoxBreakoutStrategy",
    "DoubleYinMoneyBagStrategy",
    "DualThrustStrategy",
    "KDJStrategy",
    "PriceVolumeSignalStrategy",
    "RSIStrategy",
    "RegimeCycleFourStrategy",
    "Strategy",
    "StrategyConfig",
    "get_enabled_strategies",
    "get_enabled_strategies_for_regime",
    "get_strategy",
    "list_strategies",
]
