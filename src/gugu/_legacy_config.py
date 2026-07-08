"""gugu 交易系统配置加载（已弃用，保留向后兼容）。

从 .env 加载敏感信息，从 config/*.yaml 加载业务配置。

⚠️ 已弃用：新代码请通过 ``from gugu.config import get_config`` 使用强类型配置。
此模块保留仅用于向后兼容，未来版本将移除。
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root() -> Path:
    """从 __file__ 所在目录向上搜索，找到包含 config/settings.yaml 的项目根目录。"""
    current = Path(__file__).resolve().parent
    for _ in range(20):
        if (current / "config" / "settings.yaml").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise RuntimeError(
        f"无法定位项目根目录（未找到 config/settings.yaml），起始路径: {Path(__file__).resolve()}"
    )


PROJECT_ROOT = _find_project_root()
CONFIG_DIR = PROJECT_ROOT / "config"


class EnvSettings(BaseSettings):
    """从 .env 加载的敏感配置。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_chat_id: str = ""
    feishu_webhook: str = ""

    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # tushare_token: str = ""  # 已清理：未被任何采集器使用

    qmt_path: str = ""
    qmt_account_id: str = ""
    qmt_password: str = ""

    log_level: str = "INFO"
    database_url: str = "sqlite:///data/gugu.db"
    run_mode: str = "paper"


@lru_cache(maxsize=1)
def load_yaml(name: str) -> dict[str, Any]:
    """加载 YAML 配置文件（带缓存）。"""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return deepcopy(yaml.safe_load(f) or {})


@lru_cache(maxsize=1)
def env() -> EnvSettings:
    """获取环境变量配置（单例）。"""
    return EnvSettings()


def settings() -> dict[str, Any]:
    """获取主配置（settings.yaml），内部委托给 AppConfig 单例的 flatten()。

    确保 dict 格式输出与 Pydantic AppConfig 强类型模型一致。
    新代码推荐使用 ``from gugu.config import get_config; cfg = get_config()``。
    """
    from gugu.config.models import AppConfig

    try:
        cfg = AppConfig.from_settings()
        return cfg.flatten()
    except Exception:
        # Fallback: direct YAML load for environments without all deps
        return load_yaml("settings")


def strategy_defaults() -> dict[str, Any]:
    """获取策略默认参数。"""
    return load_yaml("strategy_defaults")
