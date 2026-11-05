"""策略代码指纹：把准入证据钉在「当时那份代码」上。

问题（2026-11-05 实证）：
evidence 块只对 ``metrics`` 做哈希，对产生这些数字的策略代码没有任何约束。
哈希只能证明「数字没被改过」，不能证明「数字还对应现在这份代码」。
于是策略改版之后（``two_b_reversal`` 在 2026-09-04 改过退出逻辑），
旧证据仍然算有效，manifest 里可以长期挂着一个再也复现不出来的
``excess_5d=+2.18%``：用项目自己的四种标准口径重跑，最高只到 +0.31%。

本模块把「模块源码字节 + 声明参数」压成指纹：验证脚本写进证据块，
运行时用当前代码重算比对。代码或参数一变指纹就变，旧证据立即失效。
失败方向是**拒绝准入**，不是默默放行。

边界（明确写清，避免被当成万能证明）：
- 指纹覆盖策略模块源码与 ``config/strategy_defaults.yaml`` 里声明的参数；
- 不覆盖策略依赖的公共工具模块（如 base.py、指标库）——那些改动不一定被捕捉；
- 不覆盖运行时覆盖参数（settings/环境变量）。所以要防的是「代码悄悄改版」，
  不是「有人在运行时改了参数」这种有日志可查的操作。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "declared_params",
    "module_source_digest",
    "strategy_fingerprint",
]

_DEFAULTS_FILENAME = "config"
_DEFAULTS_FILE = "strategy_defaults.yaml"


def _repo_root() -> Path:
    """按本文件位置推仓库根，避免依赖导入期绑定的 PROJECT_ROOT 常量。"""
    return Path(__file__).resolve().parents[3]


def module_source_digest(module_name: str) -> str | None:
    """哈希模块源文件字节；模块解析不到源文件时返回 None（不伪造）。"""
    name = str(module_name or "").strip()
    if not name:
        return None
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None or not spec.origin:
        return None
    origin = Path(spec.origin)
    if not origin.is_file():
        return None
    try:
        return hashlib.sha256(origin.read_bytes()).hexdigest()
    except OSError:
        return None


@lru_cache(maxsize=1)
def _load_defaults() -> dict[str, Any]:
    import yaml

    path = _repo_root() / _DEFAULTS_FILENAME / _DEFAULTS_FILE
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def declared_params(strategy: str) -> dict[str, Any]:
    """取 ``strategy_defaults.yaml`` 中该策略的声明参数；缺失返回空字典。"""
    entry = _load_defaults().get(str(strategy or ""))
    return dict(entry) if isinstance(entry, dict) else {}


def _params_digest(params: Mapping[str, Any] | None) -> str:
    payload = json.dumps(dict(params or {}), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def strategy_fingerprint(
    module_name: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> str | None:
    """生成策略代码指纹；源码不可读时返回 None，由调用方决定拒绝还是跳过。"""
    source = module_source_digest(module_name)
    if source is None:
        return None
    payload = json.dumps(
        {
            "module": str(module_name).strip(),
            "source": source,
            "params": _params_digest(params),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
