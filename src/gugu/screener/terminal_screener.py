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

数据源策略：优先 stock_zh_a_spot_em（含所有字段），降级 stock_zh_a_spot 或 SinaCollector。

P0 性能修复（迭代 11）：
- 快照硬过滤：用快照已有的 量比/换手率/流通市值 字段先过滤，640 → 50-100 只
- 并发获取 K 线：Semaphore(5) + gather，5x 加速
- 总超时控制：20 分钟硬上限，超时返回已扫描结果
- HTTP 失败熔断：连续 5 只失败时中止该批
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import akshare as ak
import numpy as np
import pandas as pd

from gugu.filters.market_regime import MarketRegimeDetector
from gugu.screener.params import ScreenerParams
from gugu.utils.log import get_logger

if TYPE_CHECKING:
    from gugu.data.manager import DataManager

logger = get_logger()

# ── 参数（P2-1 低吸逻辑）──────────────────────────────
MIN_CHG_PCT = -2.0
MAX_CHG_PCT = 1.0
RECENT_STRONG_PCT = 5.0
RECENT_STRONG_DAYS = 5
MAX_VOL_RATIO = 1.0
MIN_TURNOVER = 3.0
MAX_TURNOVER = 15.0
MIN_MCAP = 5_000_000_000
MAX_MCAP = 50_000_000_000
MAX_CANDIDATES = 10

# P0 性能参数
_KLINE_CONCURRENCY = 5
_TOTAL_TIMEOUT_SEC = 1200
_PER_STOCK_TIMEOUT = 15
_FAILURE_THRESHOLD = 5


@dataclass
class ScreeningResult:
    """单只股票的筛选结果。"""

    symbol: str
    name: str
    price: float
    change_pct: float
    volume_ratio: float
    turnover_pct: float
    mcap_billion: float
    volume_trend: str
    beats_index: bool
    late_session_high: bool
    passed: bool
    failed_conditions: list[str] = field(default_factory=list)
    # P0-2: 可执行性信息（入场区间/止损位/仓位/目标位）
    ma20: float = 0.0  # MA20 支撑位
    recent_high: float = 0.0  # 前 20 日最高价（止盈目标位）
    suggested_position: float = 0.10  # 建议仓位占比（默认 10%）
    # P2: 十大视角评分（可选，通过筛选后附加）
    perspective_score: float = 0.0  # 综合评分 0-10

    @property
    def pass_count(self) -> int:
        return 8 - len(self.failed_conditions)

    @property
    def entry_low(self) -> float:
        """入场区间下限（MA20 ± 0.5%）。"""
        return self.ma20 * 0.995 if self.ma20 > 0 else self.price * 0.99

    @property
    def entry_high(self) -> float:
        """入场区间上限。"""
        return self.price * 1.005

    @property
    def stop_loss_price(self) -> float:
        """止损位：MA20 × 0.97（3% 缓冲）。"""
        return self.ma20 * 0.97 if self.ma20 > 0 else self.price * 0.95

    @property
    def take_profit_price(self) -> float:
        """止盈目标位：前高。"""
        return self.recent_high if self.recent_high > 0 else self.price * 1.10

    @property
    def risk_reward_ratio(self) -> float:
        """盈亏比 = (止盈 - 当前价) / (当前价 - 止损）。"""
        if self.stop_loss_price <= 0 or self.take_profit_price <= self.price:
            return 0.0
        risk = self.price - self.stop_loss_price
        reward = self.take_profit_price - self.price
        return round(reward / risk, 2) if risk > 0 else 0.0


@dataclass
class ScreenerScanResult:
    """尾盘选股一次扫描的完整结果。"""

    results: list[ScreeningResult] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    market_regime: str = ""
    total_scanned: int = 0

    def __len__(self) -> int:
        """P1-1 修复：支持 len() 操作（生产环境 7月22日触发 TypeError）。"""
        return len(self.results)


