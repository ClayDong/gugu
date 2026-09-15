"""事件研究数据源与规范化工具。"""

from __future__ import annotations

import json
import math
import ssl
import urllib.request
from collections.abc import Callable
from typing import Any, TypeAlias

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()
DEFAULT_DATA_DAYS = 600
KlineLoader: TypeAlias = Callable[[str, int], pd.DataFrame]


def normalize_symbol(symbol: Any) -> str:
    value = str(symbol or "").strip().lower()
    if value in {"", "nan", "none", "nat"}:
        return ""
    if value[:2] in {"sh", "sz", "bj"}:
        value = value[2:]
    if not value.isdigit() or len(value) > 6:
        return ""
    return value.zfill(6)


def tencent_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    if not code:
        return ""
    if code.startswith(("4", "8", "920")):
        market = "bj"
    elif code.startswith(("6", "9")):
        market = "sh"
    else:
        market = "sz"
    return f"{market}{code}"


def tencent_index_code(index_code: str) -> str:
    value = str(index_code or "").strip().lower()
    if value[:2] in {"sh", "sz", "bj"}:
        return value
    code = normalize_symbol(value)
    return f"sz{code}" if code.startswith("399") else f"sh{code}"


def naive_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return pd.NaT
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize()


def safe_timestamp(value: Any) -> pd.Timestamp | pd.NaT:
    try:
        return naive_timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty or "date" not in df.columns:
        return pd.DataFrame()
    result = df.copy()
    result["date"] = result["date"].map(safe_timestamp)
    result = result.dropna(subset=["date"])
    required = ["open", "high", "low", "close", "volume"]
    if any(col not in result.columns for col in required):
        return pd.DataFrame()
    for col in required:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    if "amount" in result.columns:
        result["amount"] = pd.to_numeric(result["amount"], errors="coerce")
    result = result.dropna(subset=["open", "high", "low", "close"])
    result = result[
        (result[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (result["volume"] >= 0)
        & (result["high"] >= result[["open", "close"]].max(axis=1))
        & (result["low"] <= result[["open", "close"]].min(axis=1))
    ]
    return result.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def parse_tencent_kline(kline: list[Any], *, volume_factor: int = 100) -> pd.DataFrame:
    rows: list[list[Any]] = []
    for row in kline:
        if len(row) < 6:
            continue
        try:
            volume = float(row[5])
            close = float(row[2])
            amount = finite_float(row[6]) if len(row) > 6 else None
            if amount is None or amount <= 0:
                amount = volume * close * volume_factor
            rows.append(
                [row[0], float(row[1]), float(row[3]), float(row[4]), close, volume, amount]
            )
        except (IndexError, TypeError, ValueError):
            continue
    return normalize_ohlcv(
        pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    )


def _http_json(url: str) -> dict[str, Any]:
    context = ssl._create_unverified_context()
    request = urllib.request.Request(
        url, headers={"Referer": "http://finance.qq.com/", "User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(request, context=context, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", errors="ignore"))


def fetch_index_klines(index_code: str, days: int = DEFAULT_DATA_DAYS) -> pd.DataFrame:
    code = tencent_index_code(index_code)
    try:
        payload = _http_json(
            f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
        )
        node = payload.get("data", {}).get(code, {})
        return parse_tencent_kline(node.get("qfqday") or node.get("day") or [])
    except Exception as exc:
        logger.warning("指数 K 线获取失败 {}: {}", index_code, exc)
        return pd.DataFrame()


def fetch_stock_klines(symbol: str, days: int = DEFAULT_DATA_DAYS) -> pd.DataFrame:
    code = tencent_symbol(symbol)
    try:
        payload = _http_json(
            f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},qfq"
        )
        node = payload.get("data", {}).get(code, {})
        factor = 1 if code.startswith(("sh688", "sh689", "bj")) else 100
        return parse_tencent_kline(
            node.get("qfqday") or node.get("day") or [], volume_factor=factor
        )
    except Exception as exc:
        logger.warning("个股 K 线获取失败 {}: {}", symbol, exc)
        return pd.DataFrame()
