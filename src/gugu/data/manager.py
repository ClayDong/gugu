"""数据管理器：主源/降级源切换 + 缓存 + 统一接口。

主源连续失败 fail_threshold 次后降级，冷却 fail_cooldown_seconds 秒后切回。
"""
from __future__ import annotations

import time
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

    def _on_success(self) -> None:
        if self._fail_count > 0:
            logger.info("主源恢复，切回 akshare")
        self._fail_count = 0
        self._degraded_until = 0.0

    def _on_failure(self, method: str) -> None:
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

    # 方法名 → 质量校验函数的映射
    _VALIDATORS: dict[str, Any] = {
        "fetch_stock_history": validate_stock_history,
        "fetch_stock_flow": validate_stock_flow,
        "fetch_sector_flow": validate_sector_flow,
    }

    def _call_with_fallback(self, method: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        """带降级的方法调用，返回前自动做数据质量校验。"""
        # 缓存键只包含方法名+核心参数值（排除 kwargs 传递方式差异）
        key_parts = [method]
        for a in args:
            key_parts.append(str(a))
        for k in sorted(kwargs):
            key_parts.append(f"{k}={kwargs[k]}")
        cache_key = "|".join(key_parts)

        cached = cache().get(cache_key)
        if cached is not None:
            return cached

        # 主源
        if not self.is_degraded:
            try:
                df = getattr(self._primary, method)(*args, **kwargs)
                self._on_success()
                df = self._validate(method, df, args)
                cache().set(cache_key, df)
                return df
            except Exception as e:
                logger.error(f"主源 {method} 异常: {e}")
                self._on_failure(method)

        # 降级源
        for fb in self._fallbacks:
            try:
                df = getattr(fb, method)(*args, **kwargs)
                if df is not None and not df.empty:
                    logger.info(f"降级源 {fb.source} 成功获取 {method}")
                    df = self._validate(method, df, args)
                    cache().set(cache_key, df)
                    return df
            except Exception as e:
                logger.warning(f"降级源 {fb.source} {method} 失败: {e}")

        logger.error(f"所有数据源 {method} 均失败，返回空 DataFrame")
        return pd.DataFrame()

    def _validate(self, method: str, df: pd.DataFrame, args: tuple) -> pd.DataFrame:
        """对采集结果做数据质量校验。"""
        validator = self._VALIDATORS.get(method)
        if validator is None:
            return df
        symbol = str(args[0]) if args else ""
        try:
            return validator(df, symbol)
        except Exception as e:
            logger.warning(f"{method} 数据质量校验异常: {e}，返回原始数据")
            return df

    # ===== 对外接口 =====

    def fetch_stock_history(self, symbol: str, days: int = 60) -> pd.DataFrame:
        return self._call_with_fallback("fetch_stock_history", symbol, days=days)

    def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        return self._call_with_fallback("fetch_stock_realtime", symbols)

    def fetch_sector_flow(self) -> pd.DataFrame:
        return self._call_with_fallback("fetch_sector_flow")

    def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        return self._call_with_fallback("fetch_stock_flow", symbol)

    def fetch_stock_meta(self, symbol: str) -> dict[str, Any]:
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
            rt = self.fetch_stock_realtime([code])
            if not rt.empty:
                row = rt.iloc[0]
                meta["name"] = str(row.get("name", ""))
                price = float(row.get("price", 0) or 0)
                meta["is_suspended"] = price <= 0
                if "ST" in meta["name"]:
                    meta["is_st"] = True
        except Exception as e:
            logger.warning(f"获取 {code} 实时元数据失败: {e}")

        # 从近两日历史取前收盘价
        try:
            hist = self.fetch_stock_history(code, days=2)
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
