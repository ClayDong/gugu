"""v0.9.11 防线1：数据入口统一清洗层测试。

覆盖 sanitize_dataframe / sanitize_scalar：
- None/NaN 数值列 → 0（不崩溃，可参与 f-string 格式化/算术）
- None/NaN 字符串列 → ""（.str 操作不崩溃）
- 全 NaN 行剔除
- 空输入/None 输入原样返回
- 标量清洗
"""

from __future__ import annotations

import pandas as pd
import pytest

from gugu.data.quality import sanitize_dataframe, sanitize_scalar


class TestSanitizeDataframe:
    def test_numeric_none_becomes_zero(self):
        """数值列 None/NaN → 0，f-string 格式化不再崩溃（v0.9.9 信号黑洞同款场景）。"""
        df = pd.DataFrame(
            {
                "symbol": ["600519", "000001", "000002"],
                "close": [10.0, None, float("nan")],
                "volume": [100, 200, None],
            }
        )
        cleaned = sanitize_dataframe(df)
        assert cleaned["close"].tolist() == [10.0, 0.0, 0.0]
        assert cleaned["volume"].tolist() == [100.0, 200.0, 0.0]
        # 关键回归：格式化 None 必须不抛 TypeError
        assert f"{cleaned['close'].iloc[1]:.1f}" == "0.0"

    def test_string_none_becomes_empty(self):
        """字符串列 None/NaN → ""，.str.contains 不崩溃。"""
        df = pd.DataFrame(
            {
                "code": ["600519", "000001", "000002"],
                "name": ["贵州茅台", None, float("nan")],
            }
        )
        cleaned = sanitize_dataframe(df)
        assert cleaned["name"].tolist() == ["贵州茅台", "", ""]
        assert ~cleaned["name"].str.contains("ST", na=False).any()

    def test_all_nan_rows_dropped(self):
        """全 NaN 行剔除（无信息行）。"""
        df = pd.DataFrame(
            {
                "code": ["600519", None, None],
                "price": [10.0, None, None],
            }
        )
        cleaned = sanitize_dataframe(df)
        assert len(cleaned) == 1
        assert cleaned.iloc[0]["code"] == "600519"

    def test_mixed_types_preserved_columns(self):
        """列结构保留，不增删业务列。"""
        df = pd.DataFrame(
            {
                "code": ["600519", "000001"],
                "name": ["贵州茅台", "平安银行"],
                "price": [1500.0, 11.0],
                "change_pct": [1.2, None],
            }
        )
        cleaned = sanitize_dataframe(df)
        assert list(cleaned.columns) == ["code", "name", "price", "change_pct"]
        assert cleaned.iloc[1]["change_pct"] == 0.0

    def test_empty_input_returns_as_is(self):
        """空 DataFrame 原样返回。"""
        df = pd.DataFrame()
        assert sanitize_dataframe(df) is df

    def test_none_input_returns_none(self):
        """None 输入返回 None（调用方处理空表）。"""
        assert sanitize_dataframe(None) is None

    def test_string_dtype_column_safe(self):
        """string dtype（非 object）列同样清洗。"""
        df = pd.DataFrame(
            {
                "symbol": pd.Series(["600519", None], dtype="string"),
                "price": [10.0, 11.0],
            }
        )
        cleaned = sanitize_dataframe(df)
        assert cleaned["symbol"].iloc[1] == ""


class TestSanitizeScalar:
    def test_none_becomes_zero(self):
        assert sanitize_scalar(None) == 0

    def test_value_passthrough(self):
        assert sanitize_scalar(3.14) == 3.14
        assert sanitize_scalar("text") == "text"
        assert sanitize_scalar(0) == 0
