"""数据质量校验。采集后必须校验，不合格数据不进系统。

使用链式校验（Chain of Responsibility）模式：
- ValidationRule：规则基类/协议
- DataValidator：规则链，支持链式调用
- 预构建校验器：STOCK_HISTORY_VALIDATOR / SECTOR_FLOW_VALIDATOR / STOCK_FLOW_VALIDATOR
- 兼容原 validate_stock_history / validate_sector_flow / validate_stock_flow 函数
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, timedelta

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class DataQualityError(Exception):
    """数据质量异常。"""


# ──────────────────────────────────────────────
# 1. 校验规则抽象基类
# ──────────────────────────────────────────────


class ValidationRule(ABC):
    """校验规则基类。每条规则实现 check(df, symbol) -> (passed, message)。

    check 方法可以修改传入的 df 对象（inplace），修改会传播到调用方。
    若需返回新 DataFrame 而非 inplace 修改，返回 (passed, new_df, message) 三元组。
    """

    @abstractmethod
    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        """执行校验。

        Args:
            df: 待校验数据（可 inplace 修改）。
            symbol: 股票代码，用于日志上下文。

        Returns:
            二元组 (passed, message) 或三元组 (passed, new_df, message)。
            passed: True=通过, False=失败。
            message: 描述信息。
        """
        ...


# ──────────────────────────────────────────────
# 2. 具体校验规则类
# ──────────────────────────────────────────────


class RequiredColumnsRule(ValidationRule):
    """必需列检查 + 空值处理。缺失列直接抛异常，空值前向/后向填充后剔除剩余空行。"""

    def __init__(self, required: list[str], context: str = "") -> None:
        self._required = required
        self._context = context

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if df.empty:
            raise DataQualityError(f"[{self._context}] {symbol} 数据为空")
        missing = [c for c in self._required if c not in df.columns]
        if missing:
            raise DataQualityError(f"[{self._context}] {symbol} 缺失列: {missing}")
        null_count = df[self._required].isnull().sum().sum()
        result = df
        if null_count > 0:
            logger.warning(f"[{self._context}] {symbol} 存在 {null_count} 个空值，将前向填充")
            result = df.ffill(limit=3).bfill(limit=3)
            result = result.dropna(subset=self._required).reset_index(drop=True)
        return True, result, "required columns ok"


class NonNegativeValuesRule(ValidationRule):
    """数值列非负检查。剔除负值行。"""

    def __init__(self, columns: list[str]) -> None:
        self._columns = columns

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        for col in self._columns:
            if col not in df.columns:
                continue
            mask = df[col] < 0
            if mask.any():
                bad = df[mask]
                logger.warning(f"{symbol} {col} 存在负值，已剔除: {len(bad)} 行")
                df.drop(mask.index[mask], inplace=True)
        return True, "non-negative check done"


class ZeroPriceRule(ValidationRule):
    """收盘零价检测。为零的 close 视为异常数据，剔除。"""

    def __init__(self, price_column: str = "close") -> None:
        self._price_column = price_column

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if self._price_column not in df.columns:
            return True, "price column not present, skip"
        mask = df[self._price_column] == 0
        if mask.any():
            logger.warning(f"{symbol} {self._price_column} 存在零价，已剔除: {mask.sum()} 行")
            df.drop(mask.index[mask], inplace=True)
        return True, "zero price check done"


class HighLowConsistencyRule(ValidationRule):
    """高低价逻辑检查：high >= low。违反的行剔除。"""

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if "high" not in df.columns or "low" not in df.columns:
            return True, "high/low columns not present, skip"
        mask = df["high"] < df["low"]
        if mask.any():
            logger.warning(f"{symbol} high<low 异常，已剔除: {mask.sum()} 行")
            df.drop(mask.index[mask], inplace=True)
        return True, "high-low consistency check done"


class FreshnessRule(ValidationRule):
    """数据时效性检查。最新数据不应超过指定自然日数（长假期间放宽）。"""

    def __init__(
        self,
        max_stale_days: int = 7,
        holiday_max_days: int = 15,
        date_column: str = "date",
    ) -> None:
        self._max_stale_days = max_stale_days
        self._holiday_max_days = holiday_max_days
        self._date_column = date_column

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if df.empty or self._date_column not in df.columns:
            return True, "no date column, skip freshness check"

        latest = pd.to_datetime(df[self._date_column].iloc[-1]).date()
        calendar_days = (date.today() - latest).days

        if calendar_days <= self._max_stale_days:
            return True, "data is fresh"

        # 超过阈值，检查是否为长假
        try:
            import chinese_calendar as ccal

            trade_days = 0
            d = latest
            while d <= date.today():
                if ccal.is_workday(d):
                    trade_days += 1
                d += timedelta(days=1)

            if trade_days <= 1 and calendar_days <= self._holiday_max_days:
                logger.debug(
                    f"{symbol} 长假期间允许较长间隔: "
                    f"自然日 {calendar_days} 天，交易日 {trade_days} 天"
                )
                return True, "holiday period, freshness waived"
            else:
                msg = (
                    f"{symbol} 最新数据日期 {latest} 超过 {self._max_stale_days} 天 "
                    f"（{calendar_days} 自然日 / {trade_days} 交易日），数据可能过期"
                )
                logger.warning(msg)
                return False, msg
        except ImportError:
            if calendar_days > self._holiday_max_days:
                msg = (
                    f"{symbol} 最新数据日期 {latest} 超过 {self._holiday_max_days} 天"
                    f"（{calendar_days} 天），数据可能过期"
                )
                logger.warning(msg)
                return False, msg
            else:
                logger.debug(
                    f"{symbol} chinese_calendar 不可用，长假期间放宽到 {self._holiday_max_days} 天: "
                    f"{calendar_days} 天"
                )
                return True, "holiday period (no ccal lib), freshness waived"


class SortByDateRule(ValidationRule):
    """按日期升序排列。"""

    def __init__(self, date_column: str = "date") -> None:
        self._date_column = date_column

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if self._date_column not in df.columns:
            return True, "date column not present, skip sort"
        df.sort_values(self._date_column, inplace=True)
        df.reset_index(drop=True, inplace=True)
        return True, "sorted by date ascending"


class DeduplicateByColumnRule(ValidationRule):
    """按指定列去重，保留首次出现的行。"""

    def __init__(self, column: str, keep: str = "first") -> None:
        self._column = column
        self._keep = keep

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if self._column in df.columns:
            before = len(df)
            df.drop_duplicates(subset=[self._column], keep=self._keep, inplace=True)
            df.reset_index(drop=True, inplace=True)
            removed = before - len(df)
            if removed:
                logger.debug(f"{symbol} 按 {self._column} 去重，移除 {removed} 行")
        return True, "dedup done"


# ──────────────────────────────────────────────
# 3. DataValidator — 规则链
# ──────────────────────────────────────────────


class DataValidator:
    """数据校验器，以链式方式组合多个 ValidationRule。"""

    def __init__(self, rules: list[ValidationRule] | None = None) -> None:
        self._rules: list[ValidationRule] = rules[:] if rules else []

    def add_rule(self, rule: ValidationRule) -> DataValidator:
        """添加校验规则，返回 self 支持链式调用。"""
        self._rules.append(rule)
        return self

    def validate(self, df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
        """对 DataFrame 逐条执行校验规则链。

        规则可以 inplace 修改 df，或通过 3 元组返回新 DataFrame。
        如果规则抛出异常，校验链中断并向上传播。
        """
        result = df.copy()
        for rule in self._rules:
            ret = rule.check(result, symbol)
            # 支持 2 元组 (passed, msg) 和 3 元组 (passed, new_df, msg)
            if len(ret) == 3:
                _, new_df, _ = ret
                result = new_df
            elif len(ret) == 2:
                passed, msg = ret
                if not passed:
                    logger.warning(f"[quality] {msg}")
        return result


# ──────────────────────────────────────────────
# 4. 预构建校验器
# ──────────────────────────────────────────────

STOCK_HISTORY_VALIDATOR: DataValidator = (
    DataValidator()
    .add_rule(RequiredColumnsRule(
        required=["date", "open", "high", "low", "close", "volume"],
        context="stock_history",
    ))
    .add_rule(NonNegativeValuesRule(
        columns=["open", "high", "low", "close", "volume"],
    ))
    .add_rule(ZeroPriceRule(price_column="close"))
    .add_rule(HighLowConsistencyRule())
    .add_rule(FreshnessRule())
    .add_rule(SortByDateRule())
)

SECTOR_FLOW_VALIDATOR: DataValidator = (
    DataValidator()
    .add_rule(RequiredColumnsRule(
        required=["sector", "main_net", "main_pct"],
        context="sector_flow",
    ))
    .add_rule(DeduplicateByColumnRule(column="sector"))
)

STOCK_FLOW_VALIDATOR: DataValidator = (
    DataValidator()
    .add_rule(RequiredColumnsRule(
        required=["main_net", "main_pct"],
        context="stock_flow",
    ))
    .add_rule(SortByDateRule())
)


# ──────────────────────────────────────────────
# 5. 向后兼容的薄包装函数
# ──────────────────────────────────────────────


def validate_stock_history(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """校验个股历史行情数据。"""
    return STOCK_HISTORY_VALIDATOR.validate(df, symbol)


def validate_sector_flow(df: pd.DataFrame) -> pd.DataFrame:
    """校验行业资金流数据。"""
    return SECTOR_FLOW_VALIDATOR.validate(df)


def validate_stock_flow(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """校验个股资金流数据。"""
    return STOCK_FLOW_VALIDATOR.validate(df, symbol)