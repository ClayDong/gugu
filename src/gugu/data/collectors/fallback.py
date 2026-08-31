"""主采集器：新浪财经（直接 HTTP API，不依赖 akshare）。

通过 requests 直连新浪原生 HTTP 接口，与 akshare 完全解耦。
支持在 settings.yaml 中配置为 primary_source: sina。
失败时可降级到 akshare（东方财富）。

注意：httpx 与新浪/东财服务器不兼容（Server disconnected），
必须使用 requests 库，且所有请求需带 Referer 头。
"""

from __future__ import annotations

import json
import re
from io import StringIO

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from gugu.data.collectors.base import BaseCollector
from gugu.data.quality import validate_stock_history
from gugu.utils.log import get_logger

logger = get_logger()

# 新浪 K 线 JSON 接口（主历史行情，已验证可用）
SINA_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData?symbol={exch_code}&scale=240&datalen={datalen}"
# 网易财经历史 CSV 接口（备用历史行情，网易 502 时自动降级）
SINA_HISTORY_URL = "https://quotes.money.163.com/service/chddata.html?code={sina_code}&start={start}&end={end}&fields=TCLOSE"
# 新浪实时行情接口
SINA_REALTIME_URL = "https://hq.sinajs.cn/list={codes}"

_SINA_PREFIX_MAP: dict[str, str] = {
    "0": "0",  # 深市主板
    "1": "1",
    "2": "1",  # 沪市B股
    "3": "0",  # 创业板
    "5": "1",  # 沪市基金/ETF（510xxx/511xxx/512xxx/513xxx/588xxx）
    "6": "1",  # 沪市主板
    "8": "0",  # 北交所（老代码段 83/87/88，语义由 _BSE_PREFIXES 特判接管）
    "9": "1",  # 沪市B股（900xxx；920xxx 北交所新代码段由 _BSE_PREFIXES 特判接管）
}

# 北交所代码段（与 risk/manager.py _BSE_PREFIXES 保持一致）：
# 43/83/87/88 为老代码，920 为 2024-10 起启用的新代码段。
# 注意：920xxx 在映射表中会被 "9"→"1"（沪市B股）误判，必须在查表前特判。
_BSE_PREFIXES: tuple[str, ...] = ("43", "83", "87", "88", "920")


def _to_sina_code(code: str) -> str:
    # v0.9.7 P0：支持显式 sh/sz 前缀 + 指数白名单（000300 沪深300 等）
    if code[:2].lower() in ("sh", "sz", "bj") and code[2:].isdigit():
        return code
    if code in BaseCollector.SH_INDEX_CODES:
        return f"sh{code}"
    if code in BaseCollector.SZ_INDEX_CODES:
        return f"sz{code}"
    # v0.9.12：北交所代码段特判——新浪 K 线用 bj 前缀（实测 bj920068/bj430047 可用，
    # 而 sh920068 返回 null）。必须放在查表之前，否则 920xxx 被 "9"→"1" 误判为沪市B股。
    if code.startswith(_BSE_PREFIXES):
        return f"bj{code}"
    prefix = _SINA_PREFIX_MAP.get(code[0], "0")
    exchange = "sh" if prefix == "1" else "sz"
    return f"{exchange}{code}"


def _to_sina_csv_code(code: str) -> str:
    # v0.9.7 P0：指数代码走沪市（1=sh），其余按映射
    if code in BaseCollector.SH_INDEX_CODES:
        return f"1{code}"
    if code in BaseCollector.SZ_INDEX_CODES:
        return f"0{code}"
    prefix = _SINA_PREFIX_MAP.get(code[0], "0")
    return f"{prefix}{code}"


