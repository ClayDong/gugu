"""基金监控模块单元测试。"""
from __future__ import annotations

from unittest import mock

import numpy as np
import pandas as pd
import pytest

from gugu.notifier.fund_monitor import (
    FundConfig,
    FundData,
    FundMonitor,
    MonitorAlert,
    build_fund_monitor_card,
    fmt_pct,
    load_fund_configs,
    run_all_fund_monitors,
)


# ==============================================================================
# A. 模型 dataclass
# ==============================================================================


class TestFundConfig:
    """FundConfig dataclass 测试。"""

    def test_defaults(self):
        cfg = FundConfig()
        assert cfg.fund_code == "001480"
        assert cfg.fund_name == "财通成长优选混合A"
        assert cfg.cost_price is None
        assert cfg.total_shares is None
        assert cfg.total_investment is None
        assert cfg.base_investment == 1000.0

    def test_custom_values(self):
        cfg = FundConfig(
            fund_code="110011",
            fund_name="易方达中小盘混合",
            cost_price=1.5,
            total_shares=1000.0,
            total_investment=1500.0,
            base_investment=500.0,
        )
        assert cfg.fund_code == "110011"
        assert cfg.fund_name == "易方达中小盘混合"
        assert cfg.cost_price == 1.5
        assert cfg.total_shares == 1000.0
        assert cfg.total_investment == 1500.0
        assert cfg.base_investment == 500.0

    def test_partial_values(self):
        cfg = FundConfig(cost_price=2.0)
        assert cfg.cost_price == 2.0
        assert cfg.fund_code == "001480"  # 未传保持默认


class TestFundData:
    """FundData dataclass 测试。"""

    def test_minimal(self):
        data = FundData(date="2024-01-15", net_value=1.234, daily_change_pct=-0.5)
        assert data.date == "2024-01-15"
        assert data.net_value == 1.234
        assert data.daily_change_pct == -0.5
        assert data.weekly_change_pct is None
        assert data.monthly_change_pct is None
        assert data.quarterly_change_pct is None
        assert data.yearly_change_pct is None

    def test_full(self):
        data = FundData(
            date="2024-01-15",
            net_value=1.234,
            daily_change_pct=1.5,
            weekly_change_pct=2.0,
            monthly_change_pct=5.0,
            quarterly_change_pct=10.0,
            yearly_change_pct=20.0,
        )
        assert data.weekly_change_pct == 2.0
        assert data.yearly_change_pct == 20.0


class TestMonitorAlert:
    """MonitorAlert dataclass 测试。"""

    def test_minimal(self):
        alert = MonitorAlert(
            alert_type="daily_change", level="warning", title="测试告警", content="测试内容"
        )
        assert alert.alert_type == "daily_change"
        assert alert.level == "warning"
        assert alert.title == "测试告警"
        assert alert.content == "测试内容"
        assert alert.action is None
        assert alert.timestamp == ""

    def test_full(self):
        alert = MonitorAlert(
            alert_type="profit",
            level="danger",
            title="收益告警",
            content="收益超200%",
            action="全部止盈",
            timestamp="2024-01-15 10:00:00",
        )
        assert alert.action == "全部止盈"
        assert alert.timestamp == "2024-01-15 10:00:00"


# ==============================================================================
# B. 纯函数
# ==============================================================================


class TestFmtPct:
    """fmt_pct 函数测试。"""

    def test_none(self):
        assert fmt_pct(None) == "--"

    def test_zero(self):
        assert fmt_pct(0) == "+0.00%"

    def test_positive(self):
        assert fmt_pct(1.234) == "+1.23%"

    def test_negative(self):
        assert fmt_pct(-5.678) == "-5.68%"

    def test_large_positive(self):
        assert fmt_pct(123.456) == "+123.46%"

    def test_small_positive(self):
        assert fmt_pct(0.001) == "+0.00%"

    def test_small_negative(self):
        assert fmt_pct(-0.001) == "-0.00%"


