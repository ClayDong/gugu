"""资金流数据采集与分析引擎。

从 flow_report.py 抽取的数据采集层，提供：
- 大盘资金流 + 北向资金（akshare → HTTP 双通道冗余）
- 行业/概念/个股资金流排名
- 行业轮动分析、背离预警、重点行业提取
- 本地缓存持久化
- 行情数据替代分析（应急降级）
"""

from __future__ import annotations

import json as _json
import math
from datetime import datetime
from pathlib import Path as _Path
from typing import Any

import httpx
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# 东方财富 push API 直连（akshare 备用通道）
_EASTMONEY_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_EASTMONEY_FFLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
_EASTMONEY_HSGT_URL = "https://push2.eastmoney.com/api/qt/kamt.rtmin/get"

# 背离检测阈值（可配置，单位：亿元 / %）
# 主力净流入绝对值超过此阈值且涨跌幅方向相反时触发背离预警
DIVERGENCE_MAIN_THRESHOLD = 1.0  # 亿元
DIVERGENCE_PRICE_THRESHOLD = 1.0  # 百分比

# User-Agent 池：东方财富会对单一 UA 限流，轮换提升成功率
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
]

_EASTMONEY_HEADERS_BASE = {
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ── HTTP 工具 ──────────────────────────────────────────────────


def _http_get_json(url: str, params: dict[str, str], max_retries: int = 3) -> dict | None:
    """直连东方财富 push API，返回 JSON dict 或 None。

    Args:
        url: 东方财富 API URL。
        params: 请求参数。
        max_retries: 最大重试次数（默认 3 次，每次换不同 User-Agent）。

    Returns:
        JSON dict 或 None。
    """
    import random

    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            headers = {**_EASTMONEY_HEADERS_BASE, "User-Agent": random.choice(_USER_AGENTS)}
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                if data and data.get("data"):
                    return data
                # 数据为空也可能是参数问题，不再重试
                return None
        except Exception as e:
            last_err = e
            logger.debug("东方财富 HTTP API 第 {}/{} 次失败: {}", attempt + 1, max_retries, e)
            if attempt < max_retries - 1:
                import time

                time.sleep(0.5 * (attempt + 1))  # 递增等待 0.5s, 1s
    logger.info("东方财富 HTTP API {} 次重试均失败: {}", max_retries, last_err)
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
                "大盘资金流(akshare): 日期={}, " "主力净流入={:.2f}亿",
                result["market"]["date"],
                result["market"]["main_net_inflow"],
            )
    except Exception as e:
        logger.info("akshare 大盘资金流采集失败: {}", e)

    # 北向资金
    try:
        df_n = ak.stock_hsgt_fund_min_em(symbol="北向资金")
        if df_n is not None and not df_n.empty:
            latest_n = df_n.iloc[-1].to_dict()
            # 北向资金可能因数据源限制获取不到，用 None 表示未获取到
            north_total_raw = latest_n.get("北向资金")
            sh_raw = latest_n.get("港股通(沪)")
            sz_raw = latest_n.get("港股通(深)")
            if north_total_raw is not None:
                result["north_bound"] = {
                    "date": str(latest_n.get("日期", "")),
                    "time": str(latest_n.get("时间", "")),
                    "total": _safe_float(north_total_raw),
                    "sh_connect": _safe_float(sh_raw) if sh_raw is not None else None,
                    "sz_connect": _safe_float(sz_raw) if sz_raw is not None else None,
                }
                logger.info("北向资金(akshare): {:.2f}亿", result["north_bound"]["total"])
            else:
                logger.warning("akshare 北向资金: 数据字段为空，标记为获取不到")
                result["north_bound"] = {
                    "date": str(latest_n.get("日期", "")),
                    "time": str(latest_n.get("时间", "")),
                    "total": None,
                    "sh_connect": None,
                    "sz_connect": None,
                }
        else:
            logger.warning("akshare 北向资金: 返回空数据，标记为获取不到")
            result["north_bound"] = {
                "date": "",
                "time": "",
                "total": None,
                "sh_connect": None,
                "sz_connect": None,
            }
    except Exception as e:
        logger.info("akshare 北向资金采集失败: {}", e)
        result["north_bound"] = {
            "date": "",
            "time": "",
            "total": None,
            "sh_connect": None,
            "sz_connect": None,
        }

    return result if result else None