class SinaCollector(BaseCollector):
    """新浪财经主采集器（直接 HTTP，不依赖 akshare）。"""

    source = "sina"
    adjust_type = "none"  # 不复权（原始价格）

    def __init__(self) -> None:
        self._client: requests.Session | None = None

    def _get_client(self) -> requests.Session:
        if self._client is None:
            self._client = requests.Session()
            self._client.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Referer": "https://finance.sina.com.cn/",
                }
            )
        return self._client

    def close(self) -> None:
        """关闭新浪 HTTP Session。"""
        if self._client is not None:
            self._client.close()
            self._client = None

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    def fetch_stock_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        code = self.normalize_symbol(symbol)
        exch_code = _to_sina_code(code)
        sina_code = _to_sina_csv_code(code)

        # 优先尝试新浪 K 线 JSON 接口（已验证可用，网易 502 故障）
        df = self._try_sina_kline(code, exch_code, days)
        if df is not None:
            return df

        # 新浪 K 线失败，降级到网易财经历史 CSV 接口（备用）
        df = self._try_netease_history(code, sina_code, days)
        if df is not None:
            return df

        # P1-10 修复：原逻辑返回空 DataFrame，不抛异常，
        # 导致 DataManager 无法感知"两源均失败"→ _on_failure_locked 永不触发
        # → 主源不会被切换到 eastmoney/akshare 兜底
        # 改为抛异常，让外层 _call_with_fallback 进入 except 分支计数并降级
        raise RuntimeError(f"SinaCollector 双源均不可用: {code} (新浪 K 线失败 + 网易 502)")

    def _try_sina_kline(self, code: str, exch_code: str, days: int) -> pd.DataFrame | None:
        """尝试新浪 K 线 JSON 接口（主历史源）。失败返回 None。"""
        # 新浪 K 线接口最多返回 200 条，取略多于 days 确保足够
        datalen = max(days + 10, 100)
        datalen = min(datalen, 200)
        url = SINA_KLINE_URL.format(exch_code=exch_code, datalen=datalen)
        try:
            client = self._get_client()
            resp = client.get(url, timeout=15.0)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"新浪 K 线 {code} 不可用: {e}")
            return None

        text = resp.text.strip()
        if not text:
            return None

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"新浪 K 线 JSON 解析失败 {code}: {e}")
            return None

        if not data:
            return None

        rows = []
        for item in data:
            try:
                rows.append(
                    {
                        "date": item["day"],
                        "open": float(item["open"]),
                        "high": float(item["high"]),
                        "low": float(item["low"]),
                        "close": float(item["close"]),
                        "volume": float(item.get("volume", 0)),
                        "amount": float(item.get("amount", 0)) if item.get("amount") else 0.0,
                    }
                )
            except (KeyError, ValueError, TypeError):
                continue

        if not rows:
            return None

        df = pd.DataFrame(rows)
        return self._normalize_history(df, code, days)

    def _try_netease_history(self, code: str, sina_code: str, days: int) -> pd.DataFrame | None:
        """尝试网易财经历史 CSV 接口（备用）。失败返回 None。"""
        # v0.9.12：网易 chddata 不支持北交所（bj）代码（实测 920xxx 恒 502），
        # 直接短路跳过，避免浪费一次 HTTP 请求并刷 WARNING 日志。
        if code.startswith(_BSE_PREFIXES):
            return None
        url = SINA_HISTORY_URL.format(
            sina_code=sina_code,
            start="20200101",
            end="21000101",
        )
        try:
            client = self._get_client()
            resp = client.get(
                url,
                headers={"Referer": "https://quotes.money.163.com/"},
                timeout=15.0,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"网易财经历史行情 {code} 不可用: {e}")
            return None

        if not resp.text.strip():
            return None

        try:
            df = pd.read_csv(StringIO(resp.text), encoding="gbk")
        except Exception:
            try:
                df = pd.read_csv(StringIO(resp.text), encoding="utf-8")
            except Exception as e:
                logger.warning(f"网易财经 CSV 解析失败 {code}: {e}")
                return None

        if df.empty:
            return None

        return self._normalize_history(df, code, days)

    def _normalize_history(self, df: pd.DataFrame, code: str, days: int) -> pd.DataFrame:
        """统一标准化历史行情 DataFrame。"""
        col_map = {
            "日期": "date",
            "股票代码": "code",
            "开盘价": "open",
            "最高价": "high",
            "最低价": "low",
            "收盘价": "close",
            "成交数量": "volume",
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
        # v0.9.10 修复：空列表 = 全市场快照（新浪 Market_Center 分页接口）。
        # 东财全市场快照被限流后（8/3 起 RemoteDisconnected），这是尾盘选股/自动选股的保底源。
        if not symbols:
            return self._fetch_full_market_snapshot()

        codes = [self.normalize_symbol(s) for s in symbols]
        sina_codes = ",".join(_to_sina_code(c) for c in codes)

        try:
            client = self._get_client()
            resp = client.get(SINA_REALTIME_URL.format(codes=sina_codes), timeout=15.0)
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
            _exchange = match.group(1)
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

            rows.append(
                {
                    "symbol": code,
                    "name": name,
                    "price": price,
                    "open": open_price,
                    "prev_close": prev_close,
                    "change_pct": round(change_pct, 2),
                    "volume": volume,
                    "amount": amount,
                }
            )

        return self._set_attrs(pd.DataFrame(rows) if rows else pd.DataFrame())

    # ── 全市场快照（v0.9.10：东财限流保底源）──
    _SINA_MARKET_URL = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1"
        "&node=hs_a&symbol=&_s_r_a=init"
    )
    _SINA_MARKET_PAGE_SIZE = 100
    _SINA_MARKET_MAX_PAGES = 80  # 5000+ 只股票，100/页

    def _fetch_full_market_snapshot(self) -> pd.DataFrame:
        """全市场快照（新浪 Market_Center 分页接口）。

        东财 clist 快照被限流（RemoteDisconnected）时，新浪是唯一可用保底源。
        字段：code/name/trade(现价)/changepercent/volume/amount/turnoverratio(换手率)/
        nmc(流通市值,元)/mktcap(总市值,元)/pb/per。兼容尾盘选股列映射。
        """
        rows: list[dict] = []
        client = self._get_client()
        pages_fetched = 0
        for page in range(1, self._SINA_MARKET_MAX_PAGES + 1):
            pages_fetched = page
            try:
                resp = client.get(
                    self._SINA_MARKET_URL.format(page=page),
                    headers={
                        "Referer": "https://finance.sina.com.cn",
                        "User-Agent": "Mozilla/5.0",
                    },
                    timeout=15.0,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("新浪全市场快照第 {} 页失败: {}", page, e)
                break
            if not data:
                break
            for item in data:
                try:
                    rows.append(
                        {
                            # 列名对齐 DataManager 实时行情约定：symbol/name/price/change_pct
                            # （selector 用 price，screener 方式3 rename 用 symbol/price/change_pct）
                            "symbol": str(item.get("code", "")),
                            "name": str(item.get("name", "")),
                            "price": float(item.get("trade", 0) or 0),
                            "change_pct": float(item.get("changepercent", 0) or 0),
                            "volume": float(item.get("volume", 0) or 0),
                            "amount": float(item.get("amount", 0) or 0),
                            "turnoverratio": float(item.get("turnoverratio", 0) or 0),
                            # 新浪 nmc/mktcap 单位=万元，选股器按元比较（min_mcap/1e8 转亿）→ ×1e4 转元
                            "circ_mv": float(item.get("nmc", 0) or 0) * 1e4,
                            "mktcap": float(item.get("mktcap", 0) or 0) * 1e4,
                            "pb": float(item.get("pb", 0) or 0) if item.get("pb") else None,
                            "per": float(item.get("per", 0) or 0) if item.get("per") else None,
                        }
                    )
                except (TypeError, ValueError):
                    continue
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        logger.info("新浪全市场快照: {} 只股票（分页 {} 页）", len(df), pages_fetched)
        return self._set_attrs(df)

    def fetch_sector_flow(self) -> pd.DataFrame:
        """新浪源不支持行业资金流，抛异常触发 DataManager 降级到 akshare。"""
        raise NotImplementedError("新浪源不支持行业资金流，请降级到 akshare")

    def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        """新浪源不支持个股资金流明细，抛异常触发 DataManager 降级到 akshare。"""
        raise NotImplementedError("新浪源不支持个股资金流明细，请降级到 akshare")