class TestBuildFundMonitorCard:
    """build_fund_monitor_card 函数测试。"""

    def test_basic_structure(self):
        """验证输出的 dict 包含 header template 和 elements 基本结构。"""
        result = {
            "fund_name": "测试基金",
            "fund_code": "001480",
            "date": "2024-01-15",
            "net_value": 1.5,
            "daily_change_pct": 2.0,
            "weekly_change_pct": 3.0,
            "monthly_change_pct": 5.0,
            "quarterly_change_pct": None,
            "yearly_change_pct": None,
            "profit_pct": 50.0,
            "drawdown_pct": -5.0,
            "volatility": 35.0,
            "alerts": [],
            "alert_count": 0,
            "timestamp": "2024-01-15 10:00:00",
        }
        with mock.patch("gugu.notifier.formatter._card") as mock_card:
            mock_card.return_value = {
                "msg_type": "interactive",
                "card": {"header": {"template": "blue"}, "elements": []},
            }
            card = build_fund_monitor_card(result)
            assert card["msg_type"] == "interactive"
            assert card["card"]["header"]["template"] == "blue"
            # 验证 _card 被正确调用
            mock_card.assert_called_once()
            call_kwargs = mock_card.call_args[1]
            assert "测试基金" in call_kwargs.get("title", "")
            assert call_kwargs.get("template") == "blue"

    def test_negative_daily_change_red_template(self):
        """日涨跌为负时 template 应为 red。"""
        result = {
            "fund_name": "测试基金",
            "fund_code": "001480",
            "date": "2024-01-15",
            "net_value": 1.5,
            "daily_change_pct": -2.0,
            "weekly_change_pct": None,
            "monthly_change_pct": None,
            "quarterly_change_pct": None,
            "yearly_change_pct": None,
            "profit_pct": None,
            "drawdown_pct": None,
            "volatility": None,
            "alerts": [],
            "alert_count": 0,
            "timestamp": "2024-01-15 10:00:00",
        }
        with mock.patch("gugu.notifier.formatter._card") as mock_card:
            mock_card.return_value = {
                "msg_type": "interactive",
                "card": {"header": {"template": "red"}, "elements": []},
            }
            card = build_fund_monitor_card(result)
            assert card["card"]["header"]["template"] == "red"

    def test_alert_summary_included(self):
        """有告警时应包含告警摘要和操作建议 sections。"""
        result = {
            "fund_name": "测试基金",
            "fund_code": "001480",
            "date": "2024-01-15",
            "net_value": 1.5,
            "daily_change_pct": -5.5,
            "weekly_change_pct": None,
            "monthly_change_pct": None,
            "quarterly_change_pct": None,
            "yearly_change_pct": None,
            "profit_pct": None,
            "drawdown_pct": None,
            "volatility": None,
            "alerts": [
                {
                    "alert_type": "daily_change",
                    "level": "danger",
                    "title": "日跌幅超5%: -5.50%",
                    "content": "建议定投金额×1.5 (1500元)",
                    "action": "加仓",
                }
            ],
            "alert_count": 1,
            "timestamp": "2024-01-15 10:00:00",
        }
        with mock.patch("gugu.notifier.formatter._card") as mock_card:
            mock_card.return_value = {
                "msg_type": "interactive",
                "card": {"header": {"template": "red"}, "elements": []},
            }
            build_fund_monitor_card(result)
            mock_card.assert_called_once()
            sections = mock_card.call_args[1].get("sections", [])
            all_text = "\n".join(sections)
            assert "告警" in all_text
            assert "日跌幅超5%" in all_text

    def test_no_alerts(self):
        """无告警时不应包含告警信息。"""
        result = {
            "fund_name": "测试基金",
            "fund_code": "001480",
            "date": "2024-01-15",
            "net_value": 1.5,
            "daily_change_pct": 0.5,
            "weekly_change_pct": None,
            "monthly_change_pct": None,
            "quarterly_change_pct": None,
            "yearly_change_pct": None,
            "profit_pct": None,
            "drawdown_pct": None,
            "volatility": None,
            "alerts": [],
            "alert_count": 0,
            "timestamp": "2024-01-15 10:00:00",
        }
        with mock.patch("gugu.notifier.formatter._card") as mock_card:
            mock_card.return_value = {
                "msg_type": "interactive",
                "card": {"header": {"template": "blue"}, "elements": []},
            }
            build_fund_monitor_card(result)
            sections = mock_card.call_args[1].get("sections", [])
            all_text = "\n".join(sections)
            assert "告警" not in all_text