class TerminalScreener:
    """尾盘选股器（P2-1 低吸逻辑 + P0 性能修复 + 市场状态过滤）。"""

    def __init__(self, dm: DataManager | None = None, params: ScreenerParams | None = None) -> None:
        self._dm = dm
        self.params = params or ScreenerParams.load()
        self._regime_detector = MarketRegimeDetector()

    async def scan(self) -> ScreenerScanResult:
        """执行全市场扫描，返回完整扫描结果。

        新增市场状态过滤：
        - bear/crash 等 skip_regimes 下跳过正式选股，避免逆势操作。
        - bull/sideways 等状态下按 candidate_modifier 缩减候选数量。

        P0 修复：总超时 20 分钟硬上限，超时返回已扫描的部分结果。
        """
        logger.info("=== 尾盘选股: 开始全市场扫描（低吸逻辑 + 市场过滤 + P0 性能修复）===")
        start_time = time.time()

        # 1. 先判市场状态
        try:
            market_state = await self._regime_detector.detect()
            regime = market_state.get("regime", "sideways")
        except Exception as e:
            logger.warning("尾盘选股: 市场状态判断失败，降级为震荡市: {}", e)
            regime = "sideways"

        if self.params.should_skip(regime):
            reason = f"市场状态为 {regime}，根据配置跳过正式选股（保护性空仓）"
            logger.info("尾盘选股: {}", reason)
            return ScreenerScanResult(
                results=[], skipped=True, skip_reason=reason, market_regime=regime
            )

        try:
            results = await asyncio.wait_for(
                self._scan_internal(start_time, regime),
                timeout=_TOTAL_TIMEOUT_SEC,
            )
            return ScreenerScanResult(
                results=results, skipped=False, market_regime=regime, total_scanned=len(results)
            )
        except TimeoutError:
            elapsed = time.time() - start_time
            logger.error(
                f"尾盘选股: 总超时 {_TOTAL_TIMEOUT_SEC}s 触发，已运行 {elapsed:.0f}s，"
                f"返回空结果（内部已尽可能提前返回部分结果）"
            )
            return ScreenerScanResult(results=[], market_regime=regime)

    async def _scan_internal(self, start_time: float, regime: str) -> list[ScreeningResult]:
        """实际扫描逻辑（带快照硬过滤 + 并发 K 线获取 + 失败熔断 + 市场状态适配）。"""
        # 1. 获取全市场实时快照（含 量比/换手率/流通市值 等所有字段）
        spot = await self._fetch_market_snapshot()
        if spot is None or spot.empty:
            logger.error("尾盘选股: 全市场快照获取失败（akshare + sina 均不可用）")
            return []

        logger.info("尾盘选股: 全市场 {} 只股票", len(spot))

        # 1.5 大盘趋势过滤：大盘MA5<MA10（下跌趋势）时跳过选股
        try:
            # 获取最近20日大盘数据用于趋势判断
            index_df = await asyncio.wait_for(
                asyncio.to_thread(ak.stock_zh_index_daily, symbol="sh000001"),
                timeout=15.0,
            )
            if index_df is not None and len(index_df) > 15:
                index_df["ma5"] = index_df["close"].rolling(5).mean()
                index_df["ma10"] = index_df["close"].rolling(10).mean()
                if index_df.iloc[-1]["ma5"] < index_df.iloc[-1]["ma10"]:
                    logger.info(
                        "尾盘选股: 大盘MA5({:.2f})<MA10({:.2f})，下跌趋势跳过选股",
                        index_df.iloc[-1]["ma5"], index_df.iloc[-1]["ma10"],
                    )
                    return []
        except Exception as e:
            logger.warning("尾盘选股: 大盘趋势判断失败，降级继续: {}", e)

        # 2. 快照硬过滤：先用快照已有的字段过滤，大幅减少候选数
        candidates = self._hard_filter_snapshot(spot)
        if candidates.empty:
            logger.info("尾盘选股: 快照硬过滤后无候选")
            return []

        total = len(candidates)
        logger.info(f"尾盘选股: 快照硬过滤后候选 {total} 只")

        # 3. 获取大盘指数今日涨幅
        index_chg = await self._fetch_index_change()

        # 4. 并发获取K线 + 深度检查
        results = await self._concurrent_check(candidates, index_chg, start_time)

        # 5. 排序：通过条件数降序 → 视角评分降序 → 涨幅升序
        passed = [r for r in results if r.passed]
        partial = [r for r in results if not r.passed and len(r.failed_conditions) <= 3]
        partial.sort(key=lambda r: (-r.pass_count, r.change_pct))
        # P2 优化：通过股票按视角评分降序排列（评分高的优先）
        passed.sort(key=lambda r: (-r.perspective_score, r.change_pct))

        max_candidates = self.params.effective_max_candidates(regime)
        top = (passed + partial)[:max_candidates]
        elapsed = time.time() - start_time
        logger.info(
            f"尾盘选股: 市场状态 {regime}，完成 完全通过 {len(passed)} 只，"
            f"上报 {len(top)} 只（上限 {max_candidates}），耗时 {elapsed:.0f}s"
        )
        return top

    def _hard_filter_snapshot(self, spot: pd.DataFrame) -> pd.DataFrame:
        """P0-2: 快照硬过滤——用快照已有字段先过滤，避免逐只请求。

        过滤条件（仅用快照字段）：
        - 涨幅 -2% ~ +1%（条件2）
        - 量比 < 1.0（条件4）
        - 换手率 3%-15%（条件7）
        - 流通市值 50亿-500亿（条件8）
        - 排除北交所、ST

        剩余的 MA20/MA60/前5日强势 需要 K 线历史，留到深度检查阶段。
        """
        spot = spot.copy()

        # 标准化列名（兼容 stock_zh_a_spot_em 和 stock_zh_a_spot）
        col_map = {
            "code": "代码",
            "name": "名称",
            "trade": "最新价",
            "changepercent": "涨跌幅",
            "volume": "成交量",
            "amount": "成交额",
            "turnoverratio": "换手率",
            "volume_ratio": "量比",
            "circ_mv": "流通市值",
        }
        spot = spot.rename(columns={k: v for k, v in col_map.items() if k in spot.columns})

        # 数值化
        for col in ["涨跌幅", "量比", "换手率", "流通市值", "最新价", "成交量"]:
            if col in spot.columns:
                spot[col] = pd.to_numeric(spot[col], errors="coerce").fillna(0)

        # 条件2: 涨幅区间（动态参数，默认可覆盖 -2% ~ +1%）
        mask = (spot["涨跌幅"] >= self.params.min_chg_pct) & (
            spot["涨跌幅"] <= self.params.max_chg_pct
        )

        # 排除北交所、ST
        mask &= ~spot["代码"].astype(str).str.startswith(("bj", "43", "83", "87", "88"))
        mask &= ~spot["名称"].str.contains("ST|退|N|C", na=False)

        # 条件4: 量比上限（缩量）
        if "量比" in spot.columns:
            mask &= (spot["量比"] > 0) & (spot["量比"] < self.params.max_vol_ratio)
            logger.info("尾盘选股: 快照硬过滤 量比<{:.2f} 应用", self.params.max_vol_ratio)

        # 条件7: 换手率区间
        if "换手率" in spot.columns:
            mask &= (spot["换手率"] >= self.params.min_turnover) & (
                spot["换手率"] <= self.params.max_turnover
            )
            logger.info(
                f"尾盘选股: 快照硬过滤 换手率{self.params.min_turnover:.0f}%-{self.params.max_turnover:.0f}% 应用"
            )

        # 条件8: 流通市值区间（单位：元）
        if "流通市值" in spot.columns:
            mask &= (spot["流通市值"] >= self.params.min_mcap) & (
                spot["流通市值"] <= self.params.max_mcap
            )
            logger.info(
                f"尾盘选股: 快照硬过滤 流通市值{self.params.min_mcap / 1e8:.0f}亿-{self.params.max_mcap / 1e8:.0f}亿 应用"
            )

        candidates = spot[mask].copy()
        return candidates

    async def _concurrent_check(
        self,
        candidates: pd.DataFrame,
        index_chg: float,
        start_time: float,
    ) -> list[ScreeningResult]:
        """P0-3/4: 并发获取 K 线 + 深度检查，带失败熔断。"""
        sem = asyncio.Semaphore(_KLINE_CONCURRENCY)
        total = len(candidates)
        results: list[ScreeningResult] = []
        failure_count = 0
        completed = 0

        async def check_one(row) -> ScreeningResult | None:
            nonlocal failure_count
            async with sem:
                code = str(row["代码"]).strip().zfill(6)
                name = str(row["名称"]).strip()
                price = float(row["最新价"])
                chg_pct = float(row["涨跌幅"])
                today_vol = float(row.get("成交量", 0))
                vol_ratio = float(row.get("量比", 0)) if "量比" in row else 0.0
                turnover_pct = float(row.get("换手率", 0)) if "换手率" in row else 0.0
                mcap_yuan = float(row.get("流通市值", 0)) if "流通市值" in row else 0.0
                mcap_b = mcap_yuan / 1e8 if mcap_yuan > 0 else 0.0

                try:
                    r = await asyncio.wait_for(
                        self._check_stock(
                            code,
                            name,
                            price,
                            chg_pct,
                            today_vol,
                            index_chg,
                            vol_ratio,
                            turnover_pct,
                            mcap_b,
                        ),
                        timeout=_PER_STOCK_TIMEOUT,
                    )
                    failure_count = 0
                    return r
                except TimeoutError:
                    failure_count += 1
                    logger.warning(f"尾盘选股: {code} K线获取超时，跳过")
                    return None
                except Exception as e:
                    failure_count += 1
                    logger.warning("尾盘选股: {} 处理异常: {}", code, e)
                    return None

        # 分批并发，便于熔断和进度日志
        batch_size = 20
        for batch_start in range(0, total, batch_size):
            # 检查总超时（剩余 60s 时主动退出）
            elapsed = time.time() - start_time
            if elapsed > _TOTAL_TIMEOUT_SEC - 60:
                logger.warning(
                    f"尾盘选股: 接近总超时（已 {elapsed:.0f}s），"
                    f"已完成 {completed}/{total}，提前返回"
                )
                break

            # 熔断检查
            if failure_count >= _FAILURE_THRESHOLD:
                logger.error(
                    f"尾盘选股: 连续失败 {_FAILURE_THRESHOLD} 次触发熔断，"
                    f"已完成 {completed}/{total}，提前返回"
                )
                break

            batch = candidates.iloc[batch_start : batch_start + batch_size]
            tasks = [check_one(row) for _, row in batch.iterrows()]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for r in batch_results:
                completed += 1
                if isinstance(r, ScreeningResult):
                    results.append(r)

            logger.info("尾盘选股: 进度 {}/{}（累计通过 {} 只）", completed, total, len(results))

        return results

    async def _fetch_market_snapshot(self) -> pd.DataFrame | None:
        """获取全市场实时快照。优先 stock_zh_a_spot_em（含所有字段）。"""
        # 方式1: ak.stock_zh_a_spot_em（推荐，包含换手率/流通市值/量比）
        try:
            import akshare as ak

            spot = await asyncio.wait_for(asyncio.to_thread(ak.stock_zh_a_spot_em), timeout=30.0)
            if spot is not None and not spot.empty:
                logger.info(f"尾盘选股: 使用 stock_zh_a_spot_em 全市场快照 ({len(spot)} 只)")
                return spot
        except Exception as e:
            logger.warning("尾盘选股: stock_zh_a_spot_em 失败: {}", e)

        # 方式2: ak.stock_zh_a_spot（旧版，字段较少）
        try:
            import akshare as ak

            spot = await asyncio.wait_for(asyncio.to_thread(ak.stock_zh_a_spot), timeout=30.0)
            if spot is not None and not spot.empty:
                logger.info("尾盘选股: 降级使用 stock_zh_a_spot ({} 只)", len(spot))
                return spot
        except Exception as e:
            logger.warning("尾盘选股: stock_zh_a_spot 失败: {}", e)

        # 方式3: DataManager 降级（SinaCollector 实时行情）
        if self._dm is not None:
            try:
                spot = await self._dm.fetch_stock_realtime([])
                if spot is not None and not spot.empty:
                    logger.info("尾盘选股: 降级使用 DataManager 实时行情 ({} 只)", len(spot))
                    spot = spot.rename(
                        columns={
                            "symbol": "代码",
                            "name": "名称",
                            "price": "最新价",
                            "change_pct": "涨跌幅",
                            "volume": "成交量",
                        }
                    )
                    return spot
            except Exception as e:
                logger.warning("尾盘选股: DataManager 降级也失败: {}", e)

        return None

    async def _fetch_index_change(self) -> float:
        """获取上证指数今日涨幅。"""
        try:
            import akshare as ak

            df = await asyncio.wait_for(
                asyncio.to_thread(ak.stock_zh_index_daily, symbol="sh000001"), timeout=30.0
            )
            if not df.empty and len(df) >= 2:
                today = df.iloc[-1]
                yesterday = df.iloc[-2]
                return float((today["close"] - yesterday["close"]) / yesterday["close"] * 100)
        except Exception as e:
            logger.debug("akshare 获取大盘涨幅失败: {}", e)

        if self._dm is not None:
            try:
                df = await self._dm.fetch_stock_history("000001", days=5)
                if not df.empty and len(df) >= 2:
                    today = df.iloc[-1]
                    yesterday = df.iloc[-2]
                    return float((today["close"] - yesterday["close"]) / yesterday["close"] * 100)
            except Exception as e:
                logger.debug(f"DataManager 获取大盘涨幅失败: {e}")
        return 0.0

    async def _score_with_perspectives(
        self,
        code: str,
        name: str,
        price: float,
        ma20_val: float,
        ma60_val: float,
    ) -> float | None:
        """对候选股运行十大视角评分（轻量版，仅使用已有K线数据）。

        Returns:
            综合评分 0-10，或 None（评分失败）
        """
        try:
            from gugu.analysis.perspectives import (
                PerspectiveEngine,
                build_context_from_kline,
            )

            df = await self._fetch_stock_kline(code)
            if df is None or df.empty or len(df) < 30:
                return None

            ctx = build_context_from_kline(
                symbol=code,
                name=name,
                kline_df=df,
                price=price,
                ma20=ma20_val,
                ma60=ma60_val,
            )
            engine = PerspectiveEngine()
            result = engine.analyze(ctx)
            return result.composite_score
        except Exception as e:
            logger.debug(f"十大视角评分失败 {code}: {e}")
            return None

    async def _check_stock(
        self,
        code: str,
        name: str,
        price: float,
        chg_pct: float,
        today_vol: float,
        index_chg: float,
        vol_ratio: float = 0.0,
        turnover_pct: float = 0.0,
        mcap_b: float = 0.0,
    ) -> ScreeningResult | None:
        """对单只候选股执行条件3-8检查（P2-1 低吸逻辑）。

        P0 优化：vol_ratio/turnover_pct/mcap_b 已在快照阶段获取，
        避免在 K 线中重复计算。
        """
        failed: list[str] = []
        try:
            df = await self._fetch_stock_kline(code)
            if df is None or df.empty or len(df) < 60:
                return None

            recent = df.tail(60).copy()

            # 条件8: 流通市值（已从快照获取，K 线降级用估算）
            if mcap_b == 0:
                latest = recent.iloc[-1]
                outstanding = float(latest.get("outstanding_share", 0))
                if outstanding == 0:
                    outstanding = (
                        float(latest.get("volume", 0))
                        / max(float(latest.get("turnover", 0.001)), 0.001)
                        if float(latest.get("turnover", 0)) > 0
                        else 1e8
                    )
                mcap_b = price * outstanding / 1e8

            min_mcap_b = self.params.min_mcap / 1e8
            max_mcap_b = self.params.max_mcap / 1e8
            if mcap_b < min_mcap_b or mcap_b > max_mcap_b:
                failed.append(f"市值{mcap_b:.0f}亿")

            # 条件4: 量比（已从快照获取）
            if vol_ratio == 0:
                volumes_tmp = recent["volume"].values.astype(float)
                avg_5d = float(np.nanmean(volumes_tmp[-6:-1])) if len(volumes_tmp) > 5 else 0.0
                vol_ratio = volumes_tmp[-1] / avg_5d if avg_5d > 0 else 0
            if vol_ratio >= self.params.max_vol_ratio:
                failed.append(f"量比{vol_ratio:.2f}(未缩量)")

            # 条件7: 换手率（已从快照获取）
            if turnover_pct == 0:
                latest = recent.iloc[-1]
                turnover_pct = float(latest.get("turnover", 0)) * 100
            if turnover_pct < self.params.min_turnover or turnover_pct > self.params.max_turnover:
                failed.append(f"换手率{turnover_pct:.1f}%")

            # 条件5: 收盘价 ≥ MA20
            ma20 = recent["close"].rolling(20).mean().iloc[-1]
            if not pd.notna(ma20) or price < ma20:
                failed.append(f"破MA20({ma20:.2f})")

            # 条件6: MA20 > MA60
            ma60 = recent["close"].rolling(60).mean().iloc[-1]
            ma20_rising = pd.notna(ma60) and ma20 > ma60
            if not ma20_rising:
                failed.append("MA20未上MA60")

            # 条件3: 前 N 日内有涨幅 >= X% 的强势表现
            recent_days = self.params.recent_strong_days
            recent_pct = self.params.recent_strong_pct
            if len(recent) >= recent_days + 1:
                recent_closes = recent["close"].iloc[-(recent_days + 1) : -1]
                recent_chg_pct = recent_closes.pct_change() * 100
                has_strong = (recent_chg_pct >= recent_pct).any()
            else:
                has_strong = False
            if not has_strong:
                failed.append(f"近{recent_days}日无强势")

            # 条件8: 回踩日不跑赢大盘
            beats = chg_pct <= index_chg + 1.0
            if not beats:
                failed.append(f"异常强势({chg_pct:+.1f}%)")

            # 成交量趋势描述
            volumes = recent["volume"].values.astype(float)
            vol_trend = self._check_volume_trend(volumes[-5:])

            # 回踩支撑标志
            pullback_hold = pd.notna(ma20) and price >= ma20 * 0.98

            # P0-2: 计算前 20 日最高价（止盈目标位）
            recent_high_val = (
                float(recent["high"].iloc[-20:].max())
                if len(recent) >= 20
                else float(recent["high"].max())
            )
            ma20_val = float(ma20) if pd.notna(ma20) else price

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
                ma20=ma20_val,
                recent_high=recent_high_val,
            )

            # P2 优化：对通过筛选的股票附加十大视角评分（用于排序和通知展示）
            if result.passed:
                try:
                    perspective_score = await self._score_with_perspectives(
                        code,
                        name,
                        price,
                        ma20_val,
                        ma60_val=float(ma60) if pd.notna(ma60) else 0,
                    )
                    if perspective_score is not None:
                        result.perspective_score = perspective_score
                except Exception:
                    pass

            return result

        except Exception as e:
            logger.debug(f"尾盘选股 {code} 检查失败: {e}")
            return None

    def _check_volume_trend(self, vols: np.ndarray) -> str:
        """检查成交量趋势（P2-1 低吸逻辑：缩量更优）。"""
        if len(vols) < 3:
            return "✅"
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
        """获取个股日K线历史（需 60 日数据用于 MA60）。"""
        try:
            import akshare as ak

            prefix = _symbol_prefix(code)
            df = await asyncio.wait_for(
                asyncio.to_thread(ak.stock_zh_a_daily, symbol=prefix, adjust="qfq"), timeout=30.0
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            logger.debug(f"akshare 日K线 {code} 失败: {e}")

        if self._dm is not None:
            try:
                df = await self._dm.fetch_stock_history(code, days=120)
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                logger.debug("DataManager 日K线 {} 失败: {}", code, e)

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