def _collect_market_flow_http() -> dict[str, Any] | None:
    """通过东方财富 push API 直连采集大盘资金流 + 北向资金（akshare 备用通道）。"""
    result: dict[str, Any] = {}

    # 大盘资金流（沪深京合并）
    try:
        data = _http_get_json(
            _EASTMONEY_FFLOW_URL,
            {
                "secid": "1.000001",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "klt": "101",
                "lmt": "1",
            },
        )
        if data and data.get("data") and data["data"].get("klines"):
            kline = data["data"]["klines"][-1].split(",")
            # 字段顺序: 日期,主力净流入,小单,中单,大单,超大单,...
            if len(kline) >= 6:
                result["market"] = {
                    "date": kline[0],
                    "sh_close": None,
                    "sh_change": None,
                    "sz_close": None,
                    "sz_change": None,
                    "main_net_inflow": _yuan_to_yi(_safe_float(kline[1])),
                    "main_net_ratio": None,
                    "small_inflow": _yuan_to_yi(_safe_float(kline[2])),
                    "medium_inflow": _yuan_to_yi(_safe_float(kline[3])),
                    "large_inflow": _yuan_to_yi(_safe_float(kline[4])),
                    "super_large_inflow": _yuan_to_yi(_safe_float(kline[5])),
                }
                logger.info(
                    "大盘资金流(HTTP): 日期={}, " "主力净流入={:.2f}亿",
                    result["market"]["date"],
                    result["market"]["main_net_inflow"],
                )
    except Exception as e:
        logger.info("HTTP 大盘资金流采集失败: {}", e)

    # 北向资金
    try:
        data = _http_get_json(
            _EASTMONEY_HSGT_URL, {"fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54,f55,f56"}
        )
        if data and data.get("data") and data["data"].get("s2n"):
            # s2n 可能是字符串（逗号分隔）或列表，统一处理
            raw = data["data"].get("s2n", "")
            if isinstance(raw, list):
                raw = ";".join(str(x) for x in raw)
            if raw:
                # 取最后一段数据
                parts = raw.split(";")
                if parts:
                    latest_parts = parts[-1].split(",")
                    if len(latest_parts) >= 4:
                        result["north_bound"] = {
                            "date": latest_parts[0] if len(latest_parts) > 0 else "",
                            "time": latest_parts[1] if len(latest_parts) > 1 else "",
                            "total": _safe_float(latest_parts[2]) / 10000
                            if len(latest_parts) > 2
                            else None,
                            "sh_connect": None,
                            "sz_connect": None,
                        }
                        if result["north_bound"]["total"] is not None:
                            logger.info("北向资金(HTTP): {:.2f}亿", result["north_bound"]["total"])
                        else:
                            logger.warning("HTTP 北向资金: 数据字段为空，标记为获取不到")
    except Exception as e:
        logger.info("HTTP 北向资金采集失败: {}", e)

    return result if result else None