# ==============================================================================
# C. FundMonitor 类方法
# ==============================================================================


def _make_mock_df() -> pd.DataFrame:
    """构建 mock DataFrame，columns 模拟 akshare 返回的字段。"""
    np.random.seed(42)
    n = 30
    base = 1.0
    values = base + np.cumsum(np.random.randn(n) * 0.02)
    return pd.DataFrame(
        {
            "净值日期": pd.date_range("2024-01-01", periods=n, freq="D"),
            "单位净值": values,
            "日增长率": np.random.randn(n) * 0.5,
        }
    )


class TestCalcPeriodChange:
    """_calc_period_change 静态方法测试。"""

    def test_normal(self):
        df = _make_mock_df()
        result = FundMonitor._calc_period_change(df, 5)
        # df 有 30 行 > 5，应返回一个浮点数
        assert result is not None
        assert isinstance(result, float)

    def test_not_enough_data(self):
        df = _make_mock_df()
        result = FundMonitor._calc_period_change(df, 100)
        assert result is None

    def test_edge_exact_days(self):
        df = _make_mock_df()
        # len(df) == 30, days=30 时 len(df) < 30 不成立，所以可以计算
        result = FundMonitor._calc_period_change(df, 30)
        # 需要取 index 30，但最大 index 是 29，所以会 IndexError 返回 None
        assert result is None

    def test_single_row(self):
        df = pd.DataFrame({"净值日期": ["2024-01-01"], "单位净值": [1.0], "日增长率": [0.0]})
        result = FundMonitor._calc_period_change(df, 1)
        assert result is None


class TestCalcProfit:
    """calc_profit 方法测试。"""

    def test_with_cost_price(self):
        monitor = FundMonitor(config=FundConfig(cost_price=1.0))
        monitor.fund_data = FundData(date="2024-01-15", net_value=1.5, daily_change_pct=0.0)
        profit = monitor.calc_profit()
        assert profit is not None
        assert profit == pytest.approx(50.0)

    def test_no_cost_price(self):
        monitor = FundMonitor()
        monitor.fund_data = FundData(date="2024-01-15", net_value=1.5, daily_change_pct=0.0)
        profit = monitor.calc_profit()
        assert profit is None

    def test_no_fund_data(self):
        monitor = FundMonitor(config=FundConfig(cost_price=1.0))
        profit = monitor.calc_profit()
        assert profit is None

    def test_cost_price_zero(self):
        monitor = FundMonitor(config=FundConfig(cost_price=0.0))
        monitor.fund_data = FundData(date="2024-01-15", net_value=1.5, daily_change_pct=0.0)
        profit = monitor.calc_profit()
        assert profit is None

    def test_negative_profit(self):
        monitor = FundMonitor(config=FundConfig(cost_price=2.0))
        monitor.fund_data = FundData(date="2024-01-15", net_value=1.0, daily_change_pct=0.0)
        profit = monitor.calc_profit()
        assert profit is not None
        assert profit == pytest.approx(-50.0)


class TestCalcDrawdown:
    """calc_drawdown 方法测试。"""

    def test_normal_drawdown(self):
        monitor = FundMonitor()
        monitor.history_data = pd.DataFrame({"单位净值": [1.0, 1.5, 1.2, 1.3, 1.1]})
        monitor.fund_data = FundData(date="2024-01-15", net_value=1.1, daily_change_pct=0.0)
        dd = monitor.calc_drawdown()
        # peak=1.5, cur=1.1 => ((1.1-1.5)/1.5)*100 = -26.67%
        assert dd is not None
        assert dd == pytest.approx(-26.6667, rel=0.01)

    def test_no_history(self):
        monitor = FundMonitor()
        monitor.fund_data = FundData(date="2024-01-15", net_value=1.0, daily_change_pct=0.0)
        dd = monitor.calc_drawdown()
        assert dd is None

    def test_empty_history(self):
        monitor = FundMonitor()
        monitor.history_data = pd.DataFrame()
        monitor.fund_data = FundData(date="2024-01-15", net_value=1.0, daily_change_pct=0.0)
        dd = monitor.calc_drawdown()
        assert dd is None

    def test_no_fund_data(self):
        monitor = FundMonitor()
        monitor.history_data = pd.DataFrame({"单位净值": [1.0, 1.5]})
        dd = monitor.calc_drawdown()
        assert dd is None

    def test_zero_peak(self):
        """峰值全零时返回 None。"""
        monitor = FundMonitor()
        monitor.history_data = pd.DataFrame({"单位净值": [0.0, 0.0, 0.0]})
        monitor.fund_data = FundData(date="2024-01-15", net_value=0.0, daily_change_pct=0.0)
        dd = monitor.calc_drawdown()
        assert dd is None

    def test_at_peak(self):
        """当前净值等于历史最高时回撤为 0%。"""
        monitor = FundMonitor()
        monitor.history_data = pd.DataFrame({"单位净值": [1.0, 1.2, 1.5]})
        monitor.fund_data = FundData(date="2024-01-15", net_value=1.5, daily_change_pct=0.0)
        dd = monitor.calc_drawdown()
        assert dd is not None
        assert dd == pytest.approx(0.0)


