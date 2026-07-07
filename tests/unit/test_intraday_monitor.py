"""盘中实时盯盘监控器单元测试。

覆盖场景：
- 性能模式自动检测
- 自动 / 显式模式初始化
- off 模式跳过
- 扫描股票列表（持仓优先 + 上限控制）
- 告警冷却去重
- AlertItem 数据类
- describe() 元数据
- 超大单净流入告警
- 行业资金流告警
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from gugu.engine.intraday_monitor import (
    PERFORMANCE_CONFIG,
    AlertItem,
    IntradayMonitor,
    detect_performance_mode,
)


# ── 测试数据构造 ──────────────────────────────────────────────


def _make_stock_flow_df(
    super_large_net: float = 0,
    main_net: float = 0,
    main_pct: float = 0,
    days: int = 5,
) -> pd.DataFrame:
    """构造个股资金流测试数据。"""
    return pd.DataFrame(
        [
            {
                "date": date(2024, 1, i + 1),
                "main_net": main_net,
                "main_pct": main_pct,
                "super_large_net": super_large_net,
                "large_net": 0,
                "medium_net": 0,
                "small_net": 0,
            }
            for i in range(days)
        ]
    )


def _make_sector_flow_df() -> pd.DataFrame:
    """构造行业资金流测试数据。"""
    return pd.DataFrame(
        [
            {
                "sector": "半导体",
                "main_net": 1_500_000_000,
                "main_pct": 5.0,
                "change_pct": 2.0,
            },
            {
                "sector": "房地产",
                "main_net": -1_200_000_000,
                "main_pct": -4.0,
                "change_pct": -1.5,
            },
            {
                "sector": "银行",
                "main_net": 500_000_000,
                "main_pct": 2.0,
                "change_pct": 0.5,
            },
        ]
    )


@pytest.fixture
def mock_dm():
    """构造 mock DataManager，避免实际调用 API。"""
    dm = MagicMock()
    dm.fetch_stock_flow = AsyncMock(
        return_value=_make_stock_flow_df()
    )
    dm.fetch_sector_flow = AsyncMock(return_value=_make_sector_flow_df())
    return dm


@pytest.fixture
def trading_time(monkeypatch):
    """mock 交易时段，使 should_run() 返回 True。"""
    monkeypatch.setattr(
        "gugu.engine.intraday_monitor.is_trading_day",
        lambda d=None: True,
    )
    monkeypatch.setattr(
        "gugu.engine.intraday_monitor.is_trading_time",
        lambda now=None: True,
    )


@pytest.fixture
def mock_stock_name(monkeypatch):
    """mock 股票名称查询。"""
    monkeypatch.setattr(
        "gugu.data.stock_names.lookup_stock_name",
        lambda code: "测试股票",
    )


# ── 性能模式检测测试 ──────────────────────────────────────────


class TestPerformanceMode:
    """性能模式检测测试。"""

    def test_detect_performance_mode_returns_valid(self):
        """detect_performance_mode() 应返回 {high, medium, low} 之一。"""
        mode = detect_performance_mode()
        assert mode in {"high", "medium", "low"}


# ── 初始化测试 ────────────────────────────────────────────────


class TestIntradayMonitorInit:
    """初始化测试。"""

    def test_init_with_auto_mode(self, mock_dm):
        """自动模式初始化应解析为有效模式。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            performance_mode="auto",
        )
        assert monitor._mode in {"high", "medium", "low"}
        assert monitor._mode in PERFORMANCE_CONFIG

    @pytest.mark.parametrize("mode", ["high", "medium", "low", "off"])
    def test_init_with_explicit_mode(self, mock_dm, mode):
        """显式模式初始化（high/medium/low/off）。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            performance_mode=mode,
        )
        assert monitor._mode == mode

    def test_off_mode_should_not_run(self, mock_dm):
        """off 模式 should_run() 应返回 False。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            performance_mode="off",
        )
        assert monitor.should_run() is False

    def test_off_mode_skips_trading_day_check(self, mock_dm, monkeypatch):
        """off 模式应在检查交易日前就返回 False。"""
        # 即使 is_trading_day 抛异常，off 模式也应正常返回 False
        monkeypatch.setattr(
            "gugu.engine.intraday_monitor.is_trading_day",
            lambda d=None: (_ for _ in ()).throw(RuntimeError("不应调用")),
        )
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            performance_mode="off",
        )
        assert monitor.should_run() is False


# ── 扫描列表测试 ──────────────────────────────────────────────


