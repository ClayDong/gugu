"""akshare 采集器（降级源）。

akshare 封装东方财富数据，通过 settings.yaml 中 fallback_sources 配置。
当主源 sina 连续失败达阈值时切换到此采集器。
注意：akshare 在部分网络环境下可能被封锁。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from gugu.data.collectors.base import BaseCollector
from gugu.data.quality import (
    validate_sector_flow,
    validate_stock_flow,
    validate_stock_history,
)
from gugu.utils.log import get_logger

logger = get_logger()


class AkshareCollector(BaseCollector):
    """akshare 主采集器。"""

    source = "akshare"
    adjust_type = "qfq"  # 前复权

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_stock_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
        code = self.normalize_symbol(symbol)
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        try:
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"
            )
        except Exception as e:
            logger.error(f"akshare 获取 {code} 历史失败: {e}")
            raise

        if df.empty:
            return df

        # 统一列名
        df = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "amount",
                "涨跌幅": "change_pct",
            }
        )
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.tail(days).reset_index(drop=True)
        return validate_stock_history(df, code)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        """获取个股实时行情（全市场快照后过滤）。

        2026-08-11 修复：接入资金流熔断（v0.9.9 熔断的同类遗漏——实时行情方法漏接）。
        ak.stock_zh_a_spot_em 全市场快照对东财限流最敏感（RemoteDisconnected），
        主源降级窗口内每轮 fallback 并发调用失败 3 次 retry，15 分钟刷 228 条 ERROR
        （v0.9.9 单日 1705 次事故同款）。熔断后冷却期静默返回空，DataManager
        识别 flow_breaker_active attrs 短路（不记 all 假失败）。
        """
        if self._flow_breaker_active():
            return self._flow_breaker_guard("实时行情")
        try:
            df = ak.stock_zh_a_spot_em()
        except Exception as e:
            logger.error(f"akshare 实时行情失败: {e}")
            self._flow_record_failure()
            raise
        self._flow_record_success()

        if symbols:
            codes = {self.normalize_symbol(s) for s in symbols}
            df = df[df["代码"].isin(codes)].copy()
        if df.empty:
            return pd.DataFrame(
                columns=["symbol", "name", "price", "change_pct", "volume", "amount"]
            )
        df = df.rename(
            columns={
                "代码": "symbol",
                "名称": "name",
                "最新价": "price",
                "涨跌幅": "change_pct",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
        return df[["symbol", "name", "price", "change_pct", "volume", "amount"]].reset_index(
            drop=True
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_sector_flow(self) -> pd.DataFrame:
        """获取行业资金流排名（东方财富）。"""
        if self._flow_breaker_active():
            return self._flow_breaker_guard("行业资金流")
        try:
            df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        except Exception as e:
            logger.error(f"akshare 行业资金流失败: {e}")
            self._flow_record_failure()
            raise
        self._flow_record_success()

        if df.empty:
            return df

        df = df.rename(
            columns={
                "名称": "sector",
                "主力净流入-净额": "main_net",
                "主力净流入-净占比": "main_pct",
                "超大单净流入-净额": "super_large_net",
                "大单净流入-净额": "large_net",
                "涨跌幅": "change_pct",
            }
        )
        # 单位转换：akshare 返回元，统一转为亿元（与 eastmoney_collector 对齐）
        for col in ("main_net", "super_large_net", "large_net"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce") / 1e8
        return validate_sector_flow(df)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        """获取个股资金流明细（近 60 日）。"""
        if self._flow_breaker_active():
            return self._flow_breaker_guard(f"个股资金流 {symbol}")
        code = self.normalize_symbol(symbol)
        try:
            df = ak.stock_individual_fund_flow(stock=code, market=_market(code))
        except Exception as e:
            logger.error(f"akshare 个股资金流 {code} 失败: {e}")
            self._flow_record_failure()
            raise
        self._flow_record_success()

        if df.empty:
            return df

        df = df.rename(
            columns={
                "日期": "date",
                "主力净流入-净额": "main_net",
                "主力净流入-净占比": "main_pct",
                "超大单净流入-净额": "super_large_net",
                "大单净流入-净额": "large_net",
                "中单净流入-净额": "medium_net",
                "小单净流入-净额": "small_net",
            }
        )
        # 单位转换：akshare 返回元，统一转为亿元（与 eastmoney_collector 对齐）
        for col in ("main_net", "super_large_net", "large_net", "medium_net", "small_net"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce") / 1e8
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return validate_stock_flow(df, code)


def _market(code: str) -> str:
    """akshare 个股资金流需要 market 参数：sh/sz/bj。"""
    if code.startswith(("60", "68")):
        return "sh"
    if code.startswith(("00", "30")):
        return "sz"
    if code.startswith(("43", "83", "87", "88")):
        return "bj"
    return "sh"
