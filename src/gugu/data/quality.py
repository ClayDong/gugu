"""数据质量校验。采集后必须校验，不合格数据不进系统。"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class DataQualityError(Exception):
    """数据质量异常。"""


def validate_stock_history(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """校验个股历史行情数据。

    检查项：必需列、缺失值、异常值（负值/零价）、high>=low、时间排序、数据时效性。
    """
    required = ["date", "open", "high", "low", "close", "volume"]
    df = _check_required(df, required, symbol, "stock_history")

    # 数值非负
    for col in ["open", "high", "low", "close", "volume"]:
        if (df[col] < 0).any():
            bad = df[df[col] < 0]
            logger.warning(f"{symbol} {col} 存在负值，已剔除: {len(bad)} 行")
            df = df[df[col] >= 0]

    # 零价检测：close=0 意味着数据异常（停牌不应返回 0 价）
    zero_price = df[df["close"] == 0]
    if not zero_price.empty:
        logger.warning(f"{symbol} close 存在零价，已剔除: {len(zero_price)} 行")
        df = df[df["close"] > 0]

    # high >= low
    invalid = df[df["high"] < df["low"]]
    if not invalid.empty:
        logger.warning(f"{symbol} high<low 异常，已剔除: {len(invalid)} 行")
        df = df[df["high"] >= df["low"]]

    # 数据时效性：最新数据不应超过 7 天（排除节假日）
    if not df.empty and "date" in df.columns:
        latest = pd.to_datetime(df["date"].iloc[-1]).date()
        max_stale_days = 7
        # 延长节假日期间允许更长的间隔
        if latest < date.today() - timedelta(days=max_stale_days):
            logger.warning(
                f"{symbol} 最新数据日期 {latest} 超过 {max_stale_days} 天，数据可能过期"
            )

    # 按日期升序
    df = df.sort_values("date").reset_index(drop=True)
    return df


def validate_sector_flow(df: pd.DataFrame) -> pd.DataFrame:
    """校验行业资金流数据。"""
    required = ["sector", "main_net", "main_pct"]
    df = _check_required(df, required, "", "sector_flow")
    # 去重（同行业保留最新）
    df = df.drop_duplicates(subset=["sector"], keep="first").reset_index(drop=True)
    return df


def validate_stock_flow(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """校验个股资金流数据。"""
    required = ["main_net", "main_pct"]
    df = _check_required(df, required, symbol, "stock_flow")
    if "date" in df.columns:
        df = df.sort_values("date").reset_index(drop=True)
    return df


def _check_required(
    df: pd.DataFrame, required: list[str], symbol: str, ctx: str
) -> pd.DataFrame:
    if df.empty:
        raise DataQualityError(f"[{ctx}] {symbol} 数据为空")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise DataQualityError(f"[{ctx}] {symbol} 缺失列: {missing}")
    null_count = df[required].isnull().sum().sum()
    if null_count > 0:
        logger.warning(f"[{ctx}] {symbol} 存在 {null_count} 个空值，将前向填充")
        # 限制连续缺失不超过 3 个交易日，防止长期停牌数据被错误填充
        df = df.ffill(limit=3).bfill(limit=3)
        # 填充后仍有空值则剔除
        df = df.dropna(subset=required).reset_index(drop=True)
    return df