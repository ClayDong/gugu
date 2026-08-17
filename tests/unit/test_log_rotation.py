"""日志轮转单元测试（2026-08-11, 告警评估员修复）

验证 gugu.utils.log 的跨日轮转逻辑:
1. 大小超过 10MB → 轮转
2. 文件名日期 != 今天 → 轮转（跨日 scheduler 不再写昨日日志文件）
3. 同一天且未超大小 → 不轮转
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gugu.utils.log import _rotate_by_size_or_date, _LOG_DATE_RE


class _FakeFile:
    def __init__(self, name: str, size: int = 0):
        self.name = name
        self._size = size

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def tell(self) -> int:
        return self._size


def _today_str() -> str:
    import time

    return time.strftime("%Y%m%d")


class TestRotateBySizeOrDate:
    def test_size_over_10mb_rotates(self):
        f = _FakeFile(f"gugu_{_today_str()}.log", size=10 * 1024 * 1024)
        assert _rotate_by_size_or_date("x", f) is True

    def test_cross_date_rotates(self):
        """文件名日期 != 今天 → 轮转（跨日进程修复点）"""
        yesterday = "20260810"
        f = _FakeFile(f"gugu_{yesterday}.log", size=100)
        assert _rotate_by_size_or_date("x", f) is True

    def test_same_date_small_file_keeps(self):
        f = _FakeFile(f"gugu_{_today_str()}.log", size=100)
        assert _rotate_by_size_or_date("x", f) is False

    def test_size_exactly_10mb_keeps(self):
        """恰好 10MB 不轮转（> 才轮转，与原 '10 MB' 语义一致）"""
        f = _FakeFile(f"gugu_{_today_str()}.log", size=10 * 1024 * 1024 - 1)
        assert _rotate_by_size_or_date("x", f) is False

    def test_regex_matches_expected_filename(self):
        m = _LOG_DATE_RE.search("/tmp/logs/gugu_20260811.log")
        assert m is not None
        assert m.group(1) == "20260811"
