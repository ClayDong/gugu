"""断板反包选股预过滤器单元测试。

覆盖核心场景：
- ST股票排除
- 创业板排除
- 市值范围过滤（50-150亿）
- 股价下限过滤（>3元）
- 位置判断（低/中/高位）
- 缓存机制
- 批量检查
"""

from __future__ import annotations

import time

import pytest

from gugu.filters.breakout_rebound_prefilter import (
    BreakoutReboundPrefilter,
    _is_gem_stock,
    _is_st_stock,
)


class TestIsStStock:
    """ST股票判断测试。"""

    def test_normal_stock_not_st(self):
        """正常股票不应被判断为ST。"""
        assert _is_st_stock("贵州茅台") == False
        assert _is_st_stock("中国平安") == False
        assert _is_st_stock("比亚迪") == False

    def test_st_stock_detected(self):
        """ST股票应被正确识别。"""
        assert _is_st_stock("ST某某") == True
        assert _is_st_stock("*ST某某") == True
        assert _is_st_stock("st某某") == True  # 大小写不敏感
        assert _is_st_stock("*st某某") == True

    def test_empty_name_not_st(self):
        """空名称不应被判断为ST。"""
        assert _is_st_stock("") == False


class TestIsGemStock:
    """创业板判断测试。"""

    def test_gem_stock_300_prefix(self):
        """300开头应为创业板。"""
        assert _is_gem_stock("300001") == True
        assert _is_gem_stock("300999") == True

    def test_main_board_not_gem(self):
        """主板股票不应被判断为创业板。"""
        assert _is_gem_stock("600519") == False  # 沪市主板
        assert _is_gem_stock("000001") == False  # 深市主板
        assert _is_gem_stock("002001") == False  # 中小板

    def test_code_padding(self):
        """代码应自动补零到6位。"""
        # 注意：zfill(6) 在前面补零，"30001" → "030001"（沪市）
        # 正确的创业板代码应该是 "300001"
        assert _is_gem_stock("300001") == True  # 创业板
        assert _is_gem_stock("30001") == False  # 补零后变成 "030001"（沪市）


