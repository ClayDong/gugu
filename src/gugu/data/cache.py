"""数据缓存。内存缓存 + SQLite 持久化。"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class DataCache:
    """带 TTL 的内存缓存。历史数据落 SQLite/Parquet 由上层管理。"""

    def __init__(self, ttl_seconds: int = 120) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        """获取缓存。过期返回 None。"""
        if key not in self._store:
            return None
        ts, val = self._store[key]
        if time.time() - ts > self._ttl:
            del self._store[key]
            return None
        return val

    def set(self, key: str, value: Any) -> None:
        """写入缓存。"""
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
        """清空缓存。"""
        self._store.clear()

    def stats(self) -> dict[str, int]:
        """缓存统计。"""
        return {"keys": len(self._store)}


# 全局单例
_cache = DataCache()


def cache() -> DataCache:
    """获取全局缓存实例。"""
    return _cache
