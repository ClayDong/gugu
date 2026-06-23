"""数据管理器：主源/降级源切换 + 缓存 + 统一接口。

主源连续失败 fail_threshold 次后降级，冷却 fail_cooldown_seconds 秒后切回。
"""
from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Any

import pandas as pd

from gugu.config import settings
from gugu.data.cache import cache
from gugu.data.collectors.akshare_collector import AkshareCollector
from gugu.data.collectors.base import BaseCollector
from gugu.data.collectors.fallback import SinaCollector
from gugu.data.quality import validate_sector_flow, validate_stock_flow, validate_stock_history
from gugu.utils.log import get_logger

logger = get_logger()


class DataManager:
    """数据管理器：封装多源切换逻辑。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        cfg = settings().get("data", {})
        self._fail_threshold = cfg.get("fail_threshold", 3)
        self._fail_cooldown = cfg.get("fail_cooldown_seconds", 300)

        self._primary = AkshareCollector()
        self._fallbacks: list[BaseCollector] = [SinaCollector()]

        self._fail_count = 0
        self._degraded_until = 0.0  # 降级截止时间戳

    @property
    def is_degraded(self) -> bool:
        """是否处于降级状态。"""
        return time.time() < self._degraded_until

    def _get_collector(self) -> BaseCollector:
        """获取当前可用采集器。冷却期过后切回主源并重置失败计数。"""
        if self.is_degraded:
            return self._fallbacks[0]
        # 冷却期结束切回主源时重置 fail_count，避免主源恢复后容错空间被压缩
        if self._fail_count >= self._fail_threshold:
            self._fail_count = 0
        return self._primary

    async def _on_success_locked(self) -> None:
        """主源成功：重置失败计数与降级状态（在锁内调用，DA-05 修复）。"""
        if self._fail_count > 0:
            logger.info("主源恢复，切回 akshare")
        self._fail_count = 0
        self._degraded_until = 0.0

    async def _on_failure_locked(self, method: str) -> None:
        """主源失败：递增失败计数，达到阈值则降级（在锁内调用，DA-05 修复）。"""
        self._fail_count += 1
        logger.warning(
            f"主源 {method} 失败 ({self._fail_count}/{self._fail_threshold})"
        )
        if self._fail_count >= self._fail_threshold:
            self._degraded_until = time.time() + self._fail_cooldown
            logger.warning(
                f"主源连续失败 {self._fail_count} 次，降级到 {self._fallbacks[0].source}，"
                f"冷却 {self._fail_cooldown} 秒"
            )

    async def _is_degraded_locked(self) -> bool:
        """在锁内读取降级状态（DA-05 修复：与 fail_count 修改在同一锁内）。"""
        return time.time() < self._degraded_until

    # 方法名 → 质量校验函数的映射
    _VALIDATORS: dict[str, Any] = {
        "fetch_stock_history": validate_stock_history,
        "fetch_stock_flow": validate_stock_flow,
        "fetch_sector_flow": validate_sector_flow,
    }

    async def _call_with_fallback(self, method: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        """带降级的方法调用，返回前自动做数据质量校验。

        锁粒度：仅对失败计数/降级状态切换加锁，数据请求本身并发执行（A-02 修复）。
        缓存键：基于 method + args 位置参数构建，kwargs 仅作透传（DA-03 修复）。
        """
        # 缓存键只包含方法名+位置参数值（排除 kwargs 传递方式差异）
        key_parts = [method]
        for a in args:
            key_parts.append(str(a))
        cache_key = "|".join(key_parts)

        cached = cache().get(cache_key)
        if cached is not None:
            return cached

        # 主源（使用 to_thread 避免同步 IO 阻塞事件循环，不加全局锁允许并发）
        # DA-05 修复：is_degraded 读取与 fail_count 修改在同一锁内
        async with self._lock:
            is_degraded = await self._is_degraded_locked()
        if not is_degraded:
            try:
                _fn = partial(getattr(self._primary, method), *args, **kwargs)
                df = await asyncio.to_thread(_fn)
                async with self._lock:  # 仅对状态切换加锁
                    await self._on_success_locked()
                df = self._validate(method, df, args)
                cache().set(cache_key, df)
                return df
            except Exception as e:
                logger.error(f"主源 {method} 异常: {e}")
                async with self._lock:  # 仅对失败计数加锁
                    await self._on_failure_locked(method)

        # 降级源：并发尝试所有 fallback，FIRST_COMPLETED 模式
        tasks: dict[BaseCollector, asyncio.Task] = {
            fb: asyncio.create_task(
                asyncio.wait_for(
                    asyncio.to_thread(lambda fb=fb: getattr(fb, method)(*args, **kwargs)),
                    timeout=30.0,
                )
            )
            for fb in self._fallbacks
        }
        done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            try:
                df = t.result()
                if df is not None and not df.empty:
                    # 找到对应的 collector 以记录 source 名称
                    success_fb = next(fb for fb, task in tasks.items() if task is t)
                    logger.info(f"降级源 {success_fb.source} 成功获取 {method}")
                    df = self._validate(method, df, args)
                    cache().set(cache_key, df)
                    # 取消仍在 pending 的任务
                    for pt in pending:
                        pt.cancel()
                    return df
            except Exception as e:
                logger.warning(f"降级源完成结果异常: {e}")
        # 所有 done 的任务都失败了：等待剩余 pending 并重试
        if pending:
            all_done, _ = await asyncio.wait(pending, timeout=30.0)
            for t in all_done:
                try:
                    df = t.result()
                    if df is not None and not df.empty:
                        success_fb = next(fb for fb, task in tasks.items() if task is t)
                        logger.info(f"降级源 {success_fb.source} 成功获取 {method}")
                        df = self._validate(method, df, args)
                        cache().set(cache_key, df)
                        return df
                except Exception:
                    continue

        logger.error(f"所有数据源 {method} 均失败，返回空 DataFrame")
        return pd.DataFrame()

    def _validate(self, method: str, df: pd.DataFrame, args: tuple) -> pd.DataFrame:
        """对采集结果做数据质量校验。"""
        validator = self._VALIDATORS.get(method)
        if validator is None:
            return df
        symbol = str(args[0]) if args else ""
        try:
            validated = validator(df, symbol)
            # 若原始数据非空但校验后全部被剔除，视为数据质量问题
            if not df.empty and validated.empty:
                logger.warning(f"{method} {symbol} 数据质量校验后全部被剔除，返回空 DataFrame")
            return validated
        except Exception as e:
            logger.warning(f"{method} 数据质量校验异常: {e}，返回空 DataFrame")
            return pd.DataFrame()

    # ===== 对外接口 =====

    async def fetch_stock_history(self, symbol: str, days: int = 60) -> pd.DataFrame:
        # days 作为位置参数传递，确保 fetch_stock_history("600519") 与
        # fetch_stock_history("600519", days=60) 生成相同缓存键（DA-03 修复）
        return await self._call_with_fallback("fetch_stock_history", symbol, days)

    async def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        return await self._call_with_fallback("fetch_stock_realtime", symbols)

    async def fetch_sector_flow(self) -> pd.DataFrame:
        return await self._call_with_fallback("fetch_sector_flow")

    async def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        return await self._call_with_fallback("fetch_stock_flow", symbol)

    async def fetch_stock_meta(self, symbol: str) -> dict[str, Any]:
        """获取股票元数据（用于风控 L3）。

        Returns:
            dict with keys prev_close, is_st, is_suspended, name.
        """
        code = BaseCollector.normalize_symbol(symbol)
        meta: dict[str, Any] = {
            "symbol": code,
            "name": "",
            "prev_close": 0.0,
            "is_st": False,
            "is_suspended": False,
        }

        # 从实时快照取名称、现价、是否停牌
        try:
            rt = await self.fetch_stock_realtime([code])
            if not rt.empty:
                row = rt.iloc[0]
                meta["name"] = str(row.get("name", ""))
                price = float(row.get("price", 0) or 0)
                volume = float(row.get("volume", 0) or 0)
                # P1-g 修复：停牌判定仅凭 price<=0，不再用 volume<=0
                # （集合竞价初期/低流动性股票 volume=0 但未停牌）
                meta["is_suspended"] = price <= 0
                # P2-9 修复：ST 判定使用更精确的匹配，避免误匹配含 ST 子串的股票名
                name = meta["name"]
                if name.startswith("ST") or name.startswith("*ST") or " ST" in name:
                    meta["is_st"] = True
        except Exception as e:
            logger.warning(f"获取 {code} 实时元数据失败: {e}")

        # 从近两日历史取前收盘价
        try:
            hist = await self.fetch_stock_history(code, days=2)
            if not hist.empty and len(hist) >= 2:
                meta["prev_close"] = float(hist.iloc[-2]["close"])
            elif not hist.empty:
                meta["prev_close"] = float(hist.iloc[-1]["close"])
        except Exception as e:
            logger.warning(f"获取 {code} 前收盘价失败: {e}")

        return meta


# 全局单例
_dm: DataManager | None = None


def data_manager() -> DataManager:
    """获取数据管理器单例。"""
    global _dm
    if _dm is None:
        _dm = DataManager()
    return _dm
