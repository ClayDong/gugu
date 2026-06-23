"""gugu 交易系统配置加载与强类型模型.

向后兼容：
  ``from gugu.config import settings, env, strategy_defaults, load_yaml``
  仍然可以工作 —— __init__.py 从 _legacy_config.py 重新导出这些函数。

新代码推荐：
  ``from gugu.config import AppConfig``
  ``cfg = AppConfig.from_settings()``
"""

# ── Re-export legacy loader functions for backward compatibility ─────
from gugu._legacy_config import (  # noqa: F401  # fmt: skip
    CONFIG_DIR,
    PROJECT_ROOT,
    EnvSettings,
    env,
    load_yaml,
    settings,
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