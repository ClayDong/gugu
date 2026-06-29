"""数据质量校验模块单元测试。

覆盖 validate_stock_history / validate_sector_flow / validate_stock_flow：
- 必需列与空数据校验（抛 DataQualityError）
- NaN 填充/剔除
- 负值/零价/high<low 异常行剔除
- 时效性检查（过期数据告警）
- 日期排序
- 日志告警消息可操作性

注意：实际实现中以下检查不存在，测试反映真实行为：
- 无最小行数检查（少量数据仍通过）
- 无重复日期去重（重复行保留，仅 validate_sector_flow 按 sector 去重）
- 非单调日期会被排序修正
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from loguru import logger as loguru_logger

from gugu.data.quality import (
    DataQualityError,
    validate_sector_flow,
    validate_stock_flow,
    validate_stock_history,
)


# ========== 辅助函数 ==========


def _recent_dates(n: int, end: date | None = None) -> pd.DatetimeIndex:
    """生成以 end（默认今天）结束的 n 个升序日期。"""
    if end is None:
        end = date.today()
    return pd.date_range(end=end, periods=n, freq="D")


def _make_ohlcv(
    n: int = 60,
    *,
    close: float | list[float] | None = None,
    volume: int | list[int] | None = None,
    dates: pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """构造合法的 OHLCV 数据。

    所有价格非负、high>=low、close>0、volume>=0，默认日期截至今天。
    """
    if dates is None:
        dates = _recent_dates(n)
    else:
        n = len(dates)
    if close is None:
        close = [10.0 + (i * 0.01) for i in range(n)]  # 10.0 → 10.59 (通过 PlausiblePriceRule)
    elif isinstance(close, (int, float)):
        close = [float(close)] * n
    if volume is None:
        volume = [1_000_000] * n
    elif isinstance(volume, (int, float)):
        volume = [int(volume)] * n
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": [c + 1.0 for c in close],
            "low": [c - 1.0 for c in close],
            "close": close,
            "volume": volume,
        }
    )


# ========== Fixtures ==========


@pytest.fixture
def captured_logs() -> list[str]:
    """捕获 loguru 日志消息，返回消息文本列表。"""
    messages: list[str] = []

    def sink(message) -> None:
        messages.append(str(message))

    handler_id = loguru_logger.add(sink, level="DEBUG")
    yield messages
    loguru_logger.remove(handler_id)


@pytest.fixture
def valid_ohlcv() -> pd.DataFrame:
    """60 日合法 OHLCV 数据，日期截至今天。"""
    return _make_ohlcv(60)


# ========== 1. 合法数据通过 ==========


def test_valid_ohlcv_passes_quality_check(valid_ohlcv: pd.DataFrame) -> None:
    """合法 OHLCV 数据应全部保留，无异常剔除。

    返回清洗后的 DataFrame（非 True/False），行数与输入一致。
    """
    out = validate_stock_history(valid_ohlcv, "600519")
    assert len(out) == len(valid_ohlcv)
    # 必需列保留
    for col in ["date", "open", "high", "low", "close", "volume"]:
        assert col in out.columns
    # 无空值、无负值
    assert not out[["open", "high", "low", "close", "volume"]].isnull().any().any()
    assert (out[["open", "high", "low", "close", "volume"]] >= 0).all().all()


# ========== 2. NaN 值处理 ==========


def test_nan_values_in_middle_are_filled() -> None:
    """中间单个 NaN 应被前向填充，行数不变。"""
    n = 10
    df = _make_ohlcv(n)
    df.loc[5, "close"] = np.nan
    out = validate_stock_history(df, "600519")
    assert len(out) == n
    assert not out["close"].isnull().any()


def test_nan_values_exceeding_fill_limit_are_dropped() -> None:
    """开头连续超过 3 个 NaN 无法被 bfill 完全填充，剩余行应被剔除。

    ffill(limit=3) 对开头 NaN 无效（无前值）；
    bfill(limit=3) 只能回填前 3 个，剩余 NaN 行被 dropna 剔除。
    """
    n = 10
    df = _make_ohlcv(n)
    # 开头连续 5 个 NaN
    df.loc[0:4, "close"] = np.nan
    out = validate_stock_history(df, "600519")
    # bfill(limit=3) 填充前 3 个，后 2 个被剔除
    assert len(out) == n - 2
    assert not out["close"].isnull().any()


# ========== 3. 负值剔除 ==========


def test_negative_prices_are_removed() -> None:
    """负价格行应被剔除。"""
    n = 5
    df = _make_ohlcv(n)
    df.loc[1, "close"] = -1.0
    df.loc[2, "open"] = -5.0
    out = validate_stock_history(df, "600519")
    # 2 行负值被剔除
    assert len(out) == n - 2
    # 剩余行无负值
    assert (out[["open", "high", "low", "close", "volume"]] >= 0).all().all()


def test_negative_volume_is_removed() -> None:
    """负成交量行应被剔除。"""
    n = 5
    df = _make_ohlcv(n)
    df.loc[0, "volume"] = -100
    out = validate_stock_history(df, "600519")
    assert len(out) == n - 1
    assert (out["volume"] >= 0).all()


# ========== 4. 零成交量（停牌） ==========


def test_zero_volume_is_preserved() -> None:
    """零成交量（停牌）但 close>0 的行应保留。

    实际实现仅剔除 close=0 的行，volume=0 不影响。
    """
    n = 5
    df = _make_ohlcv(n)
    df.loc[1, "volume"] = 0  # 停牌
    out = validate_stock_history(df, "600519")
    # 零成交行保留
    assert len(out) == n
    assert (out["volume"] == 0).any()


def test_zero_price_is_removed() -> None:
    """close=0 的行应被剔除（停牌不应返回 0 价）。"""
    n = 5
    df = _make_ohlcv(n)
    df.loc[1, "close"] = 0.0
    df.loc[1, "open"] = 0.0
    df.loc[1, "high"] = 0.0
    df.loc[1, "low"] = 0.0
    out = validate_stock_history(df, "600519")
    assert len(out) == n - 1
    assert (out["close"] > 0).all()


# ========== 5. 少量数据 ==========


def test_small_dataset_still_passes() -> None:
    """少量数据（< 30 行）仍通过校验。

    注意：实际实现无最小行数检查，少量数据不会被拒绝。
    """
    df = _make_ohlcv(5)
    out = validate_stock_history(df, "600519")
    assert len(out) == 5


# ========== 6. 时效性检查 ==========


def test_stale_data_logs_warning(captured_logs: list[str]) -> None:
    """最新日期过旧（> 15 天）应记录告警日志。"""
    # 构造 30 天前结束的数据（确保超过 15 天阈值，无视长假放宽）
    end = date.today() - timedelta(days=30)
    dates = pd.date_range(end=end, periods=5, freq="D")
    df = _make_ohlcv(5, dates=dates)
    validate_stock_history(df, "600519")
    # 应有告警日志提及 symbol 与过期
    warnings = [m for m in captured_logs if "WARNING" in m and "600519" in m]
    assert any("过期" in m or "超过" in m for m in warnings), (
        f"期望包含过期告警，实际告警: {warnings}"
    )


def test_recent_data_no_stale_warning(captured_logs: list[str]) -> None:
    """最新日期为今天时不应记录过期告警。"""
    df = _make_ohlcv(5)  # 默认日期截至今天
    validate_stock_history(df, "600519")
    stale_warnings = [m for m in captured_logs if "WARNING" in m and "过期" in m]
    assert not stale_warnings, f"不应有过期告警: {stale_warnings}"


# ========== 7. 重复日期 ==========


def test_duplicate_dates_are_preserved() -> None:
    """重复日期行应保留（实际实现无去重）。

    注意：validate_stock_history 仅按日期排序，不去重；
    validate_sector_flow 会按 sector 去重。
    """
    dates = pd.date_range("2024-01-01", periods=3, freq="D").tolist()
    df = pd.DataFrame(
        {
            "date": dates + dates,  # 6 行，3 个日期各重复一次
            "open": [10.0] * 6,
            "high": [11.0] * 6,
            "low": [9.0] * 6,
            "close": [10.5] * 6,
            "volume": [1000] * 6,
        }
    )
    out = validate_stock_history(df, "600519")
    # 无去重，6 行保留
    assert len(out) == 6


# ========== 8. 非单调日期排序 ==========


def test_non_monotonic_dates_are_sorted() -> None:
    """非单调日期应被排序为升序。"""
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    df = _make_ohlcv(5, dates=dates)
    # 打乱顺序
    df = df.iloc[[3, 1, 4, 0, 2]].reset_index(drop=True)
    assert not df["date"].is_monotonic_increasing  # 确认输入确实非单调

    out = validate_stock_history(df, "600519")
    # 输出应按日期升序
    assert out["date"].is_monotonic_increasing


# ========== 9. 空数据 ==========


def test_empty_dataframe_raises_error() -> None:
    """空 DataFrame 应抛 DataQualityError。"""
    df = pd.DataFrame()
    with pytest.raises(DataQualityError, match="数据为空"):
        validate_stock_history(df, "600519")


def test_empty_rows_raises_error() -> None:
    """有列但无行的 DataFrame 应抛 DataQualityError。"""
    df = pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "open": pd.Series(dtype=float),
            "high": pd.Series(dtype=float),
            "low": pd.Series(dtype=float),
            "close": pd.Series(dtype=float),
            "volume": pd.Series(dtype=int),
        }
    )
    with pytest.raises(DataQualityError, match="数据为空"):
        validate_stock_history(df, "600519")


def test_missing_columns_raises_error() -> None:
    """缺失必需列应抛 DataQualityError。"""
    df = pd.DataFrame({"date": [1, 2, 3]})
    with pytest.raises(DataQualityError, match="缺失列"):
        validate_stock_history(df, "600519")


# ========== 10. 告警消息可操作性 ==========


def test_error_message_contains_symbol_and_context() -> None:
    """DataQualityError 消息应包含 symbol 与上下文，便于定位。"""
    df = pd.DataFrame()
    with pytest.raises(DataQualityError) as exc_info:
        validate_stock_history(df, "600519")
    msg = str(exc_info.value)
    assert "600519" in msg
    assert "stock_history" in msg


def test_missing_column_error_lists_columns() -> None:
    """缺失列错误应列出具体缺失的列名。"""
    df = pd.DataFrame({"date": [1], "open": [1.0]})
    with pytest.raises(DataQualityError) as exc_info:
        validate_stock_history(df, "000001")
    msg = str(exc_info.value)
    assert "000001" in msg
    # 列出缺失的列
    assert "high" in msg
    assert "close" in msg


def test_negative_value_warning_contains_symbol(captured_logs: list[str]) -> None:
    """负值告警应包含 symbol，便于定位。"""
    n = 3
    df = _make_ohlcv(n)
    df.loc[1, "close"] = -1.0
    validate_stock_history(df, "600519")
    warnings = [m for m in captured_logs if "WARNING" in m and "600519" in m]
    assert any("负值" in m for m in warnings), f"期望负值告警，实际: {warnings}"


def test_zero_price_warning_contains_symbol(captured_logs: list[str]) -> None:
    """零价告警应包含 symbol。"""
    n = 3
    df = _make_ohlcv(n)
    df.loc[1, "close"] = 0.0
    df.loc[1, "open"] = 0.0
    df.loc[1, "high"] = 0.0
    df.loc[1, "low"] = 0.0
    validate_stock_history(df, "600519")
    warnings = [m for m in captured_logs if "WARNING" in m and "600519" in m]
    assert any("零价" in m for m in warnings), f"期望零价告警，实际: {warnings}"


# ========== 额外：high < low 校验 ==========


def test_high_less_than_low_is_removed() -> None:
    """high < low 的行应被剔除。"""
    n = 5
    df = _make_ohlcv(n)
    # 第 1 行 high < low
    df.loc[1, "high"] = 5.0
    df.loc[1, "low"] = 9.0
    out = validate_stock_history(df, "600519")
    assert len(out) == n - 1
    assert (out["high"] >= out["low"]).all()


# ========== 额外：validate_sector_flow / validate_stock_flow ==========


def test_validate_sector_flow_dedupes_by_sector() -> None:
    """行业资金流应按 sector 去重，保留首条。"""
    df = pd.DataFrame(
        {
            "sector": ["白酒", "白酒", "银行"],
            "main_net": [1e8, 2e8, 3e8],
            "main_pct": [0.1, 0.2, 0.3],
        }
    )
    out = validate_sector_flow(df)
    assert len(out) == 2
    assert "白酒" in out["sector"].values
    assert "银行" in out["sector"].values
    # 保留首条
    assert out[out["sector"] == "白酒"]["main_net"].iloc[0] == 1e8


def test_validate_sector_flow_empty_raises() -> None:
    """空行业资金流应抛 DataQualityError。"""
    with pytest.raises(DataQualityError, match="数据为空"):
        validate_sector_flow(pd.DataFrame())


def test_validate_stock_flow_sorts_by_date() -> None:
    """个股资金流应按日期升序排序。"""
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-03", "2024-01-01", "2024-01-02"]),
            "main_net": [1e6, 2e6, 3e6],
            "main_pct": [0.01, 0.02, 0.03],
        }
    )
    out = validate_stock_flow(df, "600519")
    assert out["date"].is_monotonic_increasing


def test_validate_stock_flow_without_date() -> None:
    """无 date 列的个股资金流应原样返回。"""
    df = pd.DataFrame({"main_net": [1e6], "main_pct": [0.01]})
    out = validate_stock_flow(df, "600519")
    assert len(out) == 1
