"""行业分散约束：限制同行业持仓数量，避免行业集中度过高。

规则：
- 同行业持仓不超过 max_same_industry 只（默认 2，可在 settings.yaml 配置 risk.max_same_industry）
- 通过 akshare 获取股票所属行业信息，带缓存避免重复请求
- 在 RiskManager.check_order 买入流程中调用
"""
from __future__ import annotations

from typing import Any

import akshare as ak

from gugu.config import settings
from gugu.utils.log import get_logger


class IndustryConstraint:
    """行业分散约束检查器。

    检查持仓的行业集中度，防止同行业持仓过多。
    行业信息通过 akshare 获取并缓存，避免重复网络请求。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config if config is not None else settings().get("risk", {})
        self.max_same_industry: int = int(cfg.get("max_same_industry", 2))
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
            self._logger.debug(f"stock_individual_info_em 获取 {symbol} 行业失败: {e}")

        # 方式二：从全市场实时行情中查找
        if not industry:
            try:
                df = ak.stock_zh_a_spot_em()
                if not df.empty and "代码" in df.columns and "行业" in df.columns:
                    row = df[df["代码"] == symbol]
                    if not row.empty:
                        industry = str(row.iloc[0]["行业"]).strip()
            except Exception as e:
                self._logger.debug(f"stock_zh_a_spot_em 获取 {symbol} 行业失败: {e}")

        # 缓存结果（包括空字符串，避免反复请求失败的股票）
        self._industry_cache[symbol] = industry
        if industry:
            self._logger.debug(f"股票 {symbol} 所属行业: {industry}")
        else:
            self._logger.warning(f"无法获取股票 {symbol} 的行业信息")

        return industry

    def check_buy(
        self,
        symbol: str,
        portfolio: dict[str, Any],
        industry: str = "",
    ) -> dict[str, Any]:
        """检查买入操作是否违反行业集中度约束。

        Args:
            symbol: 待买入股票代码，如 "600519"。
            portfolio: 当前持仓，key 为股票代码，value 需要有 symbol 属性或直接为字符串代码。
            industry: 股票所属行业，为空时自动通过 get_industry 获取。

        Returns:
            检查结果字典：
            - allowed: 是否允许买入
            - same_industry_count: 同行业已有持仓数
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
                "industry": "",
                "reason": f"无法获取 {symbol} 行业信息，放行",
            }

        # 统计同行业已有持仓数
        same_industry_count = 0
        for held_symbol in portfolio:
            held_industry = self.get_industry(held_symbol)
            if held_industry == industry:
                same_industry_count += 1

        # 判断是否超过限制
        if same_industry_count >= self.max_same_industry:
            return {
                "allowed": False,
                "same_industry_count": same_industry_count,
                "industry": industry,
                "reason": (
                    f"同行业（{industry}）已有 {same_industry_count} 只持仓，"
                    f"超过上限 {self.max_same_industry}，禁止买入 {symbol}"
                ),
            }

        return {
            "allowed": True,
            "same_industry_count": same_industry_count,
            "industry": industry,
            "reason": (
                f"同行业（{industry}）已有 {same_industry_count} 只持仓，"
                f"未超上限 {self.max_same_industry}，允许买入 {symbol}"
            ),
        }
