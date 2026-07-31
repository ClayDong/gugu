"""另类数据采集器基类。

提供统一的限流、缓存、降级机制，所有另类数据采集器继承此类。
不继承 BaseCollector（接口不同：另类数据是全市场批量获取，非单股OHLCV）。
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


def _find_project_root() -> Path:
    """从当前文件向上查找项目根目录（含 pyproject.toml 的目录）。

    比硬编码 parent.parent.parent... 更健壮，文件移动不会出错。
    """
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    # 降级：回退到6层parent（base.py 在 src/gugu/data/collectors/altalpha/ 下）
    return here.parent.parent.parent.parent.parent.parent


# 项目根目录下的数据缓存目录
_PROJECT_ROOT = _find_project_root()
_DEFAULT_CACHE_DIR = _PROJECT_ROOT / "data" / "altalpha"


class AltAlphaCollector:
    """另类数据采集器基类。

    特性：
    - 500ms 限流：防止 akshare 底层源站封IP
    - Parquet 持久化缓存：12小时TTL，避免重复调用
    - 过期缓存兜底：API失败时返回上次缓存（即使过期）
    - async 接口：通过 asyncio.to_thread 调用同步 akshare API
    """

    _last_call_ts: float = 0.0
    _min_interval: float = 0.5  # 500ms 限流
    _cache_ttl_hours: float = 12.0  # 12小时缓存
    _data_type: str = "base"  # 子类覆盖

    def __init__(self, cache_dir: Path | str | None = None) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir else _DEFAULT_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ---- 缓存管理 ----

    @staticmethod
    def _build_cache_key(api_fn: Any, *args: Any, **kwargs: Any) -> str:
        """根据 API 函数名与参数构建稳定缓存键。

        不同参数（如股票代码、日期范围、报告期）必须对应不同缓存键，
        避免共享缓存导致返回错误数据。
        """
        fn_name = getattr(api_fn, "__name__", str(api_fn))
        parts = [fn_name]
        for arg in args:
            parts.append(str(arg))
        for key, value in sorted(kwargs.items()):
            parts.append(f"{key}={value}")
        raw = "_".join(parts)
        # 文件名安全化：仅保留字母、数字、下划线、等号、连字符
        return re.sub(r"[^\w=\-]", "_", raw)

    def _cache_path(self, date_str: str | None = None, cache_key: str | None = None) -> Path:
        """获取缓存文件路径。"""
        if date_str is None:
            from datetime import datetime

            date_str = datetime.now().strftime("%Y%m%d")
        if cache_key:
            return self._cache_dir / f"{date_str}_{self._data_type}_{cache_key}.parquet"
        return self._cache_dir / f"{date_str}_{self._data_type}.parquet"

    def _read_cache(
        self, ignore_ttl: bool = False, cache_key: str | None = None
    ) -> pd.DataFrame | None:
        """读取 Parquet 缓存。"""
        path = self._cache_path(cache_key=cache_key)
        if not path.exists():
            # 尝试读取最近的同 key 缓存文件
            pattern = (
                f"*_{self._data_type}_{cache_key}.parquet"
                if cache_key
                else f"*_{self._data_type}.parquet"
            )
            files = sorted(self._cache_dir.glob(pattern))
            if not files:
                return None
            path = files[-1]  # 最新的缓存文件

        # 检查 TTL
        if not ignore_ttl:
            mtime = path.stat().st_mtime
            age_hours = (time.time() - mtime) / 3600
            if age_hours > self._cache_ttl_hours:
                logger.debug(f"{self._data_type} 缓存已过期 ({age_hours:.1f}h)")
                return None

        try:
            df = pd.read_parquet(path)
            if not df.empty:
                logger.debug(f"{self._data_type} 缓存命中: {len(df)} 行")
                return df
        except Exception as e:
            logger.warning(f"{self._data_type} 缓存读取失败: {e}")
        return None

    def _write_cache(self, df: pd.DataFrame, cache_key: str | None = None) -> None:
        """写入 Parquet 缓存。"""
        if df is None or df.empty:
            return
        path = self._cache_path(cache_key=cache_key)
        try:
            df.to_parquet(path, index=False)
            logger.debug(f"{self._data_type} 缓存写入: {len(df)} 行")
        except Exception as e:
            logger.warning(f"{self._data_type} 缓存写入失败: {e}")

    # ---- 限流 ----

    async def _rate_limit(self) -> None:
        """确保距上次调用 >= _min_interval 秒。"""
        elapsed = time.time() - AltAlphaCollector._last_call_ts
        if elapsed < AltAlphaCollector._min_interval:
            await asyncio.sleep(AltAlphaCollector._min_interval - elapsed)
        AltAlphaCollector._last_call_ts = time.time()

    # ---- 核心获取方法 ----

    async def fetch_with_cache(self, api_fn: Any, *args: Any, **kwargs: Any) -> pd.DataFrame:
        """带限流+缓存+降级的数据获取。

        Args:
            api_fn: akshare API 函数
            *args, **kwargs: 传递给 api_fn 的参数

        Returns:
            DataFrame，失败时返回空 DataFrame 或过期缓存
        """
        cache_key = self._build_cache_key(api_fn, *args, **kwargs)

        # 1. 先查缓存
        cached = self._read_cache(cache_key=cache_key)
        if cached is not None:
            return cached

        # 2. 限流
        await self._rate_limit()

        # 3. 调用 API
        try:
            df = await asyncio.to_thread(api_fn, *args, **kwargs)
            if df is not None and not df.empty:
                # 4. 数据质量校验（子类可覆盖 _validate）
                df = self._validate(df)
                # 5. 写入缓存
                self._write_cache(df, cache_key=cache_key)
                logger.info(f"{self._data_type} 采集成功: {len(df)} 行")
                return df
            logger.warning(f"{self._data_type} API 返回空数据")
        except Exception as e:
            logger.error(f"{self._data_type} 采集失败: {e}")

        # 6. 降级：返回过期缓存
        stale = self._read_cache(ignore_ttl=True, cache_key=cache_key)
        if stale is not None:
            logger.info(f"{self._data_type} 使用过期缓存兜底: {len(stale)} 行")
            return stale

        return pd.DataFrame()

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """数据质量校验（子类可覆盖）。"""
        return df
