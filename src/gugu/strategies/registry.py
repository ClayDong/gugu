"""策略注册表。按名称获取策略实例。"""
from __future__ import annotations

from typing import Any

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


def get_enabled_strategies() -> list[Strategy]:
    """获取配置中启用的策略实例列表。"""
    from gugu.config import settings

    enabled = settings().get("strategy", {}).get("enabled", [])
    return [get_strategy(name) for name in enabled if name in _REGISTRY]
