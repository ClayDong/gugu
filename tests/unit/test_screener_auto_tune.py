"""尾盘选股参数自动调优单元测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from gugu.screener.auto_tune import (
    TuneMetrics,
    auto_tune_screener_params,
    compute_metrics,
    generate_adjustments,
    load_recent_validation,
)
from gugu.screener.params import ScreenerParams


def _make_record(
    open_premium: float,
    benchmark_open_change: float = 0.0,
    stop_loss_hit: bool = False,
    intraday_max_drawdown: float = -1.0,
    validation_date: str | None = None,
) -> dict:
    if validation_date is None:
        validation_date = datetime.now().date().isoformat()
    return {
        "symbol": "600519",
        "name": "茅台",
        "open_premium": open_premium,
        "benchmark_open_change": benchmark_open_change,
        "stop_loss_hit": stop_loss_hit,
        "intraday_max_drawdown": intraday_max_drawdown,
        "validation_date": validation_date,
    }


class TestComputeMetrics:
    """验证指标计算。"""

    def test_empty_records(self):
        metrics = compute_metrics([])
        assert metrics.avg_open_excess == 0.0
        assert metrics.win_rate == 0.0
        assert metrics.stop_loss_rate == 0.0
        assert metrics.sample_size == 0

    def test_basic_metrics(self):
        records = [
            _make_record(open_premium=1.0, benchmark_open_change=0.2),
            _make_record(open_premium=-0.5, benchmark_open_change=0.2),
            _make_record(open_premium=0.3, benchmark_open_change=-0.1),
        ]
        metrics = compute_metrics(records)
        # 超额收益: 0.8, -0.7, 0.4 -> avg = 0.1667
        assert metrics.avg_open_excess == pytest.approx(0.17, abs=0.01)
        # 胜率: 2/3
        assert metrics.win_rate == pytest.approx(0.67, abs=0.01)
        assert metrics.sample_size == 3

    def test_stop_loss_rate(self):
        records = [
            _make_record(open_premium=1.0, stop_loss_hit=True),
            _make_record(open_premium=-0.5, stop_loss_hit=True),
            _make_record(open_premium=0.3, stop_loss_hit=False),
        ]
        metrics = compute_metrics(records)
        assert metrics.stop_loss_rate == pytest.approx(0.67, abs=0.01)


class TestGenerateAdjustments:
    """调优决策逻辑。"""

    def test_tighten_when_poor_performance(self):
        params = ScreenerParams()
        metrics = TuneMetrics(
            avg_open_excess=-1.0,
            win_rate=0.20,
            stop_loss_rate=0.30,
            avg_drawdown=-2.5,
            sample_size=10,
        )
        adj = generate_adjustments(metrics, params)
        assert adj["max_vol_ratio"] < 0
        assert adj["min_turnover"] > 0
        assert adj["min_chg_pct"] < 0

    def test_tighten_when_high_stop_loss_rate(self):
        params = ScreenerParams()
        metrics = TuneMetrics(
            avg_open_excess=0.1,
            win_rate=0.35,
            stop_loss_rate=0.50,
            avg_drawdown=-1.5,
            sample_size=10,
        )
        adj = generate_adjustments(metrics, params)
        assert adj["max_vol_ratio"] < 0

    def test_loosen_when_good_performance(self):
        params = ScreenerParams()
        metrics = TuneMetrics(
            avg_open_excess=0.50,
            win_rate=0.50,
            stop_loss_rate=0.20,
            avg_drawdown=-1.0,
            sample_size=10,
        )
        adj = generate_adjustments(metrics, params)
        assert adj["max_vol_ratio"] > 0
        assert adj["min_turnover"] < 0
        assert adj["min_chg_pct"] > 0

    def test_no_adjustment_in_neutral_zone(self):
        params = ScreenerParams()
        metrics = TuneMetrics(
            avg_open_excess=0.0,
            win_rate=0.35,
            stop_loss_rate=0.30,
            avg_drawdown=-1.5,
            sample_size=10,
        )
        adj = generate_adjustments(metrics, params)
        assert adj == {}

    def test_no_loosen_if_drawdown_too_deep(self):
        params = ScreenerParams()
        metrics = TuneMetrics(
            avg_open_excess=0.50,
            win_rate=0.50,
            stop_loss_rate=0.20,
            avg_drawdown=-3.0,
            sample_size=10,
        )
        adj = generate_adjustments(metrics, params)
        assert adj == {}


class TestLoadRecentValidation:
    """加载最近验证历史。"""

    def test_loads_recent_records(self, tmp_path):
        validation_file = tmp_path / "screener_validation.jsonl"
        today = datetime.now().date()
        records = [
            _make_record(1.0, validation_date=(today - timedelta(days=i)).isoformat())
            for i in range(12)
        ]
        validation_file.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
            encoding="utf-8",
        )

        with patch("gugu.screener.auto_tune._VALIDATION_FILE", validation_file):
            loaded = load_recent_validation(days=10)
            assert len(loaded) == 10

    def test_deduplicates_by_validation_date(self, tmp_path):
        validation_file = tmp_path / "screener_validation.jsonl"
        today = datetime.now().date().isoformat()
        records = [
            _make_record(1.0, validation_date=today),
            _make_record(-0.5, validation_date=today),
            _make_record(0.3, validation_date=today),
        ]
        validation_file.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
            encoding="utf-8",
        )

        with patch("gugu.screener.auto_tune._VALIDATION_FILE", validation_file):
            loaded = load_recent_validation(days=10)
            # 同一交易日保留全部记录（一天可能选多只股票）
            assert len(loaded) == 3

    def test_returns_empty_when_file_missing(self, tmp_path):
        validation_file = tmp_path / "not_exists.jsonl"
        with patch("gugu.screener.auto_tune._VALIDATION_FILE", validation_file):
            assert load_recent_validation(days=10) == []

    def test_skips_old_records(self, tmp_path):
        validation_file = tmp_path / "screener_validation.jsonl"
        today = datetime.now().date()
        records = [
            _make_record(1.0, validation_date=(today - timedelta(days=40)).isoformat()),
            _make_record(0.5, validation_date=today.isoformat()),
        ]
        validation_file.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
            encoding="utf-8",
        )

        with patch("gugu.screener.auto_tune._VALIDATION_FILE", validation_file):
            loaded = load_recent_validation(days=10)
            assert len(loaded) == 1
            assert loaded[0]["open_premium"] == 0.5


class TestAutoTuneScreenerParams:
    """端到端自动调优入口。"""

    def test_disabled_auto_tune_returns_original(self, tmp_path):
        params_file = tmp_path / "screener_params.json"
        params = ScreenerParams(auto_tune_enabled=False)
        params.save(params_file)

        validation_file = tmp_path / "screener_validation.jsonl"
        validation_file.write_text("", encoding="utf-8")

        with (
            patch("gugu.screener.params._PARAMS_FILE", params_file),
            patch("gugu.screener.auto_tune._VALIDATION_FILE", validation_file),
        ):
            result = auto_tune_screener_params()
            assert result.auto_tune_enabled is False
            assert result.max_vol_ratio == params.max_vol_ratio

    def test_skips_when_sample_insufficient(self, tmp_path):
        params_file = tmp_path / "screener_params.json"
        params = ScreenerParams(auto_tune_enabled=True)
        params.save(params_file)

        validation_file = tmp_path / "screener_validation.jsonl"
        records = [_make_record(1.0)] * 3
        validation_file.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
            encoding="utf-8",
        )

        with (
            patch("gugu.screener.params._PARAMS_FILE", params_file),
            patch("gugu.screener.auto_tune._VALIDATION_FILE", validation_file),
        ):
            result = auto_tune_screener_params()
            assert result.max_vol_ratio == params.max_vol_ratio

    def test_applies_tighten_adjustment(self, tmp_path):
        params_file = tmp_path / "screener_params.json"
        params = ScreenerParams(
            auto_tune_enabled=True,
            max_vol_ratio=1.0,
            min_turnover=3.0,
            min_chg_pct=-2.0,
        )
        params.save(params_file)

        validation_file = tmp_path / "screener_validation.jsonl"
        today = datetime.now().date().isoformat()
        records = [_make_record(-1.0, validation_date=today, stop_loss_hit=True) for _ in range(10)]
        validation_file.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
            encoding="utf-8",
        )

        with (
            patch("gugu.screener.params._PARAMS_FILE", params_file),
            patch("gugu.screener.auto_tune._VALIDATION_FILE", validation_file),
        ):
            result = auto_tune_screener_params(days=10)
            assert result.max_vol_ratio < 1.0
            assert result.min_turnover > 3.0
            assert result.min_chg_pct < -2.0

    def test_saves_adjusted_params(self, tmp_path):
        params_file = tmp_path / "screener_params.json"
        params = ScreenerParams(auto_tune_enabled=True, max_vol_ratio=1.0)
        params.save(params_file)

        validation_file = tmp_path / "screener_validation.jsonl"
        today = datetime.now().date().isoformat()
        records = [_make_record(0.5, validation_date=today) for _ in range(10)]
        validation_file.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
            encoding="utf-8",
        )

        with (
            patch("gugu.screener.params._PARAMS_FILE", params_file),
            patch("gugu.screener.auto_tune._VALIDATION_FILE", validation_file),
        ):
            auto_tune_screener_params(days=10)

        loaded = ScreenerParams.load(params_file)
        assert loaded.max_vol_ratio <= 1.05  # 放宽或不变
