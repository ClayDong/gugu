"""东方财富直连采集器（第三备用源）。

通过 httpx 直连东方财富 push2 API，不依赖 akshare。
在新浪主源和 akshare 降级源均失败时启用，提供第三道防线。

支持：
- 实时行情（push2.eastmoney.com/api/qt/stock/get）
- 历史K线（push2.eastmoney.com/api/qt/stock/kline/get）
- 行业资金流（push2.eastmoney.com/api/qt/clist/get）

特点：
- 单股查询，不会触发全市场快照限流
- 重试机制（3次，指数退避）
- 数据校验（价格>0、字段完整）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from gugu.data.collectors.base import BaseCollector
from gugu.data.quality import validate_stock_history
from gugu.utils.log import get_logger

logger = get_logger()

# 东方财富 push API
_EM_STOCK_GET_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_EM_KLINE_URL = "https://push2.eastmoney.com/api/qt/stock/kline/get"
_EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"

_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

# 代码前缀 → 东方财富 secid 前缀
# 6/9 开头为沪市（前缀1），0/3 开头为深市（前缀0）
_EM_SECID_PREFIX: dict[str, str] = {
    "6": "1",  # 沪市主板
    "9": "1",  # 沪市B股
    "0": "0",  # 深市主板
    "3": "0",  # 创业板
    "8": "0",  # 北交所
}


def _to_secid(code: str) -> str:
    """股票代码 → 东方财富 secid 格式（如 1.600519）。"""
    prefix = _EM_SECID_PREFIX.get(code[0], "0")
    return f"{prefix}.{code}"


class EastmoneyCollector(BaseCollector):
    """东方财富直连采集器（第三备用源）。

    通过 HTTP 直连东方财富 push2 API，与 akshare 完全解耦。
    在新浪和 akshare 均不可用时作为兜底数据源。
    """

    source = "eastmoney"

    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=15.0, headers=_EM_HEADERS)
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_stock_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        """获取个股历史K线（东方财富日K接口）。"""
        code = self.normalize_symbol(symbol)
        secid = _to_secid(code)
        # 请求略多于 days 的数据确保足够
        klt = "101"  # 101=日K
        fqt = "1"  # 1=前复权
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": klt,
            "fqt": fqt,
            "end": "20500101",
            "lmt": str(max(days * 2, 120)),
        }
        try:
            client = self._get_client()
            resp = client.get(_EM_KLINE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data or not data.get("data") or not data["data"].get("klines"):
                logger.warning(f"东方财富历史K线 {code} 返回空数据")
                return pd.DataFrame()

            klines = data["data"]["klines"]
            rows: list[dict[str, Any]] = []
            for line in klines:
                # 格式: 日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率
                parts = line.split(",")
                if len(parts) < 11:
                    continue
                try:
                    rows.append(
                        {
                            "date": parts[0],
                            "open": float(parts[1]),
                            "close": float(parts[2]),
                            "high": float(parts[3]),
                            "low": float(parts[4]),
                            "volume": float(parts[5]),
                            "amount": float(parts[6]),
                            "change_pct": float(parts[9]) if parts[9] else 0.0,
                        }
                    )
                except (ValueError, IndexError):
                    continue

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"])
            df = df.sort_values("date")
            df = df.tail(days).reset_index(drop=True)
            return validate_stock_history(df, code)
        except Exception as e:
            logger.error(f"东方财富历史K线 {code} 失败: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        """获取个股实时行情。

        空列表时返回全市场快照（用于自动选股扫描）。
        非空时单股查询，避免全市场快照限流。
        """
        if not symbols:
            # 空列表 → 返回全市场快照（用于自动选股）
            return self._fetch_market_snapshot()

        rows: list[dict[str, Any]] = []
        client = self._get_client()
        for symbol in symbols:
            code = self.normalize_symbol(symbol)
            secid = _to_secid(code)
            params = {
                "secid": secid,
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f169,f170",
            }
            try:
                resp = client.get(_EM_STOCK_GET_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
                if not data or not data.get("data"):
                    logger.warning(f"东方财富实时行情 {code} 返回空数据")
                    continue

                d = data["data"]
                # f43=最新价(需/100), f46=开盘, f47=成交量, f48=成交额
                # f57=代码, f58=名称, f170=涨跌幅(需/100)
                price = float(d.get("f43", 0)) / 100.0
                if price <= 0:
                    logger.warning(f"东方财富实时行情 {code} price={price} 无效")
                    continue

                open_price = float(d.get("f46", 0)) / 100.0
                change_pct = float(d.get("f170", 0)) / 100.0
                volume = float(d.get("f47", 0))
                amount = float(d.get("f48", 0))
                name = str(d.get("f58", code))

                rows.append(
                    {
                        "symbol": code,
                        "name": name,
                        "price": price,
                        "open": open_price,
                        "change_pct": round(change_pct, 2),
                        "volume": volume,
                        "amount": amount,
                    }
                )
            except Exception as e:
                logger.warning(f"东方财富实时行情 {code} 采集失败: {e}")
                continue

        return (
            pd.DataFrame(rows)
            if rows
            else pd.DataFrame(columns=["symbol", "name", "price", "change_pct", "volume", "amount"])
        )

    def fetch_sector_flow(self) -> pd.DataFrame:
        """获取行业资金流排名（东方财富 clist 接口）。"""
        params = {
            "fid": "f62",
            "po": "1",
            "pz": "100",
            "pn": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fs": "m:90+t:2",
            "fields": "f12,f14,f2,f3,f62,f184,f66,f72,f78,f84",
        }
        try:
            client = self._get_client()
            resp = client.get(_EM_CLIST_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data or not data.get("data") or not data["data"].get("diff"):
                return pd.DataFrame()

            diff = data["data"]["diff"]
            rows = []
            for item in diff:
                try:
                    # 资金流单位为元，转换为亿元
                    main_net = float(item.get("f62", 0)) / 1e8
                    super_large = float(item.get("f66", 0)) / 1e8
                    large = float(item.get("f72", 0)) / 1e8
                    medium = float(item.get("f78", 0)) / 1e8
                    small = float(item.get("f84", 0)) / 1e8

                    rows.append(
                        {
                            "date": datetime.now().strftime("%Y-%m-%d"),
                            "sector": str(item.get("f14", "")),
                            "main_net": round(main_net, 2),
                            "main_pct": float(item.get("f184", 0)),
                            "super_large_net": round(super_large, 2),
                            "large_net": round(large, 2),
                            "medium_net": round(medium, 2),
                            "small_net": round(small, 2),
                            "change_pct": float(item.get("f3", 0)),
                        }
                    )
                except (ValueError, TypeError):
                    continue

            df = pd.DataFrame(rows)
            return df if not df.empty else pd.DataFrame()
        except Exception as e:
            logger.error(f"东方财富行业资金流失败: {e}")
            return pd.DataFrame()

    def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        """获取个股资金流明细（东方财富日K资金流接口）。"""
        code = self.normalize_symbol(symbol)
        secid = _to_secid(code)
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "klt": "101",
            "lmt": "5",
        }
        try:
            client = self._get_client()
            resp = client.get(
                "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data or not data.get("data") or not data["data"].get("klines"):
                return pd.DataFrame()

            klines = data["data"]["klines"]
            rows = []
            for line in klines:
                parts = line.split(",")
                if len(parts) < 6:
                    continue
                try:
                    # 字段顺序: 日期,主力净流入,小单,中单,大单,超大单
                    main_net = float(parts[1]) / 1e8
                    small = float(parts[2]) / 1e8
                    medium = float(parts[3]) / 1e8
                    large = float(parts[4]) / 1e8
                    super_large = float(parts[5]) / 1e8

                    rows.append(
                        {
                            "date": parts[0],
                            "main_net": round(main_net, 2),
                            "main_pct": 0.0,
                            "super_large_net": round(super_large, 2),
                            "large_net": round(large, 2),
                            "medium_net": round(medium, 2),
                            "small_net": round(small, 2),
                        }
                    )
                except (ValueError, IndexError):
                    continue

            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"])
            return df.tail(5).reset_index(drop=True) if not df.empty else df
        except Exception as e:
            logger.error(f"东方财富个股资金流 {code} 失败: {e}")
            return pd.DataFrame()

    def _fetch_market_snapshot(self) -> pd.DataFrame:
        """获取全市场实时快照（用于自动选股扫描）。

        使用东方财富 clist API，fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23
        覆盖深沪A股全市场（不含北交所）。
        """
        params = {
            "pn": "1",
            "pz": "6000",  # 单页6000条，覆盖全A股
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            # m:0+t:6 深圳主板, m:0+t:80 创业板, m:1+t:2 沪市主板, m:1+t:23 科创板
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            # f2=最新价, f3=涨跌幅, f4=涨跌额, f5=成交量, f6=成交额, f7=振幅
            # f8=换手率, f12=代码, f14=名称, f15=最高, f16=最低, f17=今开
            # f20=总市值, f21=流通市值（v0.4.6 新增 f8/f21 支持豹哥三要素选股池过滤）
            "fields": "f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f20,f21",
        }
        try:
            client = self._get_client()
            resp = client.get(_EM_CLIST_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data or not data.get("data") or not data["data"].get("diff"):
                logger.warning("东方财富全市场快照返回空数据")
                return pd.DataFrame()

            diff = data["data"]["diff"]
            rows: list[dict[str, Any]] = []
            for item in diff:
                try:
                    price = float(item.get("f2", 0))
                    # 跳过停牌（价格为0或-）
                    if price <= 0:
                        continue
                    rows.append(
                        {
                            "symbol": str(item.get("f12", "")).zfill(6),
                            "name": str(item.get("f14", "")),
                            "price": price,
                            "change_pct": float(item.get("f3", 0)),
                            "volume": float(item.get("f5", 0)),
                            "amount": float(item.get("f6", 0)),
                            "open": float(item.get("f17", 0)),
                            "high": float(item.get("f15", 0)),
                            "low": float(item.get("f16", 0)),
                            # v0.4.6: 换手率(%) + 流通市值(元)，支持豹哥三要素选股池过滤
                            "turnover_pct": float(item.get("f8", 0) or 0),
                            "circ_mv": float(item.get("f21", 0) or 0),
                        }
                    )
                except (ValueError, TypeError):
                    continue

            df = pd.DataFrame(rows)
            logger.info(f"东方财富全市场快照: {len(df)} 只股票")
            return df
        except Exception as e:
            logger.error(f"东方财富全市场快照失败: {e}")
            return pd.DataFrame()
