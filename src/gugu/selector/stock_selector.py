"""自动选股模块。

基于资金流 + 策略信号 + 综合评分，从全市场筛选候选股票。
流程：
1. 获取全市场快照
2. 基础过滤：价格 > 0、成交额 > 1000万、非 ST（停牌/涨跌停/新股
   需要前收盘价，延后到步骤4的 _process_stock 中通过 is_tradable 检查）
2.5 [可选] 豹哥三要素选股池预筛（v0.4.6 新增，默认关闭）
3. 按主力净占比排序取前 N
4. 对候选股跑策略信号（并发，Semaphore(10)），内含涨跌停过滤
5. StockRanker 综合评分排名（因子30%+基本面25%+资金流25%+板块20%）
6. 返回候选信号列表（含买入和卖出信号，供上层决策）
"""

from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd

from gugu.analysis.stock_ranker import StockRanker
from gugu.config import get_config
from gugu.data import DataManager
from gugu.data import data_manager as get_data_manager
from gugu.engine.signal_router import SignalRouter
from gugu.filters.leopard_universe import LeopardUniverseFilter
from gugu.risk import RiskManager
from gugu.strategies.registry import get_enabled_strategies
from gugu.utils.log import get_logger

logger = get_logger()


class StockSelector:
    """自动选股器。

    v2: 接入 StockRanker 综合评分，替换单一 main_pct 排序。
    v0.4.6: 支持豹哥三要素选股池预筛（可选开关）。
    """

    def __init__(
        self,
        data_manager: DataManager | None = None,
        top_n: int = 50,
        max_candidates: int = 10,
    ) -> None:
        self._dm = data_manager or get_data_manager()
        self._top_n = top_n
        self._max_candidates = max_candidates
        self._router = SignalRouter(get_enabled_strategies())
        self._risk = RiskManager()
        self._ranker = StockRanker()
        # v0.4.6: 豹哥三要素选股池过滤器（从配置加载，默认关闭）
        self._leopard_filter = self._init_leopard_filter()

    def _init_leopard_filter(self) -> LeopardUniverseFilter | None:
        """从配置初始化豹哥三要素过滤器，未启用时返回 None。"""
        try:
            cfg = get_config().strategy.leopard_universe_filter
            if not cfg.enabled:
                return None
            return LeopardUniverseFilter(
                params={
                    "turnover_min": cfg.turnover_min,
                    "amount_min": cfg.amount_min,
                    "circ_mv_min": cfg.circ_mv_min,
                }
            )
        except Exception as e:
            logger.warning("豹哥三要素过滤器初始化失败，跳过预筛: {}", e)
            return None

    async def select(self) -> list[dict[str, Any]]:
        """执行选股。

        Returns:
            候选信号列表（含买入和卖出信号），每个元素含
            symbol, name, direction("buy"/"sell"), confidence, price, strategies。
            上层根据 direction 决定是买入候选还是卖出预警。
        """
        logger.info("开始自动选股...")

        # 1. 全市场快照
        try:
            market_df = await self._dm.fetch_stock_realtime([])
        except Exception as e:
            logger.error(f"选股：获取全市场快照失败: {e}")
            return []

        if market_df.empty:
            logger.warning("选股：全市场快照为空")
            return []

        # 2. 基础过滤
        candidates = self._filter_basic(market_df)
        if candidates.empty:
            return []

        # 2.5 [可选] 豹哥三要素选股池预筛（v0.4.6 新增）
        # 三要素是选股池准入（换手率≥15% + 成交额≥10亿 + 流通市值≥70亿）
        # 不产生买卖信号，只缩小候选范围。默认关闭，仅与突破策略配合时开启。
        if self._leopard_filter is not None:
            _before = len(candidates)
            candidates = self._leopard_filter.filter(candidates)
            if candidates.empty:
                logger.info("豹哥三要素预筛后无候选股，跳过本次选股")
                return []

        # 3. 按主力净占比排序（如果列存在）
        if "main_pct" in candidates.columns:
            candidates = candidates.sort_values("main_pct", ascending=False)
        candidates = candidates.head(self._top_n)

        # 4. 跑策略信号，并发处理所有候选股
        sem = asyncio.Semaphore(10)

        async def _process_stock(row: pd.Series) -> dict[str, Any] | None:
            """处理单只候选股票，返回信号字典或 None。"""
            symbol = str(row["symbol"]).zfill(6)
            async with sem:
                try:
                    df = await asyncio.wait_for(
                        self._dm.fetch_stock_history(symbol, days=90), timeout=15
                    )
                    if df.empty or len(df) < 30:
                        return None

                    signal = self._router.route(df, symbol)
                    if not signal or signal["direction"] not in ("buy", "sell"):
                        return None

                    signal["name"] = row.get("name", "")
                    signal["price"] = float(row.get("price", df.iloc[-1]["close"]))
                    # 过滤涨跌停（传入买卖方向）
                    prev_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else 0
                    if prev_close > 0 and not self._risk.is_tradable(
                        symbol,
                        signal["price"],
                        prev_close,
                        direction=signal["direction"],
                    ):
                        logger.info("选股：{} 涨跌停，跳过", symbol)
                        return None

                    return signal
                except Exception as e:
                    logger.warning(f"选股：{symbol} 处理失败: {e}")
                    return None

        # 启动所有候选股的处理任务
        tasks = [_process_stock(row) for _, row in candidates.iterrows()]
        results = await asyncio.gather(*tasks)

        # 收集非空结果
        signal_stocks = [r for r in results if r is not None]
        # 如果收集到的结果已超过上限，只取前 max_candidates*2 个
        if len(signal_stocks) > self._max_candidates * 2:
            signal_stocks = signal_stocks[: self._max_candidates * 2]

        if not signal_stocks:
            logger.info("选股完成：无候选股")
            return []

        # 5. StockRanker 综合评分排名
        symbols = [s["symbol"] for s in signal_stocks]
        try:
            ranked = await self._ranker.rank(symbols, top_n=self._max_candidates)
        except Exception as e:
            logger.warning(f"综合评分失败，使用原始排序: {e}")
            ranked = []

        # 6. 合并评分到信号
        if ranked:
            rank_map = {r["symbol"]: r for r in ranked}
            for sig in signal_stocks:
                r = rank_map.get(sig["symbol"])
                if r:
                    sig["total_score"] = r["total_score"]
                    sig["factor_score"] = r["factor_score"]
                    sig["fundamental_score"] = r["fundamental_score"]
                    sig["money_flow_score"] = r["money_flow_score"]
                    sig["sector_score"] = r["sector_score"]
                    sig["chip_score"] = r.get("chip_score", 0.0)  # v0.6.0 新增
            # 按综合评分排序
            signal_stocks.sort(key=lambda x: x.get("total_score", 0), reverse=True)

        # 无论评分是否成功，都截断到 max_candidates
        signal_stocks = signal_stocks[: self._max_candidates]

        logger.info("选股完成：{} 只候选股", len(signal_stocks))
        return signal_stocks

    @staticmethod
    def _filter_basic(df: pd.DataFrame) -> pd.DataFrame:
        """基础过滤：

        - 价格 > 0
        - 成交额 > 1000 万
        - 非 ST（名称不含 ST）
        - 仅沪深 A 股代码段（排除北交所 920/8/4 开头——单日 ±30% 波动，
          对新手模拟盘风险过高，且不在自选股内）
        """
        df = df.copy()
        df = df[df["price"] > 0]
        if "amount" in df.columns:
            df = df[df["amount"] > 10_000_000]
        if "name" in df.columns:
            df = df[~df["name"].str.contains("ST", na=False, case=False)]
        # 2026-09-04 优化（数据专家 review）：只保留沪深 A 股代码段
        # 沪：60/68 开头；深：00/30 开头
        # 排除：北交所 920/8/4 开头（±30% 限制）、B 股 900/200
        if "symbol" in df.columns:
            _sym = df["symbol"].astype(str).str.zfill(6)
            df = df[_sym.str.startswith(("60", "68", "00", "30"))]
        return df.reset_index(drop=True)
