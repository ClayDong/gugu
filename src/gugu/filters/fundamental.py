"""基本面过滤器。

使用 akshare 获取个股基本面数据（PE、PB、ROE、营收增长率、行业），
根据可配置的阈值过滤股票，排除亏损股、泡沫股和衰退股。

过滤规则（默认值，可通过 settings.yaml 的 fundamental 节覆盖）：
  - PE > 0 且 < 100（排除亏损股和泡沫股）
  - PB > 0 且 < 15
  - ROE > 0%（至少有盈利能力）
  - 营收增长率 > -20%（排除衰退股）
  - 任一指标获取失败时，该指标不参与过滤（宽松降级）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import akshare as ak
import pandas as pd

from gugu.config import settings
from gugu.utils.log import get_logger

logger = get_logger()

# 默认过滤阈值
_DEFAULT_THRESHOLDS: dict[str, Any] = {
    "pe_min": 0,
    "pe_max": 100,
    "pb_min": 0,
    "pb_max": 15,
    "roe_min": 0,
    "revenue_growth_min": -20,
}


@dataclass
class _Cache:
    """简单的字典缓存，避免同一股票重复请求。"""

    _store: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, symbol: str) -> dict[str, Any] | None:
        return self._store.get(symbol)

    def set(self, symbol: str, data: dict[str, Any]) -> None:
        self._store[symbol] = data

    def clear(self) -> None:
        self._store.clear()


# 全局缓存实例
_cache = _Cache()


def _load_thresholds() -> dict[str, Any]:
    """从 settings.yaml 加载基本面过滤阈值，缺失字段用默认值。"""
    cfg = settings().get("fundamental", {})
    merged = {**_DEFAULT_THRESHOLDS, **cfg}
    return merged


def _safe_float(value: Any) -> float | None:
    """安全转换为 float，失败返回 None。"""
    try:
        v = float(value)
        if pd.isna(v):
            return None
        return v
    except (ValueError, TypeError):
        return None


class FundamentalFilter:
    """基本面过滤器。

    使用 akshare 获取个股基本面数据并按阈值过滤。
    """

    def __init__(self) -> None:
        self._thresholds = _load_thresholds()

    def check(self, symbol: str) -> dict[str, Any]:
        """检查个股是否通过基本面过滤。

        Args:
            symbol: 股票代码，如 "600519"。

        Returns:
            过滤结果字典，包含 pass、pe、pb、roe、revenue_growth、industry、reasons。
        """
        # 查缓存
        cached = _cache.get(symbol)
        if cached is not None:
            logger.debug(f"基本面过滤：{symbol} 使用缓存数据")
            return cached

        code = symbol.strip().zfill(6)
        pe: float | None = None
        pb: float | None = None
        roe: float | None = None
        revenue_growth: float | None = None
        industry: str = ""

        # 1. 获取实时行情中的 PE/PB
        pe, pb = self._fetch_pe_pb(code)

        # 2. 获取个股信息（行业）
        industry = self._fetch_industry(code)

        # 3. 获取财务指标（ROE、营收增长率）
        roe, revenue_growth = self._fetch_financial(code)

        # 过滤判断
        reasons: list[str] = []
        passed = True

        # PE 过滤
        if pe is not None:
            pe_min = self._thresholds["pe_min"]
            pe_max = self._thresholds["pe_max"]
            if not (pe_min < pe < pe_max):
                passed = False
                if pe <= pe_min:
                    reasons.append(f"PE={pe:.2f} ≤ {pe_min}，亏损股")
                else:
                    reasons.append(f"PE={pe:.2f} ≥ {pe_max}，泡沫股")
            else:
                reasons.append(f"PE={pe:.2f} 在 ({pe_min}, {pe_max}) 范围内")
        else:
            reasons.append("PE 获取失败，跳过过滤")

        # PB 过滤
        if pb is not None:
            pb_min = self._thresholds["pb_min"]
            pb_max = self._thresholds["pb_max"]
            if not (pb_min < pb < pb_max):
                passed = False
                if pb <= pb_min:
                    reasons.append(f"PB={pb:.2f} ≤ {pb_min}，破净或异常")
                else:
                    reasons.append(f"PB={pb:.2f} ≥ {pb_max}，估值过高")
            else:
                reasons.append(f"PB={pb:.2f} 在 ({pb_min}, {pb_max}) 范围内")
        else:
            reasons.append("PB 获取失败，跳过过滤")

        # ROE 过滤
        if roe is not None:
            roe_min = self._thresholds["roe_min"]
            if roe <= roe_min:
                passed = False
                reasons.append(f"ROE={roe:.2f}% ≤ {roe_min}%，盈利能力不足")
            else:
                reasons.append(f"ROE={roe:.2f}% > {roe_min}%")
        else:
            reasons.append("ROE 获取失败，跳过过滤")

        # 营收增长率过滤
        if revenue_growth is not None:
            rg_min = self._thresholds["revenue_growth_min"]
            if revenue_growth <= rg_min:
                passed = False
                reasons.append(f"营收增长率={revenue_growth:.2f}% ≤ {rg_min}%，衰退股")
            else:
                reasons.append(f"营收增长率={revenue_growth:.2f}% > {rg_min}%")
        else:
            reasons.append("营收增长率获取失败，跳过过滤")

        result: dict[str, Any] = {
            "pass": passed,
            "pe": pe,
            "pb": pb,
            "roe": roe,
            "revenue_growth": revenue_growth,
            "industry": industry,
            "reasons": reasons,
        }

        # 写入缓存
        _cache.set(symbol, result)

        status = "通过" if passed else "未通过"
        logger.info(f"基本面过滤：{symbol} {status}，原因：{'; '.join(reasons)}")

        return result

    def _fetch_pe_pb(self, code: str) -> tuple[float | None, float | None]:
        """从实时行情快照获取 PE/PB。"""
        try:
            df = ak.stock_zh_a_spot_em()
            row = df[df["代码"] == code]
            if row.empty:
                logger.warning(f"基本面过滤：{code} 未在实时行情中找到")
                return None, None
            pe = _safe_float(row.iloc[0].get("市盈率-动态"))
            pb = _safe_float(row.iloc[0].get("市净率"))
            return pe, pb
        except Exception as e:
            logger.warning(f"基本面过滤：{code} 获取 PE/PB 失败: {e}")
            return None, None

    def _fetch_industry(self, code: str) -> str:
        """获取个股所属行业。"""
        try:
            df = ak.stock_individual_info_em(symbol=code)
            # 返回两列：item / value，找"行业"行
            industry_rows = df[df["item"] == "行业"]
            if not industry_rows.empty:
                return str(industry_rows.iloc[0]["value"])
            return ""
        except Exception as e:
            logger.warning(f"基本面过滤：{code} 获取行业失败: {e}")
            return ""

    def _fetch_financial(self, code: str) -> tuple[float | None, float | None]:
        """获取财务指标：ROE 和营收同比增长率。"""
        roe: float | None = None
        revenue_growth: float | None = None

        try:
            df = ak.stock_financial_analysis_indicator(symbol=code)
            if df.empty:
                logger.warning(f"基本面过滤：{code} 财务指标为空")
                return None, None

            # 取最新一期数据（第一行通常是最新的）
            latest = df.iloc[0]

            # ROE：尝试多个可能的列名
            for col in ("净资产收益率", "加权净资产收益率", "净资产收益率(%)"):
                if col in df.columns:
                    roe = _safe_float(latest[col])
                    if roe is not None:
                        break

            # 营收同比增长率
            for col in ("营业收入同比增长率(%)", "营业收入同比增长率", "营收同比增长率"):
                if col in df.columns:
                    revenue_growth = _safe_float(latest[col])
                    if revenue_growth is not None:
                        break

        except Exception as e:
            logger.warning(f"基本面过滤：{code} 获取财务指标失败: {e}")

        return roe, revenue_growth
