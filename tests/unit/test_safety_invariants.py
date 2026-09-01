"""Critical safety invariants for production-like risk paths."""

from __future__ import annotations

from pathlib import Path

from gugu.risk.manager import RiskManager
from gugu.risk.rules import RiskAction


def _risk_config(**overrides: float | int | bool) -> dict:
    config: dict[str, float | int | bool] = {
        "max_position_ratio": 0.30,
        "daily_loss_warn": 0.03,
        "daily_loss_halt": 0.05,
        "max_drawdown_halt": 0.15,
        "max_total_positions": 5,
        "t_plus_1": True,
    }
    config.update(overrides)
    return config


def test_constructor_cannot_bypass_safety_caps() -> None:
    manager = RiskManager(
        _risk_config(
            max_position_ratio=0.99,
            daily_loss_warn=0.50,
            daily_loss_halt=0.50,
            max_drawdown_halt=0.99,
        )
    )
    assert manager.max_position_ratio == 0.50
    assert manager.daily_loss_warn == 0.05
    assert manager.daily_loss_halt == 0.10
    assert manager._max_drawdown_halt == 0.30


def test_halt_state_restores_after_new_manager(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "risk_state.json"
    monkeypatch.setattr("gugu.risk.manager.RISK_STATE_FILE", state_path)
    config = _risk_config()

    first = RiskManager(config)
    result = first.check_daily_loss(0.05)
    assert result.action == RiskAction.HALT
    assert first.is_halted is True
    assert state_path.exists()

    second = RiskManager(config)
    assert second.is_halted is True


def test_reload_clears_memory_halt_when_state_file_removed(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "risk_state.json"
    monkeypatch.setattr("gugu.risk.manager.RISK_STATE_FILE", state_path)
    manager = RiskManager(_risk_config())
    manager.check_daily_loss(0.05)
    assert manager.is_halted is True
    state_path.unlink()
    manager.reload()
    assert manager.is_halted is False


def test_backtest_scope_does_not_share_production_halt(tmp_path: Path, monkeypatch) -> None:
    production_path = tmp_path / "production.json"
    backtest_path = tmp_path / "backtest.json"
    monkeypatch.setattr("gugu.risk.manager.RISK_STATE_FILE", production_path)
    monkeypatch.setattr("gugu.risk.manager.RiskManager._BACKTEST_STATE_FILE", backtest_path)
    config = _risk_config()

    production = RiskManager(config, scope="production")
    production.check_daily_loss(0.05)
    assert production.is_halted is True

    backtest = RiskManager(config, scope="backtest")
    assert backtest.is_halted is False