class TestBreakoutReboundPrefilterBasic:
    """预过滤器基础功能测试。"""

    def test_pass_normal_stock(self):
        """正常主板股票应通过预过滤。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("600519", name="贵州茅台", price=1500.0, market_cap=2000e8)
        # 股价>3元，市值>150亿（超出范围）
        # 注意：贵州茅台市值超出150亿范围，应不通过
        assert result["pass"] == False  # 市值超限
        assert result["data"]["is_st"] == False
        assert result["data"]["is_gem"] == False

    def test_pass_mid_cap_stock(self):
        """中等市值股票应通过预过滤。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("600519", name="测试股票", price=50.0, market_cap=100e8)
        assert result["pass"] == True
        assert result["data"]["market_cap_billion"] == 100.0
        assert result["data"]["is_st"] == False
        assert result["data"]["is_gem"] == False

    def test_reject_st_stock(self):
        """ST股票应被排除。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("600519", name="ST某某", price=50.0, market_cap=100e8)
        assert result["pass"] == False
        assert result["data"]["is_st"] == True
        assert any("ST" in r for r in result["reasons"])

    def test_reject_gem_stock(self):
        """创业板股票应被排除。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("300001", name="某某科技", price=50.0, market_cap=100e8)
        assert result["pass"] == False
        assert result["data"]["is_gem"] == True
        assert any("创业板" in r for r in result["reasons"])

    def test_reject_low_price_stock(self):
        """股价<3元应被排除。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("600519", name="低价股", price=2.5, market_cap=100e8)
        assert result["pass"] == False
        assert any("股价过低" in r for r in result["reasons"])

    def test_reject_small_cap_stock(self):
        """市值<50亿应被排除。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("600519", name="小市值", price=50.0, market_cap=30e8)
        assert result["pass"] == False
        assert any("市值过小" in r for r in result["reasons"])

    def test_reject_large_cap_stock(self):
        """市值>150亿应被排除。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("600519", name="大市值", price=50.0, market_cap=200e8)
        assert result["pass"] == False
        assert any("市值过大" in r for r in result["reasons"])


class TestBreakoutReboundPrefilterCache:
    """缓存机制测试。"""

    def test_code_cache_hit(self):
        """相同代码第二次查询应命中缓存。"""
        prefilter = BreakoutReboundPrefilter()
        result1 = prefilter.check("600519", name="测试", price=50.0, market_cap=100e8)
        result2 = prefilter.check("600519", name="测试", price=50.0, market_cap=100e8)
        # 两次结果应相同（第二次命中缓存）
        assert result1["pass"] == result2["pass"]

    def test_cache_expiry(self):
        """缓存过期后应重新计算。"""
        prefilter = BreakoutReboundPrefilter()
        # 手动设置缓存TTL为0，强制过期
        prefilter._code_cache._ttl = 0
        result1 = prefilter.check("600519", name="测试", price=50.0, market_cap=100e8)
        time.sleep(0.01)  # 等待过期
        result2 = prefilter.check("600519", name="测试", price=60.0, market_cap=120e8)
        # 缓存过期后，新值应生效
        assert result2["data"]["price"] == 60.0

    def test_clear_cache(self):
        """清空缓存后应重新计算。"""
        prefilter = BreakoutReboundPrefilter()
        prefilter.check("600519", name="测试", price=50.0, market_cap=100e8)
        prefilter.clear_cache()
        # 清空后缓存应为空
        assert prefilter._code_cache._cache == {}
        assert prefilter._snapshot_cache._cache == {}


class TestBreakoutReboundPrefilterBulkCheck:
    """批量检查测试。"""

    def test_bulk_check_multiple_stocks(self):
        """批量检查多个标的。"""
        prefilter = BreakoutReboundPrefilter()
        symbols = ["600519", "300001", "000001"]
        names = {"600519": "测试1", "300001": "测试2", "000001": "测试3"}
        prices = {"600519": 50.0, "300001": 50.0, "000001": 50.0}
        market_caps = {"600519": 100e8, "300001": 100e8, "000001": 100e8}

        results = prefilter.bulk_check(symbols, names, prices, market_caps)

        assert len(results) == 3
        assert results["600519"]["pass"] == True  # 正常主板
        assert results["300001"]["pass"] == False  # 创业板
        assert results["000001"]["pass"] == True  # 深市主板

    def test_bulk_check_empty_symbols(self):
        """空列表批量检查应返回空结果。"""
        prefilter = BreakoutReboundPrefilter()
        results = prefilter.bulk_check([])
        assert results == {}


class TestBreakoutReboundPrefilterSnapshot:
    """快照缓存测试。"""

    def test_update_snapshot(self):
        """更新快照后应能从快照获取数据。"""
        prefilter = BreakoutReboundPrefilter()
        prefilter.update_snapshot("600519", price=50.0, market_cap=100e8, name="测试")

        # 不传入price和market_cap，应从快照获取
        result = prefilter.check("600519", name="测试")
        assert result["data"]["price"] == 50.0
        assert result["data"]["market_cap"] == 100e8

    def test_position_level_mid_without_history(self):
        """无历史数据时位置判断默认为mid。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("600519", name="测试", price=50.0, market_cap=100e8)
        # 无60日高点数据，默认为mid
        assert result["data"]["position_level"] == "mid"


class TestBreakoutReboundPrefilterEdgeCases:
    """边界场景测试。"""

    def test_code_with_leading_zeros(self):
        """代码应自动补零。"""
        prefilter = BreakoutReboundPrefilter()
        result1 = prefilter.check("600519", name="测试", price=50.0, market_cap=100e8)
        result2 = prefilter.check("0600519", name="测试", price=50.0, market_cap=100e8)
        # 两次结果应相同（代码补零后相同）
        assert result1["pass"] == result2["pass"]

    def test_price_exactly_at_threshold(self):
        """价格恰好在阈值（3元）应通过。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("600519", name="测试", price=3.0, market_cap=100e8)
        # 3.0 >= 3.0 应通过
        assert result["pass"] == True

    def test_market_cap_at_threshold(self):
        """市值恰好在阈值应通过。"""
        prefilter = BreakoutReboundPrefilter()
        # 市值恰好50亿
        result_min = prefilter.check("600519", name="测试", price=50.0, market_cap=50e8)
        assert result_min["pass"] == True

        # 市值恰好150亿
        result_max = prefilter.check("600519", name="测试2", price=50.0, market_cap=150e8)
        assert result_max["pass"] == True

    def test_st_and_gem_both_true(self):
        """ST且创业板应同时被排除。"""
        prefilter = BreakoutReboundPrefilter()
        result = prefilter.check("300001", name="ST科技", price=50.0, market_cap=100e8)
        assert result["pass"] == False
        assert result["data"]["is_st"] == True
        assert result["data"]["is_gem"] == True
        # 应有两个排除原因
        assert len(result["reasons"]) >= 2
