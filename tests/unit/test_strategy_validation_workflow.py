"""策略验证工作流测试。

覆盖策略准入、启用策略读取、参数调优网格等实盘前验证关键路径。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from gugu.config import get_config
from gugu.strategies.registry import get_runtime_strategy_names

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_script(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, _PROJECT_ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEnabledStrategySources:
    """所有验证脚本必须读取真实配置，而不是硬编码 fallback。"""

    def test_backtest_all_reads_current_enabled_strategies(self):
        script = _load_script("backtest_all_strategies", "scripts/backtest_all_strategies.py")
        expected = get_runtime_strategy_names()
        assert script.get_enabled_strategies() == expected

    def test_backtest_offline_reads_current_runtime_strategies(self):
        script = _load_script("backtest_offline", "scripts/backtest_offline.py")
        expected = get_runtime_strategy_names()
        assert script.get_enabled_strategies() == expected

    def test_verify_enabled_reads_current_enabled_strategies(self):
        script = _load_script("verify_enabled_strategies", "scripts/verify_enabled_strategies.py")
        expected = get_runtime_strategy_names()
        assert script.get_enabled_strategies() == expected

    def test_regime_backtest_default_uses_current_enabled_strategies(self):
        script = _load_script("backtest_by_regime", "scripts/backtest_by_regime.py")
        expected = get_runtime_strategy_names()
        assert script.resolve_strategy_names(None) == expected


class TestDoubleYinOptimization:
    """当前启用的 double_yin 必须能进入 Walk-Forward 参数调优。"""

    def test_double_yin_has_param_grid_and_constraints(self):
        script = _load_script("optimize_params", "scripts/optimize_params.py")
        grid = script.build_param_grid("double_yin", {})
        assert grid
        assert "ma_slope_window" in grid
        assert "breakout_vol_ratio_min" in grid
        assert "take_profit_pct" in grid
        assert "double_yin" in script.STRATEGY_PARAM_CONSTRAINTS


class TestStrategyManifestValidation:
    """启用策略必须有结构化市场验证记录。"""

    def test_enabled_strategies_have_structured_validation_gate(self):
        manifest_path = _PROJECT_ROOT / "config/strategy_manifest.yaml"
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        for strategy, meta in data["signal_strategies"].items():
            if not meta.get("enabled"):
                continue
            gate = meta.get("validation_gate")
            assert gate, f"{strategy} 缺少 validation_gate"
            assert "status" in gate
            assert "sample" in gate
            assert "metrics" in gate
            assert "next_action" in gate


class TestStrategyGateScript:
    """策略准入脚本必须能用结构化门槛判断是否可启用。"""

    def test_gate_passes_when_all_thresholds_met(self):
        script = _load_script("validate_strategy_gate", "scripts/validate_strategy_gate.py")
        report = script.evaluate_gate(
            strategy="box_breakout",
            metrics={
                "total_trades": 120,
                "avg_return": 0.03,
                "median_return": 0.01,
                "avg_sharpe": 0.35,
                "max_drawdown": 0.12,
                "profitable_ratio": 0.56,
                "profit_factor": 2.0,
                "param_stability": 0.8,
                "paper_signals": 35,
                "paper_return": 0.02,
                # 2026-11-05 起门禁还要求选股alpha > 0：仅超额达标不足以证明
                # 策略在选股，池子同期涨幅也能造出超额。
                "selection_alpha": 0.0008,
            },
        )
        assert report["passed"] is True
        assert report["failed_checks"] == []

    def test_gate_rejects_weak_sample_and_negative_out_of_sample(self):
        script = _load_script("validate_strategy_gate", "scripts/validate_strategy_gate.py")
        report = script.evaluate_gate(
            strategy="double_yin",
            metrics={
                "total_trades": 20,
                "avg_return": -0.01,
                "median_return": -0.02,
                "avg_sharpe": -0.1,
                "max_drawdown": 0.3,
                "profitable_ratio": 0.4,
                "param_stability": 0.2,
                "paper_signals": 5,
                "paper_return": -0.01,
            },
        )
        assert report["passed"] is False
        assert "total_trades" in report["failed_checks"]
        assert "avg_return" in report["failed_checks"]
        assert "paper_return" in report["failed_checks"]
