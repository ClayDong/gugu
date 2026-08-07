"""v0.9.11 防线3：真实脏数据回归测试。

用 tests/fixtures/dirty_data.py 的真实脏数据样本，验证历史上实际触发过的 bug
不再复发：
1. 信号历史写入：perspective_check 全 None 不崩溃（v0.9.9 信号黑洞）
2. 数据清洗层：None/NaN 净化后消费点安全（v0.9.11 防线1）
3. 快照硬过滤：脏快照不误判/不崩溃
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from gugu.data.quality import sanitize_dataframe
from gugu.engine.signal_pipeline import record_signal_history


class TestSignalHistoryDirtyData:
    """信号历史写入的脏数据回归（v0.9.9 信号黑洞场景）。"""

    def test_record_signal_history_with_none_perspective(self, tmp_path, none_fields_signal):
        """perspective_check 全 None → 不崩溃、正确写入（回归 v0.9.9）。"""
        import gugu.engine.signal_pipeline as sp

        original_root = sp.gugu_config.PROJECT_ROOT
        sp.gugu_config.PROJECT_ROOT = tmp_path
        try:
            risk_result = SimpleNamespace(allowed=True, message="approved")
            order_result = SimpleNamespace(
                success=True,
                quantity=100,
                price=1500.0,
                commission=3.75,
                status="filled",
                filled_quantity=100,
                filled_price=1500.0,
                avg_price=1500.0,
            )
            record_signal_history(none_fields_signal, risk_result, order_result)

            history_file = tmp_path / "data" / "signals_history.jsonl"
            assert history_file.exists()
            lines = history_file.read_text().strip().split("\n")
            assert len(lines) == 1
            record = json.loads(lines[0])
            step_36 = next(s for s in record["decision_chain"] if s["step"] == 3.6)
            assert "综合0.0" in step_36["result"]
        finally:
            sp.gugu_config.PROJECT_ROOT = original_root


class TestSanitizeDirtyData:
    """清洗层对真实脏数据的净化效果（v0.9.11 防线1）。"""

    def test_dirty_history_is_safe_after_sanitize(self, dirty_history_df):
        """含 None/NaN 的历史行情清洗后：数值全 0 填充、可安全计算。"""
        cleaned = sanitize_dataframe(dirty_history_df)
        assert cleaned["close"].isna().sum() == 0
        assert cleaned["volume"].isna().sum() == 0
        # 滚动均线不再产生全 NaN（原 bug：NaN 传播）
        ma = cleaned["close"].rolling(3).mean()
        assert ma.iloc[-1] == ma.iloc[-1]  # 非 NaN

    def test_dirty_snapshot_safe_for_filter(self, dirty_snapshot_df):
        """脏快照清洗后：换手率/市值 None → 0，过滤逻辑可执行。"""
        cleaned = sanitize_dataframe(dirty_snapshot_df)
        # None 换手率 → 0（会被过滤条件排除，而不是崩溃）
        assert cleaned["turnoverratio"].tolist() == [0.5, 2.0, 0.0, 5.5]
        # 市值 None → 0
        assert cleaned["circ_mv"].iloc[3] == 0.0

    def test_empty_df_passthrough(self, empty_df):
        """空 DataFrame 原样返回（调用方负责空表判定）。"""
        assert sanitize_dataframe(empty_df) is empty_df

    def test_truncated_snapshot_survives(self, truncated_snapshot_df):
        """残缺快照（80只）清洗后仍可过滤（不崩溃，候选数少是数据问题不是代码问题）。"""
        cleaned = sanitize_dataframe(truncated_snapshot_df)
        assert len(cleaned) == 80
        assert cleaned["price"].isna().sum() == 0
