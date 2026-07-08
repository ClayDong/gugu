"""gugu 交易系统配置加载与强类型模型.

向后兼容：
  ``from gugu.config import settings, env, strategy_defaults, load_yaml``
  仍然可以工作 —— settings() 现在内部委托给 AppConfig 单例的 flatten()。

新代码推荐：
  ``from gugu.config import get_config``
  ``cfg = get_config()``

P2 优化：支持配置热加载（refresh_config），修改 settings.yaml 后无需重启。
"""

from __future__ import annotations

import contextlib
import warnings
from functools import lru_cache

# ── Re-export legacy loader functions for backward compatibility ─────
# 已弃用：新代码请使用 get_config() 强类型接口
from gugu._legacy_config import (  # noqa: F401  # fmt: skip
    CONFIG_DIR,
    PROJECT_ROOT,
    EnvSettings,
    env,
    load_yaml,
    strategy_defaults,
)

# ── New strong-typed models ──────────────────────────────────────────
from gugu.config.models import (  # noqa: F401  # fmt: skip
    AppConfig,
    DataConfig,
    ExecutionConfig,
    FeishuConfig,
    FundamentalConfig,
    LeopardUniverseConfig,
    LiveConfig,
    LogConfig,
    PaperConfig,
    RiskConfig,
    SchedulerConfig,
    StrategyConfig,
    WisdomConfig,
    flatten_config,
)

# 配置文件路径（用于热加载检测）
_SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
_SETTINGS_MTIME: float = 0.0


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return the cached AppConfig singleton loaded from settings.yaml.

    Access individual sections with attribute syntax:
        cfg = get_config()
        cfg.risk.max_position_ratio   # 0.30
        cfg.strategy.enabled           # ['box_breakout', 'turtle', 'dual_ma']
    """
    global _SETTINGS_MTIME
    with contextlib.suppress(Exception):
        _SETTINGS_MTIME = _SETTINGS_PATH.stat().st_mtime
    return AppConfig.from_settings()


def refresh_config() -> AppConfig:
    """强制刷新配置缓存（热加载）。

    调用场景：
    - 用户手动修改 settings.yaml 后调用
    - scheduler 定时检测文件变更（每 5 分钟）
    - Web 仪表盘提供"重载配置"按钮

    Returns:
        新的 AppConfig 实例
    """
    get_config.cache_clear()
    return get_config()


def config_has_changed() -> bool:
    """检查配置文件是否已被修改（基于 mtime 比较）。

    Returns:
        True 如果文件已修改，调用方应调用 refresh_config()
    """
    try:
        current_mtime = _SETTINGS_PATH.stat().st_mtime
        return current_mtime > _SETTINGS_MTIME
    except Exception:
        return False


def settings() -> dict:
    """Backward-compatible dict-style access. Delegates to get_config().flatten().

    Prefer ``get_config()`` for new code.
    """
    warnings.warn(
        "settings() is deprecated, use get_config() for strong-typed access",
        DeprecationWarning, stacklevel=2,
    )
    return get_config().flatten()