def collect_market_flow() -> dict[str, Any] | None:
    """采集大盘资金流 + 北向资金（akshare → HTTP 双通道冗余）。

    HTTP 降级触发条件（任一满足即降级）：
    - akshare 完全失败
    - market 字段缺失
    - market 中 main_net_inflow 为 None（akshare 部分失败常见场景）
    """
    # 通道1: akshare
    result = _collect_market_flow_akshare()

    # 判断是否需要降级到 HTTP：market 数据不完整时也降级
    needs_http = (
        not result
        or "market" not in result
        or (
            result.get("market", {}).get("main_net_inflow") is None
            and result.get("market", {}).get("sh_close") is None
        )
    )

    # 通道2: HTTP 直连（akshare 失败或 market 数据不完整时）
    if needs_http:
        logger.info("akshare 大盘资金流不可用或数据不完整，降级到 HTTP 直连...")
        http_result = _collect_market_flow_http()
        if http_result:
            # 合并：HTTP 结果补充 akshare 缺失的字段
            if not result:
                result = http_result
            else:
                # market：akshare 缺失关键字段时用 HTTP 补全
                ak_market = result.get("market", {})
                http_market = http_result.get("market", {})
                if http_market and (
                    not ak_market
                    or (
                        ak_market.get("main_net_inflow") is None
                        and http_market.get("main_net_inflow") is not None
                    )
                ):
                    # 保留 akshare 已有的字段（更丰富），仅补充 HTTP 独有的字段
                    for k, v in http_market.items():
                        if ak_market.get(k) is None and v is not None:
                            ak_market[k] = v
                    result["market"] = ak_market
                if "north_bound" not in result and "north_bound" in http_result:
                    result["north_bound"] = http_result["north_bound"]

    if not result:
        return None

    # 大盘资金流不可用时，用北向资金作为市场情绪代理
    if "market" not in result and "north_bound" in result:
        nb = result["north_bound"]
        nb_total = nb.get("total")
        result["market"] = {
            "date": nb.get("date", _date_str()),
            "sh_close": None,
            "sh_change": None,
            "sz_close": None,
            "sz_change": None,
            "main_net_inflow": nb_total if nb_total is not None else None,
            "main_net_ratio": None,
            "super_large_inflow": None,
            "large_inflow": None,
            "medium_inflow": None,
            "small_inflow": -nb_total if nb_total is not None else None,
            "north_only": True,
        }
        if nb_total is not None:
            logger.info("大盘资金流不可用，使用北向资金作为市场情绪代理")
        else:
            logger.warning("大盘资金流和北向资金均不可用")

    return result if result else None


