"""自动选股模块。

基于资金流 + 策略信号 + 综合评分，从全市场筛选候选股票。
流程：
1. 获取全市场快照
2. 过滤：ST、停牌、涨跌停、新股
3. 按主力净占比排序取前 N
4. 对候选股跑策略信号
5. StockRanker 综合评分排名（因子30%+基本面25%+资金流25%+板块20%）
6. 返回买入信号股票
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from gugu.analysis.stock_ranker import StockRanker
from gugu.data import DataManager
from gugu.data import data_manager as get_data_manager
from gugu.engine.signal_router import SignalRouter
from gugu.risk import RiskManager
from gugu.strategies.registry import get_enabled_strategies
from gugu.utils.log import get_logger

logger = get_logger()


class StockSelector:
    """自动选股器。

    v2: 接入 StockRanker 综合评分，替换单一 main_pct 排序。
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

    async def select(self) -> list[dict[str, Any]]:
        """执行选股。

        Returns:
            候选信号列表，每个元素含 symbol, name, direction, confidence, price, strategies
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

        # 3. 按主力净占比排序（如果列存在）
        if "main_pct" in candidates.columns:
            candidates = candidates.sort_values("main_pct", ascending=False)
        candidates = candidates.head(self._top_n)

        # 4. 跑策略信号，收集有信号的股票
        signal_stocks: list[dict[str, Any]] = []
        for _, row in candidates.iterrows():
            symbol = str(row["symbol"]).zfill(6)
            try:
                df = await self._dm.fetch_stock_history(symbol, days=60)
                if df.empty or len(df) < 30:
                    continue

                signal = self._router.route(df, symbol)
                if signal and signal["direction"] in ("buy", "sell"):
                    signal["name"] = row.get("name", "")
                    signal["price"] = float(row.get("price", df.iloc[-1]["close"]))
                    # 过滤涨跌停
                    prev_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else 0
                    if prev_close > 0 and not self._risk.is_tradable(
                        symbol, signal["price"], prev_close
                    ):
                        logger.info(f"选股：{symbol} 涨跌停，跳过")
                        continue
                    signal_stocks.append(signal)

                if len(signal_stocks) >= self._max_candidates * 2:
                    break
            except Exception as e:
                logger.warning(f"选股：{symbol} 处理失败: {e}")
                continue

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
            # 按综合评分排序
            signal_stocks.sort(
                key=lambda x: x.get("total_score", 0), reverse=True
            )

        # 无论评分是否成功，都截断到 max_candidates
        signal_stocks = signal_stocks[: self._max_candidates]

        logger.info(f"选股完成：{len(signal_stocks)} 只候选股")
        return signal_stocks

    @staticmethod
    def _filter_basic(df: pd.DataFrame) -> pd.DataFrame:
        """基础过滤：

        - 价格 > 0
        - 成交额 > 1000 万
        - 非 ST（名称不含 ST）
        """
        df = df.copy()
        df = df[df["price"] > 0]
        if "amount" in df.columns:
            df = df[df["amount"] > 10_000_000]
        if "name" in df.columns:
            df = df[~df["name"].str.contains("ST", na=False, case=False)]
        return df.reset_index(drop=True)
