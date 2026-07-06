"""尾盘选股验证模块测试。"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from gugu.screener.validator import (
    ScreenerRecord,
    ValidationResult,
    _load_records_by_date,
    _rotate_file,
    save_screener_records,
    validate_yesterday_screener,
)
from gugu.utils.calendar import last_trading_day


class _MockResponse:
    """模拟 httpx.Response。"""
    def __init__(self, json_data):
        self._json = json_data
    def raise_for_status(self):
        pass
    def json(self):
        return self._json


class _MockAsyncClient:
    """模拟 httpx.AsyncClient，根据 secids 参数返回东财 push2 批量实时行情。

    P0 修复后 validator 改用 ulist.np/get 批量接口（非日K线），
    mock 数据格式同步更新为 {data: {diff: [{f12, f14, f15, f17, f18}]}}。
    """
    def __init__(self, stock_data, hs300_data):
        self._stock = stock_data
        self._hs300 = hs300_data
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    async def get(self, url, params=None, **kwargs):
        # 东财批量接口: secids=0.600519,1.603190,...
        secids = params.get("secids", "") if params else ""
        if "000300" in secids:
            return _MockResponse(self._hs300)
        return _MockResponse(self._stock)


class TestScreenerRecord:
    """ScreenerRecord dataclass 基础测试。"""

    def test_create(self):
        r = ScreenerRecord(
            symbol="600519",
            name="贵州茅台",
            close_price=1520.5,
            change_pct=3.2,
            volume_ratio=1.8,
            screener_date="2026-06-29",
            passed=True,
            failed_conditions=[],
        )
        assert r.symbol == "600519"
        assert r.close_price == 1520.5
        assert r.passed is True

    def test_create_partial(self):
        r = ScreenerRecord(
            symbol="300750",
            name="宁德时代",
            close_price=245.8,
            change_pct=4.5,
            volume_ratio=2.1,
            screener_date="2026-06-29",
            passed=False,
            failed_conditions=["量比不足"],
        )
        assert r.passed is False
        assert "量比不足" in r.failed_conditions


class TestSaveAndLoadRecords:
    """JSONL 读写测试。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_save_and_load(self):
        records = [
            ScreenerRecord("600519", "茅台", 1520.5, 3.2, 1.8, "2026-06-29", True, []),
            ScreenerRecord("300750", "宁德", 245.8, 4.5, 2.1, "2026-06-29", True, []),
        ]
        filepath = os.path.join(self.tmpdir, "test_screener.jsonl")
        with patch("gugu.screener.validator._SCREENER_HISTORY_FILE", filepath):
            save_screener_records(records)
            assert os.path.exists(filepath)
            with open(filepath, encoding="utf-8") as f:
                lines = f.readlines()
            assert len(lines) == 2
            d = json.loads(lines[0])
            assert d["symbol"] == "600519"

    def test_append_not_overwrite(self):
        records1 = [ScreenerRecord("600519", "茅台", 1520, 3, 1, "2026-06-28", True, [])]
        records2 = [ScreenerRecord("000858", "五粮液", 148, 2, 1, "2026-06-29", True, [])]
        filepath = os.path.join(self.tmpdir, "test_append.jsonl")
        with patch("gugu.screener.validator._SCREENER_HISTORY_FILE", filepath):
            save_screener_records(records1)
            save_screener_records(records2)
            with open(filepath, encoding="utf-8") as f:
                lines = f.readlines()
            assert len(lines) == 2

    def test_load_by_date(self):
        filepath = os.path.join(self.tmpdir, "test_load.jsonl")
        rows = [
            {"symbol": "600519", "name": "茅台", "close_price": 1520, "change_pct": 3, "volume_ratio": 1, "screener_date": "2026-06-29", "passed": True, "failed_conditions": [], "created_at": ""},
            {"symbol": "000858", "name": "五粮液", "close_price": 148, "change_pct": 2, "volume_ratio": 1, "screener_date": "2026-06-28", "passed": True, "failed_conditions": [], "created_at": ""},
            {"symbol": "300750", "name": "宁德", "close_price": 245, "change_pct": 4, "volume_ratio": 2, "screener_date": "2026-06-29", "passed": False, "failed_conditions": ["量比不足"], "created_at": ""},
        ]
        with open(filepath, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with patch("gugu.screener.validator._SCREENER_HISTORY_FILE", filepath):
            loaded = _load_records_by_date("2026-06-29")
            assert len(loaded) == 1  # 只加载 passed=True 的记录
            assert loaded[0].symbol == "600519"


class TestRotateFile:
    """JSONL 轮转测试。"""

    def test_no_rotate_when_under_limit(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".jsonl") as f:
            for _ in range(10):
                f.write('{"test": 1}\n')
            fpath = f.name
        _rotate_file(fpath)
        with open(fpath) as f:
            assert len(f.readlines()) == 10
        os.unlink(fpath)

    def test_rotate_when_over_limit(self):
        limit = 100
        trim = 50
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".jsonl") as f:
            for _ in range(limit + 10):
                f.write('{"test": 1}\n')
            fpath = f.name
        with (
            patch("gugu.screener.validator.MAX_RECORDS", limit),
            patch("gugu.screener.validator.TRIM_TO", trim),
        ):
            _rotate_file(fpath)
        with open(fpath) as f:
            assert len(f.readlines()) == trim
        os.unlink(fpath)


