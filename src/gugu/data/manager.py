"""数据管理器：主源/降级源切换 + 缓存（内存+Parquet持久化） + 统一接口。

主源连续失败 fail_threshold 次后降级，冷却 fail_cooldown_seconds 秒后切回。

缓存策略（三层）：
1. Parquet 持久化缓存（跨进程、重启后复用历史数据）
2. 内存缓存（2分钟 TTL，进程内共享）
3. API 请求（主源 → 降级源）
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from functools import partial
from typing import Any

import pandas as pd

from gugu.data.cache import cache, persistent_cache
from gugu.data.collectors.akshare_collector import AkshareCollector
from gugu.data.collectors.base import BaseCollector
from gugu.data.collectors.eastmoney_collector import EastmoneyCollector
from gugu.data.collectors.fallback import SinaCollector
from gugu.data.collectors.tencent_collector import TencentCollector
from gugu.data.evidence import set_evidence
from gugu.data.quality import (
    sanitize_dataframe,
    validate_sector_flow,
    validate_stock_flow,
    validate_stock_history,
)
from gugu.data.source_health import DataSourceHealthMonitor
from gugu.data.stock_names import lookup_stock_name
from gugu.utils.log import get_logger

logger = get_logger()

_COLLECTOR_TIMEOUT_SECONDS = 45.0

# 采集器注册表：配置名 → 采集器类
# P0-3 修复：新增 tencent 作为第四备用源（本机最稳定的 qfq 日 K 源）
_COLLECTOR_REGISTRY: dict[str, type[BaseCollector]] = {
    "akshare": AkshareCollector,
    "sina": SinaCollector,
    "eastmoney": EastmoneyCollector,
    "tencent": TencentCollector,
}


def _stable_cache_key(method: str, *args: Any, **kwargs: Any) -> str:
    """生成稳定的缓存键。

    对于列表参数，排序后转为稳定字符串表示，确保相同数据无论顺序生成相同键。

    Args:
        method: 方法名
        args: 位置参数

    Returns:
        稳定缓存键字符串
    """
    key_parts = [method]
    for a in args:
        if isinstance(a, list):
            key_parts.append("[" + ",".join(sorted(str(x) for x in a)) + "]")
        elif isinstance(a, dict):
            key_parts.append("{" + ",".join(f"{k}:{v}" for k, v in sorted(a.items())) + "}")
        else:
            key_parts.append(str(a))
    for key, value in sorted(kwargs.items()):
        key_parts.append(f"{key}={value}")
    return "|".join(key_parts)


class DataManager:
    """数据管理器：封装多源切换逻辑。"""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        from gugu.config import get_config

        cfg = get_config()
        dc = cfg.data
        self._fail_threshold = dc.fail_threshold
        self._fail_cooldown = dc.fail_cooldown_seconds

        # 从配置读取数据源选择（支持通过 settings.yaml 一键切换）
        primary_name = dc.primary_source
        fallback_names = dc.fallback_sources

        primary_cls = _COLLECTOR_REGISTRY.get(primary_name)
        if primary_cls is None:
            logger.warning("未知主数据源 '{}'，回退到 akshare", primary_name)
            primary_cls = AkshareCollector

        self._primary = primary_cls()
        self._fallbacks: list[BaseCollector] = []
        for name in fallback_names:
            cls = _COLLECTOR_REGISTRY.get(name)
            if cls is not None:
                self._fallbacks.append(cls())

        if not self._fallbacks:
            logger.warning("无有效的降级数据源配置")
            self._fallbacks = [SinaCollector()]

        self._fail_count = 0
        self._degraded_until = 0.0  # 降级截止时间戳
        # P2 修复：降级源并发信号量，限制同时运行的 fallback 任务数
        self._fallback_semaphore: asyncio.Semaphore | None = None
        # Parquet 持久化缓存
        self._parquet_cache = persistent_cache()
        # 数据源健康监控（单例）
        self._health = DataSourceHealthMonitor()

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _get_fallback_semaphore(self) -> asyncio.Semaphore:
        if self._fallback_semaphore is None:
            self._fallback_semaphore = asyncio.Semaphore(len(self._fallbacks))
        return self._fallback_semaphore

    @property
    def is_degraded(self) -> bool:
        """是否处于降级状态。"""
        return time.time() < self._degraded_until

    @property
    def current_source(self) -> str:
        """获取当前数据源名称。"""
        if self.is_degraded:
            return self._fallbacks[0].source if self._fallbacks else "unknown"
        return self._primary.source

    @property
    def current_adjust_type(self) -> str:
        """获取当前数据源的复权方式。"""
        if self.is_degraded:
            return self._fallbacks[0].adjust_type if self._fallbacks else "none"
        return self._primary.adjust_type

    def _get_collector(self) -> BaseCollector:
        """获取当前可用采集器。冷却期过后切回主源并重置失败计数。"""
        if self.is_degraded:
            return self._fallbacks[0]
        # 冷却期结束切回主源时重置 fail_count，避免主源恢复后容错空间被压缩
        if self._fail_count >= self._fail_threshold:
            self._fail_count = 0
        return self._primary

    async def _on_success_locked(self, method: str = "") -> None:
        """主源成功：重置失败计数与降级状态（在锁内调用，DA-05 修复）。"""
        was_degraded = self._degraded_until > 0
        if was_degraded:
            logger.info("主源恢复，切回 {}", self._primary.source)
            self._health.record_recovered(self._primary.source, method)
        self._health.record_success(self._primary.source, method)
        self._fail_count = 0
        self._degraded_until = 0.0

    async def _on_failure_locked(self, method: str) -> None:
        """主源失败：递增失败计数，达到阈值则降级（在锁内调用，DA-05 修复）。"""
        self._fail_count += 1
        self._health.record_failure(
            self._primary.source, method, detail=f"{self._fail_count}/{self._fail_threshold}"
        )
        logger.warning("主源 {} 失败 ({}/{})", method, self._fail_count, self._fail_threshold)
        if self._fail_count >= self._fail_threshold:
            self._degraded_until = time.time() + self._fail_cooldown
            fallback_source = self._fallbacks[0].source if self._fallbacks else "unknown"
            self._health.record_degraded(self._primary.source, fallback_source, method)
            logger.warning(
                "主源连续失败 {} 次，降级到 {}，冷却 {} 秒",
                self._fail_count,
                fallback_source,
                self._fail_cooldown,
            )

    async def _is_degraded_locked(self) -> bool:
        """在锁内读取降级状态（DA-05 修复：与 fail_count 修改在同一锁内）。"""
        return time.time() < self._degraded_until

    # 主源（sina）不支持的方法——直接走降级源，不浪费请求+避免无效失败计数
    _UNSUPPORTED_BY_PRIMARY: frozenset[str] = frozenset({"fetch_stock_flow", "fetch_sector_flow"})

    # 方法名 → 质量校验函数的映射
    _VALIDATORS: dict[str, Any] = {
        "fetch_stock_history": validate_stock_history,
        "fetch_stock_flow": validate_stock_flow,
        "fetch_sector_flow": validate_sector_flow,
    }

    async def _call_with_fallback(self, method: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        """带降级的方法调用，返回前自动做数据质量校验。

        缓存策略（三层）：
        1. Parquet 持久化缓存（仅 fetch_stock_history，跨进程使用）
        2. 内存缓存（2min TTL，进程内）
        3. API 实时请求

        锁粒度：仅对失败计数/降级状态切换加锁，数据请求本身并发执行（A-02 修复）。
        缓存键：基于稳定的 method + args 序列化构建（DA-03 修复 + 列表顺序稳定）。

        特殊处理：fetch_stock_realtime([]) 空列表请求是全市场快照，
        sina 源不支持（返回空），需跳过主源直接用降级源。
        """
        collector = self._get_collector()
        adjust_type = getattr(collector, "adjust_type", "none")
        cache_key = _stable_cache_key(
            method, *args, source=collector.source, adjust_type=adjust_type
        )

        def _cache_key_for(source_collector: BaseCollector) -> str:
            return _stable_cache_key(
                method,
                *args,
                source=source_collector.source,
                adjust_type=getattr(source_collector, "adjust_type", "none"),
            )

        self._cache_key_for = _cache_key_for  # 供 _return_fallback_success 复用（2026-08-10）

        # 直接跳过主源的场景：sina不支持的方法直接走降级源，避免无效失败计数。
        # v0.9.10：fetch_stock_realtime([]) 全市场快照不再跳过 sina——
        # SinaCollector 已实现新浪 Market_Center 分页快照（东财限流保底源）。
        skip_primary = method in self._UNSUPPORTED_BY_PRIMARY

        # 1. 持久化缓存：仅对历史数据请求
        if method == "fetch_stock_history" and len(args) >= 1:
            symbol = str(args[0])
            days = int(args[1]) if len(args) > 1 else 60
            parquet_df = self._parquet_cache.get(symbol, days, adjust_type)
            if parquet_df is not None and not parquet_df.empty:
                self._set_attrs(parquet_df, collector, quality_status="validated")
                parquet_df.attrs["data_available"] = True
                parquet_df.attrs["data_stale"] = False
                cache().set(cache_key, parquet_df)
                return parquet_df

        # 2. 内存缓存
        cached = cache().get(cache_key)
        if cached is not None:
            self._set_attrs(cached, collector, quality_status="validated")
            cached.attrs.setdefault("data_available", not cached.empty)
            cached.attrs["data_stale"] = False
            return cached

        # 3. 实时请求
        async with self._get_lock():
            is_degraded = await self._is_degraded_locked()

        if not is_degraded and not skip_primary:
            # 非全市场快照请求，正常走主源
            try:
                _fn = partial(getattr(self._primary, method), *args, **kwargs)
                df = await asyncio.wait_for(
                    asyncio.to_thread(_fn), timeout=_COLLECTOR_TIMEOUT_SECONDS
                )
                df = self._validate(method, df, args)
                if df.empty and not df.attrs.get("flow_breaker_active"):
                    raise RuntimeError(f"主源 {method} 返回空数据")
                async with self._get_lock():
                    await self._on_success_locked(method)
                self._set_attrs(df, self._primary, quality_status="validated")
                df.attrs["data_available"] = not df.empty
                df.attrs["data_stale"] = False
                # P0 修复：空结果不缓存，避免 120s 内持续返回空且不触发降级
                if not df.empty:
                    cache().set(cache_key, df)
                    # 持久化缓存：仅对历史数据
                    if method == "fetch_stock_history" and len(args) >= 1:
                        self._parquet_cache.set(
                            str(args[0]), int(args[1]) if len(args) > 1 else 60, df, adjust_type
                        )
                return df
            except Exception as e:
                logger.error("主源 {} 异常: {}", method, e)
                async with self._get_lock():
                    await self._on_failure_locked(method)

        # 降级源：并发尝试所有 fallback，FIRST_COMPLETED 模式
        # 信号量保护整个 fallback 请求 + 等待过程，防止并发降级请求过多
        async with self._get_fallback_semaphore():
            tasks: dict[BaseCollector, asyncio.Task] = {
                fb: asyncio.create_task(
                    asyncio.wait_for(
                        asyncio.to_thread(lambda fb=fb: getattr(fb, method)(*args, **kwargs)),
                        timeout=_COLLECTOR_TIMEOUT_SECONDS,
                    )
                )
                for fb in self._fallbacks
            }
            done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)

            # 收集已完成任务结果（安全取值，容忍异常/取消）
            def _collect(
                done_tasks: set[asyncio.Task],
            ) -> list[tuple[asyncio.Task, pd.DataFrame | None]]:
                results: list[tuple[asyncio.Task, pd.DataFrame | None]] = []
                for t in done_tasks:
                    try:
                        if not t.cancelled():
                            res = t.result()
                            results.append((t, res if isinstance(res, pd.DataFrame) else None))
                    except Exception:
                        results.append((t, None))
                return results

            done_results = _collect(done)

            # v0.9.x 修复：所有分支 finally 统一取消 pending 任务，防止
            # "Task exception was never retrieved" asyncio 未检索异常泄漏刷屏
            try:
                # 快速路径：已有真实成功 → 立即返回（保持 FIRST_COMPLETED 快速语义）
                for t, df in done_results:
                    if df is not None and not df.empty:
                        return self._return_fallback_success(
                            t, df, tasks, method, args, adjust_type
                        )

                # 熔断判定（2026-08-10 修复）：已完成结果中有熔断空标记
                breaker_results = [
                    res
                    for _, res in done_results
                    if res is not None and res.attrs.get("flow_breaker_active")
                ]
                if breaker_results:
                    # 等 pending 完成（最多 _COLLECTOR_TIMEOUT_SECONDS），确认没有真实成功
                    if pending:
                        all_done, _ = await asyncio.wait(
                            pending, timeout=_COLLECTOR_TIMEOUT_SECONDS
                        )
                        pending_results = _collect(all_done)
                        done_results += pending_results
                    # 熔断短路：所有 fallback 均熔断（或空）→ 静默返回，不记录 all 失败
                    if not any(res is not None and not res.empty for _, res in done_results):
                        df = breaker_results[0]
                        df.attrs["data_available"] = False
                        df.attrs["data_error"] = "flow_breaker_active"
                        return df

                # 常规路径：等待剩余 pending 完成后返回首个成功
                if pending:
                    all_done, _ = await asyncio.wait(pending, timeout=_COLLECTOR_TIMEOUT_SECONDS)
                    done_results += _collect(all_done)
                for t, df in done_results:
                    if df is not None and not df.empty:
                        return self._return_fallback_success(
                            t, df, tasks, method, args, adjust_type
                        )
            finally:
                # 统一取消未完成任务（幂等：done/cancelled 任务为 no-op）
                self._cancel_pending_tasks(tasks)

        # 所有真实 fallback 都失败（非熔断）→ 记录 all 失败 + 缓存兜底
        logger.warning("所有数据源 {} 均失败，尝试缓存兜底", method)
        self._health.record_failure("all", method, detail="all_sources_failed")
        stale_cached = cache().get(cache_key, ignore_ttl=True)
        if stale_cached is not None and not stale_cached.empty:
            self._set_attrs(
                stale_cached,
                collector,
                quality_status="stale",
                data_stale=True,
                data_error="all_sources_failed",
            )
            stale_cached.attrs["data_available"] = True
            stale_cached.attrs["data_error"] = "all_sources_failed"
            logger.info("{} 使用过期缓存兜底，下次请求自动重试", method)
            return stale_cached
        with contextlib.suppress(Exception):
            cache().store.pop(cache_key, None)
        unavailable = pd.DataFrame()
        unavailable.attrs.update(
            data_available=False,
            data_error="all_sources_failed",
            all_sources_failed=True,
        )
        return unavailable

    @staticmethod
    def _set_attrs(
        df: pd.DataFrame,
        collector: BaseCollector,
        *,
        quality_status: str = "validated",
        data_stale: bool = False,
        data_error: str | None = None,
    ) -> pd.DataFrame:
        return set_evidence(
            df,
            data_source=collector.source,
            period="daily",
            adjust_type=getattr(collector, "adjust_type", "none"),
            quality_status=quality_status,
            fetch_timestamp=df.attrs.get("fetch_timestamp"),
            data_stale=data_stale,
            data_available=not df.empty,
            data_error=data_error,
        )

    @staticmethod
    def _cancel_pending_tasks(tasks: dict[BaseCollector, asyncio.Task]) -> None:
        """取消所有未完成的 fallback 任务，避免"Task exception was never retrieved"。

        在 _call_with_fallback 的每个 return 前调用，确保 pending 任务被取消后
        其 CancelledError 被 asyncio 正常处理，不会泄漏为未检索异常。
        """
        for t in tasks.values():
            if not t.done():
                t.cancel()
            # 无论 done/pending 都检索异常，防止 done 任务异常未被 _collect 覆盖时泄漏
            with contextlib.suppress(asyncio.CancelledError, asyncio.InvalidStateError, Exception):
                t.exception()

    def _return_fallback_success(
        self,
        task: asyncio.Task,
        df: pd.DataFrame,
        tasks: dict[BaseCollector, asyncio.Task],
        method: str,
        args: tuple,
        adjust_type: str,
    ) -> pd.DataFrame:
        """降级源成功结果的统一返回路径（校验/属性/内存缓存/持久化缓存）。"""
        self._cancel_pending_tasks(tasks)
        success_fb = next(fb for fb, t in tasks.items() if t is task)
        self._health.record_fallback_used(success_fb.source, method)
        logger.info("降级源 {} 成功获取 {}", success_fb.source, method)
        df = self._validate(method, df, args)
        self._set_attrs(df, success_fb, quality_status="validated")
        df.attrs["data_available"] = not df.empty
        df.attrs["data_stale"] = False
        cache().set(self._cache_key_for(success_fb), df)
        # 持久化缓存
        if method == "fetch_stock_history" and len(args) >= 1 and not df.empty:
            self._parquet_cache.set(
                str(args[0]), int(args[1]) if len(args) > 1 else 60, df, adjust_type
            )
        return df

    def _validate(self, method: str, df: pd.DataFrame, args: tuple) -> pd.DataFrame:
        """对采集结果做数据质量校验。"""
        validator = self._VALIDATORS.get(method)
        if validator is None:
            # v0.9.11 防线1：无专用校验器的方法（如 fetch_stock_realtime 快照）也统一清洗，
            # 消灭消费点各自防御 None/NaN 的重复修补
            return sanitize_dataframe(df)
        symbol = str(args[0]) if args else ""
        try:
            validated = validator(sanitize_dataframe(df), symbol)
            validated.attrs.update(df.attrs)
            # 若原始数据非空但校验后全部被剔除，视为数据质量问题
            if not df.empty and validated.empty:
                logger.warning("{} {} 数据质量校验后全部被剔除，返回空 DataFrame", method, symbol)
            return validated
        except Exception as e:
            logger.warning("{} 数据质量校验异常: {}，返回空 DataFrame", method, e)
            return pd.DataFrame()

    # ===== 对外接口 =====

    async def fetch_stock_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        # days 作为位置参数传递，确保 fetch_stock_history("600519") 与
        # fetch_stock_history("600519", days=90) 生成相同缓存键
        return await self._call_with_fallback("fetch_stock_history", symbol, days)

    async def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        return await self._call_with_fallback("fetch_stock_realtime", symbols)

    async def fetch_sector_flow(self) -> pd.DataFrame:
        return await self._call_with_fallback("fetch_sector_flow")

    async def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        return await self._call_with_fallback("fetch_stock_flow", symbol)

    async def fetch_stock_history_batch(
        self, symbols: list[str], days: int = 60
    ) -> dict[str, pd.DataFrame]:
        """批量拉取多只股票的历史行情数据。

        与 fetch_stock_realtime 对称，避免 N 次串行 HTTP 请求。
        每只股票独立处理失败，不中断整体。

        Args:
            symbols: 股票代码列表。
            days: 回看天数。

        Returns:
            {symbol: DataFrame} 字典，失败的 symbol 对应空 DataFrame。
        """
        # 并发拉取（Semaphore(10) 并发控制，与选股模块约定一致）
        sem = asyncio.Semaphore(10)

        async def _fetch_one(sym: str) -> tuple[str, pd.DataFrame]:
            async with sem:
                try:
                    df = await self.fetch_stock_history(sym, days=days)
                    return sym, df
                except Exception as e:
                    logger.warning("批量拉取 {} 历史数据失败: {}", sym, e)
                    return sym, pd.DataFrame()

        pairs = await asyncio.gather(*[_fetch_one(s) for s in symbols])
        return dict(pairs)

    async def fetch_stock_meta(self, symbol: str) -> dict[str, Any]:
        """获取股票元数据（用于风控 L3）。

        Returns:
            dict with keys prev_close, is_st, is_suspended, name.
        """
        code = BaseCollector.normalize_symbol(symbol)
        meta: dict[str, Any] = {
            "symbol": code,
            "name": lookup_stock_name(code),
            "prev_close": 0.0,
            "is_st": False,
            "is_suspended": False,
        }

        # 从实时快照取名称、现价、是否停牌
        try:
            rt = await self.fetch_stock_realtime([code])
            if not rt.empty:
                row = rt.iloc[0]
                api_name = str(row.get("name", "")).strip()
                # A-04 修复：实时快照在非交易时间可能返回空/异常名称
                # 此时保留 _STOCK_NAMES 的 fallback 而非覆盖为空
                if api_name and api_name != code and api_name != f"{code}":
                    meta["name"] = api_name
                price = float(row.get("price", 0) or 0)
                float(row.get("volume", 0) or 0)
                # P1-g 修复：停牌判定仅凭 price<=0，不再用 volume<=0
                # （集合竞价初期/低流动性股票 volume=0 但未停牌）
                meta["is_suspended"] = price <= 0
                # P2-9 修复：ST 判定使用更精确的匹配，避免误匹配含 ST 子串的股票名
                name = meta["name"]
                if name.startswith("ST") or name.startswith("*ST") or " ST" in name:
                    meta["is_st"] = True
        except Exception as e:
            logger.warning("获取 {} 实时元数据失败: {}", code, e)

        # 从近两日历史取前收盘价
        try:
            hist = await self.fetch_stock_history(code, days=2)
            if not hist.empty and len(hist) >= 2:
                meta["prev_close"] = float(hist.iloc[-2]["close"])
            elif not hist.empty:
                meta["prev_close"] = 0.0
        except Exception as e:
            logger.warning("获取 {} 前收盘价失败: {}", code, e)

        return meta


# 全局单例
_dm: DataManager | None = None


def data_manager() -> DataManager:
    """获取数据管理器单例。"""
    global _dm
    if _dm is None:
        _dm = DataManager()
    return _dm
