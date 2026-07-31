"""珍大户价值投资策略预过滤器。

选股过滤条件：
1. 流动性过滤：日成交额大于3亿元（价值股相对宽松）
2. ST股票排除
3. 创业板/北交所排除（可选）
4. 股价>1元（排除仙股）
5. PE < 25（排除高估股，基于珍大户估值规则）
6. PB < 2.0（排除高估股，基于珍大户估值规则）
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pandas as pd

_DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_daily_amount": 3e8,
    "min_price": 1.0,
    "max_price": 500.0,
    "max_pe": 25.0,
    "max_pb": 2.0,
    "min_dividend_yield": 0.0,
}


def _is_st_stock(name: str) -> bool:
    """判断是否为ST股票。"""
    if not name:
        return False
    name_upper = name.upper()
    return "ST" in name_upper or "*ST" in name_upper


def _is_gem_stock(code: str) -> bool:
    """判断是否为创业板股票。"""
    code = code.strip().zfill(6)
    return code.startswith("300")


def _is_bse_stock(code: str) -> bool:
    """判断是否为北交所股票。"""
    code = code.strip().zfill(6)
    return code.startswith("8") or code.startswith("4")


class _SnapshotCache:
    """行情快照缓存。"""

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
                            "name": row.get("name", ""),
                            "amount": row.get("amount"),
                            "pe": row.get("pe"),
                            "pb": row.get("pb"),
                            "dividend_yield": row.get("dividend_yield"),
                        },
                        "ts": now,
                    }


class _CodeCache:
    """代码级结果缓存。"""

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


class ZhenDahuPrefilter:
    """珍大户价值投资策略预过滤器。"""

    def __init__(self) -> None:
        self._thresholds = _DEFAULT_THRESHOLDS.copy()
        self._snapshot_cache = _SnapshotCache()
        self._code_cache = _CodeCache()

    def check(
        self,
        symbol: str,
        name: str = "",
        price: float | None = None,
        amount: float | None = None,
        pe: float | None = None,
        pb: float | None = None,
        dividend_yield: float | None = None,
    ) -> dict[str, Any]:
        """检查个股是否满足珍大户价值投资策略的选股条件。

        Args:
            symbol: 股票代码
            name: 股票名称
            price: 当前价格
            amount: 当日成交额
            pe: 市盈率
            pb: 市净率
            dividend_yield: 股息率（百分比，如3.5表示3.5%）

        Returns:
            包含pass、reasons、data的字典
        """
        code = symbol.strip().zfill(6)
        cached = self._code_cache.get(code)
        if cached is not None:
            return cached

        reasons: list[str] = []
        passed = True
        is_st = _is_st_stock(name)
        is_gem = _is_gem_stock(code)
        is_bse = _is_bse_stock(code)

        # ST股票排除
        if is_st:
            passed = False
            reasons.append(f"ST股票（{name}），排除")

        # 创业板排除
        if is_gem:
            passed = False
            reasons.append(f"创业板股票（{code}），排除")

        # 北交所排除
        if is_bse:
            passed = False
            reasons.append(f"北交所股票（{code}），排除")

        actual_price = price
        actual_amount = amount
        actual_pe = pe
        actual_pb = pb
        actual_dividend_yield = dividend_yield

        if actual_price is None or actual_amount is None:
            snapshot = self._snapshot_cache.get(code)
            if snapshot:
                actual_price = actual_price or snapshot.get("price")
                actual_amount = actual_amount or snapshot.get("amount")
                actual_pe = actual_pe or snapshot.get("pe")
                actual_pb = actual_pb or snapshot.get("pb")
                actual_dividend_yield = actual_dividend_yield or snapshot.get("dividend_yield")
                if not is_st and not name:
                    name = snapshot.get("name", "")
                    is_st = _is_st_stock(name)
                    if is_st:
                        passed = False
                        reasons.append(f"ST股票（{name}），排除")

        # 价格过滤
        if actual_price is not None:
            if actual_price < self._thresholds["min_price"]:
                passed = False
                reasons.append(
                    f"股价过低 {actual_price:.2f}元 < {self._thresholds['min_price']}元（仙股）"
                )
            elif actual_price > self._thresholds["max_price"]:
                passed = False
                reasons.append(f"股价过高 {actual_price:.2f}元 > {self._thresholds['max_price']}元")

        # 流动性过滤（日成交额>3亿）
        if actual_amount is not None:
            amount_b = actual_amount / 1e8
            if actual_amount < self._thresholds["min_daily_amount"]:
                passed = False
                reasons.append(f"成交额不足 {amount_b:.2f}亿 < 3亿（流动性不足）")

        # PE过滤（珍大户规则：PE >= 25 偏高，不买入）
        if actual_pe is not None:
            if actual_pe < 0:
                passed = False
                reasons.append(f"PE为负 {actual_pe:.2f}（亏损股），排除")
            elif actual_pe > self._thresholds["max_pe"]:
                passed = False
                reasons.append(f"PE过高 {actual_pe:.2f} > {self._thresholds['max_pe']}（估值偏高）")

        # PB过滤（珍大户规则：PB >= 2.0 需谨慎）
        if actual_pb is not None:
            if actual_pb < 0:
                passed = False
                reasons.append(f"PB为负 {actual_pb:.2f}（资不抵债），排除")
            elif actual_pb > self._thresholds["max_pb"]:
                passed = False
                reasons.append(f"PB过高 {actual_pb:.2f} > {self._thresholds['max_pb']}（估值偏高）")

        result: dict[str, Any] = {
            "pass": passed,
            "reasons": reasons,
            "data": {
                "price": actual_price,
                "amount": actual_amount,
                "amount_billion": actual_amount / 1e8 if actual_amount else None,
                "pe": actual_pe,
                "pb": actual_pb,
                "dividend_yield": actual_dividend_yield,
                "is_st": is_st,
                "is_gem": is_gem,
                "is_bse": is_bse,
            },
        }

        self._code_cache.set(code, result)
        return result

    def bulk_check(
        self,
        symbols: list[str],
        names: dict[str, str] | None = None,
        prices: dict[str, float] | None = None,
        amounts: dict[str, float] | None = None,
        pes: dict[str, float] | None = None,
        pbs: dict[str, float] | None = None,
        dividend_yields: dict[str, float] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """批量检查多个股票。"""
        names = names or {}
        prices = prices or {}
        amounts = amounts or {}
        pes = pes or {}
        pbs = pbs or {}
        dividend_yields = dividend_yields or {}

        results: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            results[symbol] = self.check(
                symbol=symbol,
                name=names.get(symbol, ""),
                price=prices.get(symbol),
                amount=amounts.get(symbol),
                pe=pes.get(symbol),
                pb=pbs.get(symbol),
                dividend_yield=dividend_yields.get(symbol),
            )
        return results

    def update_snapshot(
        self,
        code: str,
        price: float,
        name: str = "",
        amount: float | None = None,
        pe: float | None = None,
        pb: float | None = None,
        dividend_yield: float | None = None,
    ) -> None:
        """更新行情快照。"""
        self._snapshot_cache.set(
            code.zfill(6),
            {
                "price": price,
                "name": name,
                "amount": amount,
                "pe": pe,
                "pb": pb,
                "dividend_yield": dividend_yield,
            },
        )

    def clear_cache(self) -> None:
        """清空缓存。"""
        self._code_cache = _CodeCache()
        self._snapshot_cache = _SnapshotCache()
