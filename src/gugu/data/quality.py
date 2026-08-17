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
from typing import Any

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
        result = df.copy()
        total_removed = 0
        for col in self._columns:
            if col not in result.columns:
                continue
            mask = result[col] < 0
            if mask.any():
                n_removed = int(mask.sum())
                total_removed += n_removed
                logger.warning(f"{symbol} {col} 存在负值，已剔除: {n_removed} 行")
                result = result[~mask].reset_index(drop=True)
        if total_removed > 0:
            return True, result, f"non-negative check done, removed {total_removed} rows"
        return True, result, "non-negative check done"


class ZeroPriceRule(ValidationRule):
    """收盘零价检测。为零的 close 视为异常数据，剔除。"""

    def __init__(self, price_column: str = "close") -> None:
        self._price_column = price_column

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if self._price_column not in df.columns:
            return True, df, "price column not present, skip"
        mask = df[self._price_column] == 0
        if mask.any():
            n_removed = int(mask.sum())
            logger.warning(f"{symbol} {self._price_column} 存在零价，已剔除: {n_removed} 行")
            result = df[~mask].reset_index(drop=True)
            return True, result, f"zero price check done, removed {n_removed} rows"
        return True, df, "zero price check done"


class PlausiblePriceRule(ValidationRule):
    """价格合理性检查：最新收盘价不应偏离过去 N 日 MA 超过 max_deviation 倍。

    用于捕获数据源返回明显错误的价格（如 akshare 被封时的垃圾数据）。
    若数据不足 N 行则跳过检查。
    """

    def __init__(
        self,
        price_column: str = "close",
        ma_window: int = 20,
        max_deviation: float = 10.0,
    ) -> None:
        self._price_column = price_column
        self._ma_window = ma_window
        self._max_deviation = max_deviation

    def _tiered_max_deviation(self, ma: float) -> float:
        """按 MA 价格区间返回分级偏离阈值。

        低价股（<¥20）相对波动大，用宽松阈值；高价股（>¥200）波动小，用严格阈值。
        ``max_deviation`` 参数作为全局上限，分级阈值取 min(分级, max_deviation)。

        分级依据（A股经验值）：
        - MA < ¥20：8.0x（低价投机股极端拉升常见）
        - ¥20 ≤ MA < ¥200：5.0x（主流个股）
        - MA ≥ ¥200：3.0x（高价股，3x 偏离几乎必然是数据错误）
        """
        if ma < 20:
            return min(8.0, self._max_deviation)
        elif ma < 200:
            return min(5.0, self._max_deviation)
        else:
            return min(3.0, self._max_deviation)

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if self._price_column not in df.columns or len(df) <= self._ma_window:
            return True, "insufficient data, skip plausibility check"
        prices = df[self._price_column].values.astype(float)
        latest = prices[-1]
        ma = float(prices[-self._ma_window - 1 : -1].mean())

        if ma <= 0 or latest <= 0:
            return True, "zero or negative price/MA, skip plausibility check"

        # Check 1: 最新价偏离历史 MA 过大 → 数据源可能返回错误
        # 分级阈值：低价股波动大（宽松），高价股波动小（严格）
        # 如 ¥5 低价股 8x 偏离可能是投机拉升，但 ¥1500 高价股 3x 偏离几乎必然是数据错误
        tiered_max_dev = self._tiered_max_deviation(ma)
        _denom = min(latest, ma)
        deviation = max(latest, ma) / _denom if _denom > 0 else float("inf")
        if deviation > tiered_max_dev:
            msg = (
                f"{symbol} 最新价 {latest:.2f} 偏离 {self._ma_window}日MA {ma:.2f} "
                f"({deviation:.1f}x > 阈值 {tiered_max_dev:.1f}x)，"
                f"怀疑数据源返回错误数据，丢弃"
            )
            logger.warning(msg)
            return False, pd.DataFrame(), msg

        # Check 2: 全区间价格变化过小（如所有价格完全相同）→ 数据源可能被封锁
        # P3 修复：增加成交量变化检查，避免低波动蓝筹股被误判为封锁
        # 只有当成交量也几乎无变化（<1% 波动）时才判定为数据源封锁
        p_min, p_max = float(prices.min()), float(prices.max())
        if p_min > 0 and p_max > 0:
            range_ratio = p_max / p_min
            if range_ratio < 1.005:  # 60 天内波动 < 0.5%，极不自然
                # 检查成交量是否也几无变化
                vol_col = "volume"
                if vol_col in df.columns and len(df) > self._ma_window:
                    vol_values = df[vol_col].values[-self._ma_window :].astype(float)
                    v_min, v_max = vol_values.min(), vol_values.max()
                    if v_min > 0 and v_max / v_min < 1.01:
                        msg = (
                            f"{symbol} 全区间价格成交量均无波动 (price_ratio={range_ratio:.4f}, "
                            f"vol_ratio={v_max / v_min:.4f})，怀疑数据源被封锁返回静态数据，丢弃"
                        )
                        logger.warning(msg)
                        return False, pd.DataFrame(), msg
                    else:
                        # BUG-02 修复：价格平坦 (<0.5%) 但成交量真实变化，且绝对低价 (<50)，
                        # 这是数据源返回了错误的价格字段（成交量来自正确源但价格被污染）。
                        # 真实股票的 60 天波动不可能 <0.5%（即使是最稳定的工商银行也 >1%）。
                        if p_max < 50.0:
                            msg = (
                                f"{symbol} 价格平坦但成交量有变化 (price_ratio={range_ratio:.4f}, "
                                f"p_max={p_max:.2f})，价格字段来自错误数据源，丢弃"
                            )
                            logger.warning(msg)
                            return False, pd.DataFrame(), msg
                        # 价格稳定但成交量有变化 = 低波动蓝筹，合理，放行
                        logger.debug(
                            f"{symbol} 价格稳定 ({range_ratio:.4f}) 但成交量有变化 "
                            f"({v_max / v_min:.2f})，判断为低波动股，放行"
                        )

        # Check 3: 绝对价格下限检查（BUG-02 修复）
        # 捕获全区间一致性错误数据（如所有价格 ~10，MA=10，无偏差）
        # 当 max_price < 0.5 元时，判定为数据源返回了分单位或错误数据
        if p_min > 0 and p_max < 0.5:
            msg = (
                f"{symbol} 全区间价格低于 0.5 元 (p_max={p_max:.4f})，"
                f"怀疑数据源返回了错误数据（分/毛单位或占位数据），丢弃"
            )
            logger.warning(msg)
            return False, pd.DataFrame(), msg

        return True, "price is plausible"