class TestScanSymbols:
    """扫描股票列表测试。"""

    def test_get_scan_symbols_respects_max(self, mock_dm):
        """get_scan_symbols() 不超过 max_symbols。"""
        symbols = [f"{i:06d}" for i in range(100)]
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=symbols[:50],
            watchlist=symbols[50:],
            performance_mode="low",  # max_symbols=5
        )
        scan_list = monitor.get_scan_symbols()
        assert len(scan_list) <= 5

    def test_get_scan_symbols_portfolio_first(self, mock_dm):
        """持仓股优先于 watchlist。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519", "000001"],
            watchlist=["600519", "000002", "000003"],
            performance_mode="high",  # max_symbols=50
        )
        scan_list = monitor.get_scan_symbols()
        # 持仓股在前
        assert scan_list[0] == "600519"
        assert scan_list[1] == "000001"
        # watchlist 中的 600519 不重复
        assert scan_list.count("600519") == 1

    def test_get_scan_symbols_deduplicates(self, mock_dm):
        """持仓和 watchlist 重叠的股票不重复。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519", "000001"],
            watchlist=["600519", "000001", "000002"],
            performance_mode="high",
        )
        scan_list = monitor.get_scan_symbols()
        assert len(scan_list) == len(set(scan_list))

    def test_low_mode_excludes_watchlist(self, mock_dm):
        """low 模式不扫描 watchlist（scan_watchlist=False）。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519"],
            watchlist=["000001", "000002"],
            performance_mode="low",  # scan_watchlist=False
        )
        scan_list = monitor.get_scan_symbols()
        assert "000001" not in scan_list
        assert "000002" not in scan_list
        assert "600519" in scan_list


# ── 告警冷却测试 ──────────────────────────────────────────────


class TestAlertCooldown:
    """告警冷却去重测试。"""

    def test_filter_cooldown_deduplicates(self, mock_dm):
        """同一告警（type+symbol 相同）5 分钟内不重复。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            performance_mode="high",
        )
        alert1 = AlertItem(
            alert_type="super_large_inflow",
            symbol="600519",
            name="茅台",
            severity="info",
            direction="buy",
            value=150_000_000,
            message="第一条",
        )
        alert2 = AlertItem(
            alert_type="super_large_inflow",
            symbol="600519",
            name="茅台",
            severity="info",
            direction="buy",
            value=150_000_000,
            message="第二条（应被去重）",
        )

        filtered = monitor._filter_cooldown([alert1, alert2])
        assert len(filtered) == 1
        assert filtered[0].message == "第一条"

    def test_filter_cooldown_different_symbols_pass(self, mock_dm):
        """不同 symbol 的告警不互相去重。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            performance_mode="high",
        )
        alert1 = AlertItem(
            alert_type="super_large_inflow",
            symbol="600519",
            name="茅台",
            severity="info",
            direction="buy",
            value=150_000_000,
            message="茅台",
        )
        alert2 = AlertItem(
            alert_type="super_large_inflow",
            symbol="000001",
            name="平安银行",
            severity="info",
            direction="buy",
            value=120_000_000,
            message="平安",
        )

        filtered = monitor._filter_cooldown([alert1, alert2])
        assert len(filtered) == 2


# ── AlertItem 数据类测试 ──────────────────────────────────────


class TestAlertItem:
    """AlertItem 数据类测试。"""

    def test_alert_item_dataclass(self):
        """AlertItem 数据类字段应正确。"""
        alert = AlertItem(
            alert_type="super_large_inflow",
            symbol="600519",
            name="贵州茅台",
            severity="info",
            direction="buy",
            value=150_000_000,
            message="超大单净流入 1.5亿",
        )
        assert alert.alert_type == "super_large_inflow"
        assert alert.symbol == "600519"
        assert alert.name == "贵州茅台"
        assert alert.severity == "info"
        assert alert.direction == "buy"
        assert alert.value == 150_000_000
        assert "1.5亿" in alert.message
        assert alert.timestamp  # 应有默认值

    def test_alert_item_timestamp_auto_generated(self):
        """AlertItem timestamp 应自动生成。"""
        alert = AlertItem(
            alert_type="test",
            symbol="000001",
            name="test",
            severity="info",
            direction="buy",
            value=0,
            message="test",
        )
        assert isinstance(alert.timestamp, str)
        assert len(alert.timestamp) > 0


# ── describe() 测试 ───────────────────────────────────────────


class TestDescribe:
    """describe() 元数据测试。"""

    def test_describe_returns_metadata(self, mock_dm):
        """describe() 应返回正确元数据。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519"],
            watchlist=["000001"],
            performance_mode="high",
        )
        desc = monitor.describe()
        assert desc["mode"] == "high"
        assert desc["interval_sec"] == 30
        assert desc["max_symbols"] == 50
        assert desc["scan_watchlist"] is True
        assert desc["watchlist_size"] == 1
        assert desc["portfolio_size"] == 1
        assert "thresholds" in desc


