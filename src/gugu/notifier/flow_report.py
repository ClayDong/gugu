"""资金流日报 — 大盘/行业/个股资金流向飞书推送。

移植自 realtime-flow 项目，在 gugu 中提供两个时段推送：
- 08:00 盘前 → 昨日(T-1)资金流复盘
- 15:10 收盘 → 今日资金流日报

数据源（双通道冗余）：
1. akshare 库（东方财富后端，约 1-3 分钟延迟）
2. 东方财富 push API 直连 HTTP（akshare 失败时自动降级）
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import httpx
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# 东方财富 push API 直连（akshare 备用通道）
_EASTMONEY_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_EASTMONEY_FFLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
_EASTMONEY_HSGT_URL = "https://push2.eastmoney.com/api/qt/kamt.rtmin/get"

_EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}


def _http_get_json(url: str, params: dict[str, str]) -> dict | None:
    """直连东方财富 push API，返回 JSON dict 或 None。"""
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params, headers=_EASTMONEY_HEADERS)
            resp.raise_for_status()
            data = resp.json()
            if data and data.get("data"):
                return data
    except Exception as e:
        logger.warning(f"东方财富 HTTP API 失败: {e}")
    return None

# ── 工具函数 ──────────────────────────────────────────────────


def _safe_float(v: Any, default: float = 0.0) -> float:
    """安全转 float，过滤 NaN / Inf。"""
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _normalize(v: float, min_v: float, max_v: float) -> float:
    """线性归一化到 [0, 1]。"""
    if max_v <= min_v:
        return 0.5
    return max(0.0, min(1.0, (v - min_v) / (max_v - min_v)))


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _yuan_to_yi(val: float | None) -> float:
    """元 → 亿转换（东方财富资金流 API 返回单位是元）。"""
    if val is None:
        return 0.0
    return round(val / 100_000_000, 2)


# ── 数据采集 ──────────────────────────────────────────────────


def _collect_market_flow_akshare() -> dict[str, Any] | None:
    """通过 akshare 采集大盘资金流 + 北向资金。"""
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare 未安装，跳过 akshare 大盘资金流采集")
        return None

    result: dict[str, Any] = {}

    # 大盘资金流历史（最新一日）
    try:
        df = ak.stock_market_fund_flow()
        if df is not None and not df.empty:
            latest = df.iloc[-1].to_dict()
            result["market"] = {
                "date": str(latest.get("日期", "")),
                "sh_close": _safe_float(latest.get("上证-收盘价")),
                "sh_change": _safe_float(latest.get("上证-涨跌幅")),
                "sz_close": _safe_float(latest.get("深证-收盘价")),
                "sz_change": _safe_float(latest.get("深证-涨跌幅")),
                "main_net_inflow": _yuan_to_yi(_safe_float(latest.get("主力净流入-净额"))),
                "main_net_ratio": _safe_float(latest.get("主力净流入-净占比")),
                "super_large_inflow": _yuan_to_yi(_safe_float(latest.get("超大单净流入-净额"))),
                "large_inflow": _yuan_to_yi(_safe_float(latest.get("大单净流入-净额"))),
                "medium_inflow": _yuan_to_yi(_safe_float(latest.get("中单净流入-净额"))),
                "small_inflow": _yuan_to_yi(_safe_float(latest.get("小单净流入-净额"))),
            }
            logger.info(
                f"大盘资金流(akshare): 日期={result['market']['date']}, "
                f"主力净流入={result['market']['main_net_inflow']:.2f}亿"
            )
    except Exception as e:
        logger.warning(f"akshare 大盘资金流采集失败: {e}")

    # 北向资金
    try:
        df_n = ak.stock_hsgt_fund_min_em(symbol="北向资金")
        if df_n is not None and not df_n.empty:
            latest_n = df_n.iloc[-1].to_dict()
            result["north_bound"] = {
                "date": str(latest_n.get("日期", "")),
                "time": str(latest_n.get("时间", "")),
                "total": _safe_float(latest_n.get("北向资金", 0)),
                "sh_connect": _safe_float(latest_n.get("港股通(沪)", 0)),
                "sz_connect": _safe_float(latest_n.get("港股通(深)", 0)),
            }
            logger.info(f"北向资金(akshare): {result['north_bound']['total']:.2f}亿")
    except Exception as e:
        logger.warning(f"akshare 北向资金采集失败: {e}")

    return result if result else None


def _collect_market_flow_http() -> dict[str, Any] | None:
    """通过东方财富 push API 直连采集大盘资金流 + 北向资金（akshare 备用通道）。"""
    result: dict[str, Any] = {}

    # 大盘资金流（沪深京合并）
    try:
        data = _http_get_json(_EASTMONEY_FFLOW_URL, {
            "secid": "1.000001",
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "klt": "101",
            "lmt": "1",
        })
        if data and data.get("data") and data["data"].get("klines"):
            kline = data["data"]["klines"][-1].split(",")
            # 字段顺序: 日期,主力净流入,小单,中单,大单,超大单,...
            if len(kline) >= 6:
                result["market"] = {
                    "date": kline[0],
                    "sh_close": None, "sh_change": None,
                    "sz_close": None, "sz_change": None,
                    "main_net_inflow": _yuan_to_yi(_safe_float(kline[1])),
                    "main_net_ratio": None,
                    "small_inflow": _yuan_to_yi(_safe_float(kline[2])),
                    "medium_inflow": _yuan_to_yi(_safe_float(kline[3])),
                    "large_inflow": _yuan_to_yi(_safe_float(kline[4])),
                    "super_large_inflow": _yuan_to_yi(_safe_float(kline[5])),
                }
                logger.info(
                    f"大盘资金流(HTTP): 日期={result['market']['date']}, "
                    f"主力净流入={result['market']['main_net_inflow']:.2f}亿"
                )
    except Exception as e:
        logger.warning(f"HTTP 大盘资金流采集失败: {e}")

    # 北向资金
    try:
        data = _http_get_json(_EASTMONEY_HSGT_URL, {"fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54,f55,f56"})
        if data and data.get("data") and data["data"].get("s2n"):
            # s2n 是无分割的字符串，尝试解析
            s2n = data["data"].get("s2n", "")
            if s2n:
                # 取最后一段数据
                parts = s2n.split(";")
                if parts:
                    latest_parts = parts[-1].split(",")
                    if len(latest_parts) >= 4:
                        result["north_bound"] = {
                            "date": latest_parts[0] if len(latest_parts) > 0 else "",
                            "time": latest_parts[1] if len(latest_parts) > 1 else "",
                            "total": _safe_float(latest_parts[2]) / 10000 if len(latest_parts) > 2 else 0,
                            "sh_connect": 0,
                            "sz_connect": 0,
                        }
                        logger.info(f"北向资金(HTTP): {result['north_bound']['total']:.2f}亿")
    except Exception as e:
        logger.warning(f"HTTP 北向资金采集失败: {e}")

    return result if result else None


def collect_market_flow() -> dict[str, Any] | None:
    """采集大盘资金流 + 北向资金（akshare → HTTP 双通道冗余）。"""
    # 通道1: akshare
    result = _collect_market_flow_akshare()

    # 通道2: HTTP 直连（akshare 失败时）
    if not result or ("market" not in result and "north_bound" not in result):
        logger.info("akshare 大盘资金流不可用，降级到 HTTP 直连...")
        http_result = _collect_market_flow_http()
        if http_result:
            # 合并：HTTP 结果补充 akshare 缺失的字段
            if not result:
                result = http_result
            else:
                if "market" not in result and "market" in http_result:
                    result["market"] = http_result["market"]
                if "north_bound" not in result and "north_bound" in http_result:
                    result["north_bound"] = http_result["north_bound"]

    if not result:
        return None

    # 大盘资金流不可用时，用北向资金作为市场情绪代理
    if "market" not in result and "north_bound" in result:
        nb = result["north_bound"]
        result["market"] = {
            "date": nb.get("date", _date_str()),
            "sh_close": None, "sh_change": None,
            "sz_close": None, "sz_change": None,
            "main_net_inflow": nb.get("total", 0),
            "main_net_ratio": None,
            "super_large_inflow": None, "large_inflow": None,
            "medium_inflow": None,
            "small_inflow": -_safe_float(nb.get("total", 0)),
            "north_only": True,
        }
        logger.info("大盘资金流不可用，使用北向资金作为市场情绪代理")

    return result if result else None


def _collect_sector_flow_akshare(sector_type: str = "行业资金流") -> list[dict[str, Any]] | None:
    """通过 akshare 采集行业/概念资金流排名。"""
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare 未安装，跳过 akshare 资金流排名采集")
        return None
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type=sector_type)
        if df is not None and not df.empty:
            records: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                rec: dict[str, Any] = {}
                for col in df.columns:
                    val = row[col]
                    if isinstance(val, (pd.Timestamp, pd.Period)):
                        val = str(val)
                    rec[str(col)] = val
                records.append(rec)
            logger.info(f"akshare {sector_type}: {len(records)} 条")
            return records
    except Exception as e:
        logger.warning(f"akshare {sector_type} 采集失败: {e}")
    return None


def _collect_clist_http(fs: str, label: str) -> list[dict[str, Any]] | None:
    """通过东方财富 clist API 直连采集行业/概念/个股资金流排名。"""
    params = {
        "fid": "f62",
        "po": "1",
        "pz": "100",
        "pn": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": fs,
        "fields": "f12,f14,f2,f3,f62,f184,f66,f72,f78,f84,f164,f174",
    }
    data = _http_get_json(_EASTMONEY_CLIST_URL, params)
    if not data or not data.get("data") or not data["data"].get("diff"):
        return None
    diff = data["data"]["diff"]
    records = []
    for item in diff:
        records.append({
            "代码": item.get("f12", ""),
            "名称": item.get("f14", ""),
            "最新价": item.get("f2", 0),
            "涨跌幅": item.get("f3", 0),
            "主力净流入-净额": item.get("f62", 0),
            "主力净流入-净占比": item.get("f184", 0),
            "超大单净流入-净额": item.get("f66", 0),
            "大单净流入-净额": item.get("f72", 0),
            "中单净流入-净额": item.get("f78", 0),
            "小单净流入-净额": item.get("f84", 0),
        })
    logger.info(f"HTTP {label}: {len(records)} 条")
    return records if records else None


def collect_sector_flow() -> list[dict[str, Any]] | None:
    """采集行业资金流排名（今日）—— akshare → HTTP 双通道。"""
    # 通道1: akshare
    records = _collect_sector_flow_akshare("行业资金流")
    if records:
        return records
    # 通道2: HTTP 直连
    logger.info("akshare 行业资金流不可用，降级到 HTTP 直连...")
    return _collect_clist_http("m:90+t:2", "行业资金流")


def collect_concept_flow() -> list[dict[str, Any]] | None:
    """采集概念资金流排名（今日）—— akshare → HTTP 双通道。"""
    # 通道1: akshare
    records = _collect_sector_flow_akshare("概念资金流")
    if records:
        return records
    # 通道2: HTTP 直连
    logger.info("akshare 概念资金流不可用，降级到 HTTP 直连...")
    return _collect_clist_http("m:90+t:3", "概念资金流")


def collect_stock_flow() -> list[dict[str, Any]] | None:
    """采集个股资金流排行（今日）—— akshare → HTTP 双通道。"""
    # 通道1: akshare
    try:
        import akshare as ak
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
        if df is not None and not df.empty:
            records: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                rec: dict[str, Any] = {}
                for col in df.columns:
                    val = row[col]
                    if isinstance(val, (pd.Timestamp, pd.Period, pd.Timedelta)):
                        val = str(val)
                    rec[str(col)] = val
                records.append(rec)
            logger.info(f"akshare 个股资金流排行: {len(records)} 只")
            return records
    except Exception as e:
        logger.warning(f"akshare 个股资金流排行采集失败: {e}")

    # 通道2: HTTP 直连
    logger.info("akshare 个股资金流不可用，降级到 HTTP 直连...")
    return _collect_clist_http("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048", "个股资金流")


# ── 分析引擎 ──────────────────────────────────────────────────


def analyze_sectors(sector_data: list[dict[str, Any]], top_n: int = 5) -> dict[str, Any]:
    """行业轮动分析：流入TOP、流出TOP、轮动强度、背离预警。"""
    result: dict[str, Any] = {
        "inflow_top": [],
        "outflow_top": [],
        "divergence": [],
        "rotation_score": 0,
    }
    if not sector_data:
        return result

    # 按主力净流入排序
    sorted_data = sorted(
        sector_data,
        key=lambda x: _safe_float(x.get("主力净流入-净额", x.get("f62", 0))),
        reverse=True,
    )

    # 流入 TOP
    result["inflow_top"] = [
        {
            "name": str(x.get("行业名称", x.get("板块名称", ""))),
            "main_inflow": _safe_float(x.get("主力净流入-净额", x.get("f62", 0))),
            "main_ratio": _safe_float(x.get("主力净流入-净占比", x.get("f184", 0))),
            "price_change": _safe_float(x.get("涨跌幅", x.get("f3", 0))),
        }
        for i, x in enumerate(sorted_data[:top_n])
        if _safe_float(x.get("主力净流入-净额", x.get("f62", 0))) > 0
    ][:top_n]

    # 流出 TOP（反向排序）
    sorted_desc = sorted(
        sector_data,
        key=lambda x: _safe_float(x.get("主力净流入-净额", x.get("f62", 0))),
    )
    result["outflow_top"] = [
        {
            "name": str(x.get("行业名称", x.get("板块名称", ""))),
            "main_inflow": _safe_float(x.get("主力净流入-净额", x.get("f62", 0))),
            "main_ratio": _safe_float(x.get("主力净流入-净占比", x.get("f184", 0))),
            "price_change": _safe_float(x.get("涨跌幅", x.get("f3", 0))),
        }
        for i, x in enumerate(sorted_desc[:top_n])
        if _safe_float(x.get("主力净流入-净额", x.get("f62", 0))) < 0
    ][:top_n]

    # 轮动强度
    top5_inflow = sum(
        _safe_float(x.get("主力净流入-净额", x.get("f62", 0))) for x in sorted_data[:5]
    )
    bottom5_outflow = abs(
        sum(_safe_float(x.get("主力净流入-净额", x.get("f62", 0))) for x in sorted_desc[:5])
    )
    if bottom5_outflow > 0.01:
        result["rotation_score"] = round(top5_inflow / bottom5_outflow, 2)
    elif top5_inflow > 0:
        result["rotation_score"] = 0.0
        result["rotation_note"] = "市场普涨，无明显轮动"

    # 背离检测
    for x in sector_data:
        main_in = _safe_float(x.get("主力净流入-净额", x.get("f62", 0)))
        pct = _safe_float(x.get("涨跌幅", x.get("f3", 0)))
        name = str(x.get("行业名称", x.get("板块名称", "")))
        if main_in > 1 and pct < -1:
            result["divergence"].append(
                {
                    "name": name,
                    "type": "底背离",
                    "detail": f"主力+{main_in:.1f}亿 但跌{pct:.1f}%",
                    "action": "资金流入但下跌，可能是吸筹",
                }
            )
        elif main_in < -1 and pct > 1:
            result["divergence"].append(
                {
                    "name": name,
                    "type": "顶背离",
                    "detail": f"主力{main_in:.1f}亿 但涨{pct:.1f}%",
                    "action": "资金流出但上涨，警惕出货",
                }
            )

    return result


def analyze_concepts(concept_data: list[dict[str, Any]], top_n: int = 3) -> list[dict[str, Any]]:
    """概念热点分析。"""
    if not concept_data:
        return []
    sorted_c = sorted(
        concept_data,
        key=lambda x: _safe_float(x.get("主力净流入-净额", x.get("f62", 0))),
        reverse=True,
    )
    return [
        {
            "name": str(x.get("概念名称", x.get("板块名称", ""))),
            "main_inflow": _safe_float(x.get("主力净流入-净额", x.get("f62", 0))),
            "main_ratio": _safe_float(x.get("主力净流入-净占比", x.get("f184", 0))),
        }
        for x in sorted_c[:top_n]
    ]


def analyze_strong_stocks(stock_data: list[dict[str, Any]], top_n: int = 5) -> list[dict[str, Any]]:
    """提取强资金流入个股（主力净占比 > 5%）。"""
    if not stock_data:
        return []
    strong = []
    for x in stock_data:
        main_ratio = _safe_float(x.get("主力净流入-净占比", x.get("f184", 0)))
        main_in = _safe_float(x.get("主力净流入-净额", x.get("f62", 0)))
        if main_ratio > 5.0:
            strong.append(
                {
                    "code": str(x.get("代码", x.get("f12", ""))),
                    "name": str(x.get("名称", x.get("f14", ""))),
                    "main_ratio": main_ratio,
                    "main_inflow": main_in,
                    "price_change": _safe_float(x.get("涨跌幅", x.get("f3", 0))),
                }
            )
    strong.sort(key=lambda s: s["main_ratio"], reverse=True)
    return strong[:top_n]


def analyze_market_overview(
    market_data: dict[str, Any] | None,
    north_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """市场全局分析：主力vs散户博弈、北向资金。"""
    result: dict[str, Any] = {
        "main_vs_retail": "",
        "north_analysis": {},
    }
    if market_data:
        main_in = market_data.get("main_net_inflow", 0) or 0
        small_in = market_data.get("small_inflow", 0) or 0
        if main_in > 0 and small_in < 0:
            result["main_vs_retail"] = "主力买入，散户卖出（健康）"
        elif main_in < 0 and small_in > 0:
            result["main_vs_retail"] = "主力卖出，散户接盘（警惕）"
        elif main_in > 0 and small_in > 0:
            result["main_vs_retail"] = "主力散户同步买入"
        else:
            result["main_vs_retail"] = "主力散户同步卖出"
    if north_data:
        total = north_data.get("total", 0) or 0
        result["north_analysis"] = {
            "total": total,
            "direction": "流入" if total > 0 else "流出",
            "significant": "显著" if abs(total) > 30 else "一般",
        }
    return result


# ── 数据持久化 ──────────────────────────────────────────────────


import json as _json
from pathlib import Path as _Path

_FLOW_CACHE_DIR = _Path(__file__).parent.parent.parent / "data" / "flow_cache"
_FLOW_CACHE_FILE = _FLOW_CACHE_DIR / "last_flow_data.json"


def _save_to_cache(data: dict[str, Any]) -> None:
    """持久化最近一次成功采集的资金流数据到本地文件。"""
    try:
        _FLOW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_FLOW_CACHE_FILE, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, default=str, indent=2)
        logger.info(f"资金流数据已缓存: {_FLOW_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"资金流数据缓存写入失败: {e}")


def _load_from_cache() -> dict[str, Any] | None:
    """从本地缓存加载最近一次成功的资金流数据。"""
    if not _FLOW_CACHE_FILE.exists():
        return None
    try:
        with open(_FLOW_CACHE_FILE, encoding="utf-8") as f:
            data = _json.load(f)
        logger.info(f"资金流数据从缓存加载: {_FLOW_CACHE_FILE}")
        return data
    except Exception as e:
        logger.warning(f"资金流数据缓存读取失败: {e}")
        return None


# ── 主流程 ──────────────────────────────────────────────────


async def collect_all() -> dict[str, Any]:
    """采集并分析全部资金流数据。"""
    import asyncio

    loop = asyncio.get_event_loop()

    logger.info("资金流数据采集开始...")

    # 并行采集（通过线程池包装同步双通道调用：akshare → HTTP 直连）
    market_task = loop.run_in_executor(None, collect_market_flow)
    sector_task = loop.run_in_executor(None, collect_sector_flow)
    concept_task = loop.run_in_executor(None, collect_concept_flow)
    stock_task = loop.run_in_executor(None, collect_stock_flow)

    market_data = await market_task
    sector_data = await sector_task
    concept_data = await concept_task
    stock_data = await stock_task

    logger.info("资金流数据采集完成，开始分析...")

    # 分析
    market_info = market_data or {}
    market_overview = analyze_market_overview(
        market_info.get("market"),
        market_info.get("north_bound"),
    )

    sector_analysis = analyze_sectors(sector_data or []) if sector_data else {}
    concept_top = analyze_concepts(concept_data or []) if concept_data else []
    strong_stocks = analyze_strong_stocks(stock_data or []) if stock_data else []

    result: dict[str, Any] = {
        "market": market_info.get("market"),
        "north_bound": market_info.get("north_bound"),
        "market_overview": market_overview,
        "sectors": sector_analysis,
        "concepts": concept_top,
        "strong_stocks": strong_stocks,
        "timestamp": _now_str(),
    }

    logger.info(
        f"资金流分析完成: 行业{len(sector_data or [])}个, "
        f"背离{len(sector_analysis.get('divergence', []))}个, "
        f"强流入{len(strong_stocks)}只"
    )

    # 如果采集到有效数据（至少有一个领域有数据），持久化到本地
    if market_data or sector_data or concept_data or stock_data:
        _save_to_cache(result)
    else:
        # 全部失败时尝试从缓存加载
        cached = _load_from_cache()
        if cached:
            logger.info("实时数据不可用，使用缓存数据")
            cached["timestamp"] = _now_str()  # 更新时间戳但保留原始数据日期
            result = cached

    return result


# ── 卡片格式化 ──────────────────────────────────────────────────


def _fmt(val: Any, suffix: str = "") -> str:
    """格式化数值，None 显示 '--'。"""
    if val is None:
        return "--"
    try:
        f = float(val)
        return f"{f:+.2f}{suffix}" if f != 0 else f"0.00{suffix}"
    except (ValueError, TypeError):
        return str(val)


def _fmt_flow(val: Any) -> str:
    """格式化资金流金额（亿），带颜色箭头。"""
    if val is None:
        return "--"
    try:
        f = float(val)
        if f > 0:
            return f"🟢 +{f:.1f}亿"
        elif f < 0:
            return f"🔴 {f:.1f}亿"
        return "⚪ 0.0亿"
    except (ValueError, TypeError):
        return str(val)


def _rotation_label(score: float) -> str:
    """轮动强度文字标签。"""
    if score >= 5:
        return "🟠 轮动极强"
    elif score >= 3:
        return "🟡 轮动较强"
    elif score >= 1.5:
        return "🔵 轮动正常"
    elif score > 0:
        return "🟢 轮动偏弱"
    return "⚪ 市场普涨"


def build_morning_card(data: dict[str, Any]) -> dict[str, Any]:
    """构建 08:00 盘前 — 昨日(T-1)资金流复盘卡片。"""
    from gugu.notifier.formatter import _card

    market = data.get("market", {})
    north = data.get("north_bound", {})
    overview = data.get("market_overview", {})
    sectors = data.get("sectors", {})
    concepts = data.get("concepts", [])
    strong_stocks = data.get("strong_stocks", [])

    sections: list[str] = []

    # ── 大盘概览 ──
    if market:
        m_date = market.get("date", "")
        north_only = market.get("north_only", False)
        lines = ["**大盘资金流**"]
        if north_only:
            nb = north or {}
            lines.append(f"📅 {m_date}")
            lines.append("📊 **北向资金代理**（东方财富 API 暂不可用）")
            n_total = _fmt_flow(nb.get("total"))
            n_sh = _fmt_flow(nb.get("sh_connect"))
            n_sz = _fmt_flow(nb.get("sz_connect"))
            lines.append(f"  北向总额: {n_total}")
            lines.append(f"  沪股通: {n_sh}")
            lines.append(f"  深股通: {n_sz}")
            if overview.get("main_vs_retail"):
                lines.append(f"  博弈: {overview['main_vs_retail']}")
        else:
            sh_pct = _fmt(market.get("sh_change"), "%")
            sz_pct = _fmt(market.get("sz_change"), "%")
            main_flow = _fmt_flow(market.get("main_net_inflow"))
            lines.append(f"📅 {m_date}")
            lines.append(f"上证 {sh_pct} | 深证 {sz_pct}")
            lines.append(f"主力净流入: {main_flow}")
            if north:
                lines.append(f"北向资金: {_fmt_flow(north.get('total'))}")
            if overview.get("main_vs_retail"):
                lines.append(f"博弈: {overview['main_vs_retail']}")
        sections.append("\n".join(lines))
    else:
        sections.append("**大盘资金流**\n数据暂不可用（非交易时段）")

    # ── 行业流入TOP5 ──
    inflow = sectors.get("inflow_top", [])
    if inflow:
        lines = ["**行业流入 TOP**"]
        for i, s in enumerate(inflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 行业流出TOP5 ──
    outflow = sectors.get("outflow_top", [])
    if outflow:
        lines = ["**行业流出 TOP**"]
        for i, s in enumerate(outflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 轮动强度 ──
    rot_score = sectors.get("rotation_score", 0)
    rot_note = sectors.get("rotation_note", "")
    rot_label = _rotation_label(rot_score)
    rot_text = f"{rot_label}（{rot_score}）"
    if rot_note:
        rot_text += f" — {rot_note}"
    sections.append(f"🔄 **轮动强度**: {rot_text}")

    # ── 概念热点 ──
    if concepts:
        lines = ["**概念热点 TOP**"]
        for i, c in enumerate(concepts[:3], 1):
            name = c.get("name", "")
            f_main = _fmt_flow(c.get("main_inflow"))
            lines.append(f"{i}. {name} {f_main}")
        sections.append("\n".join(lines))

    # ── 背离预警 ──
    divergences = sectors.get("divergence", [])
    if divergences:
        lines = ["⚠️ **背离预警**"]
        for d in divergences:
            icon = "🔻" if "底" in d.get("type", "") else "🔺"
            lines.append(f"{icon} {d['name']}: {d['detail']}")
        sections.append("\n".join(lines))

    # ── 强流入个股 ──
    if strong_stocks:
        lines = ["**强流入个股 TOP**"]
        for i, s in enumerate(strong_stocks[:5], 1):
            name = s.get("name", "")
            code = s.get("code", "")
            ratio = s.get("main_ratio", 0)
            inflow_val = s.get("main_inflow", 0)
            pct = s.get("price_change", 0)
            lines.append(
                f"{i}. {name}({code}) 主力+{ratio:.1f}% {_fmt_flow(inflow_val)} ({_fmt(pct, '%')})"
            )
        sections.append("\n".join(lines))

    note = f"明策 · gugu · 资金流复盘 | 📈 数据截至 {data.get('timestamp', _now_str())}"

    title = f"📊 昨日资金流复盘（{market.get('date', _date_str()) if market else _date_str()}）"
    template = "blue"

    return _card(title, template, sections, note=note)


def build_close_card(data: dict[str, Any]) -> dict[str, Any]:
    """构建 15:10 收盘 — 今日资金流日报卡片。"""
    from gugu.notifier.formatter import _card

    market = data.get("market", {})
    north = data.get("north_bound", {})
    overview = data.get("market_overview", {})
    sectors = data.get("sectors", {})
    concepts = data.get("concepts", [])
    strong_stocks = data.get("strong_stocks", [])

    sections: list[str] = []

    # ── 大盘概览 ──
    if market:
        m_date = market.get("date", "")
        north_only = market.get("north_only", False)
        lines = ["**今日大盘**"]
        if north_only:
            nb = north or {}
            lines.append(f"📅 {m_date}")
            lines.append("📊 **北向资金代理**（东方财富 API 暂不可用）")
            n_total = _fmt_flow(nb.get("total"))
            n_sh = _fmt_flow(nb.get("sh_connect"))
            n_sz = _fmt_flow(nb.get("sz_connect"))
            lines.append(f"  北向总额: {n_total}")
            lines.append(f"  沪股通: {n_sh}")
            lines.append(f"  深股通: {n_sz}")
            if overview.get("main_vs_retail"):
                lines.append(f"  博弈: {overview['main_vs_retail']}")
        else:
            sh_pct = _fmt(market.get("sh_change"), "%")
            sz_pct = _fmt(market.get("sz_change"), "%")
            main_flow = _fmt_flow(market.get("main_net_inflow"))
            lines.append(f"📅 {m_date}")
            lines.append(f"上证 {sh_pct} | 深证 {sz_pct}")
            lines.append(f"主力净流入: {main_flow}")
            if north:
                lines.append(f"北向资金: {_fmt_flow(north.get('total'))}")
            if overview.get("main_vs_retail"):
                lines.append(f"博弈: {overview['main_vs_retail']}")
        sections.append("\n".join(lines))
    else:
        sections.append("**今日大盘**\n数据暂不可用（非交易时段）")

    # ── 行业流入TOP5 ──
    inflow = sectors.get("inflow_top", [])
    if inflow:
        lines = ["**行业流入 TOP**"]
        for i, s in enumerate(inflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 行业流出TOP5 ──
    outflow = sectors.get("outflow_top", [])
    if outflow:
        lines = ["**行业流出 TOP**"]
        for i, s in enumerate(outflow[:5], 1):
            name = s.get("name", "")
            f_main = _fmt_flow(s.get("main_inflow"))
            pct = _fmt(s.get("price_change"), "%")
            lines.append(f"{i}. {name} {f_main} ({pct})")
        sections.append("\n".join(lines))

    # ── 轮动强度 ──
    rot_score = sectors.get("rotation_score", 0)
    rot_note = sectors.get("rotation_note", "")
    rot_label = _rotation_label(rot_score)
    rot_text = f"{rot_label}（{rot_score}）"
    if rot_note:
        rot_text += f" — {rot_note}"
    sections.append(f"🔄 **轮动强度**: {rot_text}")

    # ── 背离预警 ──
    divergences = sectors.get("divergence", [])
    if divergences:
        lines = ["⚠️ **今日背离预警**"]
        for d in divergences:
            icon = "🔻" if "底" in d.get("type", "") else "🔺"
            lines.append(f"{icon} {d['name']}: {d['detail']} → {d.get('action', '观察')}")
        sections.append("\n".join(lines))

    # ── 强流入个股 ──
    if strong_stocks:
        lines = ["**今日强流入个股**"]
        for i, s in enumerate(strong_stocks[:5], 1):
            name = s.get("name", "")
            code = s.get("code", "")
            ratio = s.get("main_ratio", 0)
            inflow_val = s.get("main_inflow", 0)
            pct = s.get("price_change", 0)
            lines.append(
                f"{i}. {name}({code}) 主力+{ratio:.1f}% {_fmt_flow(inflow_val)} ({_fmt(pct, '%')})"
            )
        sections.append("\n".join(lines))

    # ── 概念热点 ──
    if concepts:
        lines = ["**热门概念**"]
        for i, c in enumerate(concepts[:3], 1):
            name = c.get("name", "")
            f_main = _fmt_flow(c.get("main_inflow"))
            lines.append(f"{i}. {name} {f_main}")
        sections.append("\n".join(lines))

    note = f"明策 · gugu · 资金流日报 | 📈 {data.get('timestamp', _now_str())}"

    title = f"📊 今日资金流日报（{_date_str()}）"
    template = "blue"

    return _card(title, template, sections, note=note)


# ── 公共入口 ──────────────────────────────────────────────────


def build_unavailable_card(report_name: str) -> dict[str, Any]:
    """当所有数据源不可用时发送状态卡片。"""
    from gugu.notifier.formatter import _card

    content = (
        f"当前资金流数据源（东方财富）暂时不可用，已跳过本次推送。\n\n"
        "可能原因：\n"
        "- 东方财富 API 网络不通\n"
        "- 盘中数据尚未到位\n\n"
        "系统会在下一执行周期自动重试。"
    )
    return _card(
        title=f"⚠️ {report_name}（数据暂不可用）",
        template="yellow",
        sections=[content],
        note=_now_str(),
    )


async def run_morning_report() -> dict[str, Any]:
    """运行盘前资金流复盘（08:00）。"""
    data = await collect_all()
    market = data.get("market")
    # 北向资金代理也算有效数据（north_only=True）
    has_valid_data = market is not None and (
        market.get("north_only", False)
        or market.get("sh_close") or market.get("sz_close") or market.get("main_net_inflow")
    )
    if not has_valid_data and not data.get("sectors") and not data.get("strong_stocks"):
        logger.warning("资金流复盘: 全部数据源不可用，跳过推送")
        card = build_unavailable_card("资金流复盘")
    else:
        card = build_morning_card(data)
    return {"card": card, "data": data}


async def run_close_report() -> dict[str, Any]:
    """运行收盘资金流日报（15:10）。"""
    data = await collect_all()
    market = data.get("market")
    has_valid_data = market is not None and (
        market.get("north_only", False)
        or market.get("sh_close") or market.get("sz_close") or market.get("main_net_inflow")
    )
    if not has_valid_data and not data.get("sectors") and not data.get("strong_stocks"):
        logger.warning("资金流日报: 全部数据源不可用，跳过推送")
        card = build_unavailable_card("资金流日报")
    else:
        card = build_close_card(data)
    return {"card": card, "data": data}
