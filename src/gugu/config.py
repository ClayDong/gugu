"""gugu 交易系统配置加载。

从 .env 加载敏感信息，从 config/*.yaml 加载业务配置。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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

    tushare_token: str = ""

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
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def env() -> EnvSettings:
    """获取环境变量配置（单例）。"""
    return EnvSettings()


def settings() -> dict[str, Any]:
    """获取主配置（settings.yaml）。"""
    return load_yaml("settings")


def strategy_defaults() -> dict[str, Any]:
    """获取策略默认参数。"""
    return load_yaml("strategy_defaults")