class TestValidateYesterdayScreener:
    """验证逻辑测试。"""

    @pytest.fixture
    def mock_history(self):
        """创建包含昨日记录的 mock JSONL。

        P0 修复：用 last_trading_day() 而非自然日昨天，
        与 validator.validate_yesterday_screener() 的取数逻辑保持一致，
        避免周一/节后首日运行时日期不匹配导致 _load_records_by_date 返回空。
        """
        yesterday = last_trading_day().isoformat()
        row = {
            "symbol": "600519",
            "name": "茅台",
            "close_price": 1520.5,
            "change_pct": 3.2,
            "volume_ratio": 1.8,
            "screener_date": yesterday,
            "passed": True,
            "failed_conditions": [],
            "created_at": "",
        }
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".jsonl") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            fpath = f.name
        yield fpath
        os.unlink(fpath)

    @pytest.fixture
    def mock_validation_file(self):
        """创建 temp validation 文件。"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as f:
            fpath = f.name
        yield fpath
        if os.path.exists(fpath):
            os.unlink(fpath)

    @pytest.mark.asyncio
    async def test_validate(self, mock_history, mock_validation_file):
        """完整验证流程：读取历史 → 获取行情 → 计算溢价 → 保存结果。

        P0 修复后 mock 数据格式改为东财 push2 批量实时行情：
        f12=代码, f14=名称, f15=最高, f17=今开, f18=昨收（价格×100）。
        """
        # 东财返回价格需÷100（如 153000 → 1530.0 元）
        mock_stock_data = {
            "data": {
                "diff": [
                    {"f12": "600519", "f14": "茅台", "f15": 154000, "f17": 153000, "f18": 152050}
                ]
            }
        }
        # 沪深300: 今开=403000→4030, 昨收=401000→4010 → 涨幅 +0.50%
        mock_hs300_data = {
            "data": {
                "diff": [
                    {"f12": "000300", "f14": "沪深300", "f15": 403000, "f17": 403000, "f18": 401000}
                ]
            }
        }

        mock_client = _MockAsyncClient(mock_stock_data, mock_hs300_data)

        with (
            patch("gugu.screener.validator._SCREENER_HISTORY_FILE", mock_history),
            patch("gugu.screener.validator._SCREENER_VALIDATION_FILE", mock_validation_file),
            patch("gugu.screener.validator.httpx.AsyncClient", return_value=mock_client),
        ):
            results = await validate_yesterday_screener()

            assert len(results) == 1
            r = results[0]
            assert r.symbol == "600519"
            assert r.name == "茅台"
            assert r.close_price == 1520.5
            assert r.open_price == 1530.0
            assert r.high_price == 1540.0
            # open_premium = (1530 - 1520.5)/1520.5 * 100
            expected_open = round((1530.0 - 1520.5) / 1520.5 * 100, 2)
            assert r.open_premium == expected_open
            # intraday_high_premium = (1540 - 1520.5)/1520.5 * 100
            expected_high = round((1540.0 - 1520.5) / 1520.5 * 100, 2)
            assert r.intraday_high_premium == expected_high

    @pytest.mark.asyncio
    async def test_validate_negative_premium(self, mock_history, mock_validation_file):
        """负溢价（低开）场景验证。

        P0 修复后 mock 数据格式改为东财 push2 批量实时行情。
        """
        # 今开=150000→1500.0, 最高=152500→1525.0, 昨收=152050→1520.5
        mock_stock_data = {
            "data": {
                "diff": [
                    {"f12": "600519", "f14": "茅台", "f15": 152500, "f17": 150000, "f18": 152050}
                ]
            }
        }
        mock_hs300_data = {
            "data": {
                "diff": [
                    {"f12": "000300", "f14": "沪深300", "f15": 403000, "f17": 403000, "f18": 401000}
                ]
            }
        }

        mock_client = _MockAsyncClient(mock_stock_data, mock_hs300_data)

        with (
            patch("gugu.screener.validator._SCREENER_HISTORY_FILE", mock_history),
            patch("gugu.screener.validator._SCREENER_VALIDATION_FILE", mock_validation_file),
            patch("gugu.screener.validator.httpx.AsyncClient", return_value=mock_client),
        ):
            results = await validate_yesterday_screener()

            assert len(results) == 1
            r = results[0]
            # open_premium = (1500 - 1520.5)/1520.5 * 100 ≈ -1.35%
            assert r.open_premium < 0
            # intraday_high_premium = (1525 - 1520.5)/1520.5 * 100 ≈ +0.3%
            assert r.intraday_high_premium > 0
            # 虽然低开，但日内最高价仍高于收盘价

    @pytest.mark.asyncio
    async def test_no_records(self):
        """无昨日选股记录时应返回空列表。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"symbol":"600519","screener_date":"2020-01-01","passed":true}\n')
            f.flush()
            fpath = f.name
        try:
            with (
                patch("gugu.screener.validator._SCREENER_HISTORY_FILE", fpath),
                patch("gugu.screener.validator._SCREENER_VALIDATION_FILE", fpath + ".val"),
            ):
                results = await validate_yesterday_screener()
                assert results == []
        finally:
            os.unlink(fpath)

    @pytest.mark.asyncio
    async def test_no_history_file(self):
        """历史文件不存在时应返回空列表。"""
        with (
            patch("gugu.screener.validator._SCREENER_HISTORY_FILE", "/nonexistent/path.jsonl"),
            patch("gugu.screener.validator._SCREENER_VALIDATION_FILE", "/nonexistent/val.jsonl"),
        ):
            results = await validate_yesterday_screener()
            assert results == []


