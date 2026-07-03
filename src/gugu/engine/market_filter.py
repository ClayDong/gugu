"""大市过滤模块：基于指数200日均线判断牛熊状态。

根据《炒股的智慧》策略体系，"只在牛市/上升大市操作"是最高优先级铁律。
大市不满足顺势条件时，应关闭选股通道，仅保留现金观察。

数据源（三级冗余，保证健壮性）：
1. akshare 的 stock_zh_index_daily_em（东方财富后端，主源）
2. 新浪 K 线 JSON 接口（备用1，akshare 失败时自动降级）
3. 东方财富 push2 API 直连 HTTP（备用2，前两级均失败时兜底）
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import akshare as ak
import httpx
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# 沪深300指数代码（akshare 格式：sh + 6 位代码）
_HS300_SYMBOL = "sh000300"
# 东方财富 secid 格式：1.000300（沪市指数）
_HS300_SECID = "1.000300"
# 新浪 K 线接口（指数日K，scale=240 表示日K）
_SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
    "?symbol={symbol}&scale=240&datalen={datalen}"
)

# 200 日均线周期
_MA200 = 200
# 近期趋势观察窗口（约 2 个月交易日）
_TREND_WINDOW = 40
# 短期成交量均线周期
_VOL_SHORT = 20
# 长期成交量均线周期（约一年）
_VOL_LONG = 252


class MarketFilter:
    """大市过滤器：基于沪深300指数判断牛熊状态。

    判断逻辑（三重验证）：
    1. 指数收盘价 > 200 日均线 → 长期趋势向上（顺势加分）
    2. 近 40 个交易日（约 2 个月）低点抬高、高点抬高 → 中期趋势向上加分
    3. 近 20 日均量 > 近 252 日均量 → 流动性验证

    三个条件全部满足 → bull；否则 → bear/range。

    当日缓存：大市状态日内不变，同一天内重复调用直接返回缓存结果，
    缓存写入实例变量 ``_cache`` 与 ``_cache_date``。
    """

    def __init__(self) -> None:
        # 当日缓存：同一天内只查询一次
        self._cache: dict[str, Any] | None = None
        self._cache_date: date | None = None

    def _fetch_index_sina(self, days: int = 250) -> pd.DataFrame | None:
        """新浪 K 线 JSON 接口获取沪深300日K线（备用通道1）。

        通过 ``CN_MarketData.getKLineData`` 接口获取指数日K，scale=240 表示日K。
        接口最大返回 datalen 条数据，为满足 MA200 需要至少 250 条。
        """
        datalen = max(days + 20, 300)  # 多取一些确保足够
        url = _SINA_KLINE_URL.format(symbol=_HS300_SYMBOL, datalen=datalen)
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://finance.sina.com.cn/",
        }
        try:
            with httpx.Client(timeout=15.0, headers=headers) as client:
                resp = client.get(url)
                resp.raise_for_status()
                text = resp.text.strip()
            if not text:
                logger.warning("新浪大市 K 线 API 返回空")
                return None

            data = json.loads(text)
            if not data:
                logger.warning("新浪大市 K 线 API 返回空列表")
                return None

            rows: list[dict[str, Any]] = []
            for item in data:
                try:
                    rows.append({
                        "date": item["day"],
                        "open": float(item["open"]),
                        "high": float(item["high"]),
                        "low": float(item["low"]),
                        "close": float(item["close"]),
                        "volume": float(item.get("volume", 0)),
                    })
                except (KeyError, ValueError, TypeError):
                    continue

            if not rows:
                return None
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"])
            df = df.sort_values("date").reset_index(drop=True)
            return df
        except Exception as e:
            logger.warning(f"新浪大市 K 线 API 失败: {e}")
            return None

    def _fetch_index_eastmoney(self, days: int = 250) -> pd.DataFrame | None:
        """东方财富 push2 API 直连获取沪深300日K线（备用通道2，最后兜底）。

        通过 kline 接口获取历史日K数据。当 akshare 和新浪均失败时使用。
        """
        url = "https://push2.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": _HS300_SECID,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",  # 101=日K
            "fqt": "0",   # 0=不复权（指数无需复权）
            "end": "20500101",
            "lmt": str(max(days * 2, 500)),
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        }
        try:
            with httpx.Client(timeout=15.0, headers=headers) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            if not data or not data.get("data") or not data["data"].get("klines"):
                logger.warning("东方财富大市 HTTP API 返回空数据")
                return None

            klines = data["data"]["klines"]
            rows: list[dict[str, Any]] = []
            for line in klines:
                # 格式: 日期,开盘,收盘,最高,最低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                try:
                    rows.append({
                        "date": parts[0],
                        "open": float(parts[1]),
                        "close": float(parts[2]),
                        "high": float(parts[3]),
                        "low": float(parts[4]),
                        "volume": float(parts[5]),
                    })
                except (ValueError, IndexError):
                    continue

            if not rows:
                return None
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"])
            df = df.sort_values("date")
            return df
        except Exception as e:
            logger.warning(f"东方财富大市 HTTP API 失败: {e}")
            return None

    async def check_market(self) -> dict[str, Any]:
        """检查大市状态。

        Returns:
            ``{"bullish": bool, "regime": "bull"/"bear"/"range",
            "reason": str, "index_close": float, "ma200": float}``

            其中 ``bullish`` 表示是否满足顺势条件（True 才允许选股/进场）。

            **fail-safe 策略**（《炒股的智慧》铁律——只在牛市操作）：
            数据获取失败时返回 ``{"bullish": False, "regime": "unknown",
            "reason": "大市数据获取失败，fail-safe 拒绝"}``，
            保守不进场，不缓存以便后续重试。
        """
        # 当日缓存：同一天内直接返回
        today = date.today()
        if self._cache_date == today and self._cache is not None:
            return self._cache

        try:
            df = await self.fetch_index_data(days=250)
            result = self._analyze(df)
            # 仅缓存成功结果，失败不缓存以便后续重试
            self._cache_date = today
            self._cache = result
        except Exception as e:
            # fail-safe：《炒股的智慧》铁律——只在牛市操作。
            # 无法确认大市状态时保守不进场，但不缓存以便后续重试。
            logger.warning(f"大市数据获取失败，fail-safe 拒绝选股: {e}")
            result = {
                "bullish": False,
                "regime": "unknown",
                "reason": f"大市数据获取失败，fail-safe 拒绝: {e}",
            }
        return result

    async def fetch_index_data(self, days: int = 250) -> pd.DataFrame:
        """获取沪深300指数日 K 数据（三级冗余，保证健壮性）。

        通道1：akshare 的 ``stock_zh_index_daily_em``（东方财富后端，主源）
        通道2：新浪 K 线 JSON 接口（备用1，akshare 失败时降级）
        通道3：东方财富 push2 API 直连 HTTP（备用2，前两级均失败时兜底）

        Args:
            days: 获取的交易日天数，默认 250（约一年）。

        Returns:
            包含 date, open, high, low, close, volume 列的 DataFrame，
            按日期升序排列。列名映射兼容中英文原始列名。
        """
        # 通道1：akshare
        raw: pd.DataFrame | None = None
        try:
            raw = ak.stock_zh_index_daily_em(symbol=_HS300_SYMBOL)
            if raw is not None and not raw.empty:
                logger.info(f"大市数据(akshare): {len(raw)} 条K线")
        except Exception as e:
            logger.warning(f"akshare 大市数据获取失败: {e}")

        # 通道2：新浪 K 线 JSON 接口（akshare 失败时降级）
        if raw is None or raw.empty:
            logger.info("akshare 大市数据不可用，降级到新浪 K 线接口...")
            raw = self._fetch_index_sina(days)
            if raw is not None and not raw.empty:
                logger.info(f"大市数据(新浪): {len(raw)} 条K线")

        # 通道3：东方财富 push2 API 直连（前两级均失败时最后兜底）
        if raw is None or raw.empty:
            logger.info("新浪大市数据不可用，最后兜底到东方财富 HTTP 直连...")
            raw = self._fetch_index_eastmoney(days)
            if raw is not None and not raw.empty:
                logger.info(f"大市数据(东方财富): {len(raw)} 条K线")

        if raw is None or raw.empty:
            logger.error("大市数据获取失败（akshare + 新浪 + 东方财富 三通道均不可用）")
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        # 列名映射：兼容中英文列名，统一为目标列名
        column_map = {
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            # 英文列名直接保留
            "date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        }
        df = raw.rename(columns=column_map)

        # 仅保留需要的列，缺失列补 NA 以保证结构一致
        needed = ["date", "open", "high", "low", "close", "volume"]
        for col in needed:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[needed]

        # 日期解析并按日期升序排列
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values("date").reset_index(drop=True)

        # 截取最近 days 个交易日
        if days > 0 and len(df) > days:
            df = df.tail(days).reset_index(drop=True)

        return df

    def _analyze(self, df: pd.DataFrame) -> dict[str, Any]:
        """根据指数行情数据计算大市状态。

        判断逻辑（三重验证）：
        1. 指数收盘价 > 200 日均线 → bullish 加分
        2. 近 40 个交易日低点抬高、高点抬高 → 趋势向上加分
        3. 近 20 日均量 > 近 252 日均量 → 流动性验证

        三个条件全部满足 → bull；全部不满足 → bear；部分满足 → range。
        """
        # 数据不足时 fail-safe 拒绝（保守不进场），避免误伤
        if df is None or df.empty or len(df) < _MA200:
            logger.warning(
                f"指数数据不足（需 {_MA200} 行，实际 {len(df) if df is not None else 0} 行），fail-safe 拒绝"
            )
            return {
                "bullish": False,
                "regime": "unknown",
                "reason": "大市数据不足，fail-safe 拒绝",
                "index_close": 0.0,
                "ma200": 0.0,
            }

        close = df["close"]
        volume = df["volume"]
        index_close = float(close.iloc[-1])

        # 计算 200 日均线
        ma200 = float(close.rolling(window=_MA200).mean().iloc[-1])

        # 条件1：收盘价 > 200 日均线
        cond_above_ma = index_close > ma200

        # 条件2：近 40 个交易日低点抬高、高点抬高
        # 将窗口拆成前后两段，比较后段与前段的低点/高点
        cond_trend_up = False
        if len(df) >= _TREND_WINDOW:
            recent = df.tail(_TREND_WINDOW)
            half = _TREND_WINDOW // 2
            first_half = recent.head(half)
            second_half = recent.tail(half)
            first_low = float(first_half["low"].min())
            second_low = float(second_half["low"].min())
            first_high = float(first_half["high"].max())
            second_high = float(second_half["high"].max())
            # 低点抬高 且 高点抬高 → 趋势向上
            cond_trend_up = second_low > first_low and second_high > first_high

        # 条件3：近 20 日均量 > 近 252 日均量（流动性验证）
        cond_liquidity = False
        if len(df) >= _VOL_LONG:
            vol_short = float(volume.tail(_VOL_SHORT).mean())
            vol_long = float(volume.tail(_VOL_LONG).mean())
            cond_liquidity = vol_short > vol_long
        elif len(df) >= _VOL_SHORT:
            # 数据不足 252 日时，用全部可用数据作为长期均量的近似
            vol_short = float(volume.tail(_VOL_SHORT).mean())
            vol_long = float(volume.mean())
            cond_liquidity = vol_short > vol_long

        # 三重验证：全部满足才判定为 bull
        satisfied = [cond_above_ma, cond_trend_up, cond_liquidity]
        satisfied_count = sum(satisfied)

        # v0.4.0：放宽选股门槛 —— 只要指数在 MA200 之上（长期趋势向上）就允许选股
        # 原逻辑要求 3 条件全满足才 bull，导致震荡市（range）也禁止选股，选不出股票
        # 陈江挺原意："大市不好时不选股"指的是熊市（指数 < MA200），而非震荡市
        if cond_above_ma:
            regime = "bull" if satisfied_count == 3 else "range"
            bullish = True  # 在 MA200 之上即允许选股
        elif satisfied_count == 0:
            regime = "bear"
            bullish = False
        else:
            regime = "range"
            bullish = False  # 在 MA200 之下但部分条件满足，仍保守不选股

        # 构建可读的判断理由
        reason_parts = [
            f"收盘价{index_close:.2f}{'>' if cond_above_ma else '<'}MA200({ma200:.2f})",
            f"近{_TREND_WINDOW}日趋势{'向上' if cond_trend_up else '未确认向上'}",
            f"流动性{'验证通过' if cond_liquidity else '验证未通过'}",
        ]
        reason = "；".join(reason_parts) + f" → {regime}"

        return {
            "bullish": bullish,
            "regime": regime,
            "reason": reason,
            "index_close": round(index_close, 4),
            "ma200": round(ma200, 4),
        }