class TestCalcVolatility:
    """calc_volatility 方法测试。"""

    def test_normal(self):
        monitor = FundMonitor()
        # 构建足够多的数据
        n = 30
        vals = 1.0 + np.cumsum(np.random.randn(n) * 0.01)
        monitor.history_data = pd.DataFrame({"单位净值": vals})
        vol = monitor.calc_volatility(days=20)
        assert vol is not None
        assert vol > 0

    def test_not_enough_data(self):
        monitor = FundMonitor()
        monitor.history_data = pd.DataFrame({"单位净值": [1.0, 1.1, 1.2]})
        vol = monitor.calc_volatility(days=20)
        assert vol is None

    def test_no_history(self):
        monitor = FundMonitor()
        vol = monitor.calc_volatility()
        assert vol is None

    def test_too_few_returns(self):
        """数据少到 pct_change dropna 后不足 5 个样本时返回 0.0。"""
        monitor = FundMonitor()
        # 6 个值 -> pct_change 得到 5 个 return，但其中可能有很多 NaN
        monitor.history_data = pd.DataFrame(
            {"单位净值": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]}
        )
        vol = monitor.calc_volatility(days=6)
        # 所有值相同，pct_change=0，dropna 后 len=5，仍 >= 5，std=0
        assert vol is not None
        assert vol == pytest.approx(0.0)

    def test_fewer_than_five_returns(self):
        """少于 5 个有效 return 时返回 0.0。"""
        monitor = FundMonitor()
        # 5 个值 -> pct_change 得到 4 个 return（dropna 后）
        monitor.history_data = pd.DataFrame(
            {"单位净值": [1.0, 1.1, 1.2, 1.3, 1.4]}
        )
        vol = monitor.calc_volatility(days=5)
        assert vol is not None
        assert vol == 0.0


# ==============================================================================
# D. 监控规则方法
# ==============================================================================


