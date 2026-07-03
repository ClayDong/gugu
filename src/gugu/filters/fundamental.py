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
import httpx
import pandas as pd

from gugu.config import get_config
from gugu.utils.log import get_logger

logger = get_logger()

# 东方财富 push2 stock/get 直连（akshare 失败时的兜底源）
_EM_STOCK_GET_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}


def _em_secid(code: str) -> str:
    """股票代码 → 东方财富 secid 格式（如 1.600519）。"""
    prefix = "1" if code[0] in ("6", "9") else "0"
    return f"{prefix}.{code}"

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


# 模块级 _cache 已被替代为实例级缓存（P1 修复）
# 保留 _Cache 类定义和 DataClass 但不再实例化


@dataclass
class _SnapshotCache:
    """P1 修复：全市场快照缓存（避免每次 check 都拉全量数据）。

    缓存整个 ak.stock_zh_a_spot_em() 返回的 DataFrame，
    盘中有效期内复用，减少 HTTP 请求。
    """

    _df: Any = None  # pd.DataFrame | None
    _cached_at: float = 0.0
    _ttl: float = 120.0  # 缓存 2 分钟（与 data.fetch_interval_seconds 一致）

    def get(self) -> Any:
        import time

        if self._df is not None and time.time() - self._cached_at < self._ttl:
            return self._df
        return None

    def set(self, df: Any) -> None:
        import time

        self._df = df
        self._cached_at = time.time()


def _load_thresholds() -> dict[str, Any]:
    """从 AppConfig 加载基本面过滤阈值，缺失字段用默认值。"""
    try:
        fc = get_config().fundamental
        cfg = {
            "pe_min": fc.pe_min,
            "pe_max": fc.pe_max,
            "pb_min": fc.pb_min,
            "pb_max": fc.pb_max,
            "roe_min": fc.roe_min,
            "revenue_growth_min": fc.revenue_growth_min,
        }
    except Exception:
        cfg = {}
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
        # P1 修复：实例级缓存，避免测试间相互污染
        self._cache = _Cache()
        self._snapshot_cache = _SnapshotCache()

    def check(self, symbol: str) -> dict[str, Any]:
        """检查个股是否通过基本面过滤。

        Args:
            symbol: 股票代码，如 "600519"。

        Returns:
            过滤结果字典，包含 pass、pe、pb、roe、revenue_growth、industry、reasons。
        """
        # 查缓存
        cached = self._cache.get(symbol)
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

        # P0 修复：全失败硬约束——PE/PB/ROE 三项指标全部获取失败时，
        # 基本面过滤器不可"放行通过"（原逻辑 passed=True 默认放行，等于跳过过滤）
        # 全部失败说明数据源不可用，应拒绝以防止"无基本面数据即放行"的逆向选择
        if pe is None and pb is None and roe is None and revenue_growth is None:
            passed = False
            reasons.append("全部基本面指标获取失败，拒绝放行（数据源不可用）")
            logger.warning(
                f"基本面过滤：{code} 全部指标获取失败（PE/PB/ROE/营收均 None），"
                f"拒绝放行。akshare 与东方财富兜底均不可用。"
            )

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
        self._cache.set(symbol, result)

        status = "通过" if passed else "未通过"
        logger.info(f"基本面过滤：{symbol} {status}，原因：{'; '.join(reasons)}")

        return result

    def _fetch_pe_pb(self, code: str) -> tuple[float | None, float | None]:
        """从实时行情快照获取 PE/PB（akshare 主源 + 东方财富兜底，P0 修复）。"""
        pe, pb = self._fetch_pe_pb_akshare(code)
        if pe is None and pb is None:
            # akshare 全失败 → 东方财富兜底
            pe_em, pb_em = self._fetch_pe_pb_eastmoney(code)
            if pe_em is not None:
                pe = pe_em
            if pb_em is not None:
                pb = pb_em
        return pe, pb

    def _fetch_pe_pb_akshare(self, code: str) -> tuple[float | None, float | None]:
        """akshare 主源获取 PE/PB（带全量快照缓存）。"""
        try:
            df = self._snapshot_cache.get()
            if df is None:
                df = ak.stock_zh_a_spot_em()
                self._snapshot_cache.set(df)
            row = df[df["代码"] == code]
            if row.empty:
                logger.warning(f"基本面过滤：{code} akshare 未在实时行情中找到")
                return None, None
            pe = _safe_float(row.iloc[0].get("市盈率-动态"))
            pb = _safe_float(row.iloc[0].get("市净率"))
            return pe, pb
        except Exception as e:
            logger.warning(f"基本面过滤：{code} akshare 获取 PE/PB 失败: {e}")
            return None, None

    def _fetch_pe_pb_eastmoney(self, code: str) -> tuple[float | None, float | None]:
        """东方财富 push2 stock/get 兜底获取 PE/PB（P0 修复新增）。

        使用单股查询，避免触发全市场快照限流。
        字段：f162=市盈率(TTM)，f167=市净率。
        """
        try:
            params = {
                "secid": _em_secid(code),
                "fields": "f162,f167",
            }
            with httpx.Client(timeout=8.0, headers=_EM_HEADERS) as client:
                resp = client.get(_EM_STOCK_GET_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
            if not data or not data.get("data"):
                logger.warning(f"基本面过滤：{code} 东方财富 push2 返回空数据")
                return None, None
            d = data["data"]
            pe = _safe_float(d.get("f162"))
            pb = _safe_float(d.get("f167"))
            if pe is not None or pb is not None:
                logger.info(f"基本面过滤：{code} 东方财富兜底成功 PE={pe} PB={pb}")
            return pe, pb
        except Exception as e:
            logger.warning(f"基本面过滤：{code} 东方财富兜底 PE/PB 失败: {e}")
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
