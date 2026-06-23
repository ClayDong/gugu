"""数据缓存。内存缓存 + SQLite 持久化。"""
from __future__ import annotations

import time
from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()


class DataCache:
    """带 TTL 的内存缓存，有最大条目限制和 LRU 淘汰策略。

    历史数据落 SQLite/Parquet 由上层管理。

    Attributes:
        _ttl: 缓存 TTL（秒）。
        _max_size: 最大缓存条目数，超出时淘汰最旧条目。
    """

    def __init__(self, ttl_seconds: int = 120, max_size: int = 1000) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._store: dict[str, tuple[float, Any]] = {}
        # 维护插入顺序用于简单淘汰
        self._order: list[str] = []

    def get(self, key: str) -> Any | None:
        """获取缓存。过期返回 None。"""
        if key not in self._store:
            return None
        ts, val = self._store[key]
        if time.time() - ts > self._ttl:
            del self._store[key]
            if key in self._order:
                self._order.remove(key)
            return None
        return val

    def set(self, key: str, value: Any) -> None:
        """写入缓存。若超出最大条目限制，淘汰最旧条目。"""
        # 淘汰策略：超出上限时移除最旧的 10% 条目
        if key not in self._store and len(self._store) >= self._max_size:
            evict_count = max(1, self._max_size // 10)
            for _ in range(evict_count):
                if self._order:
                    oldest = self._order.pop(0)
                    self._store.pop(oldest, None)

        if key not in self._store:
            self._order.append(key)
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
        """清空缓存。"""
        self._store.clear()
        self._order.clear()

    def stats(self) -> dict[str, int]:
        """缓存统计。"""
        return {"keys": len(self._store), "max_size": self._max_size}


# 全局单例
_cache = DataCache()


def cache() -> DataCache:
    """获取全局缓存实例。"""
    return _cache