def _collect_sector_flow_akshare(sector_type: str = "行业资金流") -> list[dict[str, Any]] | None:
    """通过 akshare 采集行业/概念资金流排名。

    统一字段名为 HTTP API 格式（主力净流入-净额 等），便于 analyze 函数统一处理。
    """
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare 未安装，跳过 akshare 资金流排名采集")
        return None
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type=sector_type)
        if df is not None and not df.empty:
            # akshare 字段名前缀 "今日" → 统一为 HTTP 格式
            field_map = {
                "今日主力净流入-净额": "主力净流入-净额",
                "今日主力净流入-净占比": "主力净流入-净占比",
                "今日涨跌幅": "涨跌幅",
                "今日超大单净流入-净额": "超大单净流入-净额",
                "今日大单净流入-净额": "大单净流入-净额",
                "今日中单净流入-净额": "中单净流入-净额",
                "今日小单净流入-净额": "小单净流入-净额",
            }
            records: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                rec: dict[str, Any] = {}
                for col in df.columns:
                    val = row[col]
                    if isinstance(val, (pd.Timestamp, pd.Period)):
                        val = str(val)
                    col_str = str(col)
                    # 字段名统一
                    rec[field_map.get(col_str, col_str)] = val
                records.append(rec)
            logger.info("akshare {}: {} 条", sector_type, len(records))
            return records
    except Exception as e:
        logger.info("akshare {} 采集失败: {}", sector_type, e)
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
        records.append(
            {
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
            }
        )
    logger.info("HTTP {}: {} 条", label, len(records))
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
            # akshare 个股字段名前缀 "今日" → 统一为 HTTP 格式
            field_map = {
                "今日主力净流入-净额": "主力净流入-净额",
                "今日主力净流入-净占比": "主力净流入-净占比",
                "今日涨跌幅": "涨跌幅",
                "今日超大单净流入-净额": "超大单净流入-净额",
                "今日大单净流入-净额": "大单净流入-净额",
                "今日中单净流入-净额": "中单净流入-净额",
                "今日小单净流入-净额": "小单净流入-净额",
            }
            records: list[dict[str, Any]] = []
            for _, row in df.iterrows():
                rec: dict[str, Any] = {}
                for col in df.columns:
                    val = row[col]
                    if isinstance(val, (pd.Timestamp, pd.Period, pd.Timedelta)):
                        val = str(val)
                    col_str = str(col)
                    rec[field_map.get(col_str, col_str)] = val
                records.append(rec)
            logger.info("akshare 个股资金流排行: {} 只", len(records))
            return records
    except Exception as e:
        logger.info("akshare 个股资金流排行采集失败: {}", e)

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

    # 按主力净流入排序（原始数据单位为元，统一转换为亿元）
    def _get_main_inflow(x: dict[str, Any]) -> float:
        return _yuan_to_yi(_safe_float(x.get("主力净流入-净额", x.get("f62", 0))))

    def _get_sector_name(x: dict[str, Any]) -> str:
        return str(x.get("行业名称", x.get("板块名称", x.get("名称", ""))))

    sorted_data = sorted(sector_data, key=_get_main_inflow, reverse=True)

    # 流入 TOP
    result["inflow_top"] = [
        {
            "name": _get_sector_name(x),
            "main_inflow": _get_main_inflow(x),
            "main_ratio": _safe_float(x.get("主力净流入-净占比", x.get("f184", 0))),
            "price_change": _safe_float(x.get("涨跌幅", x.get("f3", 0))),
        }
        for x in sorted_data[:top_n]
        if _get_main_inflow(x) > 0
    ][:top_n]

    # 流出 TOP（反向排序）
    sorted_desc = sorted(sector_data, key=_get_main_inflow)
    result["outflow_top"] = [
        {
            "name": _get_sector_name(x),
            "main_inflow": _get_main_inflow(x),
            "main_ratio": _safe_float(x.get("主力净流入-净占比", x.get("f184", 0))),
            "price_change": _safe_float(x.get("涨跌幅", x.get("f3", 0))),
        }
        for x in sorted_desc[:top_n]
        if _get_main_inflow(x) < 0
    ][:top_n]

    # 轮动强度
    top5_inflow = sum(_get_main_inflow(x) for x in sorted_data[:5])
    bottom5_outflow = abs(sum(_get_main_inflow(x) for x in sorted_desc[:5]))
    if bottom5_outflow > 0.01:
        result["rotation_score"] = round(top5_inflow / bottom5_outflow, 2)
    elif top5_inflow > 0:
        result["rotation_score"] = 0.0
        result["rotation_note"] = "市场普涨，无明显轮动"

    # 背离检测（单位：亿元）
    # 阈值可配置：DIVERGENCE_MAIN_THRESHOLD 主力净流入绝对值阈值（亿）
    #            DIVERGENCE_PRICE_THRESHOLD 涨跌幅绝对值阈值（%）
    for x in sector_data:
        main_in = _get_main_inflow(x)
        pct = _safe_float(x.get("涨跌幅", x.get("f3", 0)))
        name = _get_sector_name(x)
        if main_in > DIVERGENCE_MAIN_THRESHOLD and pct < -DIVERGENCE_PRICE_THRESHOLD:
            result["divergence"].append(
                {
                    "name": name,
                    "type": "底背离",
                    "detail": f"主力+{main_in:.1f}亿 但跌{pct:.1f}%",
                    "action": "资金流入但下跌，可能是吸筹",
                }
            )
        elif main_in < -DIVERGENCE_MAIN_THRESHOLD and pct > DIVERGENCE_PRICE_THRESHOLD:
            result["divergence"].append(
                {
                    "name": name,
                    "type": "顶背离",
                    "detail": f"主力{main_in:.1f}亿 但涨{pct:.1f}%",
                    "action": "资金流出但上涨，警惕出货",
                }
            )

    return result


