"""行业分散约束：限制同行业持仓集中度，避免行业集中度过高。

规则：
- 同行业持仓市值占组合总市值比例不超过 max_same_industry_value_ratio（默认 30%）
- 同行业持仓数量不超过 max_same_industry 只（默认 2，双重保护）
- 通过 akshare 获取股票所属行业信息，带缓存避免重复请求
- 在 RiskManager.check_order 买入流程中调用
"""

from __future__ import annotations

import os
from typing import Any

import akshare as ak

from gugu.config import get_config
from gugu.utils.log import get_logger


class IndustryConstraint:
    """行业分散约束检查器。

    检查持仓的行业集中度，防止同行业持仓过多。
    行业信息通过 akshare 获取并缓存，避免重复网络请求。

    P2-2 升级：从纯数量限制升级为市值权重 + 数量双重限制。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else get_config().flatten().get("risk", {})
        self.max_same_industry: int = int(cfg.get("max_same_industry", 2))
        # P2-2：同行业市值占比上限（默认 30%）
        self.max_same_industry_value_ratio: float = float(
            cfg.get("max_same_industry_value_ratio", 0.30)
        )
        self._industry_cache: dict[str, str] = {}  # symbol -> industry 缓存
        self._logger = get_logger()

    def get_industry(self, symbol: str) -> str:
        """获取股票所属行业，带缓存。

        优先从缓存读取；缓存未命中时通过 akshare 获取。
        先尝试 stock_individual_info_em 获取行业字段，
        失败则从 stock_zh_a_spot_em 全市场快照中查找。

        Args:
            symbol: 股票代码，如 "600519"。

        Returns:
            行业名称，获取失败返回空字符串。
        """
        if symbol in self._industry_cache:
            return self._industry_cache[symbol]

        # 测试隔离：GUGU_TEST_MODE 下禁止发起真实网络请求。
        # 行业信息缺失时 check_buy 走"放行但不计数"分支，语义与线上
        # 数据源不可用时的降级路径完全一致；避免 E2E 测试被外部 API 拖死。
        if os.environ.get("GUGU_TEST_MODE") == "1":
            self._industry_cache[symbol] = ""
            return ""

        industry = ""

        # 方式一：stock_individual_info_em 获取个股信息
        try:
            df = ak.stock_individual_info_em(symbol=symbol)
            if not df.empty:
                # 返回两列：item / value，查找"行业"行
                industry_row = df[df["item"] == "行业"]
                if not industry_row.empty:
                    industry = str(industry_row.iloc[0]["value"]).strip()
        except Exception as e:
            self._logger.debug("stock_individual_info_em 获取 {} 行业失败: {}", symbol, e)

        # 方式二：从全市场实时行情中查找
        if not industry:
            try:
                df = ak.stock_zh_a_spot_em()
                if not df.empty and "代码" in df.columns and "行业" in df.columns:
                    row = df[df["代码"] == symbol]
                    if not row.empty:
                        industry = str(row.iloc[0]["行业"]).strip()
            except Exception as e:
                self._logger.debug("stock_zh_a_spot_em 获取 {} 行业失败: {}", symbol, e)

        # 缓存结果（包括空字符串，避免反复请求失败的股票）
        self._industry_cache[symbol] = industry
        if industry:
            self._logger.debug("股票 {} 所属行业: {}", symbol, industry)
        else:
            self._logger.warning("无法获取股票 {} 的行业信息", symbol)

        return industry

    def check_buy(
        self,
        symbol: str,
        portfolio: dict[str, Any],
        industry: str = "",
        buy_value: float = 0.0,
        portfolio_total_value: float = 0.0,
    ) -> dict[str, Any]:
        """检查买入操作是否违反行业集中度约束。

        P2-2 升级：数量 + 市值权重双重检查。
        市值权重检查需要 buy_value 和 portfolio_total_value，
        如果未传入则跳过市值检查，只做数量检查（向后兼容）。

        Args:
            symbol: 待买入股票代码，如 "600519"。
            portfolio: 当前持仓，key 为股票代码。
            industry: 股票所属行业，为空时自动通过 get_industry 获取。
            buy_value: 本次买入金额（元），用于计算买入后行业市值占比。
            portfolio_total_value: 组合当前总市值（元）。

        Returns:
            检查结果字典：
            - allowed: 是否允许买入
            - same_industry_count: 同行业已有持仓数
            - same_industry_value_ratio: 同行业市值占比
            - industry: 股票所属行业
            - reason: 原因说明
        """
        # 获取行业信息
        if not industry:
            industry = self.get_industry(symbol)

        # 无法获取行业信息时，放行但不计数
        if not industry:
            return {
                "allowed": True,
                "same_industry_count": 0,
                "same_industry_value_ratio": 0.0,
                "industry": "",
                "reason": f"无法获取 {symbol} 行业信息，放行",
            }

        # 统计同行业已有持仓数和市值
        same_industry_count = 0
        same_industry_value = 0.0
        for held_symbol, pos in portfolio.items():
            held_industry = self.get_industry(held_symbol)
            if held_industry == industry:
                same_industry_count += 1
                # 估算持仓市值
                if hasattr(pos, "market_value"):
                    same_industry_value += float(pos.market_value)
                elif hasattr(pos, "quantity") and hasattr(pos, "current_price"):
                    same_industry_value += float(pos.quantity) * float(pos.current_price)
                elif hasattr(pos, "avg_cost") and hasattr(pos, "quantity"):
                    same_industry_value += float(pos.avg_cost) * float(pos.quantity)

        # 1. 数量限制检查（硬限制）
        if same_industry_count >= self.max_same_industry:
            return {
                "allowed": False,
                "same_industry_count": same_industry_count,
                "same_industry_value_ratio": 0.0,
                "industry": industry,
                "reason": (
                    f"同行业（{industry}）已有 {same_industry_count} 只持仓，"
                    f"超过数量上限 {self.max_same_industry}，禁止买入 {symbol}"
                ),
            }

        # 2. P2-2：市值权重限制检查
        same_industry_value_ratio = 0.0
        if buy_value > 0 and portfolio_total_value > 0:
            # 计算买入后同行业总市值占比
            # 如果是加仓（已持有该股），buy_value 是增量
            is_add_position = symbol in portfolio
            if is_add_position:
                # 加仓：same_industry_value 已包含该股当前市值，加上增量即可
                post_industry_value = same_industry_value + buy_value
            else:
                # 新开仓：加上新股市值
                post_industry_value = same_industry_value + buy_value

            # 买入后总市值 = 当前总市值 + 买入金额（现金转持仓，总值不变，近似处理）
            # 更准确的做法是用 post_trade_total，但这里用 portfolio_total_value 近似
            post_total = portfolio_total_value
            same_industry_value_ratio = post_industry_value / post_total if post_total > 0 else 0.0

            if same_industry_value_ratio >= self.max_same_industry_value_ratio:
                return {
                    "allowed": False,
                    "same_industry_count": same_industry_count,
                    "same_industry_value_ratio": round(same_industry_value_ratio, 4),
                    "industry": industry,
                    "reason": (
                        f"同行业（{industry}）市值占比 "
                        f"{same_industry_value_ratio:.1%} 超过上限 "
                        f"{self.max_same_industry_value_ratio:.0%}，禁止买入 {symbol}"
                    ),
                }

        return {
            "allowed": True,
            "same_industry_count": same_industry_count,
            "same_industry_value_ratio": round(same_industry_value_ratio, 4),
            "industry": industry,
            "reason": (
                f"同行业（{industry}）已有 {same_industry_count} 只持仓"
                + (
                    f"，市值占比 {same_industry_value_ratio:.1%}"
                    if same_industry_value_ratio > 0
                    else ""
                )
                + f"，未超上限，允许买入 {symbol}"
            ),
        }
