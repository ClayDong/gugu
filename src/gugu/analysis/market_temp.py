"""市场温度计：基于 PE/PB/巴菲特指数的多维度市场估值温度。

核心逻辑（参考博格市场温度计方法）：
- 采集多个宽基指数的 PE（TTM）和 PB 历史序列
- 采集巴菲特指数（总市值/GDP）历史序列
- 计算各指标近 10 年百分位
- 加权综合得到全市场温度（0°-100°）

温度规则（仅供参考，非交易指令）：
  < 20°  低估区，加倍买入
  < 30°  偏低区，正常买入
  30°-50° 适中区，正常定投
  > 50°  偏高区，停止买入/逐步减仓
  > 70°  高估区，大幅减仓

注意事项（来自博格原文）：
- 10 年滚动窗口有"数据迷惑性"——剔除极值后温度显涨
- PE=股价/盈利，盈利下滑时即使股价不变温度也会升高
- 内部行业/指数冷热不均，温度代表整体状态
- 不适用于港股（A 股温度计）
- ⚠️ 本温度计为估值参考，所有策略输出均为指导决策，非自动交易指令

数据源（多源冗余）：
1. 乐咕乐股 stock_index_pe_lg（PE 历史，12 宽基指数，10 年+日频）
2. 乐咕乐股 stock_index_pb_lg（PB 历史，沪深300/中证500 等）
3. 乐咕乐股 stock_buffett_index_lg（巴菲特指数=总市值/GDP）
4. 东方财富 push2（备用 PE 源）
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# ── 指数列表（akshare stock_index_pe_lg 支持的宽基指数）──────────
# 覆盖大盘价值/大盘核心/中盘/小盘/红利/创业板，足以反映市场整体估值
WIDE_INDEXES = [
    {"name": "上证50", "lg_symbol": "上证50", "weight": 1.0},
    {"name": "沪深300", "lg_symbol": "沪深300", "weight": 1.0},
    {"name": "上证380", "lg_symbol": "上证380", "weight": 1.0},
    {"name": "创业板50", "lg_symbol": "创业板50", "weight": 1.0},
    {"name": "中证500", "lg_symbol": "中证500", "weight": 1.0},
    {"name": "上证180", "lg_symbol": "上证180", "weight": 1.0},
    {"name": "深证红利", "lg_symbol": "深证红利", "weight": 1.0},
    {"name": "深证100", "lg_symbol": "深证100", "weight": 1.0},
    {"name": "中证1000", "lg_symbol": "中证1000", "weight": 1.0},
    {"name": "上证红利", "lg_symbol": "上证红利", "weight": 1.0},
    {"name": "中证100", "lg_symbol": "中证100", "weight": 1.0},
    {"name": "中证800", "lg_symbol": "中证800", "weight": 1.0},
]

# ── PB 指数列表（stock_index_pb_lg 支持的指数）──────────────────
PB_INDEXES = [
    {"name": "沪深300", "lg_symbol": "沪深300"},
    {"name": "中证500", "lg_symbol": "中证500"},
]

# 温度阀值
_TEMP_BUY_DOUBLE = 20  # < 20° 加倍买入
_TEMP_BUY_NORMAL = 30  # < 30° 正常买入
_TEMP_HOLD = 50  # < 50° 正常定投
_TEMP_SELL_HEAVY = 70  # ≥ 70° 大幅减仓

# 历史回看年数
_LOOKBACK_YEARS = 10

# 缓存 TTL（分钟）
_CACHE_TTL_MINUTES = 30

# ── 综合温度权重 ──────────────────────────────────────────────
# PE 百分位（12 宽基平均）权重 0.7 —— 主维度，反映盈利端估值
# PB 百分位（沪深300+中证500 平均）权重 0.2 —— 辅助维度，反映资产端估值
# 巴菲特指数（总市值/GDP）权重 0.1 —— 宏观维度，反映证券化率
_WEIGHT_PE = 0.7
_WEIGHT_PB = 0.2
_WEIGHT_BUFFETT = 0.1

# 东方财富 push2 备用 PE 接口
_EASTMONEY_PE_URL = "https://push2.eastmoney.com/api/qt/stock/kline/get"
_EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


@dataclass
class IndexTempResult:
    """单个指数的温度计算结果。"""

    name: str
    current_pe: float | None
    pe_percentile: float | None  # 0-100
    min_pe: float | None
    max_pe: float | None
    median_pe: float | None
    data_points: int = 0
    error: str | None = None


@dataclass
class PBTempResult:
    """单个指数 PB 百分位结果。"""

    name: str
    current_pb: float | None
    pb_percentile: float | None  # 0-100
    min_pb: float | None
    max_pb: float | None
    median_pb: float | None
    data_points: int = 0
    error: str | None = None


@dataclass
class BuffettResult:
    """巴菲特指数（总市值/GDP）结果。"""

    current_ratio: float | None  # 当前总市值/GDP 比值
    ratio_percentile: float | None  # 0-100
    min_ratio: float | None
    max_ratio: float | None
    median_ratio: float | None
    data_points: int = 0
    error: str | None = None


@dataclass
class MarketTempResult:
    """全市场温度计算结果（多维度加权）。"""

    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    temperature: float | None = None  # 0.0-100.0 综合温度
    pe_temperature: float | None = None  # PE 百分位平均温度
    pb_temperature: float | None = None  # PB 百分位平均温度
    buffett_temperature: float | None = None  # 巴菲特指数百分位温度
    indexes: list[IndexTempResult] = field(default_factory=list)
    pb_indexes: list[PBTempResult] = field(default_factory=list)
    buffett: BuffettResult | None = None
    hottest: IndexTempResult | None = None
    coldest: IndexTempResult | None = None
    total_indexes: int = 0
    success_count: int = 0
    pb_success_count: int = 0
    buffett_success: bool = False
    data_source: str = "legulegu"
    error: str | None = None

    @property
    def zone(self) -> str:
        if self.temperature is None:
            return "unknown"
        if self.temperature < _TEMP_BUY_DOUBLE:
            return "低估区（加倍买入）"
        if self.temperature < _TEMP_BUY_NORMAL:
            return "偏低区（正常买入）"
        if self.temperature < _TEMP_HOLD:
            return "适中区（正常定投）"
        if self.temperature < _TEMP_SELL_HEAVY:
            return "偏高区（逐步减仓）"
        return "高估区（大幅减仓）"

    @property
    def action_advice(self) -> str:
        if self.temperature is None:
            return "数据不足，无法给出建议"
        if self.temperature < _TEMP_BUY_DOUBLE:
            return "加倍买入低估值指数/行业（仅供参考）"
        if self.temperature < _TEMP_BUY_NORMAL:
            return "正常买入，重点配置低估行业（仅供参考）"
        if self.temperature < _TEMP_HOLD:
            return "正常定投，注意行业冷热不均（仅供参考）"
        if self.temperature < _TEMP_SELL_HEAVY:
            return "停止加仓，分批减仓盈利仓位（仅供参考）"
        return "大幅减仓，保留现金等待低估机会（仅供参考）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "temperature": self.temperature,
            "pe_temperature": self.pe_temperature,
            "pb_temperature": self.pb_temperature,
            "buffett_temperature": self.buffett_temperature,
            "zone": self.zone,
            "action_advice": self.action_advice,
            "total_indexes": self.total_indexes,
            "success_count": self.success_count,
            "pb_success_count": self.pb_success_count,
            "buffett_success": self.buffett_success,
            "data_source": self.data_source,
            "error": self.error,
            "hottest": {
                "name": self.hottest.name,
                "pe_percentile": self.hottest.pe_percentile,
                "current_pe": self.hottest.current_pe,
            }
            if self.hottest
            else None,
            "coldest": {
                "name": self.coldest.name,
                "pe_percentile": self.coldest.pe_percentile,
                "current_pe": self.coldest.current_pe,
            }
            if self.coldest
            else None,
        }


# ── 全局缓存 ──────────────────────────────────────────────────
_cache: dict[str, Any] = {}
_cache_time: float = 0.0
_cache_lock = threading.Lock()


def _fetch_pe_history_lg(symbol: str, years: int = _LOOKBACK_YEARS) -> pd.DataFrame | None:
    """乐咕乐股数据源：获取指数 PE（TTM）历史序列。

    通过 akshare 的 stock_index_pe_lg 接口获取日频 PE 数据。
    主数据源，覆盖 12 个宽基指数，历史 10 年+。

    Args:
        symbol: 指数名称（如 "沪深300"）。
        years: 回看年数。

    Returns:
        DataFrame，含 date 和 pe 列，或 None。
    """
    try:
        import akshare as ak

        raw = ak.stock_index_pe_lg(symbol=symbol)
        if raw is None or raw.empty:
            return None

        # 列名：日期, 指数, 等权静态市盈率, 静态市盈率, 静态市盈率中位数,
        #       等权滚动市盈率, 滚动市盈率, 滚动市盈率中位数
        # 优先使用滚动市盈率（TTM），缺失时降级到静态市盈率
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["日期"], errors="coerce"),
                "pe": pd.to_numeric(raw.get("滚动市盈率"), errors="coerce"),
            }
        )
        df["pe"] = df["pe"].fillna(pd.to_numeric(raw.get("静态市盈率"), errors="coerce"))
        df = df.dropna(subset=["date", "pe"])
        df = df[df["pe"] > 0]  # 过滤负 PE（亏损）
        df = df.sort_values("date").drop_duplicates(subset="date").reset_index(drop=True)

        # 仅保留近 years 年
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
        df = df[df["date"] >= cutoff].reset_index(drop=True)
        return df if not df.empty else None
    except Exception as e:
        logger.debug("乐咕乐股 {} PE 数据获取失败: {}", symbol, e)
        return None


def _fetch_pe_history_eastmoney(code: str, years: int = _LOOKBACK_YEARS) -> pd.DataFrame | None:
    """东方财富 push2 备用数据源：获取指数 PE 历史序列。

    当乐咕乐股数据源失败时使用。通过 kline 接口获取历史数据，
    f57 字段为 PE（TTM）。

    Args:
        code: 指数代码（如 "000300"）。
        years: 回看年数。
    """
    secid_prefix = "0" if code.startswith("399") else "1"
    secid = f"{secid_prefix}.{code}"
    needed_days = years * 365

    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f57",  # 日期, PE(TTM)
        "klt": "101",  # 日K
        "lmt": str(needed_days),
    }
    try:
        with httpx.Client(timeout=15.0, headers=_EASTMONEY_HEADERS) as client:
            resp = client.get(_EASTMONEY_PE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        if not data or not data.get("data") or not data["data"].get("klines"):
            return None

        records: list[dict] = []
        for kline in data["data"]["klines"]:
            parts = kline.split(",")
            if len(parts) < 2:
                continue
            try:
                pe_val = float(parts[1]) if parts[1] not in ("", "-") else None
                if pe_val is not None and pe_val > 0:
                    records.append({"date": parts[0], "pe": pe_val})
            except (ValueError, IndexError):
                continue

        if not records:
            return None
        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
        df = df.drop_duplicates(subset="date").reset_index(drop=True)
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
        df = df[df["date"] >= cutoff].reset_index(drop=True)
        return df if not df.empty else None
    except Exception as e:
        logger.debug(f"东方财富 {code} PE 数据获取失败: {e}")
        return None


def _fetch_pb_history_lg(symbol: str, years: int = _LOOKBACK_YEARS) -> pd.DataFrame | None:
    """乐咕乐股数据源：获取指数 PB 历史序列。

    通过 akshare 的 stock_index_pb_lg 接口获取日频 PB 数据。

    Args:
        symbol: 指数名称（如 "沪深300"）。
        years: 回看年数。

    Returns:
        DataFrame，含 date 和 pb 列，或 None。
    """
    try:
        import akshare as ak

        raw = ak.stock_index_pb_lg(symbol=symbol)
        if raw is None or raw.empty:
            return None

        # 列名：日期, 指数, 市净率, 等权市净率, 市净率中位数
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["日期"], errors="coerce"),
                "pb": pd.to_numeric(raw.get("市净率"), errors="coerce"),
            }
        )
        df["pb"] = df["pb"].fillna(pd.to_numeric(raw.get("等权市净率"), errors="coerce"))
        df = df.dropna(subset=["date", "pb"])
        df = df[df["pb"] > 0]
        df = df.sort_values("date").drop_duplicates(subset="date").reset_index(drop=True)

        cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
        df = df[df["date"] >= cutoff].reset_index(drop=True)
        return df if not df.empty else None
    except Exception as e:
        logger.debug(f"乐咕乐股 {symbol} PB 数据获取失败: {e}")
        return None


def _fetch_buffett_index_lg(years: int = _LOOKBACK_YEARS) -> pd.DataFrame | None:
    """乐咕乐股数据源：获取巴菲特指数（总市值/GDP）历史序列。

    通过 akshare 的 stock_buffett_index_lg 接口获取日频数据。

    Args:
        years: 回看年数。

    Returns:
        DataFrame，含 date 和 ratio 列，或 None。
    """
    try:
        import akshare as ak

        raw = ak.stock_buffett_index_lg()
        if raw is None or raw.empty:
            return None

        # 列名：日期, 收盘价, 总市值, GDP
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["日期"], errors="coerce"),
            }
        )
        market_cap = pd.to_numeric(raw.get("总市值"), errors="coerce")
        gdp = pd.to_numeric(raw.get("GDP"), errors="coerce")
        # 巴菲特指数 = 总市值 / GDP（单位：均为亿元，比值无单位）
        df["ratio"] = market_cap / gdp
        df = df.dropna(subset=["date", "ratio"])
        df = df[df["ratio"] > 0]
        df = df.sort_values("date").drop_duplicates(subset="date").reset_index(drop=True)

        cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
        df = df[df["date"] >= cutoff].reset_index(drop=True)
        return df if not df.empty else None
    except Exception as e:
        logger.debug("巴菲特指数数据获取失败: {}", e)
        return None


def calc_pb_percentile(df: pd.DataFrame) -> dict[str, Any]:
    """计算 PB 历史百分位。

    Args:
        df: 含 pb 列的 DataFrame。

    Returns:
        dict 含 current_pb, pb_percentile, min_pb, max_pb, median_pb, data_points。
    """
    if df is None or df.empty:
        return {
            "current_pb": None,
            "pb_percentile": None,
            "min_pb": None,
            "max_pb": None,
            "median_pb": None,
            "data_points": 0,
        }

    pb_series = df["pb"].dropna()
    if pb_series.empty:
        return {
            "current_pb": None,
            "pb_percentile": None,
            "min_pb": None,
            "max_pb": None,
            "median_pb": None,
            "data_points": 0,
        }

    current_pb = float(pb_series.iloc[-1])
    min_pb = float(pb_series.min())
    max_pb = float(pb_series.max())
    median_pb = float(pb_series.median())

    count_below = int((pb_series < current_pb).sum())
    count_equal = int((pb_series == current_pb).sum())
    count_total = len(pb_series)
    pb_percentile = round((count_below + 0.5 * count_equal) / count_total * 100, 1)

    return {
        "current_pb": round(current_pb, 3),
        "pb_percentile": pb_percentile,
        "min_pb": round(min_pb, 3),
        "max_pb": round(max_pb, 3),
        "median_pb": round(median_pb, 3),
        "data_points": count_total,
    }


def calc_buffett_percentile(df: pd.DataFrame) -> dict[str, Any]:
    """计算巴菲特指数历史百分位。

    Args:
        df: 含 ratio 列的 DataFrame。

    Returns:
        dict 含 current_ratio, ratio_percentile, min_ratio, max_ratio, median_ratio, data_points。
    """
    if df is None or df.empty:
        return {
            "current_ratio": None,
            "ratio_percentile": None,
            "min_ratio": None,
            "max_ratio": None,
            "median_ratio": None,
            "data_points": 0,
        }

    ratio_series = df["ratio"].dropna()
    if ratio_series.empty:
        return {
            "current_ratio": None,
            "ratio_percentile": None,
            "min_ratio": None,
            "max_ratio": None,
            "median_ratio": None,
            "data_points": 0,
        }

    current_ratio = float(ratio_series.iloc[-1])
    min_ratio = float(ratio_series.min())
    max_ratio = float(ratio_series.max())
    median_ratio = float(ratio_series.median())

    count_below = int((ratio_series < current_ratio).sum())
    count_equal = int((ratio_series == current_ratio).sum())
    count_total = len(ratio_series)
    ratio_percentile = round((count_below + 0.5 * count_equal) / count_total * 100, 1)

    return {
        "current_ratio": round(current_ratio, 3),
        "ratio_percentile": ratio_percentile,
        "min_ratio": round(min_ratio, 3),
        "max_ratio": round(max_ratio, 3),
        "median_ratio": round(median_ratio, 3),
        "data_points": count_total,
    }


async def fetch_pb_temp(index: dict) -> PBTempResult:
    """计算单个指数的 PB 温度。

    Args:
        index: 指数配置 dict，含 name, lg_symbol。

    Returns:
        PBTempResult。
    """
    name = index["name"]
    lg_symbol = index.get("lg_symbol", name)

    df = await asyncio.to_thread(_fetch_pb_history_lg, lg_symbol)

    if df is None:
        return PBTempResult(
            name=name,
            current_pb=None,
            pb_percentile=None,
            min_pb=None,
            max_pb=None,
            median_pb=None,
            error="PB 历史数据获取失败",
        )

    result = calc_pb_percentile(df)
    return PBTempResult(
        name=name,
        current_pb=result["current_pb"],
        pb_percentile=result["pb_percentile"],
        min_pb=result["min_pb"],
        max_pb=result["max_pb"],
        median_pb=result["median_pb"],
        data_points=result["data_points"],
    )


async def fetch_buffett_temp() -> BuffettResult:
    """计算巴菲特指数温度。

    Returns:
        BuffettResult。
    """
    df = await asyncio.to_thread(_fetch_buffett_index_lg)

    if df is None:
        return BuffettResult(
            current_ratio=None,
            ratio_percentile=None,
            min_ratio=None,
            max_ratio=None,
            median_ratio=None,
            error="巴菲特指数历史数据获取失败",
        )

    result = calc_buffett_percentile(df)
    return BuffettResult(
        current_ratio=result["current_ratio"],
        ratio_percentile=result["ratio_percentile"],
        min_ratio=result["min_ratio"],
        max_ratio=result["max_ratio"],
        median_ratio=result["median_ratio"],
        data_points=result["data_points"],
    )


def calc_pe_percentile(df: pd.DataFrame) -> dict[str, Any]:
    """计算 PE 历史百分位。

    Args:
        df: 含 pe 列的 DataFrame。

    Returns:
        dict 含 current_pe, pe_percentile, min_pe, max_pe, median_pe, data_points。
    """
    if df is None or df.empty:
        return {
            "current_pe": None,
            "pe_percentile": None,
            "min_pe": None,
            "max_pe": None,
            "median_pe": None,
            "data_points": 0,
        }

    pe_series = df["pe"].dropna()
    if pe_series.empty:
        return {
            "current_pe": None,
            "pe_percentile": None,
            "min_pe": None,
            "max_pe": None,
            "median_pe": None,
            "data_points": 0,
        }

    current_pe = float(pe_series.iloc[-1])
    min_pe = float(pe_series.min())
    max_pe = float(pe_series.max())
    median_pe = float(pe_series.median())

    # 百分位：当前 PE 在历史序列中的位置
    # 使用 (count_below + 0.5 * count_equal) / count_total 避免极端 0/100
    count_below = int((pe_series < current_pe).sum())
    count_equal = int((pe_series == current_pe).sum())
    count_total = len(pe_series)
    pe_percentile = round((count_below + 0.5 * count_equal) / count_total * 100, 1)

    return {
        "current_pe": round(current_pe, 2),
        "pe_percentile": pe_percentile,
        "min_pe": round(min_pe, 2),
        "max_pe": round(max_pe, 2),
        "median_pe": round(median_pe, 2),
        "data_points": count_total,
    }


async def fetch_index_temp(index: dict) -> IndexTempResult:
    """计算单个指数的温度。

    数据源优先级：乐咕乐股 → 东方财富 push2。

    Args:
        index: 指数配置 dict，含 name, lg_symbol, code（可选）。

    Returns:
        IndexTempResult。
    """
    name = index["name"]
    lg_symbol = index.get("lg_symbol", name)
    code = index.get("code", "")

    # 主数据源：乐咕乐股
    df = await asyncio.to_thread(_fetch_pe_history_lg, lg_symbol)

    # 备用数据源：东方财富 push2
    if df is None and code:
        df = await asyncio.to_thread(_fetch_pe_history_eastmoney, code)

    if df is None:
        return IndexTempResult(
            name=name,
            current_pe=None,
            pe_percentile=None,
            min_pe=None,
            max_pe=None,
            median_pe=None,
            error="PE 历史数据获取失败",
        )

    result = calc_pe_percentile(df)
    return IndexTempResult(
        name=name,
        current_pe=result["current_pe"],
        pe_percentile=result["pe_percentile"],
        min_pe=result["min_pe"],
        max_pe=result["max_pe"],
        median_pe=result["median_pe"],
        data_points=result["data_points"],
    )


async def calc_market_temp(use_cache: bool = True) -> MarketTempResult:
    """计算全市场温度（PE + PB + 巴菲特指数加权）。

    并行采集：
    - 12 个宽基指数 PE 百分位 → 算术平均（权重 0.7）
    - 2 个宽基指数 PB 百分位 → 算术平均（权重 0.2）
    - 巴菲特指数百分位（权重 0.1）

    当 PB 或巴菲特指数获取失败时，权重自动归零，PE 权重相应提升。
    所有数据源均失败时返回温度 None。

    Args:
        use_cache: 是否使用缓存（默认 True，30 分钟内有效）。

    Returns:
        MarketTempResult。
    """
    global _cache, _cache_time

    if use_cache:
        with _cache_lock:
            if _cache and time.time() - _cache_time < _CACHE_TTL_MINUTES * 60:
                logger.debug("市场温度计使用缓存")
                return _cache

    result = MarketTempResult()

    # 并行采集：PE 指数 + PB 指数 + 巴菲特指数
    pe_tasks = [fetch_index_temp(idx) for idx in WIDE_INDEXES]
    pb_tasks = [fetch_pb_temp(idx) for idx in PB_INDEXES]
    buffett_task = fetch_buffett_temp()

    all_results = await asyncio.gather(*pe_tasks, *pb_tasks, buffett_task, return_exceptions=True)

    # 解析 PE 结果
    pe_results = all_results[: len(pe_tasks)]
    result.indexes = [r for r in pe_results if isinstance(r, IndexTempResult)]
    result.total_indexes = len(WIDE_INDEXES)
    result.success_count = sum(1 for r in result.indexes if r.pe_percentile is not None)

    # 解析 PB 结果
    pb_results = all_results[len(pe_tasks) : len(pe_tasks) + len(pb_tasks)]
    result.pb_indexes = [r for r in pb_results if isinstance(r, PBTempResult)]
    result.pb_success_count = sum(1 for r in result.pb_indexes if r.pb_percentile is not None)

    # 解析巴菲特指数结果
    buffett_result = all_results[-1]
    if isinstance(buffett_result, BuffettResult):
        result.buffett = buffett_result
        result.buffett_success = buffett_result.ratio_percentile is not None

    # ── 计算各维度温度 ──
    pe_valid = [r.pe_percentile for r in result.indexes if r.pe_percentile is not None]
    if pe_valid:
        result.pe_temperature = round(sum(pe_valid) / len(pe_valid), 1)

    pb_valid = [r.pb_percentile for r in result.pb_indexes if r.pb_percentile is not None]
    if pb_valid:
        result.pb_temperature = round(sum(pb_valid) / len(pb_valid), 1)

    if result.buffett and result.buffett.ratio_percentile is not None:
        result.buffett_temperature = result.buffett.ratio_percentile

    # ── 综合温度加权计算 ──
    # 动态权重：缺失维度权重归零，其他维度等比放大
    weight_pe = _WEIGHT_PE if result.pe_temperature is not None else 0.0
    weight_pb = _WEIGHT_PB if result.pb_temperature is not None else 0.0
    weight_buffett = _WEIGHT_BUFFETT if result.buffett_temperature is not None else 0.0
    total_weight = weight_pe + weight_pb + weight_buffett

    if total_weight > 0:
        # 各维度温度（None 时视为 0，但因 weight=0 不会影响结果）
        pe_temp = result.pe_temperature or 0.0
        pb_temp = result.pb_temperature or 0.0
        buffett_temp = result.buffett_temperature or 0.0
        result.temperature = round(
            (pe_temp * weight_pe + pb_temp * weight_pb + buffett_temp * weight_buffett)
            / total_weight,
            1,
        )
        result.data_source = (
            f"PE(×{weight_pe / total_weight:.1f})+PB(×{weight_pb / total_weight:.1f})"
            + (f"+Buffett(×{weight_buffett / total_weight:.1f})" if weight_buffett > 0 else "")
        )
    else:
        result.temperature = None
        result.error = "所有数据源均获取失败"

    # 最热/最冷指数（基于 PE）
    valid_idx = [r for r in result.indexes if r.pe_percentile is not None]
    if valid_idx:
        result.hottest = max(valid_idx, key=lambda r: r.pe_percentile)
        result.coldest = min(valid_idx, key=lambda r: r.pe_percentile)

    # 写缓存
    with _cache_lock:
        _cache = result
        _cache_time = time.time()

    logger.info(
        f"市场温度: {result.temperature}° ({result.zone}), "
        f"PE={result.pe_temperature} PB={result.pb_temperature} "
        f"Buffett={result.buffett_temperature}, "
        f"有效 PE {result.success_count}/{result.total_indexes} "
        f"PB {result.pb_success_count}/{len(PB_INDEXES)} "
        f"Buffett {'✓' if result.buffett_success else '✗'}"
    )
    return result


def _temp_to_template(temp: float | None) -> str:
    """温度映射到飞书卡片 header 颜色。"""
    if temp is None:
        return "grey"
    if temp < 20:
        return "green"
    if temp < 30:
        return "green"
    if temp < 50:
        return "yellow"
    if temp < 70:
        return "orange"
    return "red"


def build_market_temp_card(result: MarketTempResult) -> dict[str, Any]:
    """构建市场温度计飞书卡片。

    Args:
        result: MarketTempResult。

    Returns:
        飞书 interactive card dict。
    """
    from gugu.notifier.formatter import _card

    if result.temperature is None:
        return _card(
            title="📊 市场温度计（数据暂不可用）",
            template="grey",
            sections=["当前数据源不可用，系统会在下一执行周期自动重试。"],
            note=result.timestamp,
        )

    # 第一段：温度总览
    overview = (
        f"**当前温度: {result.temperature}°**\n"
        f"区间: {_format_zone(result.zone)}\n"
        f"操作建议: {result.action_advice}\n"
        f"_温度=PE(70%)+PB(20%)+巴菲特指数(10%)加权，10年滚动窗口百分位_"
    )

    # 第二段：各维度温度
    dim_lines: list[str] = []
    if result.pe_temperature is not None:
        dim_lines.append(
            f"📊 PE 百分位: {result.pe_temperature}° ({result.success_count}/{result.total_indexes} 指数)"
        )
    if result.pb_temperature is not None:
        dim_lines.append(
            f"💰 PB 百分位: {result.pb_temperature}° ({result.pb_success_count}/{len(PB_INDEXES)} 指数)"
        )
    if result.buffett_temperature is not None and result.buffett:
        dim_lines.append(
            f"🏦 巴菲特指数: {result.buffett_temperature}° "
            f"(市值/GDP={result.buffett.current_ratio})"
        )
    dimension = "\n".join(dim_lines) if dim_lines else "无维度数据"

    # 第三段：各指数 PE 百分位（按温度从高到低）
    sorted_idx = sorted(
        result.indexes,
        key=lambda r: r.pe_percentile if r.pe_percentile is not None else -1,
        reverse=True,
    )
    idx_lines: list[str] = []
    for idx in sorted_idx:
        if idx.pe_percentile is not None:
            emoji = (
                "🔴"
                if idx.pe_percentile >= 70
                else "🟠"
                if idx.pe_percentile >= 50
                else "🟡"
                if idx.pe_percentile >= 30
                else "🟢"
            )
            idx_lines.append(f"{emoji} {idx.name} PE{idx.current_pe} 历史{idx.pe_percentile}%分位")
        else:
            idx_lines.append(f"⚪ {idx.name} 数据暂缺")
    detail = "\n".join(idx_lines)

    # 第四段：最热/最冷
    hotspot = ""
    if result.hottest and result.coldest:
        hotspot = (
            f"🔥 最热: {result.hottest.name}({result.hottest.pe_percentile}°)\n"
            f"❄️ 最冷: {result.coldest.name}({result.coldest.pe_percentile}°)"
        )

    note = f"⚠️ 估值参考，非交易指令 | {result.data_source} | 10年滚动窗口 | {result.timestamp}"

    sections = [overview, dimension, detail]
    if hotspot:
        sections.append(hotspot)

    return _card(
        title=f"📊 市场温度计 {result.temperature}° {result.zone}",
        template=_temp_to_template(result.temperature),
        sections=sections,
        note=note,
    )


def _format_zone(zone: str) -> str:
    """格式化温度区间为 emoji + 文本。"""
    if "加倍" in zone:
        return "🟢 " + zone
    if "正常买入" in zone:
        return "🟢 " + zone
    if "定投" in zone:
        return "🟡 " + zone
    if "高估" in zone:
        return "🔴 " + zone
    if "减仓" in zone:
        return "🟠 " + zone
    return zone


def format_temp_report(result: MarketTempResult) -> str:
    """格式化为纯文本报告（用于日志或简单展示）。"""
    lines: list[str] = []

    if result.temperature is None:
        lines.append("📊 **市场温度计**\n\n数据暂不可用（数据源失败）")
        return "\n".join(lines)

    temp_emoji = (
        "🟢"
        if result.temperature < 30
        else "🟡"
        if result.temperature < 50
        else "🟠"
        if result.temperature < 70
        else "🔴"
    )
    lines.append(f"📊 **市场温度计** {temp_emoji} **{result.temperature}°**")
    lines.append(f"区间: {_format_zone(result.zone)}")
    lines.append(f"建议: {result.action_advice}")
    lines.append("")

    # 各维度温度
    lines.append("**各维度温度**")
    if result.pe_temperature is not None:
        lines.append(f"  PE 百分位: {result.pe_temperature}°")
    if result.pb_temperature is not None:
        lines.append(f"  PB 百分位: {result.pb_temperature}°")
    if result.buffett_temperature is not None and result.buffett:
        lines.append(
            f"  巴菲特指数: {result.buffett_temperature}° (市值/GDP={result.buffett.current_ratio})"
        )
    lines.append("")

    # 各指数温度明细
    lines.append("**宽基指数 PE 百分位**")
    sorted_idx = sorted(
        result.indexes,
        key=lambda r: r.pe_percentile if r.pe_percentile is not None else -1,
        reverse=True,
    )
    for idx in sorted_idx:
        if idx.pe_percentile is not None:
            emoji = (
                "🔴"
                if idx.pe_percentile >= 70
                else "🟠"
                if idx.pe_percentile >= 50
                else "🟡"
                if idx.pe_percentile >= 30
                else "🟢"
            )
            lines.append(f"  {emoji} {idx.name} PE{idx.current_pe} 历史{idx.pe_percentile}%分位")
        else:
            lines.append(f"  ⚪ {idx.name} 数据暂缺")
    lines.append("")

    # 最热/最冷
    if result.hottest and result.coldest:
        lines.append(
            f"🔥 最热: {result.hottest.name}({result.hottest.pe_percentile}°) | "
            f"❄️ 最冷: {result.coldest.name}({result.coldest.pe_percentile}°)"
        )

    lines.append("")
    lines.append(f"_⚠️ 估值参考，非交易指令 | {result.data_source} | 10年滚动窗口_")
    return "\n".join(lines)
