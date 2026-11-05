"""数据缓存。内存缓存 + Parquet 持久化缓存。"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class DataCache:
    """带 TTL 的内存缓存，有最大条目限制和 LRU 淘汰策略。

    线程安全：所有公共方法使用 RLock 保护，防止 asyncio/多线程并发竞态。
    LRU 实现基于 OrderedDict，set/get 均 O(1) 复杂度。

    Attributes:
        _ttl: 缓存 TTL（秒）。
        _max_size: 最大缓存条目数，超出时淘汰最旧条目。
    """

    def __init__(self, ttl_seconds: int = 120, max_size: int = 1000) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str, ignore_ttl: bool = False) -> Any | None:
        """获取缓存。过期返回 None。ignore_ttl=True 时即使过期也返回（兜底用）。

        OrderedDict 实现 LRU：命中时调用 move_to_end() 更新访问顺序。
        """
        with self._lock:
            if key not in self._store:
                return None
            ts, val = self._store[key]
            if not ignore_ttl and time.time() - ts > self._ttl:
                # 保留过期值，供上层在所有数据源失败时通过 ignore_ttl=True 兜底。
                # 正常读取仍视为 miss；显式 clear/淘汰负责回收长期过期条目。
                return None
            self._store.move_to_end(key)
            return val

    def set(self, key: str, value: Any) -> None:
        """写入缓存。若超出最大条目限制，淘汰最旧条目。"""
        with self._lock:
            self._store[key] = (time.time(), value)
            self._store.move_to_end(key)

            if len(self._store) >= self._max_size:
                evict_count = max(1, self._max_size // 10)
                for _ in range(evict_count):
                    if self._store:
                        self._store.popitem(last=False)

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            self._store.clear()

    def stats(self) -> dict[str, int]:
        """缓存统计。"""
        with self._lock:
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
        # P0 修复：进程级锁，防止并发写入同一 Parquet 文件
        self._lock = threading.RLock()
        logger.info("Parquet 持久化缓存目录: {}", self._cache_dir)

    def _key(self, symbol: str, days: int, adjust_type: str = "none") -> str:
        """生成缓存文件名。"""
        sym = str(symbol).strip().zfill(6)
        return f"{sym}_{days}d_{adjust_type}.parquet"

    def get(
        self,
        symbol: str,
        days: int,
        adjust_type: str = "none",
        now: datetime | None = None,
    ) -> pd.DataFrame | None:
        """从持久化缓存读取历史数据。

        检查条件：
        1. 文件存在
        2. 数据的新鲜度（交易日盘中必须含当日 bar；非盘中容忍 7 天老化）

        Args:
            symbol: 股票代码
            days: 回看天数
            adjust_type: 复权类型
            now: 参考时间（测试注入用，默认当前时间）

        Returns:
            DataFrame（满足新鲜度要求）或 None
        """
        path = self._cache_dir / self._key(symbol, days, adjust_type)
        if not path.exists():
            return None

        # 参考时间（测试注入用，默认当前时间）——新鲜度判定与质量校验共用同一基准
        ref_time = now or datetime.now()

        try:
            df = pd.read_parquet(path)
            if df.empty:
                return None

            # 检查 freshness：最后一条数据日期
            if "date" in df.columns:
                last_date = pd.to_datetime(df["date"].iloc[-1])
                last_date_only = last_date.date()
                ref_date = ref_time.date()
                # P1 修复：用交易日而非自然日计算缓存新鲜度
                # 长假后首个交易日（如国庆后第一天），自然日已过 7 天但交易日仅 1 天，
                # 此时缓存仍是最新可用数据，不应判定过期。
                from gugu.utils.calendar import is_trading_day, trading_days_between

                now_min = ref_time.hour * 60 + ref_time.minute
                is_intraday = is_trading_day(ref_date) and 9 * 60 + 30 <= now_min <= 15 * 60

                # 计算交易日差（从缓存最后日期到当前日期之间的交易日数）
                if last_date_only >= ref_date:
                    trading_days_stale = 0
                else:
                    trading_days_stale = trading_days_between(last_date_only, ref_date)

                # 新鲜度阈值（交易日）：
                #   - 交易日盘后（>15:00）：当日 K线已生成，缓存必须含当日 bar
                #     （trading_days_stale==0）。
                #   - 交易日盘中/盘前（<=15:00）：日K线源无当日 bar，
                #     最新可得 = 昨日收盘 → 允许 trading_days_stale<=1。
                #   - 非交易日：容忍 5 个交易日老化（约一周交易周，覆盖长假）
                if is_intraday:
                    max_stale = 1
                elif is_trading_day(ref_date):
                    max_stale = 0
                else:
                    max_stale = 5

                if trading_days_stale > max_stale:
                    logger.debug(
                        "Parquet 缓存过期: {} {}d, 最后日期={}, 距今 {} 交易日(自然日{}天), 盘中={}",
                        symbol,
                        days,
                        last_date_only,
                        trading_days_stale,
                        (ref_date - last_date_only).days,
                        is_intraday,
                    )
                    path.unlink(missing_ok=True)
                    return None

            # BUG-03 修复：缓存读取后重新做数据质量校验
            # 防止缓存了坏数据（如数据源返回错误价格后写入缓存）
            # 2026-08-12：透传 ref_time → 质量校验新鲜度与缓存新鲜度同一时间基准
            # （此前 validate 用真实 date.today()，缓存注入 now 测试随时间漂移变红）
            try:
                from gugu.data.quality import validate_stock_history

                validated = validate_stock_history(df, symbol, now=ref_time.date())
                if validated.empty:
                    logger.warning("Parquet 缓存数据 {} {}d 校验失败，删除缓存", symbol, days)
                    path.unlink(missing_ok=True)
                    return None
                df = validated
            except Exception as ve:
                logger.warning("Parquet 缓存 {} {}d 校验异常: {}，删除缓存", symbol, days, ve)
                path.unlink(missing_ok=True)
                return None

            logger.debug("Parquet 缓存命中: {} {}d ({} 行)", symbol, days, len(df))
            return df
        except Exception as e:
            logger.debug("Parquet 缓存读取失败: {}", e)
            path.unlink(missing_ok=True)
            return None

    def set(self, symbol: str, days: int, df: pd.DataFrame, adjust_type: str = "none") -> None:
        """写入持久化缓存。

        Args:
            symbol: 股票代码
            days: 回看天数
            df: 行情 DataFrame
        """
        if df is None or df.empty:
            return
        path = self._cache_dir / self._key(symbol, days, adjust_type)
        try:
            with self._lock:
                # P0 修复：原子写入，先写临时文件再 rename，避免并发读取到半写文件
                tmp_path = path.with_suffix(".parquet.tmp")
                df.to_parquet(tmp_path, index=False)
                # 使用文件锁防止跨进程并发写入冲突
                lock_path = path.with_suffix(".lock")
                with open(lock_path, "w") as lock_file:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        os.replace(tmp_path, path)
                    finally:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                # 清理可能遗留的临时文件
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            logger.debug("Parquet 缓存写入: {} {}d ({} 行)", symbol, days, len(df))
        except Exception as e:
            logger.warning("Parquet 缓存写入失败: {}", e)

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
        logger.info("Parquet 缓存清理: {} 个文件", count)
        return count

    def cleanup_stale(self, max_age_days: int = 7) -> int:
        """清理过期的 Parquet 缓存文件。

        基于文件修改时间清理超过 ``max_age_days`` 天未更新的缓存文件，
        防止缓存目录无限膨胀（如 watchlist 调整后遗留的旧股票缓存）。

        与 ``get()`` 的惰性清理互补：``get()`` 清理单个过期文件，
        本方法批量清理整个目录的过期文件，建议每日定时调用。

        Args:
            max_age_days: 文件最大存活天数，默认 7 天（覆盖长假场景）。

        Returns:
            清理的文件数
        """
        import time as _time

        count = 0
        now = _time.time()
        cutoff = now - max_age_days * 86400  # 86400 秒/天
        for f in self._cache_dir.glob("*.parquet"):
            try:
                mtime = f.stat().st_mtime
                if mtime < cutoff:
                    f.unlink()
                    count += 1
            except OSError:
                continue
        if count > 0:
            logger.info("Parquet 缓存定期清理: 删除 {} 个过期文件(>{}天)", count, max_age_days)
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
