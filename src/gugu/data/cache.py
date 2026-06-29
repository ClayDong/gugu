"""数据缓存。内存缓存 + Parquet 持久化缓存。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class DataCache:
    """带 TTL 的内存缓存，有最大条目限制和 LRU 淘汰策略。

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


class ParquetPersistentCache:
    """Parquet 文件持久化缓存，用于跨进程/重启历史数据缓存。

    缓存文件存储在 ``data/parquet_cache/`` 目录下，
    以 ``{symbol}_{days}d.parquet`` 命名。

    缓存策略：
    - 写入时存储完整 DataFrame
    - 读取时检查文件是否存在、是否包含当日数据（fresh）
    - 未过期且包含最新数据则返回，否则返回 None
    """

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        if cache_dir is None:
            from gugu.config import PROJECT_ROOT

            cache_dir = PROJECT_ROOT / "data" / "parquet_cache"
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Parquet 持久化缓存目录: {self._cache_dir}")

    def _key(self, symbol: str, days: int) -> str:
        """生成缓存文件名。"""
        # 标准化 symbol：去除前缀 0 和后缀差异
        sym = str(symbol).strip().zfill(6)
        return f"{sym}_{days}d.parquet"

    def get(self, symbol: str, days: int) -> pd.DataFrame | None:
        """从持久化缓存读取历史数据。

        检查条件：
        1. 文件存在
        2. 数据的最后日期不早于昨天（够新）

        Args:
            symbol: 股票代码
            days: 回看天数

        Returns:
            DataFrame（含当日前数据）或 None
        """
        path = self._cache_dir / self._key(symbol, days)
        if not path.exists():
            return None

        try:
            df = pd.read_parquet(path)
            if df.empty:
                return None

            # 检查 freshness：最后一条数据日期
            if "date" in df.columns:
                last_date = pd.to_datetime(df["date"].iloc[-1])
                # 如果最后数据日期早于昨天，视为过期
                from datetime import datetime, timedelta

                yesterday = datetime.now() - timedelta(days=2)
                if last_date < pd.Timestamp(yesterday):
                    logger.debug(
                        f"Parquet 缓存过期: {symbol} {days}d, 最后日期={last_date.date()}, 清理"
                    )
                    path.unlink(missing_ok=True)
                    return None

            logger.debug(f"Parquet 缓存命中: {symbol} {days}d ({len(df)} 行)")
            return df
        except Exception as e:
            logger.debug(f"Parquet 缓存读取失败: {e}")
            path.unlink(missing_ok=True)
            return None

    def set(self, symbol: str, days: int, df: pd.DataFrame) -> None:
        """写入持久化缓存。

        Args:
            symbol: 股票代码
            days: 回看天数
            df: 行情 DataFrame
        """
        if df is None or df.empty:
            return
        path = self._cache_dir / self._key(symbol, days)
        try:
            df.to_parquet(path, index=False)
            logger.debug(f"Parquet 缓存写入: {symbol} {days}d ({len(df)} 行)")
        except Exception as e:
            logger.warning(f"Parquet 缓存写入失败: {e}")

    def clear(self, symbol: str | None = None, days: int | None = None) -> int:
        """清理缓存。

        Args:
            symbol: 可选，指定股票
            days: 可选，指定天数

        Returns:
            清理文件数
        """
        count = 0
        if symbol and days:
            path = self._cache_dir / self._key(symbol, days)
            if path.exists():
                path.unlink()
                count = 1
        elif symbol:
            for f in self._cache_dir.glob(f"{symbol.zfill(6)}_*.parquet"):
                f.unlink()
                count += 1
        else:
            for f in self._cache_dir.glob("*.parquet"):
                f.unlink()
                count += 1
        logger.info(f"Parquet 缓存清理: {count} 个文件")
        return count


# 全局单例
_cache = DataCache()
_persistent_cache = ParquetPersistentCache()


def cache() -> DataCache:
    """获取全局内存缓存实例。"""
    return _cache


def persistent_cache() -> ParquetPersistentCache:
    """获取全局持久化缓存实例。"""
    return _persistent_cache
