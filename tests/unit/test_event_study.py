"""统一事件研究框架单元测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.event_study import (
    EventStudy,
    HorizonResult,
    StudyConfig,
    StudyEvent,
    StudyResult,
    welch_t,
)


class TestWelchT:
    """Welch t 检验单元测试。"""

    def test_positive_returns_significant(self) -> None:
        """持续正收益应显著大于零。"""
        t, p = welch_t(np.array([0.01, 0.02, 0.015, 0.018, 0.025]))
        assert t > 0
        assert p < 0.05

    def test_negative_returns_significant(self) -> None:
        """持续负收益应显著小于零。"""
        t, p = welch_t(np.array([-0.01, -0.02, -0.015, -0.018]))
        assert t < 0
        assert p < 0.05

    def test_random_noise_not_significant(self) -> None:
        """噪声数据不应显著。"""
        np.random.seed(42)
        noise = np.random.randn(20) * 0.005
        t, p = welch_t(noise)
        assert p > 0.05

    def test_less_than_3_samples_returns_1(self) -> None:
        """少于 3 样本返回 p=1.0。"""
        t, p = welch_t(np.array([0.01]))
        assert p == 1.0

    def test_empty_array_returns_1(self) -> None:
        """空数组返回 p=1.0。"""
        t, p = welch_t(np.array([]))
        assert p == 1.0

    def test_handles_nan(self) -> None:
        """NaN 值应被忽略。"""
        t, p = welch_t(np.array([0.01, np.nan, 0.02, 0.015, np.nan]))
        assert p < 0.05  # 有效值 [0.01, 0.02, 0.015]


class TestStudyConfig:
    """配置验证。"""

    def test_default_config(self) -> None:
        config = StudyConfig(strategy_name="test")
        assert config.strategy_name == "test"
        assert config.horizons == [3, 5, 10]
        assert config.index_code == "000300"
        assert config.entry_mode == "next_open"
        assert config.cost_rate == 0.004

    def test_custom_config(self) -> None:
        config = StudyConfig(
            strategy_name="custom",
            horizons=[1, 5],
            entry_mode="close",
            cost_rate=0.002,
        )
        assert config.horizons == [1, 5]
        assert config.entry_mode == "close"
        assert config.cost_rate == 0.002

    def test_invalid_overlapping_periods_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能重叠") as exc_info:
            StudyConfig(
                strategy_name="test",
                train_start="2024-01-01",
                train_end="2024-06-30",
                val_start="2024-06-30",
                val_end="2024-12-31",
            )
        assert "不能重叠" in str(exc_info.value)

    def test_invalid_numeric_config_rejected(self) -> None:
        with pytest.raises(ValueError, match="cost_rate") as exc_info:
            StudyConfig(strategy_name="test", cost_rate=float("nan"))
        assert "cost_rate" in str(exc_info.value)


class TestClassifyPeriod:
    """时期分类逻辑。"""

    def setup_method(self) -> None:
        self.config = StudyConfig(
            strategy_name="test",
            train_start="2024-01-01",
            train_end="2024-06-30",
            val_start="2024-07-01",
            val_end="2024-12-31",
        )
        self.study = EventStudy(self.config)

    def test_train_period(self) -> None:
        assert self.study._classify_period("2024-03-15") == "train"

    def test_train_period_boundary(self) -> None:
        assert self.study._classify_period("2024-06-30") == "train"

    def test_val_period(self) -> None:
        assert self.study._classify_period("2024-09-01") == "val"

    def test_val_period_boundary(self) -> None:
        assert self.study._classify_period("2024-07-01") == "val"
        assert self.study._classify_period("2024-12-31") == "val"

    def test_outside_period(self) -> None:
        assert self.study._classify_period("2025-03-01") == "outside"

    def test_outside_before_train(self) -> None:
        assert self.study._classify_period("2023-06-01") == "outside"


class TestForwardReturn:
    """前瞻收益计算。"""

    def setup_method(self) -> None:
        self.config = StudyConfig(strategy_name="test", entry_mode="close")
        self.study = EventStudy(self.config)

    def test_simple_forward_return(self) -> None:
        """5 日后上涨应返回正收益。"""
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=20, freq="D"),
                "open": [10 + i * 0.1 for i in range(20)],
                "close": [10 + i * 0.1 for i in range(20)],
                "high": [10 + i * 0.15 for i in range(20)],
                "low": [10 + i * 0.05 for i in range(20)],
                "volume": [1000000] * 20,
            }
        )
        ret = self.study._forward_return(df, 0, 5, 10.0)
        assert ret is not None
        # close[5]=10.5, entry=10.0, raw=0.05, net=0.05-0.004=0.046
        assert abs(ret - 0.046) < 0.001

    def test_forward_return_last_bar_returns_none(self) -> None:
        """超出 K 线范围的应返回 None。"""
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=5, freq="D"),
                "open": [10.0] * 5,
                "close": [10.0] * 5,
                "high": [10.5] * 5,
                "low": [9.5] * 5,
                "volume": [1000000] * 5,
            }
        )
        ret = self.study._forward_return(df, 3, 5, 10.0)  # idx 3 + 5 = 8 >= 5
        assert ret is None

    def test_forward_return_next_open_mode(self) -> None:
        """next_open 模式使用 T+1 open 价。"""
        study = EventStudy(StudyConfig(strategy_name="test", entry_mode="next_open"))
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=10, freq="D"),
                "open": [9.5, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9],
                "close": [10.0, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0],
                "high": [10.5] * 10,
                "low": [9.5] * 10,
                "volume": [1000000] * 10,
            }
        )
        ret = study._forward_return(df, 0, 3, 10.0)
        assert ret is not None
        # next_open=10.1 (idx 1), exit=close[3]=10.4
        # raw=(10.4-10.1)/10.1=0.0297, net≈0.0297-0.004=0.0257
        assert abs(ret - 0.0257) < 0.01

    def test_forward_return_with_entry_price(self) -> None:
        """显式传入 entry_price 应覆盖默认 close。"""
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=10, freq="D"),
                "open": [9.5, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
                "close": [10.0, 10.5, 10.0, 10.0, 10.0, 11.0, 10.0, 10.0, 10.0, 10.0],
                "high": [10.5] * 10,
                "low": [9.5] * 10,
                "volume": [1000000] * 10,
            }
        )
        self.study.config.entry_mode = "close"
        ret = self.study._forward_return(df, 0, 5, 10.0)
        assert ret is not None
        # entry=10.0 (close mode), exit=close[5]=11.0, raw=0.10, net=0.096
        assert abs(ret - 0.096) < 0.001


class TestCheckConcentration:
    """集中度检查。"""

    def test_no_concentration_issue(self) -> None:
        events = [
            StudyEvent(symbol="600001", date="2024-01-01", entry=10.0, period="val", year=2024),
            StudyEvent(symbol="600002", date="2024-01-02", entry=11.0, period="val", year=2024),
            StudyEvent(symbol="600003", date="2024-01-03", entry=12.0, period="val", year=2024),
            StudyEvent(symbol="600001", date="2024-01-04", entry=13.0, period="val", year=2024),
        ]
        issues = EventStudy._check_concentration(events)
        # 600001 出现 2/4=50% > 30%
        assert len(issues) == 1
        assert issues[0]["symbol"] == "600001"

    def test_all_diversified(self) -> None:
        events = [
            StudyEvent(
                symbol=f"60000{i}",
                date="2024-01-01",
                entry=10.0,
                period="val",
                year=2024,
                returns={"5": 0.01},
                excess={"5": 0.005},
            )
            for i in range(10)
        ]
        issues = EventStudy._check_concentration(events)
        assert len(issues) == 0

    def test_empty_events_returns_empty(self) -> None:
        assert EventStudy._check_concentration([]) == []


class TestRunFromHistory:
    """从信号历史运行事件研究。"""

    def test_empty_history_returns_empty(self, tmp_path: Path) -> None:
        history = tmp_path / "empty.jsonl"
        history.write_text("")
        config = StudyConfig(strategy_name="test")
        study = EventStudy(config)
        result = study.run_from_history(str(history))
        assert result.n_events == 0

    def test_no_matching_strategy_returns_empty(self, tmp_path: Path) -> None:
        history = tmp_path / "no_match.jsonl"
        history.write_text(
            json.dumps(
                {
                    "timestamp": "2024-06-15T10:30:00",
                    "symbol": "600519",
                    "direction": "buy",
                    "strategy": "other_strat",
                    "strategies": ["other_strat"],
                    "price": 150.0,
                }
            )
            + "\n"
        )
        config = StudyConfig(strategy_name="my_strat")
        study = EventStudy(config)
        result = study.run_from_history(str(history))
        assert result.n_events == 0

    def test_only_buy_signals_are_selected(self, tmp_path: Path) -> None:
        """只有买入信号被选中，卖出应被跳过。

        注：测试环境可能成功获取 K 线数据（腾讯 API），所以不假设网络不通。
        验证买入信号被处理、卖出信号被跳过即可。
        """
        history = tmp_path / "mixed.jsonl"
        data = [
            {
                "timestamp": "2024-06-15T10:30:00",
                "symbol": "600519",
                "direction": "buy",
                "strategy": "test",
                "strategies": ["test"],
                "price": 150.0,
            },
            {
                "timestamp": "2024-06-16T10:30:00",
                "symbol": "600519",
                "direction": "sell",
                "strategy": "test",
                "strategies": ["test"],
                "price": 155.0,
            },
        ]
        history.write_text("\n".join(json.dumps(d) for d in data) + "\n")
        dates = pd.bdate_range("2024-06-14", periods=20)
        close = np.arange(150.0, 170.0)
        bars = pd.DataFrame(
            {
                "date": dates,
                "open": close,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": [1000.0] * len(dates),
            }
        )
        config = StudyConfig(strategy_name="test", entry_mode="close")
        study = EventStudy(
            config,
            stock_loader=lambda symbol, days: bars,
            index_loader=lambda symbol, days: bars,
        )
        result = study.run_from_history(str(history), max_days=20)
        # 买入信号 1 条被提取，卖出被跳过；固定 loader 保证测试不访问外网
        assert result.n_events == 1
        assert [event.symbol for event in result.events] == ["600519"]
        assert result.n_symbols == 1


class TestResultDataClasses:
    """结果数据类验证。"""

    def test_horizon_result_defaults(self) -> None:
        r = HorizonResult(
            horizon=5,
            n_events=100,
            avg_return=0.02,
            avg_excess=0.015,
            std_excess=0.05,
            t_stat=3.0,
            p_value=0.003,
            win_rate=0.55,
            sharpe=0.4,
            excess_sharpe=0.3,
            significant=True,
        )
        assert r.horizon == 5
        assert r.significant is True

    def test_study_event_defaults(self) -> None:
        e = StudyEvent(symbol="600519", date="2024-01-01", entry=150.0, period="val", year=2024)
        assert e.returns == {}
        assert e.excess == {}

    def test_study_result_empty(self) -> None:
        config = StudyConfig(strategy_name="test")
        study = EventStudy(config)
        empty = study._empty_result()
        assert empty.n_events == 0
        assert empty.per_horizon == []

    def test_to_dict_serializable(self) -> None:
        config = StudyConfig(strategy_name="test")
        result = StudyResult(
            strategy_name="test",
            config=config,
            n_events=50,
            n_symbols=5,
            concentration_issues=[],
            per_horizon=[
                HorizonResult(
                    horizon=5,
                    n_events=50,
                    avg_return=0.02,
                    avg_excess=0.015,
                    std_excess=0.05,
                    t_stat=3.0,
                    p_value=0.003,
                    win_rate=0.55,
                    sharpe=0.4,
                    excess_sharpe=0.3,
                    significant=True,
                ),
            ],
            per_year={2024: []},
            per_period={"val": []},
            events=[],
        )
        d = EventStudy._to_dict(result)
        assert json.dumps(d)  # 可序列化
        assert d["strategy"] == "test"
        assert d["summary"]["n_events"] == 50


class TestAggregateByHorizon:
    """horizon 聚合统计。"""

    def test_simple_aggregation(self) -> None:
        config = StudyConfig(strategy_name="test", horizons=[5])
        study = EventStudy(config)
        events = [
            StudyEvent(
                symbol="600519",
                date="2024-01-01",
                entry=10.0,
                period="val",
                year=2024,
                returns={"5": 0.05},
                excess={"5": 0.03},
            ),
            StudyEvent(
                symbol="600519",
                date="2024-01-02",
                entry=10.0,
                period="val",
                year=2024,
                returns={"5": 0.03},
                excess={"5": 0.01},
            ),
            StudyEvent(
                symbol="600519",
                date="2024-01-03",
                entry=10.0,
                period="val",
                year=2024,
                returns={"5": -0.02},
                excess={"5": -0.04},
            ),
        ]
        results = study._aggregate_by_horizon(events)
        assert len(results) == 1
        r = results[0]
        assert r.horizon == 5
        assert r.n_events == 3

    def test_insufficient_samples_returns_empty(self) -> None:
        """少于 min_samples 应返回空列表。"""
        config = StudyConfig(strategy_name="test", horizons=[5], min_samples=5)
        study = EventStudy(config)
        events = [
            StudyEvent(
                symbol="600519",
                date="2024-01-01",
                entry=10.0,
                period="val",
                year=2024,
                returns={"5": 0.01},
                excess={"5": 0.005},
            ),
        ]
        results = study._aggregate_by_horizon(events)
        assert len(results) == 0


class TestDeterministicRun:
    """使用注入行情加载器验证事件口径，不访问外部网络。"""

    @staticmethod
    def _bars(start: str = "2024-01-01", count: int = 12) -> pd.DataFrame:
        dates = pd.bdate_range(start, periods=count)
        close = np.arange(10.0, 10.0 + count)
        return pd.DataFrame(
            {
                "date": dates,
                "open": close + 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": [1000.0] * count,
            }
        )

    def test_next_open_horizon_is_from_actual_entry(self) -> None:
        bars = self._bars()
        study = EventStudy(
            StudyConfig(strategy_name="test", horizons=[1], cost_rate=0.0),
            stock_loader=lambda symbol, days: bars,
            index_loader=lambda symbol, days: bars,
        )
        result = study.run(
            pd.DataFrame([{"symbol": "sh600519", "date": bars.iloc[0]["date"], "entry": 10.0}]),
            data_days=12,
        )
        assert result.n_events == 1
        event = result.events[0]
        assert event.entry_source == "next_open"
        assert event.entry_date == bars.iloc[1]["date"].strftime("%Y-%m-%d")
        # 入场为第 2 根 open=11.5，h=1 退出第 3 根 close=12。
        assert event.returns["1"] == round(12.0 / 11.5 - 1, 4)

    def test_non_trading_signal_uses_next_trading_day(self) -> None:
        bars = self._bars()
        study = EventStudy(
            StudyConfig(strategy_name="test", horizons=[1], entry_mode="close", cost_rate=0.0),
            stock_loader=lambda symbol, days: bars,
            index_loader=lambda symbol, days: bars,
        )
        result = study.run(
            pd.DataFrame([{"symbol": "600519", "date": "2024-01-06", "entry": 99.0}]),
            data_days=12,
        )
        assert result.n_events == 1
        assert result.events[0].date == "2024-01-06"
        assert result.events[0].entry_date == bars.iloc[5]["date"].strftime("%Y-%m-%d")

    def test_missing_benchmark_keeps_absolute_returns(self) -> None:
        bars = self._bars()
        study = EventStudy(
            StudyConfig(strategy_name="test", horizons=[1], entry_mode="close", cost_rate=0.0),
            stock_loader=lambda symbol, days: bars,
            index_loader=lambda symbol, days: pd.DataFrame(),
        )
        result = study.run(
            pd.DataFrame(
                [
                    {"symbol": "600519", "date": bars.iloc[0]["date"], "entry": 10.0},
                    {"symbol": "600519", "date": bars.iloc[1]["date"], "entry": 11.0},
                    {"symbol": "600519", "date": bars.iloc[2]["date"], "entry": 12.0},
                ]
            ),
            data_days=12,
        )
        assert result.n_events == 3
        assert result.benchmark_available is False
        assert result.events[0].returns["1"] == 0.1
        assert result.per_horizon[0].n_returns == 3
        assert result.per_horizon[0].n_excess == 0

    def test_concentration_threshold_comes_from_config(self) -> None:
        events = [
            StudyEvent(symbol="600001", date="2024-01-01", entry=10.0, period="val", year=2024),
            StudyEvent(symbol="600001", date="2024-01-02", entry=10.0, period="val", year=2024),
            StudyEvent(symbol="600002", date="2024-01-03", entry=10.0, period="val", year=2024),
        ]
        study = EventStudy(StudyConfig(strategy_name="test", concentration_threshold=0.8))
        assert (
            study._check_concentration(events, threshold=study.config.concentration_threshold) == []
        )

    def test_loader_receives_normalized_symbol_and_requested_days(self) -> None:
        bars = self._bars()
        calls: list[tuple[str, int]] = []

        def loader(symbol: str, days: int) -> pd.DataFrame:
            calls.append((symbol, days))
            return bars

        study = EventStudy(
            StudyConfig(strategy_name="test", horizons=[1], entry_mode="close", cost_rate=0.0),
            stock_loader=loader,
            index_loader=lambda symbol, days: bars,
        )
        study.run(
            pd.DataFrame([{"symbol": "sh600519", "date": bars.iloc[0]["date"], "entry": 10.0}]),
            data_days=12,
        )
        assert ("600519", 12) in calls


class TestPrintSummary:
    """打印摘要不抛异常。"""

    def test_print_empty_result(self, capsys: pytest.CaptureFixture) -> None:
        config = StudyConfig(strategy_name="test")
        study = EventStudy(config)
        empty = study._empty_result()
        study.print_summary(empty)
        captured = capsys.readouterr()
        assert "0" in captured.out

    def test_print_normal_result(self, capsys: pytest.CaptureFixture) -> None:
        config = StudyConfig(strategy_name="test")
        study = EventStudy(config)
        result = StudyResult(
            strategy_name="test",
            config=config,
            n_events=50,
            n_symbols=5,
            concentration_issues=[{"symbol": "600519", "count": 30, "ratio": 0.6, "action": "..."}],
            per_horizon=[
                HorizonResult(
                    horizon=5,
                    n_events=50,
                    avg_return=0.02,
                    avg_excess=0.015,
                    std_excess=0.05,
                    t_stat=3.0,
                    p_value=0.003,
                    win_rate=0.55,
                    sharpe=0.4,
                    excess_sharpe=0.3,
                    significant=True,
                ),
            ],
            per_year={2024: []},
            per_period={"val": []},
            events=[],
        )
        study.print_summary(result)
        captured = capsys.readouterr()
        assert "50" in captured.out
        assert "600519" in captured.out