# ── run_cycle 测试 ─────────────────────────────────────────────


class TestRunCycle:
    """run_cycle 扫描周期测试。"""

    async def test_run_cycle_returns_empty_when_off(self, mock_dm):
        """off 模式 run_cycle() 应返回空列表。"""
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            performance_mode="off",
        )
        result = await monitor.run_cycle()
        assert result == []

    async def test_run_cycle_returns_empty_when_not_trading(
        self, mock_dm, monkeypatch
    ):
        """非交易日 run_cycle() 应返回空列表。"""
        monkeypatch.setattr(
            "gugu.engine.intraday_monitor.is_trading_day",
            lambda d=None: False,
        )
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519"],
            performance_mode="high",
        )
        result = await monitor.run_cycle()
        assert result == []

    async def test_super_large_inflow_alert(
        self, mock_dm, trading_time, mock_stock_name
    ):
        """超大单净流入 > 1 亿应触发告警。"""
        mock_dm.fetch_stock_flow.return_value = _make_stock_flow_df(
            super_large_net=150_000_000,  # 1.5 亿 > 1 亿阈值
        )
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519"],
            performance_mode="high",
        )
        result = await monitor.run_cycle()

        inflow_alerts = [a for a in result if a.alert_type == "super_large_inflow"]
        assert len(inflow_alerts) >= 1
        assert inflow_alerts[0].direction == "buy"
        assert inflow_alerts[0].value == 150_000_000

    async def test_super_large_outflow_alert(
        self, mock_dm, trading_time, mock_stock_name
    ):
        """超大单净流出 > 1 亿应触发告警。"""
        mock_dm.fetch_stock_flow.return_value = _make_stock_flow_df(
            super_large_net=-120_000_000,  # -1.2 亿 < -1 亿阈值
        )
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519"],
            performance_mode="high",
        )
        result = await monitor.run_cycle()

        outflow_alerts = [
            a for a in result if a.alert_type == "super_large_outflow"
        ]
        assert len(outflow_alerts) >= 1
        assert outflow_alerts[0].direction == "sell"

    async def test_industry_inflow_alert(
        self, mock_dm, trading_time, mock_stock_name
    ):
        """行业流入 > 10 亿应触发告警。"""
        # 个股无异动，仅行业触发
        mock_dm.fetch_stock_flow.return_value = _make_stock_flow_df(
            super_large_net=0,
            main_net=0,
        )
        # 半导体 main_net=15亿 > 10亿阈值
        mock_dm.fetch_sector_flow.return_value = _make_sector_flow_df()

        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519"],
            performance_mode="high",
        )
        result = await monitor.run_cycle()

        industry_alerts = [
            a for a in result if a.alert_type == "industry_inflow"
        ]
        assert len(industry_alerts) >= 1
        assert industry_alerts[0].symbol == "半导体"
        assert industry_alerts[0].direction == "buy"

    async def test_industry_outflow_alert(
        self, mock_dm, trading_time, mock_stock_name
    ):
        """行业流出 > 10 亿应触发告警。"""
        mock_dm.fetch_stock_flow.return_value = _make_stock_flow_df(
            super_large_net=0,
            main_net=0,
        )
        mock_dm.fetch_sector_flow.return_value = _make_sector_flow_df()

        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519"],
            performance_mode="high",
        )
        result = await monitor.run_cycle()

        outflow_alerts = [
            a for a in result if a.alert_type == "industry_outflow"
        ]
        assert len(outflow_alerts) >= 1
        assert outflow_alerts[0].symbol == "房地产"
        assert outflow_alerts[0].direction == "sell"

    async def test_position_warning_alert(
        self, mock_dm, trading_time, mock_stock_name
    ):
        """持仓股主力净流出超 5000 万应触发持仓预警。"""
        mock_dm.fetch_stock_flow.return_value = _make_stock_flow_df(
            main_net=-80_000_000,  # -8000万 < -5000万阈值
        )
        monitor = IntradayMonitor(
            data_manager=mock_dm,
            portfolio_symbols=["600519"],
            performance_mode="high",
        )
        result = await monitor.run_cycle()

        position_alerts = [
            a for a in result if a.alert_type == "position_warning"
        ]
        assert len(position_alerts) >= 1
        assert position_alerts[0].severity == "critical"
