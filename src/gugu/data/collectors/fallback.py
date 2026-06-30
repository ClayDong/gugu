"""主采集器：新浪财经（直接 HTTP API，不依赖 akshare）。

通过 httpx 直连新浪/网易原生 HTTP 接口，与 akshare 完全解耦。
支持在 settings.yaml 中配置为 primary_source: sina。
失败时可降级到 akshare（东方财富）。
"""
from __future__ import annotations

import re
from io import StringIO

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from gugu.data.collectors.base import BaseCollector
from gugu.data.quality import validate_stock_history
from gugu.utils.log import get_logger

logger = get_logger()

SINA_HISTORY_URL = "https://quotes.money.163.com/service/chddata.html?code={sina_code}&start={start}&end={end}&fields=TCLOSE"
SINA_REALTIME_URL = "https://hq.sinajs.cn/list={codes}"

_SINA_PREFIX_MAP: dict[str, str] = {
    "0": "0",
    "1": "1",
    "3": "0",
    "6": "1",
    "8": "0",
    "9": "1",
}


def _to_sina_code(code: str) -> str:
    prefix = _SINA_PREFIX_MAP.get(code[0], "0")
    exchange = "sh" if prefix == "1" else "sz"
    return f"{exchange}{code}"


def _to_sina_csv_code(code: str) -> str:
    prefix = _SINA_PREFIX_MAP.get(code[0], "0")
    return f"{prefix}{code}"


class SinaCollector(BaseCollector):
    """新浪财经主采集器（直接 HTTP，不依赖 akshare）。"""

    source = "sina"

    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=15.0)
        return self._client

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    def fetch_stock_history(self, symbol: str, days: int = 60) -> pd.DataFrame:
        code = self.normalize_symbol(symbol)
        sina_code = _to_sina_csv_code(code)
        url = SINA_HISTORY_URL.format(
            sina_code=sina_code,
            start="20200101",
            end="21000101",
        )
        try:
            client = self._get_client()
            resp = client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer": "https://quotes.money.163.com/",
            })
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"新浪 HTTP 历史行情 {code} 失败: {e}")
            raise

        if not resp.text.strip():
            return pd.DataFrame()

        try:
            df = pd.read_csv(StringIO(resp.text), encoding="gbk")
        except Exception:
            df = pd.read_csv(StringIO(resp.text), encoding="utf-8")

        if df.empty:
            return df

        col_map = {
            "日期": "date", "股票代码": "code",
            "开盘价": "open", "最高价": "high", "最低价": "low",
            "收盘价": "close", "成交数量": "volume",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

        if "close" not in df.columns:
            return pd.DataFrame()

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df.sort_values("date")
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        df = df.tail(days).reset_index(drop=True)
        return validate_stock_history(df, code)

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()

        codes = [self.normalize_symbol(s) for s in symbols]
        sina_codes = ",".join(_to_sina_code(c) for c in codes)

        try:
            client = self._get_client()
            resp = client.get(
                SINA_REALTIME_URL.format(codes=sina_codes),
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Referer": "https://finance.sina.com.cn/",
                },
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"新浪 HTTP 实时行情失败: {e}")
            return pd.DataFrame()

        text = resp.text
        if not text.strip():
            return pd.DataFrame()

        rows = []
        for line in text.strip().split("\n"):
            if not line.strip():
                continue
            match = re.search(r'hq_str_(\w{2})(\d{6})="(.+)"', line)
            if not match:
                continue
            exchange = match.group(1)
            code = match.group(2)
            fields = match.group(3).split(",")
            if len(fields) < 32:
                continue

            name = fields[0]
            try:
                open_price = float(fields[1]) if fields[1] else 0.0
                prev_close = float(fields[2]) if fields[2] else 0.0
                price = float(fields[3]) if fields[3] else 0.0
                change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
                volume = float(fields[8]) if fields[8] else 0.0
                amount = float(fields[9]) if fields[9] else 0.0
            except (ValueError, IndexError):
                continue

            rows.append({
                "symbol": code,
                "name": name,
                "price": price,
                "open": open_price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
                "volume": volume,
                "amount": amount,
            })

        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def fetch_sector_flow(self) -> pd.DataFrame:
        logger.warning("新浪源不支持行业资金流，返回空")
        return pd.DataFrame()

    def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        logger.warning("新浪源不支持个股资金流明细，返回空")
        return pd.DataFrame()