# 重点行业关键词映射（用于从全量行业资金流中提取用户关注的行业）
_KEY_INDUSTRY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("医疗/医药", ["医疗", "医药", "生物", "医疗器械", "医疗服务", "医药生物"]),
    ("半导体/芯片", ["半导体", "芯片", "集成电路", "消费电子", "电子元件", "光刻"]),
    ("金融", ["银行", "证券", "保险", "金融"]),
    ("新能源", ["光伏", "锂电", "新能源", "风电", "储能", "电池", "新能源车"]),
    ("消费", ["食品饮料", "白酒", "家电", "商贸零售", "纺织服装", "消费"]),
    ("科技/软件", ["软件", "互联网", "计算机", "通信", "信息技术", "AI", "人工智能"]),
    ("军工", ["军工", "国防", "航天", "航空"]),
    ("房地产", ["房地产", "地产", "建材", "建筑"]),
]


def analyze_key_industries(sector_data: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """从全量行业资金流中提取重点行业（医疗/半导体/金融等）的资金流数据。

    Returns:
        [{"category": "医疗/医药", "name": "医药生物", "main_inflow": 12.5,
          "main_ratio": 5.2, "price_change": 2.1}, ...]
    """
    if not sector_data:
        return []

    result: list[dict[str, Any]] = []
    matched_names: set[str] = set()

    for category, keywords in _KEY_INDUSTRY_KEYWORDS:
        best_match: dict[str, Any] | None = None
        best_abs_inflow = -1.0

        for item in sector_data:
            name = str(item.get("行业名称", item.get("板块名称", item.get("名称", ""))))
            if not name or name in matched_names:
                continue
            # 检查是否匹配任一关键词
            if any(kw in name for kw in keywords):
                # 原始数据单位为元，转换为亿元
                main_in = _yuan_to_yi(_safe_float(item.get("主力净流入-净额", item.get("f62", 0))))
                # 取资金流绝对值最大的匹配项
                if abs(main_in) > best_abs_inflow:
                    best_abs_inflow = abs(main_in)
                    best_match = {
                        "category": category,
                        "name": name,
                        "main_inflow": main_in,
                        "main_ratio": _safe_float(
                            item.get("主力净流入-净占比", item.get("f184", 0))
                        ),
                        "price_change": _safe_float(item.get("涨跌幅", item.get("f3", 0))),
                    }

        if best_match:
            result.append(best_match)
            matched_names.add(best_match["name"])
        else:
            # 未匹配到也记录，让用户知道该行业暂无数据
            result.append(
                {
                    "category": category,
                    "name": "暂无数据",
                    "main_inflow": None,
                    "main_ratio": None,
                    "price_change": None,
                }
            )

    return result


def analyze_concepts(concept_data: list[dict[str, Any]], top_n: int = 3) -> list[dict[str, Any]]:
    """概念热点分析。"""
    if not concept_data:
        return []
    sorted_c = sorted(
        concept_data,
        key=lambda x: _yuan_to_yi(_safe_float(x.get("主力净流入-净额", x.get("f62", 0)))),
        reverse=True,
    )
    return [
        {
            "name": str(x.get("概念名称", x.get("板块名称", x.get("名称", "")))),
            "main_inflow": _yuan_to_yi(_safe_float(x.get("主力净流入-净额", x.get("f62", 0)))),
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
        # 原始数据单位为元，转换为亿元
        main_in = _yuan_to_yi(_safe_float(x.get("主力净流入-净额", x.get("f62", 0))))
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
        main_in = market_data.get("main_net_inflow") or 0
        small_in = market_data.get("small_inflow") or 0
        if main_in is None:
            main_in = 0
        if small_in is None:
            small_in = 0
        if main_in > 0 and small_in < 0:
            result["main_vs_retail"] = "主力买入，散户卖出（健康）"
        elif main_in < 0 and small_in > 0:
            result["main_vs_retail"] = "主力卖出，散户接盘（警惕）"
        elif main_in > 0 and small_in > 0:
            result["main_vs_retail"] = "主力散户同步买入"
        else:
            result["main_vs_retail"] = "主力散户同步卖出"
    if north_data:
        total = north_data.get("total")
        if total is not None:
            result["north_analysis"] = {
                "total": total,
                "direction": "流入" if total > 0 else "流出",
                "significant": "显著" if abs(total) > 30 else "一般",
            }
        else:
            result["north_analysis"] = {
                "total": None,
                "direction": "获取不到",
                "significant": "未知",
            }
    return result


# ── 数据持久化 ──────────────────────────────────────────────────


_FLOW_CACHE_DIR = _Path(__file__).parent.parent.parent / "data" / "flow_cache"
_FLOW_CACHE_FILE = _FLOW_CACHE_DIR / "last_flow_data.json"


def _save_to_cache(data: dict[str, Any]) -> None:
    """持久化最近一次成功采集的资金流数据到本地文件。"""
    try:
        _FLOW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_FLOW_CACHE_FILE, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, default=str, indent=2)
        logger.info("资金流数据已缓存: {}", _FLOW_CACHE_FILE)
    except Exception as e:
        logger.warning("资金流数据缓存写入失败: {}", e)


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
    # 每个任务60s超时，防止单源卡死全部采集
    market_task = asyncio.wait_for(loop.run_in_executor(None, collect_market_flow), timeout=60.0)
    sector_task = asyncio.wait_for(loop.run_in_executor(None, collect_sector_flow), timeout=60.0)
    concept_task = asyncio.wait_for(loop.run_in_executor(None, collect_concept_flow), timeout=60.0)
    stock_task = asyncio.wait_for(loop.run_in_executor(None, collect_stock_flow), timeout=60.0)

    # 容错采集：单个超时不影响其他
    results = await asyncio.gather(
        market_task, sector_task, concept_task, stock_task, return_exceptions=True
    )
    market_data = results[0] if not isinstance(results[0], Exception) else None
    sector_data = results[1] if not isinstance(results[1], Exception) else None
    concept_data = results[2] if not isinstance(results[2], Exception) else None
    stock_data = results[3] if not isinstance(results[3], Exception) else None

    logger.info("资金流数据采集完成，开始分析...")

    # 分析
    market_info = market_data or {}
    market_overview = analyze_market_overview(
        market_info.get("market"),
        market_info.get("north_bound"),
    )

    sector_analysis = analyze_sectors(sector_data or []) if sector_data else {}
    key_industries = analyze_key_industries(sector_data) if sector_data else []
    concept_top = analyze_concepts(concept_data or []) if concept_data else []
    strong_stocks = analyze_strong_stocks(stock_data or []) if stock_data else []

    result: dict[str, Any] = {
        "market": market_info.get("market"),
        "north_bound": market_info.get("north_bound"),
        "market_overview": market_overview,
        "sectors": sector_analysis,
        "key_industries": key_industries,
        "concepts": concept_top,
        "strong_stocks": strong_stocks,
        "timestamp": _now_str(),
    }

    logger.info(
        f"资金流分析完成: 行业{len(sector_data or [])}个, "
        f"重点行业{len(key_industries)}个, "
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
        else:
            # 既无实时数据也无缓存 → 标记为不可用
            result["sources_unavailable"] = True
            logger.warning(
                "资金流数据全链路不可用（akshare blocked + HTTP fallback 失败 + 无缓存），将发送状态通知"
            )

    return result


# ── 应急降级：行情数据替代分析 ──────────────────────────────────


async def _collect_fallback_market_data() -> dict[str, Any] | None:
    """资金流数据源不可用时的行情数据替代分析。

    使用 DataManager 获取指数和持仓股的行情数据，
    通过量价关系推算市场强弱，作为资金流的替代参考。

    注意：此方法为应急降级方案，估算公式基于经验系数：
    - 放量场景（量比>1.0）：涨跌幅 × 量比 × 3（资金驱动明显）
    - 缩量场景（量比<0.8）：涨跌幅 × 量比 × 1.5（资金参与度低）
    - 中性场景：涨跌幅 × 量比 × 2
    这些系数是经验值，仅用于方向判断而非精确量化。
    """
    from gugu.data import data_manager as dm_factory

    dm = dm_factory()
    result: dict[str, Any] = {}

    # 1. 获取指数行情（上证、深证）
    try:
        sh_df = await dm.fetch_stock_history("000001", days=5)
        sz_df = await dm.fetch_stock_history("399001", days=5)
        if sh_df is not None and not sh_df.empty:
            sh_latest = sh_df.iloc[-1]
            sh_close = float(sh_latest.get("close", 0))
            sh_prev = float(sh_df.iloc[-2].get("close", 0)) if len(sh_df) >= 2 else sh_close
            sh_change = round((sh_close - sh_prev) / sh_prev * 100, 2) if sh_prev > 0 else 0.0
            sh_vol = float(sh_latest.get("volume", 0))
            sh_prev_vol = float(sh_df.iloc[-2].get("volume", 0)) if len(sh_df) >= 2 else 0
            vol_ratio = round(sh_vol / sh_prev_vol, 2) if sh_prev_vol > 0 else 1.0

            # 量价关系推算资金方向（经验公式，仅用于方向判断）
            # 乘数含义：放量时资金驱动效应放大，缩量时效应缩小
            if sh_change > 0 and vol_ratio > 1.0:
                flow_direction = "放量上涨（资金流入信号）"
                flow_estimate = round(sh_change * vol_ratio * 3, 1)
            elif sh_change > 0 and vol_ratio < 0.8:
                flow_direction = "缩量上涨（资金谨慎）"
                flow_estimate = round(sh_change * vol_ratio * 1.5, 1)
            elif sh_change < 0 and vol_ratio > 1.0:
                flow_direction = "放量下跌（资金流出信号）"
                flow_estimate = round(sh_change * vol_ratio * 3, 1)
            elif sh_change < 0 and vol_ratio < 0.8:
                flow_direction = "缩量下跌（抛压减轻）"
                flow_estimate = round(sh_change * vol_ratio * 1.5, 1)
            else:
                flow_direction = "量价平衡（资金中性）"
                flow_estimate = round(sh_change * vol_ratio * 2, 1)

            # 安全计算深证涨跌幅（修复：除数为 prev_close 而非硬编码 1）
            sz_close_val = None
            sz_change_val = None
            if sz_df is not None and not sz_df.empty and len(sz_df) >= 2:
                sz_close_val = float(sz_df.iloc[-1].get("close", 0))
                sz_prev_close = float(sz_df.iloc[-2].get("close", 0))
                if sz_prev_close > 0:
                    sz_change_val = round((sz_close_val - sz_prev_close) / sz_prev_close * 100, 2)

            result["market"] = {
                "date": str(sh_latest.get("date", _date_str())),
                "sh_close": sh_close,
                "sh_change": sh_change,
                "sz_close": sz_close_val,
                "sz_change": sz_change_val,
                "main_net_inflow": flow_estimate,
                "main_net_ratio": None,
                "vol_ratio": vol_ratio,
                "flow_direction": flow_direction,
                "data_source": "量价推算（资金流API不可用）",
            }
            logger.info(f"行情替代分析: 上证{sh_change}% 量比{vol_ratio} → {flow_direction}")
    except Exception as e:
        logger.warning(f"行情数据替代分析失败: {e}")

    # 2. 获取北向资金（直接尝试 akshare）
    try:
        import akshare as ak

        df_n = ak.stock_hsgt_fund_min_em(symbol="北向资金")
        if df_n is not None and not df_n.empty:
            latest_n = df_n.iloc[-1].to_dict()
            north_total = latest_n.get("北向资金")
            if north_total is not None:
                result["north_bound"] = {
                    "date": str(latest_n.get("日期", "")),
                    "time": str(latest_n.get("时间", "")),
                    "total": _safe_float(north_total),
                    "sh_connect": None,
                    "sz_connect": None,
                }
    except Exception:
        pass  # 北向资金也失败时不阻塞

    return result if result else None