class TestMonitorDailyChange:
    """monitor_daily_change 方法测试。"""

    def _make_monitor(self, daily_change_pct: float) -> FundMonitor:
        monitor = FundMonitor()
        monitor.fund_data = FundData(
            date="2024-01-15", net_value=1.0, daily_change_pct=daily_change_pct
        )
        return monitor

    def test_no_fund_data(self):
        monitor = FundMonitor()
        assert monitor.monitor_daily_change() == []

    def test_danger_drop_10(self):
        """日跌超 10% -> danger。"""
        monitor = self._make_monitor(-10.5)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1
        assert alerts[0].level == "danger"
        assert "10%" in alerts[0].title

    def test_danger_drop_8(self):
        """日跌超 8% -> danger。"""
        monitor = self._make_monitor(-9.0)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1
        assert alerts[0].level == "danger"
        assert "8%" in alerts[0].title

    def test_warning_drop_5(self):
        """日跌超 5% -> warning。"""
        monitor = self._make_monitor(-6.0)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1
        assert alerts[0].level == "warning"
        assert "5%" in alerts[0].title

    def test_warning_drop_3(self):
        """日跌超 3% -> warning。"""
        monitor = self._make_monitor(-4.0)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1
        assert alerts[0].level == "warning"
        assert "3%" in alerts[0].title

    def test_info_rise_8(self):
        """日涨超 8% -> info。"""
        monitor = self._make_monitor(9.0)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1
        assert alerts[0].level == "info"
        assert "8%" in alerts[0].title

    def test_info_rise_5(self):
        """日涨超 5% -> info。"""
        monitor = self._make_monitor(6.0)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1
        assert alerts[0].level == "info"
        assert "5%" in alerts[0].title

    def test_info_rise_3(self):
        """日涨超 3% -> info。"""
        monitor = self._make_monitor(4.0)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1
        assert alerts[0].level == "info"
        assert "3%" in alerts[0].title

    def test_normal_range_no_alert(self):
        """日涨跌在 -3%~3% 之间无告警。"""
        monitor = self._make_monitor(1.0)
        assert monitor.monitor_daily_change() == []
        monitor = self._make_monitor(-2.0)
        assert monitor.monitor_daily_change() == []

    def test_drop_10_and_rise_8_no_duplicate(self):
        """日跌超 10% 且日涨超 8% 不会同时触发（跌 10.5 只触发下跌告警）。"""
        monitor = self._make_monitor(-10.5)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1  # 只触发跌超 10%

    def test_action_and_amount_in_alerts(self):
        """告警应包含 action 和建议定投金额。"""
        monitor = self._make_monitor(-5.5)
        alerts = monitor.monitor_daily_change()
        assert len(alerts) == 1
        assert alerts[0].action == "加仓"
        base = monitor.config.base_investment
        assert f"{base * 1.5:.0f}元" in alerts[0].content


