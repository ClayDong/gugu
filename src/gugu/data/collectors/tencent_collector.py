"""腾讯财经直连采集器（第四备用源）。

通过 ifzq.gtimg.cn 直连腾讯财经 API，提供稳定的 qfq 日 K 数据。
在新浪/东方财富/akshare 均失败时作为最后兜底数据源。

特点：
- 稳定性高：腾讯 ifzq.gtimg.cn 接口长期可用，是本机最稳定的 qfq 日 K 源
- 前复权数据：返回 qfq（前复权）日 K，与 eastmoney/akshare 一致
- 重试机制（2次，指数退避）
- 数据校验（价格>0、字段完整）

注意：
- 仅支持历史 K 线（日 K），不支持实时行情/资金流（抛 NotImplementedError 触发降级）
- 腾讯代码格式：sh600519 / sz000001 / bj430047
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime
from typing import Any

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from gugu.data.collectors.base import BaseCollector
from gugu.data.quality import validate_stock_history
from gugu.utils.log import get_logger

logger = get_logger()

# 腾讯数据源标识
_DATA_SOURCE = "tencent"
_ADJUST_TYPE = "qfq"

# 腾讯 K 线接口
_TENCENT_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"

# 代码前缀 → 腾讯前缀
# 6/9 开头为沪市（sh），0/3 开头为深市（sz），4/8 开头为北交所（bj）
_TENCENT_PREFIX: dict[str, str] = {
    "6": "sh",  # 沪市主板
    "9": "sh",  # 沪市B股
    "0": "sz",  # 深市主板
    "3": "sz",  # 创业板
    "4": "bj",  # 北交所
    "8": "bj",  # 北交所
}

# 复用 SSL 上下文（避免重复创建）
_UNVERIFIED_CTX = ssl._create_unverified_context()


def _to_tencent_code(code: str) -> str:
    """股票代码 → 腾讯带前缀格式（如 sh600519）。

    v0.9.7 P0：指数代码自动识别交易所（000300 沪深300 → sh000300）。
    """
    # 显式 sh/sz 前缀（调用方指定交易所，最高优先级）
    if code[:2].lower() in ("sh", "sz", "bj") and code[2:].isdigit():
        return code
    if code in BaseCollector.SH_INDEX_CODES:
        return f"sh{code}"
    if code in BaseCollector.SZ_INDEX_CODES:
        return f"sz{code}"
    prefix = _TENCENT_PREFIX.get(code[0], "sz")
    return f"{prefix}{code}"


class TencentCollector(BaseCollector):
    """腾讯财经直连采集器（第四备用源，qfq 日 K 兜底）。

    通过 ifzq.gtimg.cn 接口获取前复权日 K 数据。
    仅实现 fetch_stock_history，其他方法抛 NotImplementedError 触发降级。
    """

    source = "tencent"
    adjust_type = "qfq"  # 前复权

    def __init__(self) -> None:
        pass

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    def fetch_stock_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        """获取个股历史 K 线（腾讯 qfq 日 K）。

        Args:
            symbol: 股票代码，如 "600519"
            days: 获取近 N 个交易日

        Returns:
            DataFrame[date, open, high, low, close, volume, amount]
        """
        code = self.normalize_symbol(symbol)
        tencent_code = _to_tencent_code(code)

        # 腾讯接口参数：param=<code>,day,,,<days>,qfq
        url = f"{_TENCENT_KLINE_URL}?param={tencent_code},day,,,{days},qfq"

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Referer": "http://finance.qq.com/",
                },
            )
            with urllib.request.urlopen(req, context=_UNVERIFIED_CTX, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"腾讯 K 线 {code} 请求失败: {e}")
            raise

        node = data.get("data", {}).get(tencent_code, {})
        # qfqday = 前复权日 K，day = 不复权日 K
        kline = node.get("qfqday") or node.get("day") or []
        if not kline:
            logger.warning(f"腾讯 K 线 {code} 返回空数据")
            return pd.DataFrame()

        rows: list[dict[str, Any]] = []
        for row in kline:
            if len(row) < 6:
                continue
            try:
                date_str = row[0]
                open_ = float(row[1])
                close = float(row[2])
                high = float(row[3])
                low = float(row[4])
                volume = float(row[5])
                # 腾讯接口 amount 字段可能缺失，用 volume × close × 单位因子 近似。
                # 注意：腾讯 K 线 volume 单位不统一（主板sh6/sz0/sz3=手×100，科创板sh688=股×1）。
                # 直接用 ×100 会导致科创板 amount 高估 100 倍，进而污染换手率计算。
                # 用代码前缀识别：68/688 开头为科创板(股单位)，其余为手单位。
                vol_factor = 1 if tencent_code.startswith(("sh688", "sh689", "bj")) else 100
                amount = float(row[6]) if len(row) > 6 and row[6] else volume * close * vol_factor
                rows.append(
                    {
                        "date": date_str,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "amount": amount,
                    }
                )
            except (ValueError, IndexError, TypeError) as e:
                logger.debug(f"腾讯 K 线 {code} 解析行失败: {e}")
                continue

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        df = df.sort_values("date")
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        df = df.tail(days).reset_index(drop=True)

        # 设置数据源元数据
        df.attrs["data_source"] = _DATA_SOURCE
        df.attrs["adjust_type"] = _ADJUST_TYPE
        df.attrs["fetch_timestamp"] = datetime.now().isoformat()

        logger.info(f"腾讯 K 线 {code} 获取成功 {len(df)} 行 (qfq)")
        return validate_stock_history(df, code)

    def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        """腾讯源不支持实时行情快照，抛异常触发 DataManager 降级。"""
        raise NotImplementedError("腾讯源不支持实时行情快照，请降级到 sina/eastmoney")

    def fetch_sector_flow(self) -> pd.DataFrame:
        """腾讯源不支持行业资金流，抛异常触发 DataManager 降级。"""
        raise NotImplementedError("腾讯源不支持行业资金流，请降级到 akshare/eastmoney")

    def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        """腾讯源不支持个股资金流明细，抛异常触发 DataManager 降级。"""
        raise NotImplementedError("腾讯源不支持个股资金流明细，请降级到 akshare/eastmoney")
