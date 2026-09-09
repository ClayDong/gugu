"""策略层统一导出。

保留生产策略 (verified) 和实验策略 (experimental) 的导出。
僵尸策略已归档到 _archive/strategies/。
"""

import logging

from gugu.strategies.base import Strategy, StrategyConfig
from gugu.strategies.breakout import BoxBreakoutStrategy
from gugu.strategies.double_yin import DoubleYinMoneyBagStrategy
from gugu.strategies.dragon_head import DragonHeadStrategy
from gugu.strategies.huamulan_ghost_breakout import HuamulanGhostBreakoutStrategy
from gugu.strategies.macd_zero_axis import MacdZeroAxisStrategy
from gugu.strategies.mean_revert import RSIStrategy
from gugu.strategies.price_volume import PriceVolumeSignalStrategy
from gugu.strategies.regime_cycle import RegimeCycleFourStrategy
from gugu.strategies.registry import (
    get_enabled_strategies,
    get_enabled_strategies_for_regime,
    get_strategy,
    list_strategies,
)
from gugu.strategies.sector_momentum import SectorMomentumStrategy
from gugu.strategies.sentiment_cycle import SentimentCycleStrategy
from gugu.strategies.two_b_reversal import TwoBReversalStrategy
from gugu.strategies.yege_mainline_explosion import YegeMainlineExplosionStrategy

logger = logging.getLogger(__name__)


__all__ = [
    "BoxBreakoutStrategy",
    "DoubleYinMoneyBagStrategy",
    "DragonHeadStrategy",
    "HuamulanGhostBreakoutStrategy",
    "PriceVolumeSignalStrategy",
    "RSIStrategy",
    "RegimeCycleFourStrategy",
    "SectorMomentumStrategy",
    "SentimentCycleStrategy",
    "TwoBReversalStrategy",
    "YegeMainlineExplosionStrategy",
    "MacdZeroAxisStrategy",
    "Strategy",
    "StrategyConfig",
    "get_enabled_strategies",
    "get_enabled_strategies_for_regime",
    "get_strategy",
    "list_strategies",
]
