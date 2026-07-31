"""首板战法选股预过滤器（北京炒家策略）。"""

from __future__ import annotations

import threading
import time
from typing import Any

import pandas as pd

_DEFAULT_THRESHOLDS: dict[str, float] = {
    "market_cap_min": 30e8,
    "market_cap_max": 100e8,
    "price_max": 20.0,
    "price_min": 3.0,
    "turnover_min": 0.05,
    "turnover_max": 0.15,
    "position_high_pct": 0.70,
}


def _is_st_stock(name: str) -> bool:
    if not name:
        return False
    name_upper = name.upper()
    return "ST" in name_upper or "*ST" in name_upper


def _is_new_stock(listed_days: int | None, threshold: int = 60) -> bool:
    if listed_days is None:
        return False
    return listed_days < threshold


def _is_gem_stock(code: str) -> bool:
    code = code.strip().zfill(6)
    return code.startswith("300")


def _is_bse_stock(code: str) -> bool:
    code = code.strip().zfill(6)
    return code.startswith("8") or code.startswith("4")


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


class FirstBoardPrefilter:
    """首板战法选股预过滤器（北京炒家策略）。"""

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
        listed_days: int | None = None,
    ) -> dict[str, Any]:
        code = symbol.strip().zfill(6)
        cached = self._code_cache.get(code)
        if cached is not None:
            return cached

        reasons: list[str] = []
        passed = True
        is_st = _is_st_stock(name)
        is_gem = _is_gem_stock(code)
        is_bse = _is_bse_stock(code)
        is_new = _is_new_stock(listed_days)

        if is_st:
            passed = False
            reasons.append(f"ST股票（{name}），排除")

        if is_gem:
            passed = False
            reasons.append(f"创业板股票（{code}），排除")

        if is_bse:
            passed = False
            reasons.append(f"北交所股票（{code}），排除")

        if is_new:
            passed = False
            reasons.append(f"次新股（上市{listed_days}天），排除")

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
            if actual_price >= self._thresholds["price_max"]:
                passed = False
                reasons.append(
                    f"股价过高 {actual_price:.2f}元 >= {self._thresholds['price_max']}元"
                )
            elif actual_price < self._thresholds["price_min"]:
                passed = False
                reasons.append(f"股价过低 {actual_price:.2f}元 < {self._thresholds['price_min']}元")

        market_cap_b = None
        if actual_market_cap is not None:
            market_cap_b = actual_market_cap / 1e8
            if market_cap_b < 30:
                passed = False
                reasons.append(f"市值过小 {market_cap_b:.0f}亿 < 30亿")
            elif market_cap_b > 100:
                passed = False
                reasons.append(f"市值过大 {market_cap_b:.0f}亿 > 100亿")

        position_level: str | None = None
        if passed and actual_price is not None:
            position_level = self._check_position_level(code, actual_price)
            if position_level == "high":
                passed = False
                reasons.append("当前价格处于近60日高位（>70%分位），排除高位股")

        result: dict[str, Any] = {
            "pass": passed,
            "reasons": reasons,
            "data": {
                "price": actual_price,
                "market_cap": actual_market_cap,
                "market_cap_billion": market_cap_b,
                "is_st": is_st,
                "is_gem": is_gem,
                "is_bse": is_bse,
                "is_new": is_new,
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
        listed_days: dict[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        names = names or {}
        prices = prices or {}
        market_caps = market_caps or {}
        listed_days = listed_days or {}

        results: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            results[symbol] = self.check(
                symbol=symbol,
                name=names.get(symbol, ""),
                price=prices.get(symbol),
                market_cap=market_caps.get(symbol),
                listed_days=listed_days.get(symbol),
            )
        return results

    def update_snapshot(
        self,
        code: str,
        price: float,
        market_cap: float,
        name: str = "",
        high_60d: float | None = None,
    ) -> None:
        self._snapshot_cache.set(
            code.zfill(6),
            {
                "price": price,
                "market_cap": market_cap,
                "name": name,
                "high_60d": high_60d,
            },
        )

    def clear_cache(self) -> None:
        self._code_cache = _CodeCache()
        self._snapshot_cache = _SnapshotCache()
