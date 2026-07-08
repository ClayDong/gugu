"""市场温度计：基于 PE 历史百分位计算全市场估值温度。

核心逻辑（参考博格市场温度计方法）：
- 采集多个宽基指数的 PE（TTM）历史序列
- 计算每个指数近 10 年 PE 百分位
- 算术平均得到全市场温度（0°-100°）

温度规则：
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

数据源：乐咕乐股 stock_index_pe_lg（akshare 封装）
- 覆盖 12 个宽基指数（大/中/小盘 + 红利 + 创业板）
- 历史 10 年+ 日频 PE 数据
- 三级冗余：乐咕乐股 → 东方财富 push2 → 当日快照
"""

from __future__ import annotations

import asyncio
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
    {"name": "上证50",   "lg_symbol": "上证50",   "weight": 1.0},
    {"name": "沪深300",  "lg_symbol": "沪深300",  "weight": 1.0},
    {"name": "上证380",  "lg_symbol": "上证380",  "weight": 1.0},
    {"name": "创业板50", "lg_symbol": "创业板50", "weight": 1.0},
    {"name": "中证500",  "lg_symbol": "中证500",  "weight": 1.0},
    {"name": "上证180",  "lg_symbol": "上证180",  "weight": 1.0},
    {"name": "深证红利", "lg_symbol": "深证红利", "weight": 1.0},
    {"name": "深证100", "lg_symbol": "深证100", "weight": 1.0},
    {"name": "中证1000", "lg_symbol": "中证1000", "weight": 1.0},
    {"name": "上证红利", "lg_symbol": "上证红利", "weight": 1.0},
    {"name": "中证100",  "lg_symbol": "中证100",  "weight": 1.0},
    {"name": "中证800",  "lg_symbol": "中证800",  "weight": 1.0},
]

# 温度阀值
_TEMP_BUY_DOUBLE = 20    # < 20° 加倍买入
_TEMP_BUY_NORMAL = 30    # < 30° 正常买入
_TEMP_HOLD = 50          # < 50° 正常定投
_TEMP_SELL_HEAVY = 70    # ≥ 70° 大幅减仓

# 历史回看年数
_LOOKBACK_YEARS = 10

# 缓存 TTL（分钟）
_CACHE_TTL_MINUTES = 30

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
class MarketTempResult:
    """全市场温度计算结果。"""
    timestamp: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M")
    )
    temperature: float | None = None  # 0.0-100.0
    indexes: list[IndexTempResult] = field(default_factory=list)
    hottest: IndexTempResult | None = None
    coldest: IndexTempResult | None = None
    total_indexes: int = 0
    success_count: int = 0
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
            return "加倍买入低估值指数/行业"
        if self.temperature < _TEMP_BUY_NORMAL:
            return "正常买入，重点配置低估行业"
        if self.temperature < _TEMP_HOLD:
            return "正常定投，注意行业冷热不均"
        if self.temperature < _TEMP_SELL_HEAVY:
            return "停止加仓，分批减仓盈利仓位"
        return "大幅减仓，保留现金等待低估机会"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "temperature": self.temperature,
            "zone": self.zone,
            "action_advice": self.action_advice,
            "total_indexes": self.total_indexes,
            "success_count": self.success_count,
            "data_source": self.data_source,
            "error": self.error,
            "hottest": {
                "name": self.hottest.name,
                "pe_percentile": self.hottest.pe_percentile,
                "current_pe": self.hottest.current_pe,
            } if self.hottest else None,
            "coldest": {
                "name": self.coldest.name,
                "pe_percentile": self.coldest.pe_percentile,
                "current_pe": self.coldest.current_pe,
            } if self.coldest else None,
        }


# ── 全局缓存 ──────────────────────────────────────────────────
_cache: dict[str, Any] = {}
_cache_time: float = 0.0


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
        df = pd.DataFrame({
            "date": pd.to_datetime(raw["日期"], errors="coerce"),
            "pe": pd.to_numeric(raw.get("滚动市盈率"), errors="coerce"),
        })
        df["pe"] = df["pe"].fillna(pd.to_numeric(raw.get("静态市盈率"), errors="coerce"))
        df = df.dropna(subset=["date", "pe"])
        df = df[df["pe"] > 0]  # 过滤负 PE（亏损）
        df = df.sort_values("date").drop_duplicates(subset="date").reset_index(drop=True)

        # 仅保留近 years 年
        cutoff = pd.Timestamp.now() - pd.DateOffset(years=years)
        df = df[df["date"] >= cutoff].reset_index(drop=True)
        return df if not df.empty else None
    except Exception as e:
        logger.debug(f"乐咕乐股 {symbol} PE 数据获取失败: {e}")
        return None


def _fetch_pe_history_eastmoney(
    code: str, years: int = _LOOKBACK_YEARS
) -> pd.DataFrame | None:
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


