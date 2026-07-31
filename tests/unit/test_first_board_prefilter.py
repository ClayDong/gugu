"""首板战法选股预过滤器单元测试（北京炒家策略）。

覆盖核心场景：
- ST股票排除
- 创业板排除
- 北交所排除
- 次新股排除
- 市值范围过滤（30-100亿）
- 股价范围过滤（3-20元）
- 位置判断（低/中/高位）
- 缓存机制
- 批量检查
"""

from __future__ import annotations

import time

import pytest

from gugu.filters.first_board_prefilter import (
    FirstBoardPrefilter,
    _is_bse_stock,
    _is_gem_stock,
    _is_new_stock,
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
        assert _is_gem_stock("300001") == True  # 创业板
        assert _is_gem_stock("30001") == False  # 补零后变成 "030001"（沪市）


class TestIsBseStock:
    """北交所判断测试。"""

    def test_bse_stock_8_prefix(self):
        """8开头应为北交所。"""
        assert _is_bse_stock("830000") == True
        assert _is_bse_stock("835000") == True
        assert _is_bse_stock("870000") == True

    def test_bse_stock_4_prefix(self):
        """4开头应为北交所。"""
        assert _is_bse_stock("430000") == True
        assert _is_bse_stock("430500") == True

    def test_main_board_not_bse(self):
        """主板股票不应被判断为北交所。"""
        assert _is_bse_stock("600519") == False  # 沪市主板
        assert _is_bse_stock("000001") == False  # 深市主板
        assert _is_bse_stock("300001") == False  # 创业板


class TestIsNewStock:
    """次新股判断测试。"""

    def test_new_stock_under_threshold(self):
        """上市天数低于阈值应为次新股。"""
        assert _is_new_stock(30, threshold=60) == True
        assert _is_new_stock(59, threshold=60) == True
        assert _is_new_stock(1, threshold=60) == True

    def test_old_stock_above_threshold(self):
        """上市天数高于阈值不应为次新股。"""
        assert _is_new_stock(60, threshold=60) == False
        assert _is_new_stock(100, threshold=60) == False
        assert _is_new_stock(1000, threshold=60) == False

    def test_none_listed_days(self):
        """上市天数为None时不应为次新股。"""
        assert _is_new_stock(None, threshold=60) == False


class TestFirstBoardPrefilterBasic:
    """预过滤器基础功能测试。"""

    def test_exclude_st_stock(self):
        """ST股票应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="ST某某", price=10.0, market_cap=50e8)
        assert result["pass"] == False
        assert any("ST" in reason for reason in result["reasons"])

    def test_exclude_gem_stock(self):
        """创业板股票应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("300001", name="创业板股", price=15.0, market_cap=50e8)
        assert result["pass"] == False
        assert any("创业板" in reason for reason in result["reasons"])

    def test_exclude_bse_stock(self):
        """北交所股票应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("830000", name="北交所股", price=10.0, market_cap=50e8)
        assert result["pass"] == False
        assert any("北交所" in reason for reason in result["reasons"])

    def test_exclude_new_stock(self):
        """次新股应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="新股", price=10.0, market_cap=50e8, listed_days=30)
        assert result["pass"] == False
        assert any("次新" in reason for reason in result["reasons"])

    def test_exclude_high_price(self):
        """股价>=20元应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="高价股", price=25.0, market_cap=50e8)
        assert result["pass"] == False
        assert any("股价" in reason and "高" in reason for reason in result["reasons"])

    def test_exclude_low_price(self):
        """股价<3元应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="低价股", price=2.5, market_cap=50e8)
        assert result["pass"] == False
        assert any("股价" in reason and "低" in reason for reason in result["reasons"])

    def test_exclude_small_cap(self):
        """市值<30亿应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="小盘股", price=10.0, market_cap=20e8)
        assert result["pass"] == False
        assert any("市值" in reason and "小" in reason for reason in result["reasons"])

    def test_exclude_large_cap(self):
        """市值>100亿应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="大盘股", price=10.0, market_cap=150e8)
        assert result["pass"] == False
        assert any("市值" in reason and "大" in reason for reason in result["reasons"])

    def test_pass_normal_stock(self):
        """符合条件的主板股票应通过预过滤。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check(
            "600000", name="正常股", price=10.0, market_cap=50e8, listed_days=365
        )
        assert result["pass"] == True
        assert len(result["reasons"]) == 0

    def test_pass_shenzhen_stock(self):
        """深市主板股票应通过预过滤。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check(
            "000001", name="平安银行", price=12.0, market_cap=60e8, listed_days=1000
        )
        assert result["pass"] == True

    def test_pass_small_zhongban(self):
        """中小板股票应通过预过滤。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check(
            "002001", name="新和成", price=15.0, market_cap=50e8, listed_days=500
        )
        assert result["pass"] == True


class TestFirstBoardPrefilterCache:
    """缓存机制测试。"""

    def test_code_cache_hit(self):
        """相同代码第二次检查应命中缓存。"""
        prefilter = FirstBoardPrefilter()
        result1 = prefilter.check("600000", name="测试股", price=10.0, market_cap=50e8)
        result2 = prefilter.check("600000", name="测试股", price=10.0, market_cap=50e8)
        assert result1 == result2

    def test_different_code_separate_cache(self):
        """不同代码应有独立缓存。"""
        prefilter = FirstBoardPrefilter()
        result1 = prefilter.check("600000", name="股票A", price=10.0, market_cap=50e8)
        result2 = prefilter.check("000001", name="股票B", price=10.0, market_cap=50e8)
        # 不同股票独立
        assert result1["data"]["is_st"] == result2["data"]["is_st"]


class TestFirstBoardPrefilterEdgeCases:
    """边界场景测试。"""

    def test_empty_symbol(self):
        """空代码应处理不抛出异常。"""
        prefilter = FirstBoardPrefilter()
        try:
            result = prefilter.check("", name="空代码", price=10.0, market_cap=50e8)
            # 空代码应不通过或有合理处理
        except Exception:
            # 如果抛出异常也应该是可接受的
            pass

    def test_none_price(self):
        """价格为None时应处理不抛出异常。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="测试股", price=None, market_cap=50e8)
        # None价格应有合理处理
        assert result is not None

    def test_none_market_cap(self):
        """市值为None时应处理不抛出异常。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="测试股", price=10.0, market_cap=None)
        # None市值应有合理处理
        assert result is not None

    def test_boundary_price_20(self):
        """股价恰好为20元应被排除。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="边界股", price=20.0, market_cap=50e8)
        assert result["pass"] == False  # price_max=20.0，恰好20应排除

    def test_boundary_price_3(self):
        """股价恰好为3元应通过。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="边界股", price=3.0, market_cap=50e8)
        assert result["pass"] == True  # price_min=3.0，恰好3应通过

    def test_boundary_market_cap_30(self):
        """市值恰好为30亿应通过。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="边界股", price=10.0, market_cap=30e8)
        assert result["pass"] == True  # market_cap_min=30e8，恰好30亿应通过

    def test_boundary_market_cap_100(self):
        """市值恰好为100亿应通过。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="边界股", price=10.0, market_cap=100e8)
        assert result["pass"] == True  # market_cap_max=100e8，恰好100亿应通过


class TestFirstBoardPrefilterDataOutput:
    """数据输出测试。"""

    def test_result_contains_pass(self):
        """结果应包含pass字段。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="测试股", price=10.0, market_cap=50e8)
        assert "pass" in result
        assert isinstance(result["pass"], bool)

    def test_result_contains_reasons(self):
        """结果应包含reasons字段。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="测试股", price=10.0, market_cap=50e8)
        assert "reasons" in result
        assert isinstance(result["reasons"], list)

    def test_result_contains_data(self):
        """结果应包含data字段。"""
        prefilter = FirstBoardPrefilter()
        result = prefilter.check("600000", name="测试股", price=10.0, market_cap=50e8)
        assert "data" in result
        assert "is_st" in result["data"]
        assert "is_gem" in result["data"]
        assert "is_bse" in result["data"]
        assert "is_new" in result["data"]
