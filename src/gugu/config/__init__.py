"""gugu 交易系统配置加载与强类型模型.

向后兼容：
  ``from gugu.config import settings, env, strategy_defaults, load_yaml``
  仍然可以工作 —— settings() 现在内部委托给 AppConfig 单例的 flatten()。

新代码推荐：
  ``from gugu.config import get_config``
  ``cfg = get_config()``
"""

from __future__ import annotations

from functools import lru_cache

# ── Re-export legacy loader functions for backward compatibility ─────
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
    LiveConfig,
    LogConfig,
    PaperConfig,
    RiskConfig,
    SchedulerConfig,
    StrategyConfig,
    WisdomConfig,
    flatten_config,
)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return the cached AppConfig singleton loaded from settings.yaml.

    Access individual sections with attribute syntax:
        cfg = get_config()
        cfg.risk.max_position_ratio   # 0.30
        cfg.strategy.enabled           # ['box_breakout', 'turtle', 'dual_ma']
    """
    return AppConfig.from_settings()


def settings() -> dict:
    """Backward-compatible dict-style access. Delegates to get_config().flatten().

    Prefer ``get_config()`` for new code.
    """
    return get_config().flatten()