class TestValidationResult:
    """ValidationResult dataclass 测试。"""

    def test_create(self):
        r = ValidationResult(
            symbol="600519",
            name="茅台",
            close_price=1520.5,
            open_price=1530.0,
            high_price=1540.0,
            open_premium=0.62,
            intraday_high_premium=1.28,
            screener_date="2026-06-29",
            validation_date="2026-06-30",
            passed=True,
            benchmark_open_change=0.25,
        )
        assert r.symbol == "600519"
        assert r.open_premium == 0.62
        assert r.intraday_high_premium == 1.28
        assert r.benchmark_open_change == 0.25

    def test_excess_return_calculation(self):
        """超额收益 = 个股开盘溢价 - 基准开盘涨幅。"""
        r = ValidationResult(
            symbol="600519",
            name="茅台",
            close_price=1520.5,
            open_price=1530.0,
            high_price=1540.0,
            open_premium=0.62,
            intraday_high_premium=1.28,
            screener_date="2026-06-29",
            validation_date="2026-06-30",
            passed=True,
            benchmark_open_change=0.25,
        )
        excess = r.open_premium - r.benchmark_open_change
        assert excess == pytest.approx(0.37, abs=0.01)


class TestGetMarket:
    """股票代码到东财市场编号的映射测试。"""

    def test_shanghai(self):
        from gugu.screener.validator import _get_market
        assert _get_market("600519") == 1
        assert _get_market("601318") == 1
        assert _get_market("603259") == 1

    def test_shenzhen(self):
        from gugu.screener.validator import _get_market
        assert _get_market("000858") == 0
        assert _get_market("000333") == 0

    def test_chinext(self):
        from gugu.screener.validator import _get_market
        assert _get_market("300750") == 0
        assert _get_market("300059") == 0

    def test_star(self):
        from gugu.screener.validator import _get_market
        assert _get_market("688396") == 1

    def test_unknown_prefix(self):
        from gugu.screener.validator import _get_market
        assert _get_market("999999") == 1  # 默认沪市