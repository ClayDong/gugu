"""断板反包战法选股预过滤器（陈学长不吃面策略）。"""

from __future__ import annotations

import threading
import time
from typing import Any

import pandas as pd

_DEFAULT_THRESHOLDS: dict[str, float] = {
    "market_cap_min": 50e8,
    "market_cap_max": 150e8,
    "price_min": 3.0,
    "position_high_pct": 0.70,
}


def _is_st_stock(name: str) -> bool:
    if not name:
        return False
    name_upper = name.upper()
    return "ST" in name_upper or "*ST" in name_upper


def _is_gem_stock(code: str) -> bool:
    code = code.strip().zfill(6)
    return code.startswith("300")


class _SnapshotCache:
    def __init__(self, ttl: int = 300) -> None:
        self._ttl = ttl
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, code: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._cache.get(code)
            if entry is None:
                return None
            if time.time() - entry["ts"] > self._ttl:
                del self._cache[code]
                return None
            return entry["data"]

    def set(self, code: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._cache[code] = {"data": data, "ts": time.time()}

    def bulk_set(self, df: pd.DataFrame) -> None:
        with self._lock:
            now = time.time()
            for _, row in df.iterrows():
                code = str(row.get("code", "")).zfill(6)
                if code:
                    self._cache[code] = {
                        "data": {
                            "price": row.get("price"),
                            "market_cap": row.get("market_cap"),
                            "name": row.get("name", ""),
                        },
                        "ts": now,
                    }


class _CodeCache:
    def __init__(self, ttl: int = 600) -> None:
        self._ttl = ttl
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, code: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._cache.get(code)
            if entry is None:
                return None
            if time.time() - entry["ts"] > self._ttl:
                del self._cache[code]
                return None
            return entry["data"]

    def set(self, code: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._cache[code] = {"data": data, "ts": time.time()}


class BreakoutReboundPrefilter:
    """断板反包战法选股预过滤器。"""

    def __init__(self) -> None:
        self._thresholds = _DEFAULT_THRESHOLDS.copy()
        self._snapshot_cache = _SnapshotCache()
        self._code_cache = _CodeCache()

    def check(
        self,
        symbol: str,
        name: str = "",
        price: float | None = None,
        market_cap: float | None = None,
    ) -> dict[str, Any]:
        code = symbol.strip().zfill(6)
        cached = self._code_cache.get(code)
        if cached is not None:
            return cached

        reasons: list[str] = []
        passed = True
        is_st = _is_st_stock(name)
        is_gem = _is_gem_stock(code)

        if is_st:
            passed = False
            reasons.append(f"ST股票（{name}），排除")

        if is_gem:
            passed = False
            reasons.append(f"创业板股票（{code}），排除")

        actual_price = price
        actual_market_cap = market_cap

        if actual_price is None or actual_market_cap is None:
            snapshot = self._snapshot_cache.get(code)
            if snapshot:
                actual_price = actual_price or snapshot.get("price")
                actual_market_cap = actual_market_cap or snapshot.get("market_cap")
                if not is_st and not name:
                    name = snapshot.get("name", "")
                    is_st = _is_st_stock(name)

        if actual_price is not None:
            if actual_price < self._thresholds["price_min"]:
                passed = False
                reasons.append(f"股价过低 {actual_price:.2f}元 < {self._thresholds['price_min']}元")
            else:
                reasons.append(f"股价 {actual_price:.2f}元 > {self._thresholds['price_min']}元")

        market_cap_b = None
        if actual_market_cap is not None:
            market_cap_b = actual_market_cap / 1e8
            if market_cap_b < 50:
                passed = False
                reasons.append(f"市值过小 {market_cap_b:.0f}亿 < 50亿")
            elif market_cap_b > 150:
                passed = False
                reasons.append(f"市值过大 {market_cap_b:.0f}亿 > 150亿")
            else:
                reasons.append(f"市值 {market_cap_b:.0f}亿 在 50-150亿范围内")

        position_level: str | None = None
        if passed and actual_price is not None:
            position_level = self._check_position_level(code, actual_price)
            if position_level == "high":
                passed = False
                reasons.append("当前价格处于近60日高位（>70%分位），排除")
            elif position_level == "low":
                reasons.append("当前价格处于近60日低位，符合选股条件")
            elif position_level == "mid":
                reasons.append("当前价格处于近60日中途，符合选股条件")

        result: dict[str, Any] = {
            "pass": passed,
            "reasons": reasons,
            "data": {
                "price": actual_price,
                "market_cap": actual_market_cap,
                "market_cap_billion": market_cap_b,
                "is_st": is_st,
                "is_gem": is_gem,
                "position_level": position_level,
            },
        }

        self._code_cache.set(code, result)
        return result

    def _check_position_level(self, code: str, current_price: float) -> str:
        snapshot = self._snapshot_cache.get(code)
        high_60d = snapshot.get("high_60d") if snapshot else None

        if high_60d is None or high_60d <= 0:
            return "mid"

        position_pct = current_price / high_60d
        if position_pct > 0.70:
            return "high"
        elif position_pct < 0.40:
            return "low"
        return "mid"

    def bulk_check(
        self,
        symbols: list[str],
        names: dict[str, str] | None = None,
        prices: dict[str, float] | None = None,
        market_caps: dict[str, float] | None = None,
    ) -> dict[str, dict[str, Any]]:
        names = names or {}
        prices = prices or {}
        market_caps = market_caps or {}

        results: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            results[symbol] = self.check(
                symbol=symbol,
                name=names.get(symbol, ""),
                price=prices.get(symbol),
                market_cap=market_caps.get(symbol),
            )
        return results

    def update_snapshot(self, code: str, price: float, market_cap: float, name: str = "") -> None:
        self._snapshot_cache.set(
            code.zfill(6),
            {
                "price": price,
                "market_cap": market_cap,
                "name": name,
            },
        )

    def clear_cache(self) -> None:
        self._code_cache = _CodeCache()
        self._snapshot_cache = _SnapshotCache()
