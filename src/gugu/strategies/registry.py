"""策略注册表。按名称获取策略实例。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from gugu.strategies.base import Strategy
from gugu.strategies.blogger_rules import LinJuDaYeStrategy
from gugu.strategies.breakout import BoxBreakoutStrategy
from gugu.strategies.double_yin import DoubleYinMoneyBagStrategy
from gugu.strategies.manifest import get_manifest
from gugu.strategies.mean_revert import RSIStrategy
from gugu.strategies.price_volume import PriceVolumeSignalStrategy
from gugu.strategies.regime_cycle import RegimeCycleFourStrategy

# 策略注册表：name -> class
_REGISTRY: dict[str, type[Strategy]] = {
    "rsi_reversal": RSIStrategy,
    "box_breakout": BoxBreakoutStrategy,
    "double_yin": DoubleYinMoneyBagStrategy,
    "linjudaye": LinJuDaYeStrategy,
    "price_volume_signal": PriceVolumeSignalStrategy,
    "regime_cycle_four": RegimeCycleFourStrategy,
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


def get_runtime_strategy_names(
    regime: str | None = None,
    execution_mode: str | None = None,
) -> list[str]:
    """返回经过配置、manifest 和注册表三重过滤的运行时策略名。

    这是所有运行入口和验证脚本的唯一策略准入入口。未传执行模式时，
    使用当前配置；``paper`` 可加载 ``conditional_pass``；
    生产模式（track_live/signal_only/live）以 settings.yaml 的
    ``verified_strategies`` 开发者白名单为最终准入（v0.9.29 P0 修复，
    见下方 2026-08-19 注释）。
    """
    from gugu.config import get_config

    cfg = get_config()
    mode = execution_mode or cfg.execution.mode
    candidates = cfg.strategy.get_enabled_for_regime(regime, mode)
    if mode in ("track_live", "signal_only", "live"):
        # 生产模式必须同时满足 settings verified 白名单和 manifest 的 GatePolicy gate-v1。
        # 白名单只是人工意图声明，不能替代可复核的样本外证据。
        candidates = [name for name in candidates if name in cfg.strategy.verified_strategies]
        runtime_enabled = set(get_manifest().list_runtime_enabled(mode, set(candidates)))
    else:
        runtime_enabled = set(get_manifest().list_runtime_enabled(mode, set(candidates)))
    return [name for name in candidates if name in runtime_enabled and name in _REGISTRY]


@lru_cache(maxsize=1)
def get_enabled_strategies() -> list[Strategy]:
    """获取当前执行模式允许加载的策略实例列表（带缓存）。"""
    return [get_strategy(name) for name in get_runtime_strategy_names()]


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

    return [get_strategy(name) for name in get_runtime_strategy_names(regime)]


def reload_strategies() -> None:
    """清除策略缓存，使下次调用重新读取配置。"""
    get_enabled_strategies.cache_clear()
    get_manifest.cache_clear()
    from gugu.utils.log import get_logger

    get_logger().info("策略缓存已清除，下次调用将重新读取配置")


def verify_registry_consistency() -> tuple[list[str], list[str]]:
    """启动自检（v0.9.11 防线2）：注册表 vs 配置一致性检查。

    两级结果（返回 (fatal, warning) 二元组）：
    - fatal：配置启用但代码未注册 → get_strategy 会 KeyError，必须拒绝启动
    - warning：代码注册但配置未引用 → 死代码残留（不会发信号，但应清理；
      hot_money 2026-08-01 证伪后 5 处注册点残留的教训——残留越少，幽灵风险越低）

    Usage:
        fatal, warning = verify_registry_consistency()
        if fatal:
            raise SystemExit(1)
        for w in warning:
            logger.warning(w)
    """
    from gugu.config import get_config

    fatal: list[str] = []
    warning: list[str] = []
    cfg = get_config()
    strategy_cfg = cfg.strategy

    # 1. 配置启用但未注册（fail-fast：get_strategy 会 KeyError，提前暴露）
    configured = strategy_cfg.get_all_enabled_unique()
    for name in configured:
        if name not in _REGISTRY:
            fatal.append(f"配置启用但代码未注册: {name}")

    # 2. 代码注册但配置完全不引用（死代码残留，warning 不阻塞）
    all_configured = set(configured) | set(strategy_cfg.experimental_strategies)
    for name in _REGISTRY:
        if name not in all_configured:
            warning.append(f"代码注册但配置未启用/未标记: {name}（死代码残留，建议清理）")

    return fatal, warning