class HighLowConsistencyRule(ValidationRule):
    """高低价逻辑检查：high >= low。违反的行剔除。"""

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if "high" not in df.columns or "low" not in df.columns:
            return True, df, "high/low columns not present, skip"
        mask = df["high"] < df["low"]
        if mask.any():
            n_removed = int(mask.sum())
            logger.warning(f"{symbol} high<low 异常，已剔除: {n_removed} 行")
            result = df[~mask].reset_index(drop=True)
            return True, result, f"high-low consistency check done, removed {n_removed} rows"
        return True, df, "high-low consistency check done"


class FreshnessRule(ValidationRule):
    """数据时效性检查。最新数据不应超过指定自然日数（长假期间放宽）。

    now 参数（可选）：参考日期，默认 date.today()。测试注入用——缓存/回测
    场景的时间基准必须与数据新鲜度判定一致，否则缓存测试随时间漂移变红
    （cache.get 注入 now 但质量校验仍用真实今天 → 基准不一致 bug，2026-08-12 修复）。
    """

    def __init__(
        self,
        max_stale_days: int = 7,
        holiday_max_days: int = 15,
        date_column: str = "date",
        now: date | None = None,
    ) -> None:
        self._max_stale_days = max_stale_days
        self._holiday_max_days = holiday_max_days
        self._date_column = date_column
        self._now = now

    def check(self, df: pd.DataFrame, symbol: str = "") -> tuple:
        if df.empty or self._date_column not in df.columns:
            return True, "no date column, skip freshness check"

        latest = pd.to_datetime(df[self._date_column].iloc[-1]).date()
        ref_date = self._now or date.today()
        calendar_days = (ref_date - latest).days

        if calendar_days <= self._max_stale_days:
            return True, "data is fresh"

        # 超过阈值，检查是否为长假
        try:
            import chinese_calendar as ccal

            trade_days = 0
            # P2-8 修复：从 latest+1 开始计数（latest 本身已有数据）
            d = latest + timedelta(days=1)
            while d <= ref_date:
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

        BUG-01 修复：2 元组 passed=False 现在硬拒绝，返回空 DataFrame。
        之前的代码只打了 warning 就继续，导致 FreshnessRule 的过时数据拒绝被静默忽略。
        """
        result = df.copy()
        for rule in self._rules:
            ret = rule.check(result, symbol)
            # 支持 2 元组 (passed, msg) 和 3 元组 (passed, new_df, msg)
            if len(ret) == 3:
                _, new_df, msg = ret
                if new_df.empty and not result.empty:
                    logger.warning(
                        f"[quality] {symbol} 规则 {type(rule).__name__} 返回空 DF: {msg}"
                    )
                    return new_df
                result = new_df
            elif len(ret) == 2:
                passed, msg = ret
                if not passed:
                    logger.warning(f"[quality] {symbol} 规则 {type(rule).__name__} 拒绝: {msg}")
                    # BUG-01 修复：硬拒绝，返回空 DataFrame
                    return pd.DataFrame()
        return result


# ──────────────────────────────────────────────
# 4. 预构建校验器
# ──────────────────────────────────────────────

STOCK_HISTORY_VALIDATOR: DataValidator = (
    DataValidator()
    .add_rule(
        RequiredColumnsRule(
            required=["date", "open", "high", "low", "close", "volume"],
            context="stock_history",
        )
    )
    .add_rule(
        NonNegativeValuesRule(
            columns=["open", "high", "low", "close", "volume"],
        )
    )
    .add_rule(ZeroPriceRule(price_column="close"))
    .add_rule(PlausiblePriceRule(price_column="close", ma_window=20, max_deviation=10.0))
    .add_rule(HighLowConsistencyRule())
    .add_rule(FreshnessRule())
    .add_rule(SortByDateRule())
)

SECTOR_FLOW_VALIDATOR: DataValidator = (
    DataValidator()
    .add_rule(
        RequiredColumnsRule(
            required=["sector", "main_net", "main_pct"],
            context="sector_flow",
        )
    )
    .add_rule(DeduplicateByColumnRule(column="sector"))
)

STOCK_FLOW_VALIDATOR: DataValidator = (
    DataValidator()
    .add_rule(
        RequiredColumnsRule(
            required=["main_net", "main_pct"],
            context="stock_flow",
        )
    )
    .add_rule(SortByDateRule())
)


# ──────────────────────────────────────────────
# 5. 向后兼容的薄包装函数
# ──────────────────────────────────────────────


def validate_stock_history(
    df: pd.DataFrame, symbol: str = "", now: date | None = None
) -> pd.DataFrame:
    """校验个股历史行情数据。

    Args:
        df: 行情 DataFrame
        symbol: 股票代码
        now: 参考日期（测试/缓存注入用）。传入时以该日期为新鲜度基准，
            否则用 date.today()。修复时间基准不一致 bug（2026-08-12）：
            cache.get 注入 now 但质量校验仍用真实今天 → 缓存测试随时间漂移变红。
    """
    if now is None:
        return STOCK_HISTORY_VALIDATOR.validate(df, symbol)
    # 注入 now：重建带参考日期的校验器（只替换 FreshnessRule）
    validator = DataValidator()
    for rule in STOCK_HISTORY_VALIDATOR._rules:  # noqa: SLF001 模块内访问
        if isinstance(rule, FreshnessRule):
            validator.add_rule(
                FreshnessRule(
                    max_stale_days=rule._max_stale_days,  # noqa: SLF001
                    holiday_max_days=rule._holiday_max_days,  # noqa: SLF001
                    date_column=rule._date_column,  # noqa: SLF001
                    now=now,
                )
            )
        else:
            validator.add_rule(rule)
    return validator.validate(df, symbol)


def validate_sector_flow(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """校验行业资金流数据。"""
    return SECTOR_FLOW_VALIDATOR.validate(df, symbol)


def validate_stock_flow(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """校验个股资金流数据。"""
    return STOCK_FLOW_VALIDATOR.validate(df, symbol)


# ──────────────────────────────────────────────
# 6. 数据入口统一清洗层（v0.9.11 防线1）
# ──────────────────────────────────────────────
# 目标：消灭"脏数据类"bug 的重复修补。
# 原则：所有外部数据在进入系统时清洗一次（DataManager._validate 入口），
#       消费点不再各自防御 None/NaN/类型/空值。
# 本层只做"净化"不做"丢弃"——空表仍由上层校验器/调用方决定是否可用。

_NUMERIC_FILL: dict[str, float] = {}


def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """统一清洗外部数据 DataFrame。

    处理（全部 inplace 语义，返回新对象）：
    1. 数值列 None/NaN → 0.0（NaN 用 0 填充，保持列 dtype 数值化）
    2. 字符串列 None/NaN → ""（避免 .str 操作崩溃）
    3. 全 NaN 行剔除（无信息行）
    4. 保留原始列结构，不增删业务列

    Args:
        df: 原始外部数据（可能含 None/NaN/混合类型）。

    Returns:
        清洗后的 DataFrame。输入为 None/空时原样返回（调用方自行处理空表）。
    """
    if df is None or len(df) == 0:
        return df

    cleaned = df.copy()

    # 先记录全 NaN/None 行（在填充前判定，填充后 NaN 已不可见）
    all_missing = cleaned.isna().all(axis=1) | cleaned.isnull().all(axis=1)

    for col in cleaned.columns:
        if pd.api.types.is_numeric_dtype(cleaned[col]):
            # 数值列：None/NaN → 0，避免下游 f"{x:.1f}" 或算术崩溃
            cleaned[col] = pd.to_numeric(cleaned[col], errors="coerce").fillna(0.0)
        elif pd.api.types.is_object_dtype(cleaned[col]) or pd.api.types.is_string_dtype(
            cleaned[col]
        ):
            # 对象/字符串列：None/NaN → ""，避免 .str.contains 等崩溃
            cleaned[col] = cleaned[col].fillna("").astype(str)

    # 剔除全 NaN 行（原始即无信息，填充前已判定）
    if all_missing.any():
        cleaned = cleaned[~all_missing]
    return cleaned


def sanitize_scalar(value: Any) -> Any:
    """清洗单个标量值（信号字典字段等非 DataFrame 场景）。

    - None → 0（数值语义）或 ""（字符串语义由调用方决定）
    - 非数值字符串 → 原样
    """
    if value is None:
        return 0
    return value
