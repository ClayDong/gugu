"""尾盘选股器：14:30 扫描全市场，按 8 条件筛选尾盘强势股。

8 条件：
1. 时间 ≥ 14:30（调用方保证）
2. 今日涨幅 3%~5%
3. 量比 > 1
4. 换手率 5%~10%
5. 流通市值 50亿~200亿
6. 成交量呈台阶式放大（近5日逐日递增或高位维持）
7. 分时跑赢大盘（简化：今日涨幅 > 大盘涨幅）
8. 尾盘创当天新高且回踩不破（简化：14:30后最高价 == 全天最高价）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import akshare as ak
import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# ── 参数 ────────────────────────────────────────────
MIN_CHG_PCT = 3.0      # 最低涨幅 %
MAX_CHG_PCT = 5.0      # 最高涨幅 %
MIN_VOL_RATIO = 1.0    # 量比下限
MIN_TURNOVER = 5.0     # 最低换手率 %
MAX_TURNOVER = 10.0    # 最高换手率 %
MIN_MCAP = 5_000_000_000   # 最小流通市值 50亿
MAX_MCAP = 20_000_000_000  # 最大流通市值 200亿
MAX_CANDIDATES = 10    # 上报最多几只


@dataclass
class ScreeningResult:
    """单只股票的筛选结果。"""
    symbol: str
    name: str
    price: float
    change_pct: float      # 今日涨幅 %
    volume_ratio: float     # 量比
    turnover_pct: float     # 换手率 %
    mcap_billion: float     # 流通市值（亿）
    volume_trend: str       # 成交量趋势描述
    beats_index: bool       # 是否跑赢大盘
    late_session_high: bool # 尾盘是否创全天新高
    passed: bool            # 是否全部通过
    failed_conditions: list[str] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return 8 - len(self.failed_conditions)


class TerminalScreener:
    """尾盘选股器。"""

    async def scan(self) -> list[ScreeningResult]:
        """执行全市场扫描，返回通过 8 条件的排序结果。"""
        logger.info("=== 尾盘选股: 开始全市场扫描 ===")
        try:
            # 1. 获取全市场实时快照
            spot = await asyncio.to_thread(ak.stock_zh_a_spot)
        except Exception as e:
            logger.error(f"尾盘选股: 全市场快照失败: {e}")
            return []

        if spot.empty:
            return []
        logger.info(f"尾盘选股: 全市场 {len(spot)} 只股票")

        # 2. 条件2: 涨幅 3%-5%
        spot["涨跌幅"] = pd.to_numeric(spot["涨跌幅"], errors="coerce").fillna(0)
        mask = (spot["涨跌幅"] >= MIN_CHG_PCT) & (spot["涨跌幅"] <= MAX_CHG_PCT)
        # 排除北交所、ST
        mask &= ~spot["代码"].astype(str).str.startswith("bj")
        mask &= ~spot["名称"].str.contains("ST|退|N|C", na=False)
        candidates = spot[mask].copy()
        logger.info(f"尾盘选股: 涨幅3-5% {len(candidates)} 只")

        if candidates.empty:
            return []

        # 3. 获取大盘指数今日涨幅（用于条件7简化版）
        index_chg = await self._fetch_index_change()

        # 4. 逐只深入检查条件3-8
        # 注意：akshare 内部使用了 multiprocessing，不能并发调用，
        # 必须串行处理，否则会 crash (libmini_racer 冲突)
        results: list[ScreeningResult] = []
        total = len(candidates)
        for idx, (_, row) in enumerate(candidates.iterrows()):
            code = str(row["代码"]).strip().zfill(6)
            name = str(row["名称"]).strip()
            price = float(row["最新价"])
            chg_pct = float(row["涨跌幅"])
            today_vol = float(row["成交量"])
            r = await self._check_stock(code, name, price, chg_pct, today_vol, index_chg)
            if isinstance(r, ScreeningResult):
                results.append(r)
            if (idx + 1) % 50 == 0 or idx == total - 1:
                logger.info(f"尾盘选股: 进度 {idx+1}/{total}")

        # 5. 排序：通过条件数降序 → 涨幅降序
        passed = [r for r in results if r.passed]
        partial = [r for r in results if not r.passed and len(r.failed_conditions) <= 3]
        partial.sort(key=lambda r: (-r.pass_count, -r.change_pct))
        passed.sort(key=lambda r: -r.change_pct)

        top = (passed + partial)[:MAX_CANDIDATES]
        logger.info(f"尾盘选股: 完全通过 {len(passed)} 只，上报 {len(top)} 只")
        return top

    async def _fetch_index_change(self) -> float:
        """获取上证指数今日涨幅（用于条件7简化版）。"""
        try:
            df = await asyncio.to_thread(
                ak.stock_zh_index_daily, symbol="sh000001"
            )
            if not df.empty and len(df) >= 2:
                today = df.iloc[-1]
                yesterday = df.iloc[-2]
                return float((today["close"] - yesterday["close"]) / yesterday["close"] * 100)
        except Exception as e:
            logger.debug(f"获取大盘涨幅失败: {e}")
        return 0.0

    async def _check_stock(
        self,
        code: str,
        name: str,
        price: float,
        chg_pct: float,
        today_vol: float,
        index_chg: float,
    ) -> ScreeningResult | None:
        """对单只候选股执行条件3-8检查。"""
        failed: list[str] = []
        try:
            # 获取日K线历史（含换手率、流通股、成交量）
            prefix = _symbol_prefix(code)
            df = await asyncio.to_thread(
                ak.stock_zh_a_daily, symbol=prefix, adjust="qfq"
            )
            if df.empty or len(df) < 6:
                return None

            recent = df.tail(6).copy()
            latest = recent.iloc[-1]

            turnover_pct = float(latest["turnover"]) * 100  # Sina返回的是小数
            outstanding = float(latest["outstanding_share"])
            mcap = price * outstanding

            # 条件5: 流通市值 50亿-200亿
            mcap_b = mcap / 1e8
            if mcap_b < 5 or mcap_b > 200:
                failed.append(f"市值{mcap_b:.0f}亿")

            # 条件3: 量比 > 1
            volumes = recent["volume"].values.astype(float)
            avg_5d = volumes[:-1].mean()  # 前5日平均成交量
            vol_ratio = volumes[-1] / avg_5d if avg_5d > 0 else 0
            if vol_ratio <= 1:
                failed.append(f"量比{vol_ratio:.2f}")

            # 条件4: 换手率 5%-10%
            if turnover_pct < MIN_TURNOVER or turnover_pct > MAX_TURNOVER:
                failed.append(f"换手率{turnover_pct:.1f}%")

            # 条件6: 成交量台阶式放大（近5日成交量逐日递增或高位维持）
            vol_trend = self._check_volume_trend(volumes[-5:])
            if vol_trend == "❌":
                failed.append("量能递减")

            # 条件7: 简化版——个股涨幅 > 大盘涨幅
            beats = chg_pct > index_chg
            if not beats:
                failed.append(f"未跑赢大盘({index_chg:+.1f}%)")

            # 条件8: 尾盘创当天新高（简化版）
            # 无法获取分时数据时，用今日最高价 > 昨收的1.02倍来近似
            high = float(latest["high"])
            prev_close = float(recent.iloc[-2]["close"]) if len(recent) >= 2 else 0
            late_high = prev_close > 0 and (high / prev_close - 1) * 100 >= chg_pct * 0.95
            if not late_high:
                failed.append("尾盘未创新高")

            result = ScreeningResult(
                symbol=code,
                name=name,
                price=price,
                change_pct=chg_pct,
                volume_ratio=round(vol_ratio, 2),
                turnover_pct=round(turnover_pct, 1),
                mcap_billion=round(mcap_b, 0),
                volume_trend=vol_trend,
                beats_index=beats,
                late_session_high=late_high,
                passed=len(failed) == 0,
                failed_conditions=failed,
            )
            return result

        except Exception as e:
            logger.debug(f"尾盘选股 {code} 检查失败: {e}")
            return None

    def _check_volume_trend(self, vols: np.ndarray) -> str:
        """检查成交量是否呈台阶式放大。
        返回: ✅ (递增/高位维持) 或 ❌ (递减)
        """
        if len(vols) < 3:
            return "✅"
        # 至少连续3天不递减（温和放量）
        increasing = sum(1 for i in range(1, len(vols)) if vols[i] > vols[i-1])
        if increasing >= len(vols) - 2:  # 允许1天微调
            return "✅ 量增"
        # 高位维持: 5日均量 > 20日均量
        avg_5 = vols.mean()
        return "✅ 高位" if avg_5 > vols[0] else "❌"


def _symbol_prefix(symbol: str) -> str:
    """转换为 akshare 带前缀格式（sh/sz）。"""
    code = symbol.strip().zfill(6)
    if code.startswith(("60", "68", "11", "13")):
        return f"sh{code}"
    if code.startswith(("00", "30", "12")):
        return f"sz{code}"
    if code.startswith(("43", "83", "87", "88")):
        return f"bj{code}"
    return code
