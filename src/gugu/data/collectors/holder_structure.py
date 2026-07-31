"""股东户数采集器：获取全市场股东户数及筹码结构数据。

数据源（按优先级降级）：
1. 主源：akshare ``ak.stock_zh_a_gdhs(symbol="最新")`` 全市场股东户数快照
2. 降级源：东方财富 datacenter-web HTTP 直连

缓存策略：
- 24 小时缓存到 ``data/holder_structure/{date}.parquet``
- 目录不存在时自动创建
- 缓存读取失败时降级到 API

返回字段（统一 schema）::

    DataFrame[
        symbol,              # 股票代码 "600519"
        name,                # 股票名称
        holder_num,          # 股东户数
        holder_num_change,   # 户数变化
        holder_num_pct,      # 户数变化率%
        avg_hold_amount,     # 户均持股数
        avg_hold_change,     # 户均持股变化率%
        end_date,            # 报告期 "2024-09-30"
        org_hold_pct,        # 机构持股比例%（可选，默认0）
    ]
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from gugu.config import PROJECT_ROOT
from gugu.utils.log import get_logger

logger = get_logger()

# 缓存目录（按日期分文件，同日 24h 内复用）
_CACHE_DIR = PROJECT_ROOT / "data" / "holder_structure"

# 东方财富 datacenter API（降级源）
_EM_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

# akshare 中文列名 → 统一字段映射（兼容不同 akshare 版本的列名差异）
_AK_COL_MAP: dict[str, str] = {
    "股票代码": "symbol",
    "股票简称": "name",
    "股东户数": "holder_num",
    "股东户数-增减": "holder_num_change",
    "股东户数-增减比例": "holder_num_pct",
    "环比变化": "holder_num_change",  # 别名（部分 akshare 版本）
    "环比变化率": "holder_num_pct",  # 别名
    "户均持股": "avg_hold_amount",
    "户均流通股": "avg_hold_amount",  # 别名
    "户均持股-增减": "avg_hold_change",
    "截止日期": "end_date",
    "机构持股比例": "org_hold_pct",
    # stock_zh_a_gdhs_detail_em 单股历史返回的列名别名
    "代码": "symbol",
    "名称": "name",
    "股东户数-本次": "holder_num",
    "股东户数-上次": "holder_num_prev",
    "户均持股市值": "avg_hold_amount",
    "户均持股数量": "avg_hold_amount",
    "股东户数统计截止日": "end_date",
    "股东户数公告日期": "announce_date",
}

# 东方财富 datacenter 列名 → 统一字段映射
_EM_COL_MAP: dict[str, str] = {
    "SECURITY_CODE": "symbol",
    "SECURITY_NAME_ABBR": "name",
    "HOLDER_NUMBER": "holder_num",
    "HOLDER_NUM_CHANGE": "holder_num_change",
    "AVG_HOLD_AMOUNT": "avg_hold_amount",
    "END_DATE": "end_date",
}

# 统一输出的列顺序
_OUTPUT_COLUMNS = [
    "symbol",
    "name",
    "holder_num",
    "holder_num_change",
    "holder_num_pct",
    "avg_hold_amount",
    "avg_hold_change",
    "end_date",
    "org_hold_pct",
]


def _compute_avg_hold_change(holder_num_pct: float) -> float:
    """由户数变化率推算户均持股变化率。

    假设总股本不变（单季度近似成立）：
    - holder_num 变化 p% → new_holder = old * (1 + p/100)
    - avg_hold = total_shares / holder_num → avg_hold 反向变化
    - avg_hold_change = -p / (100 + p) * 100

    Examples:
        holder_num_pct=30  → avg_hold_change ≈ -23.08%
        holder_num_pct=-20 → avg_hold_change ≈ +25.00%
    """
    if holder_num_pct is None or (isinstance(holder_num_pct, float) and pd.isna(holder_num_pct)):
        return 0.0
    denominator = 100.0 + holder_num_pct
    if abs(denominator) < 1e-6:
        return 0.0
    return round(-holder_num_pct / denominator * 100.0, 2)


class HolderStructureCollector:
    """股东户数采集器。

    主源 akshare 失败时降级到东方财富 datacenter HTTP 直连。
    24 小时 Parquet 缓存，避免重复请求。

    注意：
    - akshare ``stock_zh_a_gdhs`` 的 ``symbol`` 参数是日期选择（"最新"/"20240930"），
      不是股票代码过滤。单 symbol 查询时在 fetch 后按代码过滤。
    - 同步的 akshare 调用用 ``asyncio.to_thread`` 包装为异步。
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0, headers=_EM_HEADERS)
        return self._client

    async def fetch_holder_structure(self, symbol: str | None = None) -> pd.DataFrame | None:
        """获取全市场（或单股）股东户数及筹码结构快照。

        Args:
            symbol: 股票代码（如 ``"600519"``）。``None`` 时返回全市场快照。

        Returns:
            统一 schema 的 DataFrame；主源和降级源均失败时返回 ``None``。
        """
        # 1. 尝试缓存
        df = self._load_cache()
        cache_hit = df is not None

        if df is None:
            # 2. 主源 akshare
            df = await self._fetch_via_akshare()
            # 3. 降级源 eastmoney
            if df is None or df.empty:
                logger.warning("akshare 股东户数失败，降级到东方财富 datacenter")
                df = await self._fetch_via_eastmoney()
            # 4. 写缓存
            if df is not None and not df.empty:
                self._save_cache(df)
            else:
                logger.error("股东户数采集失败：主源和降级源均不可用")

        if df is None or df.empty:
            return None

        if not cache_hit:
            logger.info(f"股东户数快照采集成功: {len(df)} 只股票")

        # 5. 单 symbol 过滤（akshare stock_zh_a_gdhs 的 symbol 参数是日期，非股票代码）
        if symbol:
            code = str(symbol).strip().zfill(6)
            df = df[df["symbol"] == code].copy()

        return df.reset_index(drop=True) if not df.empty else df

    async def fetch_holder_history(self, symbol: str, quarters: int = 4) -> pd.DataFrame | None:
        """获取单股最近 N 个季度的股东户数历史数据。

        用于过滤器判断"连续 2 季度"趋势。

        Args:
            symbol: 股票代码（如 ``"600519"``）。
            quarters: 获取近 N 个季度（默认 4）。

        Returns:
            统一 schema 的 DataFrame（按 end_date 降序）；失败返回 ``None``。
        """
        code = str(symbol).strip().zfill(6)
        try:
            import akshare as ak

            # akshare stock_zh_a_gdhs_detail_em 返回单股历史股东户数
            # 同步函数，用 asyncio.to_thread 包装
            try:
                raw = await asyncio.to_thread(ak.stock_zh_a_gdhs_detail_em, symbol=code)
            except AttributeError:
                # 部分 akshare 版本无此函数
                logger.debug("akshare 无 stock_zh_a_gdhs_detail_em，历史数据不可用")
                return None

            if raw is None or raw.empty:
                return None

            df = self._normalize_akshare(raw)
            if df is None or df.empty:
                return None

            # 按 end_date 降序取最近 N 期
            df = df.sort_values("end_date", ascending=False).head(quarters)
            return df.reset_index(drop=True)
        except Exception as e:
            logger.warning(f"获取 {code} 股东户数历史失败: {e}")
            return None

    # ── 主源：akshare ──────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def _fetch_via_akshare(self) -> pd.DataFrame | None:
        """主源：akshare stock_zh_a_gdhs 获取全市场最新股东户数。"""
        try:
            import akshare as ak

            # stock_zh_a_gdhs 的 symbol 参数是日期选择（"最新"/"20240930"），非股票代码
            raw = await asyncio.to_thread(ak.stock_zh_a_gdhs, symbol="最新")
            if raw is None or raw.empty:
                logger.warning("akshare 股东户数返回空数据")
                return None
            return self._normalize_akshare(raw)
        except Exception as e:
            logger.error(f"akshare 股东户数采集失败: {e}")
            return None

    def _normalize_akshare(self, raw: pd.DataFrame) -> pd.DataFrame | None:
        """归一化 akshare 返回的中文列名为统一 schema。"""
        try:
            df = raw.rename(columns={k: v for k, v in _AK_COL_MAP.items() if k in raw.columns})
        except Exception as e:
            logger.error(f"akshare 股东户数列名映射失败: {e}")
            return None

        return self._finalize(df, source="akshare")

    # ── 降级源：东方财富 datacenter ────────────────────────────────

    async def _fetch_via_eastmoney(self) -> pd.DataFrame | None:
        """降级源：东方财富 datacenter HTTP 直连，分页拉取全市场数据。"""
        try:
            client = await self._get_client()
            all_rows: list[dict[str, Any]] = []
            page_number = 1
            page_size = 5000  # 单页 5000 条，A股约 5000+ 只，2 页可覆盖

            while True:
                params = {
                    "reportName": "RPT_F10_EH_HOLDERNUMBER",
                    "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,HOLDER_NUMBER,"
                    "HOLDER_NUM_CHANGE,AVG_HOLD_AMOUNT,END_DATE",
                    "filter": "(HOLDER_NUMBER>0)",
                    "pageNumber": str(page_number),
                    "pageSize": str(page_size),
                    "sortColumns": "END_DATE",
                    "sortTypes": "-1",
                }
                resp = await client.get(_EM_DATACENTER_URL, params=params)
                resp.raise_for_status()
                data = resp.json()

                if not data or not data.get("success") or not data.get("result"):
                    logger.warning(f"东方财富 datacenter 第 {page_number} 页返回空")
                    break

                result = data["result"]
                rows = result.get("data", [])
                if not rows:
                    break

                all_rows.extend(rows)

                # 判断是否还有下一页
                total_count = int(result.get("count", 0))
                if len(all_rows) >= total_count or len(rows) < page_size:
                    break
                page_number += 1

            if not all_rows:
                logger.warning("东方财富 datacenter 股东户数返回空数据")
                return None

            raw = pd.DataFrame(all_rows)
            return self._normalize_eastmoney(raw)
        except Exception as e:
            logger.error(f"东方财富 datacenter 股东户数采集失败: {e}")
            return None

    def _normalize_eastmoney(self, raw: pd.DataFrame) -> pd.DataFrame | None:
        """归一化东方财富 datacenter 返回的英文列名为统一 schema。"""
        try:
            df = raw.rename(columns={k: v for k, v in _EM_COL_MAP.items() if k in raw.columns})
        except Exception as e:
            logger.error(f"东方财富 datacenter 列名映射失败: {e}")
            return None

        return self._finalize(df, source="eastmoney")

    # ── 统一字段补全 ───────────────────────────────────────────────

    def _finalize(self, df: pd.DataFrame, source: str) -> pd.DataFrame | None:
        """统一字段类型、补全缺失列、计算派生字段。"""
        try:
            # P0 修复：处理重复列名（akshare 可能返回重复列）
            # 重复列名会导致 df[col] 返回 DataFrame 而非 Series，引发下游错误
            if df.columns.duplicated().any():
                dup_cols = df.columns[df.columns.duplicated()].tolist()
                logger.warning(f"akshare 返回重复列名 {dup_cols}，保留首次出现列")
                df = df.loc[:, ~df.columns.duplicated(keep="first")]

            # 数值列转换（确保是 Series 而非 DataFrame）
            for col in ("holder_num", "holder_num_change", "avg_hold_amount"):
                if col in df.columns:
                    col_data = df[col]
                    # 防御：如果列重复导致返回 DataFrame，取第一列
                    if isinstance(col_data, pd.DataFrame):
                        col_data = col_data.iloc[:, 0]
                    df[col] = pd.to_numeric(col_data, errors="coerce").fillna(0)
            if "org_hold_pct" in df.columns:
                org_data = df["org_hold_pct"]
                if isinstance(org_data, pd.DataFrame):
                    org_data = org_data.iloc[:, 0]
                df["org_hold_pct"] = pd.to_numeric(org_data, errors="coerce").fillna(0.0)
            else:
                df["org_hold_pct"] = 0.0

            # holder_num_pct：东方财富不返回，需从 holder_num_change 推算
            if "holder_num_pct" not in df.columns or df["holder_num_pct"].isna().all():
                df["holder_num_pct"] = df.apply(
                    lambda r: self._compute_pct_from_change(
                        float(r.get("holder_num", 0)),
                        float(r.get("holder_num_change", 0)),
                    ),
                    axis=1,
                )
            else:
                pct_data = df["holder_num_pct"]
                if isinstance(pct_data, pd.DataFrame):
                    pct_data = pct_data.iloc[:, 0]
                df["holder_num_pct"] = pd.to_numeric(pct_data, errors="coerce").fillna(0.0)

            # holder_num_change 缺失时补 0
            if "holder_num_change" not in df.columns:
                df["holder_num_change"] = 0.0
            else:
                change_data = df["holder_num_change"]
                if isinstance(change_data, pd.DataFrame):
                    change_data = change_data.iloc[:, 0]
                df["holder_num_pct"] = pd.to_numeric(pct_data, errors="coerce").fillna(0.0)
                df["holder_num_change"] = pd.to_numeric(change_data, errors="coerce").fillna(0.0)

            # avg_hold_change：若数据源未直接提供，由 holder_num_pct 反推
            # 假设总股本不变：avg_hold_change = -p / (100 + p) * 100
            if "avg_hold_change" not in df.columns or df["avg_hold_change"].isna().all():
                df["avg_hold_change"] = df["holder_num_pct"].apply(_compute_avg_hold_change)
            else:
                avg_change_data = df["avg_hold_change"]
                if isinstance(avg_change_data, pd.DataFrame):
                    avg_change_data = avg_change_data.iloc[:, 0]
                df["avg_hold_change"] = pd.to_numeric(avg_change_data, errors="coerce").fillna(0.0)

            # symbol 归一化为 6 位
            if "symbol" in df.columns:
                df["symbol"] = df["symbol"].astype(str).str.strip().str.zfill(6)

            # name 缺失时用 symbol 占位
            if "name" not in df.columns:
                df["name"] = df["symbol"]
            df["name"] = df["name"].fillna(df["symbol"]).astype(str)

            # end_date 归一化为字符串
            if "end_date" in df.columns:
                df["end_date"] = df["end_date"].astype(str).str[:10]

            # 补全输出列
            for col in _OUTPUT_COLUMNS:
                if col not in df.columns:
                    df[col] = 0.0

            df = df[_OUTPUT_COLUMNS]
            # 过滤无效行（holder_num <= 0 的数据无意义）
            df = df[df["holder_num"] > 0]
            logger.debug(f"股东户数归一化完成 ({source}): {len(df)} 行")
            return df.reset_index(drop=True)
        except Exception as e:
            logger.error(f"股东户数字段归一化失败 ({source}): {e}")
            return None

    @staticmethod
    def _compute_pct_from_change(holder_num: float, holder_num_change: float) -> float:
        """由当前户数和变化量推算变化率%。

        prev = holder_num - holder_num_change
        pct = holder_num_change / prev * 100
        """
        prev = holder_num - holder_num_change
        if abs(prev) < 1e-6:
            return 0.0
        return round(holder_num_change / prev * 100.0, 2)

    # ── 缓存 ──────────────────────────────────────────────────────

    @staticmethod
    def _cache_path() -> Path:
        """当日缓存文件路径（按日期命名，同日复用）。"""
        from datetime import date as _date

        return _CACHE_DIR / f"{_date.today().isoformat()}.parquet"

    def _load_cache(self) -> pd.DataFrame | None:
        """加载当日缓存。缓存不存在或读取失败时返回 None。"""
        try:
            path = self._cache_path()
            if not path.exists():
                return None
            df = pd.read_parquet(path)
            if df.empty:
                return None
            logger.debug(f"股东户数缓存命中: {path}")
            return df
        except Exception as e:
            logger.warning(f"股东户数缓存读取失败，降级到 API: {e}")
            return None

    def _save_cache(self, df: pd.DataFrame) -> None:
        """写入当日缓存。目录不存在时自动创建。"""
        try:
            path = self._cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=False)
            logger.debug(f"股东户数缓存写入: {path}")
        except Exception as e:
            logger.warning(f"股东户数缓存写入失败（不影响功能）: {e}")
