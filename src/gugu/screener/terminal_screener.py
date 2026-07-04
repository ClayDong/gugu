"""尾盘选股器：14:30 扫描全市场，按"缩量回踩低吸"逻辑筛选。

P2-1 重设计（炒股专家审查）：
原逻辑筛选"涨幅 3%-5%"的股票 → 高位接盘，次日回调概率大。
改为陈江挺《炒股的智慧》低吸逻辑："强势股缩量回踩不破 MA20"。

8 条件（低吸逻辑）：
1. 时间 ≥ 14:30（调用方保证）
2. 当日涨幅 -2% ~ +1%（回踩而非追涨）
3. 前 5 日内有涨幅 ≥ 5% 的强势表现（有资金关注）
4. 当日缩量（量比 < 1.0，回踩缩量才是健康）
5. 收盘价 ≥ MA20（趋势未破）
6. MA20 > MA60（中期趋势向上）
7. 换手率 3%-15%（活跃但非过度投机）
8. 流通市值 50亿-500亿（避免小盘操纵 + 大盘滞涨）

数据源策略：优先使用 akshare 全市场快照，失败时降级到 SinaCollector。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

if TYPE_CHECKING:
    from gugu.data.manager import DataManager

logger = get_logger()

# ── 参数（P2-1 低吸逻辑）──────────────────────────────
MIN_CHG_PCT = -2.0  # 当日最低涨幅 %（回踩区间下限）
MAX_CHG_PCT = 1.0   # 当日最高涨幅 %（回踩区间上限）
RECENT_STRONG_PCT = 5.0  # 前 5 日内最高单日涨幅阈值 %
RECENT_STRONG_DAYS = 5   # 强势表现回看天数
MAX_VOL_RATIO = 1.0      # 量比上限（缩量）
MIN_TURNOVER = 3.0       # 最低换手率 %（活跃度）
MAX_TURNOVER = 15.0      # 最高换手率 %（避免过度投机）
MIN_MCAP = 5_000_000_000     # 最小流通市值 50亿
MAX_MCAP = 50_000_000_000    # 最大流通市值 500亿
MAX_CANDIDATES = 10          # 上报最多几只


@dataclass
class ScreeningResult:
    """单只股票的筛选结果。"""

    symbol: str
    name: str
    price: float
    change_pct: float  # 今日涨幅 %
    volume_ratio: float  # 量比
    turnover_pct: float  # 换手率 %
    mcap_billion: float  # 流通市值（亿）
    volume_trend: str  # 成交量趋势描述
    beats_index: bool  # 是否跑赢大盘
    late_session_high: bool  # 是否满足回踩支撑
    passed: bool  # 是否全部通过
    failed_conditions: list[str] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return 8 - len(self.failed_conditions)


class TerminalScreener:
    """尾盘选股器（P2-1 低吸逻辑）。

    Args:
        dm: DataManager 实例（可选，用于 akshare 失败时降级获取全市场快照）。
    """

    def __init__(self, dm: DataManager | None = None) -> None:
        self._dm = dm

    async def scan(self) -> list[ScreeningResult]:
        """执行全市场扫描，返回通过 8 条件的排序结果。"""
        logger.info("=== 尾盘选股: 开始全市场扫描（低吸逻辑）===")

        # 1. 获取全市场实时快照（优先 akshare，失败时降级到 SinaCollector）
        spot = await self._fetch_market_snapshot()
        if spot is None or spot.empty:
            logger.error("尾盘选股: 全市场快照获取失败（akshare + sina 均不可用）")
            return []

        logger.info(f"尾盘选股: 全市场 {len(spot)} 只股票")

        # 2. 条件2: 当日涨幅 -2% ~ +1%（回踩区间）
        spot["涨跌幅"] = pd.to_numeric(spot["涨跌幅"], errors="coerce").fillna(0)
        mask = (spot["涨跌幅"] >= MIN_CHG_PCT) & (spot["涨跌幅"] <= MAX_CHG_PCT)
        # 排除北交所、ST
        mask &= ~spot["代码"].astype(str).str.startswith("bj")
        mask &= ~spot["名称"].str.contains("ST|退|N|C", na=False)
        candidates = spot[mask].copy()
        logger.info(f"尾盘选股: 回踩区间{-MAX_CHG_PCT}~+{MAX_CHG_PCT}% {len(candidates)} 只")

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
            try:
                r = await asyncio.wait_for(
                    self._check_stock(code, name, price, chg_pct, today_vol, index_chg),
                    timeout=15,
                )
                if isinstance(r, ScreeningResult):
                    results.append(r)
            except TimeoutError:
                logger.warning(f"尾盘选股: {code} 超时，跳过")
            except Exception as e:
                logger.warning(f"尾盘选股: {code} 处理异常: {e}")
            if (idx + 1) % 50 == 0 or idx == total - 1:
                logger.info(f"尾盘选股: 进度 {idx + 1}/{total}")

        # 5. 排序：通过条件数降序 → 涨幅升序（回踩越深越优先）
        passed = [r for r in results if r.passed]
        partial = [r for r in results if not r.passed and len(r.failed_conditions) <= 3]
        partial.sort(key=lambda r: (-r.pass_count, r.change_pct))
        passed.sort(key=lambda r: r.change_pct)

        top = (passed + partial)[:MAX_CANDIDATES]
        logger.info(f"尾盘选股: 完全通过 {len(passed)} 只，上报 {len(top)} 只")
        return top

    async def _fetch_market_snapshot(self) -> pd.DataFrame | None:
        """获取全市场实时快照。优先 akshare，失败时降级到 DataManager。"""
        # 方式1: akshare 全市场快照
        try:
            import akshare as ak
            spot = await asyncio.to_thread(ak.stock_zh_a_spot)
            if spot is not None and not spot.empty:
                logger.info("尾盘选股: 使用 akshare 全市场快照")
                return spot
        except Exception as e:
            logger.warning(f"尾盘选股: akshare 全市场快照失败: {e}")

        # 方式2: DataManager 降级（使用 SinaCollector 实时行情）
        if self._dm is not None:
            try:
                spot = await self._dm.fetch_stock_realtime([])
                if spot is not None and not spot.empty:
                    logger.info(f"尾盘选股: 降级使用 DataManager 实时行情 ({len(spot)} 只)")
                    # 标准化列名以兼容后续处理
                    spot = spot.rename(columns={
                        "symbol": "代码", "name": "名称",
                        "price": "最新价", "change_pct": "涨跌幅",
                        "volume": "成交量",
                    })
                    return spot
            except Exception as e:
                logger.warning(f"尾盘选股: DataManager 降级也失败: {e}")

        return None

    async def _fetch_index_change(self) -> float:
        """获取上证指数今日涨幅（用于条件7简化版）。"""
        # 优先 akshare
        try:
            import akshare as ak
            df = await asyncio.to_thread(ak.stock_zh_index_daily, symbol="sh000001")
            if not df.empty and len(df) >= 2:
                today = df.iloc[-1]
                yesterday = df.iloc[-2]
                return float((today["close"] - yesterday["close"]) / yesterday["close"] * 100)
        except Exception as e:
            logger.debug(f"akshare 获取大盘涨幅失败: {e}")

        # 降级：DataManager 获取指数历史
        if self._dm is not None:
            try:
                df = await self._dm.fetch_stock_history("000001", days=5)
                if not df.empty and len(df) >= 2:
                    today = df.iloc[-1]
                    yesterday = df.iloc[-2]
                    return float(
                        (today["close"] - yesterday["close"]) / yesterday["close"] * 100
                    )
            except Exception as e:
                logger.debug(f"DataManager 获取大盘涨幅失败: {e}")
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
        """对单只候选股执行条件3-8检查（P2-1 低吸逻辑）。"""
        failed: list[str] = []
        try:
            # 获取日K线历史（优先 akshare，降级 DataManager）
            df = await self._fetch_stock_kline(code)
            if df is None or df.empty or len(df) < 60:
                return None

            # 需要至少 60 日数据用于计算 MA20/MA60
            recent = df.tail(60).copy()
            latest = recent.iloc[-1]

            # 换手率和流通股本（akshare 有这些字段，DataManager 可能没有）
            turnover_pct = float(latest.get("turnover", 0)) * 100
            outstanding = float(latest.get("outstanding_share", 0))
            if outstanding == 0:
                # 无法获取流通股本时，用成交额/价格估算
                outstanding = float(latest.get("volume", 0)) / max(
                    float(latest.get("turnover", 0.001)), 0.001
                ) if float(latest.get("turnover", 0)) > 0 else 1e8
            mcap = price * outstanding

            # 条件8: 流通市值 50亿-500亿
            mcap_b = mcap / 1e8
            if mcap_b < 5 or mcap_b > 500:
                failed.append(f"市值{mcap_b:.0f}亿")

            # 条件4: 当日缩量（量比 < 1.0）
            volumes = recent["volume"].values.astype(float)
            avg_5d = float(np.nanmean(volumes[-6:-1])) if len(volumes) > 5 else 0.0
            vol_ratio = volumes[-1] / avg_5d if avg_5d > 0 else 0
            if vol_ratio >= MAX_VOL_RATIO:
                failed.append(f"量比{vol_ratio:.2f}(未缩量)")

            # 条件7: 换手率 3%-15%
            if turnover_pct < MIN_TURNOVER or turnover_pct > MAX_TURNOVER:
                failed.append(f"换手率{turnover_pct:.1f}%")

            # 条件5: 收盘价 ≥ MA20（趋势未破）
            ma20 = recent["close"].rolling(20).mean().iloc[-1]
            if not pd.notna(ma20) or price < ma20:
                failed.append(f"破MA20({ma20:.2f})")

            # 条件6: MA20 > MA60（中期趋势向上）
            ma60 = recent["close"].rolling(60).mean().iloc[-1]
            ma20_rising = pd.notna(ma60) and ma20 > ma60
            if not ma20_rising:
                failed.append("MA20未上MA60")

            # 条件3: 前 5 日内有涨幅 ≥ 5% 的强势表现
            if len(recent) >= RECENT_STRONG_DAYS + 1:
                recent_closes = recent["close"].iloc[-(RECENT_STRONG_DAYS + 1):-1]
                recent_chg_pct = recent_closes.pct_change() * 100
                has_strong = (recent_chg_pct >= RECENT_STRONG_PCT).any()
            else:
                has_strong = False
            if not has_strong:
                failed.append(f"近{RECENT_STRONG_DAYS}日无强势")

            # 条件8: 个股表现不强于大盘（回踩日反而跑赢大盘 = 主力出货嫌疑）
            # 改为：回踩日不跑赢大盘（健康回踩）
            beats = chg_pct <= index_chg + 1.0  # 允许略超大盘 1%
            if not beats:
                failed.append(f"异常强势({chg_pct:+.1f}%)")

            # 成交量趋势描述（用于展示）
            vol_trend = self._check_volume_trend(volumes[-5:])

            # late_session_high 改为 "回踩支撑" 标志
            pullback_hold = pd.notna(ma20) and price >= ma20 * 0.98  # 回踩 MA20 附近 2% 内

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
                late_session_high=pullback_hold,
                passed=len(failed) == 0,
                failed_conditions=failed,
            )
            return result

        except Exception as e:
            logger.debug(f"尾盘选股 {code} 检查失败: {e}")
            return None

    def _check_volume_trend(self, vols: np.ndarray) -> str:
        """检查成交量趋势（P2-1 低吸逻辑：缩量更优）。

        返回: ✅ (缩量) / ⚠️ (持平) / ❌ (放量)
        """
        if len(vols) < 3:
            return "✅"
        # 计算近 3 日量能趋势
        recent_avg = float(np.nanmean(vols[-3:]))
        earlier_avg = float(np.nanmean(vols[:-3])) if len(vols) > 3 else float(vols[0])
        if earlier_avg == 0:
            return "✅"
        ratio = recent_avg / earlier_avg
        if ratio < 0.8:
            return "✅ 缩量"
        elif ratio < 1.2:
            return "⚠️ 持平"
        else:
            return "❌ 放量"

    async def _fetch_stock_kline(self, code: str) -> pd.DataFrame | None:
        """获取个股日K线历史（需 60 日数据用于 MA60）。优先 akshare，降级 DataManager。"""
        # 方式1: akshare 日K线（含换手率、流通股本）
        try:
            import akshare as ak
            prefix = _symbol_prefix(code)
            df = await asyncio.to_thread(ak.stock_zh_a_daily, symbol=prefix, adjust="qfq")
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.debug(f"akshare 日K线 {code} 失败: {e}")

        # 方式2: DataManager 降级
        if self._dm is not None:
            try:
                df = await self._dm.fetch_stock_history(code, days=120)
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                logger.debug(f"DataManager 日K线 {code} 失败: {e}")

        return None


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
