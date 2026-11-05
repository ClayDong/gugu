"""离线回测数据门面：统一缓存命名、样本量与 Point-in-Time 校验。

2026-11-04 评审 P0-3：离线脚本按 ``{sym}_{days}d.parquet`` 查找，而缓存实际
写的是 ``{sym}_{days}d_none.parquet``，导致 ``*_500d.parquet`` 命中 0、回测
零样本却静默产出空结果。本模块把命名解析、行数断言、未来 bar 拒绝收敛到一处。
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")
ADJUST_TYPES = ("none", "qfq", "hfq")
_CACHE_NAME_RE = re.compile(
    r"^(?P<symbol>[0-9A-Za-z]+)_(?P<days>\d+)d(?:_(?P<adjust>none|qfq|hfq))?\.parquet$"
)


def parse_cache_name(path: Path) -> tuple[str, int, str] | None:
    """解析 ``{sym}_{days}d[_adjust].parquet``，兼容新旧两种命名。"""
    match = _CACHE_NAME_RE.match(Path(path).name)
    if not match:
        return None
    return match.group("symbol"), int(match.group("days")), match.group("adjust") or "unknown"


def resolve_cache_path(symbol: str, days: int, cache_dir: Path) -> Path | None:
    """按优先级解析缓存路径：旧命名 → none → qfq → hfq。"""
    candidates = [Path(cache_dir) / f"{symbol}_{days}d.parquet"]
    candidates += [Path(cache_dir) / f"{symbol}_{days}d_{adj}.parquet" for adj in ADJUST_TYPES]
    for path in candidates:
        if path.exists():
            return path
    return None


def iter_cache_files(cache_dir: Path, *, days: int | None = None) -> list[Path]:
    """枚举兼容后缀的行情缓存（修复 ``*_500d.parquet`` 命中 0）。"""
    result: list[Path] = []
    for path in sorted(Path(cache_dir).glob("*.parquet")):
        parsed = parse_cache_name(path)
        if parsed is None:
            continue
        if days is not None and parsed[1] != days:
            continue
        result.append(path)
    return result


def validate_stock_history(
    df: pd.DataFrame,
    *,
    symbol: str,
    min_rows: int = 1,
    asof: str | None = None,
) -> pd.DataFrame | None:
    """校验历史数据；不合格返回 None 并给出明确原因（绝不静默当有效数据）。"""
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        logger.warning("{} 缓存缺少必需列 {}，拒绝用于回测", symbol, missing)
        return None
    if len(df) < min_rows:
        logger.warning(
            "{} 缓存仅 {} 行，要求 >= {} 行，拒绝用于回测（禁止短样本冒充长历史）",
            symbol,
            len(df),
            min_rows,
        )
        return None
    clean = df.copy()
    clean["date"] = pd.to_datetime(clean["date"])
    if asof is not None:
        future = clean[clean["date"] > pd.Timestamp(asof)]
        if not future.empty:
            logger.warning(
                "{} 缓存含 {} 根晚于 asof={} 的未来 bar，拒绝", symbol, len(future), asof
            )
            return None
    if not clean["date"].is_monotonic_increasing:
        clean = clean.sort_values("date").reset_index(drop=True)
    return clean


def load_stock_history(
    symbol: str,
    *,
    days: int,
    cache_dir: Path,
    asof: str | None = None,
) -> pd.DataFrame | None:
    """读取单股历史；文件缺失或样本不足（< days 行）时返回 None。"""
    path = resolve_cache_path(symbol, days, cache_dir)
    if path is None:
        logger.debug("{} {}d 缓存不存在", symbol, days)
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        logger.warning("读取 {} 失败: {}", path, exc)
        return None
    return validate_stock_history(df, symbol=symbol, min_rows=days, asof=asof)