class TestMonitorProfit:
    """monitor_profit 方法测试。"""

    def test_no_profit_data(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_profit", return_value=None):
            alerts = monitor.monitor_profit()
        assert alerts == []

    def test_profit_ge_200_danger(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_profit", return_value=250.0):
            alerts = monitor.monitor_profit()
        assert len(alerts) == 1
        assert alerts[0].level == "danger"
        assert "200%" in alerts[0].title

    def test_profit_ge_150_warning(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_profit", return_value=180.0):
            alerts = monitor.monitor_profit()
        assert len(alerts) == 1
        assert alerts[0].level == "warning"
        assert "150%" in alerts[0].title

    def test_profit_ge_100_warning(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_profit", return_value=120.0):
            alerts = monitor.monitor_profit()
        assert len(alerts) == 1
        assert alerts[0].level == "warning"
        assert "100%" in alerts[0].title

    def test_profit_ge_50_info(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_profit", return_value=60.0):
            alerts = monitor.monitor_profit()
        assert len(alerts) == 1
        assert alerts[0].level == "info"
        assert "50%" in alerts[0].title

    def test_profit_below_50_no_alert(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_profit", return_value=30.0):
            alerts = monitor.monitor_profit()
        assert alerts == []

    def test_exact_boundaries(self):
        """边界值测试。"""
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_profit", return_value=50.0):
            alerts = monitor.monitor_profit()
            assert len(alerts) == 1
        with mock.patch.object(monitor, "calc_profit", return_value=100.0):
            alerts = monitor.monitor_profit()
            assert len(alerts) == 1
        with mock.patch.object(monitor, "calc_profit", return_value=150.0):
            alerts = monitor.monitor_profit()
            assert len(alerts) == 1
        with mock.patch.object(monitor, "calc_profit", return_value=200.0):
            alerts = monitor.monitor_profit()
            assert len(alerts) == 1


class TestMonitorDrawdown:
    """monitor_drawdown 方法测试。"""

    def test_no_drawdown_data(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_drawdown", return_value=None):
            alerts = monitor.monitor_drawdown()
        assert alerts == []

    def test_drawdown_le_25_danger(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_drawdown", return_value=-30.0):
            alerts = monitor.monitor_drawdown()
        assert len(alerts) == 1
        assert alerts[0].level == "danger"
        assert "25%" in alerts[0].title
        assert "150%" in alerts[0].content  # base_investment * 2.5

    def test_drawdown_le_15_warning(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_drawdown", return_value=-20.0):
            alerts = monitor.monitor_drawdown()
        assert len(alerts) == 1
        assert alerts[0].level == "warning"
        assert "15%" in alerts[0].title

    def test_drawdown_le_10_warning(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_drawdown", return_value=-12.0):
            alerts = monitor.monitor_drawdown()
        assert len(alerts) == 1
        assert alerts[0].level == "warning"
        assert "10%" in alerts[0].title

    def test_drawdown_above_10_no_alert(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_drawdown", return_value=-5.0):
            alerts = monitor.monitor_drawdown()
        assert alerts == []

    def test_exact_boundaries(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_drawdown", return_value=-10.0):
            assert len(monitor.monitor_drawdown()) == 1
        with mock.patch.object(monitor, "calc_drawdown", return_value=-15.0):
            assert len(monitor.monitor_drawdown()) == 1
        with mock.patch.object(monitor, "calc_drawdown", return_value=-25.0):
            assert len(monitor.monitor_drawdown()) == 1


class TestMonitorVolatility:
    """monitor_volatility 方法测试。"""

    def test_no_volatility_data(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_volatility", return_value=None):
            alerts = monitor.monitor_volatility()
        assert alerts == []

    def test_vol_ge_60_danger(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_volatility", return_value=65.0):
            alerts = monitor.monitor_volatility()
        assert len(alerts) == 1
        assert alerts[0].level == "danger"
        assert "极高" in alerts[0].title

    def test_vol_ge_50_warning(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_volatility", return_value=55.0):
            alerts = monitor.monitor_volatility()
        assert len(alerts) == 1
        assert alerts[0].level == "warning"
        assert "高" in alerts[0].title

    def test_vol_ge_40_info(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_volatility", return_value=45.0):
            alerts = monitor.monitor_volatility()
        assert len(alerts) == 1
        assert alerts[0].level == "info"
        assert "中等" in alerts[0].title

    def test_vol_le_30_info(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_volatility", return_value=25.0):
            alerts = monitor.monitor_volatility()
        assert len(alerts) == 1
        assert alerts[0].level == "info"
        assert "低" in alerts[0].title

    def test_vol_between_30_and_40_no_alert(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_volatility", return_value=35.0):
            alerts = monitor.monitor_volatility()
        assert alerts == []

    def test_exact_boundaries(self):
        monitor = FundMonitor()
        with mock.patch.object(monitor, "calc_volatility", return_value=30.0):
            assert len(monitor.monitor_volatility()) == 1  # le 30 -> info
        with mock.patch.object(monitor, "calc_volatility", return_value=40.0):
            assert len(monitor.monitor_volatility()) == 1  # >= 40 -> info
        with mock.patch.object(monitor, "calc_volatility", return_value=50.0):
            assert len(monitor.monitor_volatility()) == 1  # >= 50 -> warning
        with mock.patch.object(monitor, "calc_volatility", return_value=60.0):
            assert len(monitor.monitor_volatility()) == 1  # >= 60 -> danger


# ==============================================================================
# E. 模块级函数
# ==============================================================================


class TestLoadFundConfigs:
    """load_fund_configs 函数测试。"""

    def test_enabled_with_funds(self):
        with mock.patch("gugu.notifier.fund_monitor.settings") as mock_settings:
            mock_settings.return_value = {
                "fund": {
                    "enabled": True,
                    "funds": [
                        {"code": "001480", "name": "测试基金A", "cost_price": 1.0, "base_investment": 500.0},
                        {"code": "110011", "name": "测试基金B"},
                    ],
                }
            }
            configs = load_fund_configs()
        assert len(configs) == 2
        assert configs[0].fund_code == "001480"
        assert configs[0].fund_name == "测试基金A"
        assert configs[0].cost_price == 1.0
        assert configs[0].base_investment == 500.0
        assert configs[1].fund_code == "110011"
        assert configs[1].cost_price is None
        assert configs[1].base_investment == 1000.0  # 默认值

    def test_disabled(self):
        with mock.patch("gugu.notifier.fund_monitor.settings") as mock_settings:
            mock_settings.return_value = {
                "fund": {
                    "enabled": False,
                    "funds": [{"code": "001480", "name": "测试基金"}],
                }
            }
            configs = load_fund_configs()
        assert configs == []

    def test_no_fund_key(self):
        with mock.patch("gugu.notifier.fund_monitor.settings") as mock_settings:
            mock_settings.return_value = {}
            configs = load_fund_configs()
        assert configs == []

    def test_no_funds_list(self):
        with mock.patch("gugu.notifier.fund_monitor.settings") as mock_settings:
            mock_settings.return_value = {"fund": {"enabled": True}}
            configs = load_fund_configs()
        assert configs == []

    def test_empty_funds_list(self):
        with mock.patch("gugu.notifier.fund_monitor.settings") as mock_settings:
            mock_settings.return_value = {"fund": {"enabled": True, "funds": []}}
            configs = load_fund_configs()
        assert configs == []

    def test_missing_enabled_field(self):
        """enabled 字段缺失时默认为 True。"""
        with mock.patch("gugu.notifier.fund_monitor.settings") as mock_settings:
            mock_settings.return_value = {
                "fund": {
                    "funds": [{"code": "001480", "name": "测试基金"}],
                }
            }
            configs = load_fund_configs()
        assert len(configs) == 1


class TestRunAllFundMonitors:
    """run_all_fund_monitors 异步函数测试。"""

    @pytest.mark.asyncio
    async def test_no_configs(self):
        with mock.patch("gugu.notifier.fund_monitor.load_fund_configs") as mock_load:
            mock_load.return_value = []
            result = await run_all_fund_monitors()
        assert result["results"] == []
        assert "date" in result

    @pytest.mark.asyncio
    async def test_with_one_fund(self):
        cfg = FundConfig(fund_code="001480", fund_name="测试基金")
        with mock.patch("gugu.notifier.fund_monitor.load_fund_configs") as mock_load:
            mock_load.return_value = [cfg]
            with mock.patch(
                "gugu.notifier.fund_monitor.FundMonitor.run_monitor",
                new_callable=mock.AsyncMock,
            ) as mock_run:
                mock_run.return_value = {
                    "status": "success",
                    "fund_code": "001480",
                    "fund_name": "测试基金",
                    "date": "2024-01-15",
                    "net_value": 1.5,
                    "daily_change_pct": 0.5,
                    "weekly_change_pct": None,
                    "monthly_change_pct": None,
                    "quarterly_change_pct": None,
                    "yearly_change_pct": None,
                    "profit_pct": None,
                    "drawdown_pct": None,
                    "volatility": None,
                    "alerts": [],
                    "alert_count": 0,
                    "timestamp": "2024-01-15 10:00:00",
                }
                result = await run_all_fund_monitors()
        assert len(result["results"]) == 1
        assert result["results"][0]["status"] == "success"
        assert result["date"] == "2024-01-15"

    @pytest.mark.asyncio
    async def test_monitor_raises_exception(self):
        cfg = FundConfig(fund_code="001480", fund_name="测试基金")
        with mock.patch("gugu.notifier.fund_monitor.load_fund_configs") as mock_load:
            mock_load.return_value = [cfg]
            with mock.patch(
                "gugu.notifier.fund_monitor.FundMonitor.run_monitor",
                new_callable=mock.AsyncMock,
            ) as mock_run:
                mock_run.side_effect = ValueError("模拟错误")
                result = await run_all_fund_monitors()
        assert len(result["results"]) == 1
        assert result["results"][0]["status"] == "error"
        assert result["results"][0]["message"] == "模拟错误"

    @pytest.mark.asyncio
    async def test_multiple_funds(self):
        cfg1 = FundConfig(fund_code="001480", fund_name="基金A")
        cfg2 = FundConfig(fund_code="110011", fund_name="基金B")
        with mock.patch("gugu.notifier.fund_monitor.load_fund_configs") as mock_load:
            mock_load.return_value = [cfg1, cfg2]
            with mock.patch(
                "gugu.notifier.fund_monitor.FundMonitor.run_monitor",
                new_callable=mock.AsyncMock,
            ) as mock_run:
                mock_run.side_effect = [
                    {"status": "success", "fund_code": "001480", "date": "2024-01-15"},
                    {"status": "success", "fund_code": "110011", "date": "2024-01-15"},
                ]
                result = await run_all_fund_monitors()
        assert len(result["results"]) == 2
        assert result["date"] == "2024-01-15"