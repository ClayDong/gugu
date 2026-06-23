"""策略注册表。按名称获取策略实例。"""
from __future__ import annotations

from functools import lru_cache

from gugu.strategies.base import Strategy
from gugu.strategies.breakout import BoxBreakoutStrategy, DualThrustStrategy
from gugu.strategies.mean_revert import BollingerStrategy, KDJStrategy, RSIStrategy
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


def list_strategies() -> list[str]:
    """列出所有已注册策略。"""
    return list(_REGISTRY.keys())


@lru_cache(maxsize=1)
def get_enabled_strategies() -> list[Strategy]:
    """获取配置中启用的策略实例列表（带缓存，全局共享同一批实例）。"""
    from gugu.config import settings

    enabled = settings().get("strategy", {}).get("enabled", [])
    return [get_strategy(name) for name in enabled if name in _REGISTRY]


def reload_strategies() -> None:
    """清除策略缓存，使下次 get_enabled_strategies 调用重新读取配置（O-01 修复）。

    用于配置热更新场景：修改 settings.yaml 后调用此方法，
    无需重启进程即可生效。也可通过飞书指令触发。
    """
    get_enabled_strategies.cache_clear()
    from gugu.utils.log import get_logger

    get_logger().info("策略缓存已清除，下次调用将重新读取配置")
