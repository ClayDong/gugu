"""策略注册表。按名称获取策略实例。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from gugu.strategies.base import Strategy
from gugu.strategies.breakout import BoxBreakoutStrategy, DualThrustStrategy
from gugu.strategies.breakout_rebound import BreakoutReboundStrategy
from gugu.strategies.capital_flow_alert import CapitalFlowAlertStrategy
from gugu.strategies.chen_jiangting import ChenJiangtingStrategy
from gugu.strategies.double_yin import DoubleYinMoneyBagStrategy
from gugu.strategies.leopard_three_factors import LeopardThreeFactorsStrategy
from gugu.strategies.mean_revert import BollingerStrategy, KDJStrategy, RSIStrategy
from gugu.strategies.dragon_tiger_short import DragonTigerShortStrategy
from gugu.strategies.ml_factor import MLFactorStrategy
from gugu.strategies.money_flow_follow import MoneyFlowFollowStrategy
from gugu.strategies.north_force_long import NorthForceLongStrategy
from gugu.strategies.pullback_low import PullbackLowStrategy
from gugu.strategies.trend import DualMAStrategy, MACDStrategy, TurtleStrategy

# 策略注册表：name -> class
_REGISTRY: dict[str, type[Strategy]] = {
    "turtle": TurtleStrategy,
    "dual_ma": DualMAStrategy,
    "macd": MACDStrategy,
    "bollinger": BollingerStrategy,
    "rsi_reversal": RSIStrategy,
    "kdj": KDJStrategy,
    "box_breakout": BoxBreakoutStrategy,
    "dual_thrust": DualThrustStrategy,
    "money_flow_follow": MoneyFlowFollowStrategy,
    "chen_jiangting": ChenJiangtingStrategy,
    "leopard_three_factors": LeopardThreeFactorsStrategy,
    "pullback_low": PullbackLowStrategy,  # 震荡市专用低吸回踩策略
    "double_yin": DoubleYinMoneyBagStrategy,  # 双阴钱袋子策略（主力洗盘拐点）
    "breakout_rebound": BreakoutReboundStrategy,  # 断板反包策略（短线主力行为）
    "capital_flow_alert": CapitalFlowAlertStrategy,  # 资金流异动监控
    "ml_factor": MLFactorStrategy,  # LightGBM 多因子策略
    "north_force_long": NorthForceLongStrategy,  # 长线主力吸筹跟随
    "dragon_tiger_short": DragonTigerShortStrategy,  # 短线龙虎榜反包跟随
}


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    """获取策略实例。

    Args:
        name: 策略名称
        params: 覆盖默认参数

    Returns:
        Strategy 实例

    Raises:
        KeyError: 策略未注册
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"策略 '{name}' 未注册，可用: {list(_REGISTRY.keys())}")
    return cls(params)


def get_strategy_cls(name: str) -> type[Strategy] | None:
    """获取策略类（非实例），用于 Walk-Forward 优化等需要 Class 而非 instance 的场景。

    Args:
        name: 策略名称

    Returns:
        策略类，未注册返回 None
    """
    return _REGISTRY.get(name)


def list_strategies() -> list[str]:
    """列出所有已注册策略。"""
    return list(_REGISTRY.keys())


@lru_cache(maxsize=1)
def get_enabled_strategies() -> list[Strategy]:
    """获取配置中启用的策略实例列表（带缓存，全局共享同一批实例）。

    v0.5.6 优化1：优先使用 enabled_by_regime 的并集（向后兼容 enabled）
    注：按 regime 过滤由 get_enabled_strategies_for_regime() 实现
    """
    from gugu.config import get_config

    cfg = get_config().strategy
    # v0.5.6: 优先使用 enabled_by_regime 的并集
    enabled = cfg.get_all_enabled_unique() if cfg.enabled_by_regime else cfg.enabled
    return [get_strategy(name) for name in enabled if name in _REGISTRY]


def get_enabled_strategies_for_regime(regime: str | None) -> list[Strategy]:
    """获取指定市场状态启用的策略实例列表（v0.5.6 优化1 新增）。

    Args:
        regime: 市场状态（bull/sideways/bear/rally/crash），None 表示未知

    Returns:
        对应 regime 启用的策略实例列表

    策略按市场状态启用，而非全局禁用：
    - 牛市：突破追势策略（box_breakout）
    - 震荡：拐点策略（double_yin）
    - 熊市：空仓观察（返回空列表）
    """
    from gugu.config import get_config

    cfg = get_config().strategy
    enabled = cfg.get_enabled_for_regime(regime)
    return [get_strategy(name) for name in enabled if name in _REGISTRY]


def reload_strategies() -> None:
    """清除策略缓存，使下次 get_enabled_strategies 调用重新读取配置（O-01 修复）。

    用于配置热更新场景：修改 settings.yaml 后调用此方法，
    无需重启进程即可生效。也可通过飞书指令触发。
    """
    get_enabled_strategies.cache_clear()
    from gugu.utils.log import get_logger

    get_logger().info("策略缓存已清除，下次调用将重新读取配置")
