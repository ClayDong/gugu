"""大市过滤模块：基于指数200日均线判断牛熊状态。

根据《炒股的智慧》策略体系，"只在牛市/上升大市操作"是最高优先级铁律。
大市不满足顺势条件时，应关闭选股通道，仅保留现金观察。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import akshare as ak
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# 沪深300指数代码（akshare 格式：sh + 6 位代码）
_HS300_SYMBOL = "sh000300"

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

    async def check_market(self) -> dict[str, Any]:
        """检查大市状态。

        Returns:
            ``{"bullish": bool, "regime": "bull"/"bear"/"range",
            "reason": str, "index_close": float, "ma200": float}``

            其中 ``bullish`` 表示是否满足顺势条件（True 才允许选股/进场）。
            数据获取失败时返回 ``{"bullish": True, "regime": "unknown",
            "reason": "大市数据获取失败，默认放行"}``，不阻塞交易。
        """
        # 当日缓存：同一天内直接返回
        today = date.today()
        if self._cache_date == today and self._cache is not None:
            return self._cache

        try:
            df = await self.fetch_index_data(days=250)
            result = self._analyze(df)
        except Exception as e:
            # 数据获取失败不阻塞交易，默认放行并记录 warning
            logger.warning(f"大市数据获取失败，默认放行: {e}")
            result = {
                "bullish": True,
                "regime": "unknown",
                "reason": "大市数据获取失败，默认放行",
            }

        # 写入缓存
        self._cache_date = today
        self._cache = result
        return result

    async def fetch_index_data(self, days: int = 250) -> pd.DataFrame:
        """获取沪深300指数日 K 数据。

        使用 akshare 的 ``stock_zh_index_daily_em`` 接口获取沪深300日 K 线，
        列名统一为 date, open, high, low, close, volume，按日期升序排列。

        Args:
            days: 获取的交易日天数，默认 250（约一年）。

        Returns:
            包含 date, open, high, low, close, volume 列的 DataFrame，
            按日期升序排列。列名映射兼容中英文原始列名。
        """
        # akshare 的 stock_zh_index_daily_em 返回全量历史日 K
        raw = ak.stock_zh_index_daily_em(symbol=_HS300_SYMBOL)

        if raw is None or raw.empty:
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
        # 数据不足时默认放行，避免误伤
        if df is None or df.empty or len(df) < _MA200:
            logger.warning(
                f"指数数据不足（需 {_MA200} 行，实际 {len(df) if df is not None else 0} 行），默认放行"
            )
            return {
                "bullish": True,
                "regime": "unknown",
                "reason": "大市数据不足，默认放行",
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

        if satisfied_count == 3:
            regime = "bull"
            bullish = True
        elif satisfied_count == 0:
            regime = "bear"
            bullish = False
        else:
            regime = "range"
            bullish = False

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
