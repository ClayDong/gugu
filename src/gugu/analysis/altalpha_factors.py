"""另类Alpha因子计算引擎。

将8类另类数据转换为横截面 z-score 因子，供 AlphaFactory / MLFactorStrategy 使用。

因子列表：
1. insider_buy       - 高管净增持（看多信号）
2. earnings_surprise  - 业绩预告超预期（PEAD效应）
3. lhb_inst_net       - 龙虎榜机构净买入（聪明钱）
4. roic_quality       - ROE+营收增长（质量因子）
5. block_discount     - 大宗折价率（反向：折价大看空）
6. margin_sentiment   - 融资余额变化率（做多情绪）
7. unlock_pressure    - 解禁压力（反向：压力大看空）
8. holder_concentrate - 筹码集中度（户数减少看多）

每个因子输出横截面 z-score（-3 ~ +3），表示当前股票在全市场中的相对位置。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# 因子名常量
FACTOR_NAMES: list[str] = [
    "insider_buy",
    "earnings_surprise",
    "lhb_inst_net",
    "roic_quality",
    "block_discount",
    "margin_sentiment",
    "unlock_pressure",
    "holder_concentrate",
]


def _zscore(series: pd.Series, clip: float = 3.0) -> pd.Series:
    """计算横截面 z-score，clip 到 [-clip, clip]，缺失值为0。"""
    s = pd.to_numeric(series, errors="coerce")
    mean = s.mean()
    std = s.std()
    if std == 0 or np.isnan(std):
        return pd.Series(0.0, index=s.index)
    z = (s - mean) / std
    return z.clip(-clip, clip).fillna(0.0)


class AltAlphaFactorEngine:
    """另类因子计算引擎。

    使用方式：
        engine = AltAlphaFactorEngine()
        factors = await engine.compute_all(symbols=["600519", "000858"])
        # factors = {"600519": {"insider_buy": 1.2, "lhb_inst_net": -0.5, ...}, ...}
    """

    def __init__(self) -> None:
        self._insider_collector = None
        self._earnings_collector = None
        self._lhb_collector = None
        self._financial_collector = None
        self._block_collector = None
        self._margin_collector = None
        self._unlock_collector = None
        self._holder_collector = None

    def _get_insider_collector(self):
        if self._insider_collector is None:
            from gugu.data.collectors.altalpha.insider_trading import (
                InsiderTradingCollector,
            )

            self._insider_collector = InsiderTradingCollector()
        return self._insider_collector

    def _get_earnings_collector(self):
        if self._earnings_collector is None:
            from gugu.data.collectors.altalpha.earnings_forecast import (
                EarningsForecastCollector,
            )

            self._earnings_collector = EarningsForecastCollector()
        return self._earnings_collector

    def _get_lhb_collector(self):
        if self._lhb_collector is None:
            from gugu.data.collectors.altalpha.dragon_tiger import (
                DragonTigerCollector,
            )

            self._lhb_collector = DragonTigerCollector()
        return self._lhb_collector

    def _get_financial_collector(self):
        if self._financial_collector is None:
            from gugu.data.collectors.altalpha.financial_data import (
                FinancialDataCollector,
            )

            self._financial_collector = FinancialDataCollector()
        return self._financial_collector

    def _get_block_collector(self):
        if self._block_collector is None:
            from gugu.data.collectors.altalpha.block_trade import (
                BlockTradeCollector,
            )

            self._block_collector = BlockTradeCollector()
        return self._block_collector

    def _get_margin_collector(self):
        if self._margin_collector is None:
            from gugu.data.collectors.altalpha.margin_trade import (
                MarginTradeCollector,
            )

            self._margin_collector = MarginTradeCollector()
        return self._margin_collector

    def _get_unlock_collector(self):
        if self._unlock_collector is None:
            from gugu.data.collectors.altalpha.share_unlock import (
                ShareUnlockCollector,
            )

            self._unlock_collector = ShareUnlockCollector()
        return self._unlock_collector

    # ---- 单因子计算 ----

    def _compute_insider_buy(self, df: pd.DataFrame, symbols: list[str]) -> pd.Series:
        """因子1：高管净增持。

        计算：net_buy_amount = sum(变动股数 * 成交均价)
              insider_factor = net_buy_amount / 流通市值（简化：用净增持金额排名）
        """
        if df.empty or "code" not in df.columns or "change_shares" not in df.columns:
            return pd.Series(0.0, index=symbols)

        df = df.copy()
        df["net_amount"] = pd.to_numeric(
            df.get("change_shares", 0), errors="coerce"
        ) * pd.to_numeric(df.get("avg_price", 0), errors="coerce")
        net_by_code = df.groupby("code")["net_amount"].sum()
        return _zscore(net_by_code.reindex(symbols, fill_value=0.0))

    def _compute_earnings_surprise(self, df: pd.DataFrame, symbols: list[str]) -> pd.Series:
        """因子2：业绩预告超预期。

        计算：growth = 业绩变动幅度（%），预增为正，预减为负
        """
        if df.empty or "code" not in df.columns:
            return pd.Series(0.0, index=symbols)

        df = df.copy()
        # akshare 返回"业绩变动幅度"列（百分比）
        if "growth" in df.columns:
            growth = pd.to_numeric(df["growth"], errors="coerce") / 100.0
        else:
            return pd.Series(0.0, index=symbols)

        # 预告类型为预减/续亏/首亏时取负值
        if "forecast_type" in df.columns:
            neg_mask = df["forecast_type"].isin(["预减", "续亏", "首亏", "预亏"])
            growth = growth.where(~neg_mask, -growth.abs())

        by_code = growth.groupby(df["code"]).first()
        return _zscore(by_code.reindex(symbols, fill_value=0.0))

    def _compute_lhb_inst_net(self, df: pd.DataFrame, symbols: list[str]) -> pd.Series:
        """因子3：龙虎榜机构净买入。

        计算：近5日龙虎榜净买额
        """
        if df.empty or "code" not in df.columns or "lhb_net_buy" not in df.columns:
            return pd.Series(0.0, index=symbols)

        df = df.copy()
        df["net"] = pd.to_numeric(df["lhb_net_buy"], errors="coerce")
        net_by_code = df.groupby("code")["net"].sum()
        return _zscore(net_by_code.reindex(symbols, fill_value=0.0))

    def _compute_roic_quality(self, df: pd.DataFrame, symbol: str) -> float:
        """因子4：质量因子（单股）。

        计算：roe_avg = mean(近4季ROE)
              quality_factor = roe_avg
        """
        if df.empty:
            return 0.0

        # akshare stock_financial_analysis_indicator 返回的列名含"净资产收益率"
        roe_cols = [c for c in df.columns if "净资产收益率" in c or "ROE" in c.upper()]
        if not roe_cols:
            return 0.0

        # 取最近4个值
        roe_values = pd.to_numeric(df[roe_cols[0]], errors="coerce").tail(4)
        roe_avg = roe_values.mean()
        if np.isnan(roe_avg):
            return 0.0
        return float(roe_avg)

    def _compute_block_discount(self, df: pd.DataFrame, symbols: list[str]) -> pd.Series:
        """因子5：大宗交易活跃度。

        计算：block_amount = sum(成交额)（大宗交易总成交额）
              block_factor = block_amount（成交额越大→机构关注度高→看多）

        注：akshare stock_dzjy_mrmx 不返回收盘价，无法计算折价率。
        改用成交额作为活跃度因子（大宗交易通常代表机构行为）。
        """
        if df.empty or "code" not in df.columns or "amount" not in df.columns:
            return pd.Series(0.0, index=symbols)

        df = df.copy()
        df["amt"] = pd.to_numeric(df["amount"], errors="coerce")
        amount_by_code = df.groupby("code")["amt"].sum()
        return _zscore(amount_by_code.reindex(symbols, fill_value=0.0))

    def _compute_margin_sentiment(self, df: pd.DataFrame, symbols: list[str]) -> pd.Series:
        """因子6：融资余额变化率。

        计算：margin_change = (今日融资余额 - 昨日融资余额) / 昨日融资余额
        （单日数据，直接用融资余额排名作为情绪因子）
        """
        if df.empty or "code" not in df.columns or "margin_balance" not in df.columns:
            return pd.Series(0.0, index=symbols)

        df = df.copy()
        balance = pd.to_numeric(df["margin_balance"], errors="coerce")
        by_code = balance.groupby(df["code"]).first()
        return _zscore(by_code.reindex(symbols, fill_value=0.0))

    def _compute_unlock_pressure(self, df: pd.DataFrame, symbols: list[str]) -> pd.Series:
        """因子7：解禁压力（反向）。

        计算：unlock_value_by_code = sum(未来30日解禁市值)
              unlock_factor = -unlock_value（解禁越大越看空）
        """
        if df.empty or "code" not in df.columns or "unlock_value" not in df.columns:
            return pd.Series(0.0, index=symbols)

        df = df.copy()
        # 过滤未来30日内的解禁
        if "unlock_date" in df.columns:
            df["unlock_date"] = pd.to_datetime(df["unlock_date"], errors="coerce")
            now = datetime.now()
            future_30d = now + timedelta(days=30)
            df = df[(df["unlock_date"] >= now) & (df["unlock_date"] <= future_30d)]

        if df.empty:
            return pd.Series(0.0, index=symbols)

        df["value"] = pd.to_numeric(df["unlock_value"], errors="coerce")
        value_by_code = df.groupby("code")["value"].sum()
        return _zscore(-value_by_code.reindex(symbols, fill_value=0.0))

    async def _compute_holder_concentrate(self, symbols: list[str]) -> pd.Series:
        """因子8：筹码集中度。

        计算：holder_change = (最新户数 - 上期户数) / 上期户数
              concentrate_factor = -holder_change（户数减少→筹码集中→看多）

        使用已有的 HolderStructureCollector。
        """
        result = pd.Series(0.0, index=symbols)
        try:
            from gugu.data.collectors.holder_structure import HolderStructureCollector

            collector = HolderStructureCollector()

            async def _fetch_one(sym: str) -> None:
                try:
                    hist = await collector.fetch_holder_history(sym, quarters=2)
                    if hist is not None and len(hist) >= 2:
                        latest = hist.iloc[0]["holder_num"]
                        prev = hist.iloc[1]["holder_num"]
                        if prev > 0:
                            change = (latest - prev) / prev
                            result[sym] = -change  # 户数减少为正
                except Exception:
                    pass

            import asyncio

            await asyncio.gather(*[_fetch_one(s) for s in symbols])
        except Exception as e:
            logger.warning(f"筹码集中度因子计算失败: {e}")

        return _zscore(result)

    # ---- 批量计算 ----

    async def compute_all(
        self, symbols: list[str], fetch_financial: bool = False
    ) -> dict[str, dict[str, float]]:
        """批量计算所有另类因子。

        Args:
            symbols: 股票代码列表
            fetch_financial: 是否获取财务数据（较慢，默认False）

        Returns:
            {symbol: {factor_name: z_score, ...}, ...}
        """
        if not symbols:
            return {}

        import asyncio

        results: dict[str, dict[str, float]] = {
            s: dict.fromkeys(FACTOR_NAMES, 0.0) for s in symbols
        }

        # 并行采集所有数据
        tasks: list[tuple[str, Any]] = [
            ("insider_buy", self._get_insider_collector().fetch_recent()),
            ("earnings_surprise", self._get_earnings_collector().fetch_latest()),
            ("lhb_inst_net", self._get_lhb_collector().fetch_detail()),
            ("block_discount", self._get_block_collector().fetch_detail()),
            ("margin_sentiment", self._get_margin_collector().fetch_detail()),
            ("unlock_pressure", self._get_unlock_collector().fetch_upcoming()),
            ("holder_concentrate", self._compute_holder_concentrate(symbols)),
        ]

        if fetch_financial:
            # 财务数据需逐股获取，较慢
            for symbol in symbols[:20]:  # 限制前20只
                tasks.append(
                    (
                        f"roic_quality_{symbol}",
                        self._get_financial_collector().fetch_indicators(symbol),
                    )
                )

        # 等待所有采集完成
        raw_data: dict[str, Any] = {}
        gathered = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
        for (name, _), result in zip(tasks, gathered, strict=True):
            if isinstance(result, Exception):
                logger.warning("因子数据采集失败 [{}]: {}", name, result)
                raw_data[name] = pd.DataFrame()
            else:
                raw_data[name] = result

        # 计算各因子 z-score
        factor_values: dict[str, pd.Series] = {}

        if "insider_buy" in raw_data:
            factor_values["insider_buy"] = self._compute_insider_buy(
                raw_data["insider_buy"], symbols
            )

        if "earnings_surprise" in raw_data:
            factor_values["earnings_surprise"] = self._compute_earnings_surprise(
                raw_data["earnings_surprise"], symbols
            )

        if "lhb_inst_net" in raw_data:
            factor_values["lhb_inst_net"] = self._compute_lhb_inst_net(
                raw_data["lhb_inst_net"], symbols
            )

        if "block_discount" in raw_data:
            factor_values["block_discount"] = self._compute_block_discount(
                raw_data["block_discount"], symbols
            )

        if "margin_sentiment" in raw_data:
            factor_values["margin_sentiment"] = self._compute_margin_sentiment(
                raw_data["margin_sentiment"], symbols
            )

        if "unlock_pressure" in raw_data:
            factor_values["unlock_pressure"] = self._compute_unlock_pressure(
                raw_data["unlock_pressure"], symbols
            )

        if "holder_concentrate" in raw_data and isinstance(
            raw_data["holder_concentrate"], pd.Series
        ):
            factor_values["holder_concentrate"] = raw_data["holder_concentrate"]

        # 财务因子（单股）
        if fetch_financial:
            for symbol in symbols[:20]:
                key = f"roic_quality_{symbol}"
                if key in raw_data:
                    val = self._compute_roic_quality(raw_data[key], symbol)
                    results.setdefault(symbol, {}).setdefault("roic_quality", 0.0)
                    results[symbol]["roic_quality"] = val

        # 将 z-score 填入结果
        for factor_name, series in factor_values.items():
            for symbol in symbols:
                if symbol in series.index:
                    results[symbol][factor_name] = float(series[symbol])

        logger.info(
            f"另类因子计算完成: {len(symbols)} 只股票, "
            f"{len(factor_values)}/{len(FACTOR_NAMES)} 个因子有值"
        )
        return results

    def compute_single(
        self, symbol: str, all_factors: dict[str, dict[str, float]]
    ) -> dict[str, float]:
        """从批量计算结果中提取单股因子。"""
        return all_factors.get(symbol, dict.fromkeys(FACTOR_NAMES, 0.0))