def calc_pe_percentile(df: pd.DataFrame) -> dict[str, Any]:
    """计算 PE 历史百分位。

    Args:
        df: 含 pe 列的 DataFrame。

    Returns:
        dict 含 current_pe, pe_percentile, min_pe, max_pe, median_pe, data_points。
    """
    if df is None or df.empty:
        return {
            "current_pe": None, "pe_percentile": None,
            "min_pe": None, "max_pe": None, "median_pe": None, "data_points": 0,
        }

    pe_series = df["pe"].dropna()
    if pe_series.empty:
        return {
            "current_pe": None, "pe_percentile": None,
            "min_pe": None, "max_pe": None, "median_pe": None, "data_points": 0,
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
            name=name, current_pe=None, pe_percentile=None,
            min_pe=None, max_pe=None, median_pe=None,
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
    """计算全市场温度。

    并行采集所有宽基指数的 PE 历史百分位，算术平均汇总。

    Args:
        use_cache: 是否使用缓存（默认 True，30 分钟内有效）。

    Returns:
        MarketTempResult。
    """
    global _cache, _cache_time

    if use_cache and _cache and time.time() - _cache_time < _CACHE_TTL_MINUTES * 60:
        logger.debug("市场温度计使用缓存")
        return _cache

    result = MarketTempResult()

    # 并行采集所有宽基指数
    tasks = [fetch_index_temp(idx) for idx in WIDE_INDEXES]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    result.indexes = [
        r for r in results if isinstance(r, IndexTempResult)
    ]
    result.total_indexes = len(WIDE_INDEXES)
    result.success_count = sum(1 for r in result.indexes if r.pe_percentile is not None)

    # 算术平均计算温度（等权）
    valid_percentiles = [
        r.pe_percentile for r in result.indexes if r.pe_percentile is not None
    ]
    if valid_percentiles:
        result.temperature = round(sum(valid_percentiles) / len(valid_percentiles), 1)
    else:
        result.temperature = None
        result.error = "所有指数 PE 数据均获取失败"

    # 最热/最冷指数
    valid = [r for r in result.indexes if r.pe_percentile is not None]
    if valid:
        result.hottest = max(valid, key=lambda r: r.pe_percentile)
        result.coldest = min(valid, key=lambda r: r.pe_percentile)

    # 写缓存
    _cache = result
    _cache_time = time.time()

    logger.info(
        f"市场温度: {result.temperature}° ({result.zone}), "
        f"有效指数 {result.success_count}/{result.total_indexes}"
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
        f"_温度=12个宽基指数PE在近10年历史中的百分位平均，越低越便宜_"
    )

    # 第二段：各指数 PE 百分位（按温度从高到低）
    sorted_idx = sorted(
        result.indexes,
        key=lambda r: r.pe_percentile if r.pe_percentile is not None else -1,
        reverse=True,
    )
    idx_lines: list[str] = []
    for idx in sorted_idx:
        if idx.pe_percentile is not None:
            emoji = (
                "🔴" if idx.pe_percentile >= 70
                else "🟠" if idx.pe_percentile >= 50
                else "🟡" if idx.pe_percentile >= 30
                else "🟢"
            )
            idx_lines.append(
                f"{emoji} {idx.name} PE{idx.current_pe} "
                f"历史{idx.pe_percentile}%分位"
            )
        else:
            idx_lines.append(f"⚪ {idx.name} 数据暂缺")
    detail = "\n".join(idx_lines)

    # 第三段：最热/最冷
    hotspot = ""
    if result.hottest and result.coldest:
        hotspot = (
            f"🔥 最热: {result.hottest.name}({result.hottest.pe_percentile}°)\n"
            f"❄️ 最冷: {result.coldest.name}({result.coldest.pe_percentile}°)"
        )

    note = (
        f"数据源: {result.data_source} | "
        f"有效 {result.success_count}/{result.total_indexes} | "
        f"10年滚动窗口 | {result.timestamp}"
    )

    sections = [overview, detail]
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
        "🟢" if result.temperature < 30
        else "🟡" if result.temperature < 50
        else "🟠" if result.temperature < 70
        else "🔴"
    )
    lines.append(f"📊 **市场温度计** {temp_emoji} **{result.temperature}°**")
    lines.append(f"区间: {_format_zone(result.zone)}")
    lines.append(f"建议: {result.action_advice}")
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
                "🔴" if idx.pe_percentile >= 70
                else "🟠" if idx.pe_percentile >= 50
                else "🟡" if idx.pe_percentile >= 30
                else "🟢"
            )
            lines.append(
                f"  {emoji} {idx.name} PE{idx.current_pe} "
                f"历史{idx.pe_percentile}%分位"
            )
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
    lines.append(
        f"_数据源: {result.data_source} | "
        f"有效 {result.success_count}/{result.total_indexes} | "
        f"10年滚动窗口_"
    )
    return "\n".join(lines)
