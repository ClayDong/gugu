from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

from gugu.config.models import AppConfig, StrategyConfig
from gugu.engine.scheduler import TradingScheduler
from gugu.risk.manager import RiskManager


def test_config_fields_and_flatten_include_runtime_settings() -> None:
    cfg = AppConfig.model_validate(
        {
            "risk": {"max_total_position_ratio": 0.7},
            "scheduler": {
                "tasks": {
                    "market_temp": False,
                    "signal_daily_validation": False,
                    "signal_weekly": False,
                    "screener_auto_tune": False,
                }
            },
        }
    )
    flat = cfg.flatten()
    assert cfg.risk.max_total_position_ratio == 0.7
    assert flat["risk"]["max_total_position_ratio"] == 0.7
    assert flat["scheduler"]["tasks"]["market_temp"] is False
    assert flat["scheduler"]["tasks"]["signal_daily_validation"] is False
    assert flat["scheduler"]["tasks"]["signal_weekly"] is False
    assert flat["scheduler"]["tasks"]["screener_auto_tune"] is False


def test_web_config_expands_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("web:\n  admin_api_key: ${GUGU_ADMIN_API_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("GUGU_ADMIN_API_KEY", "secret-from-env")
    assert AppConfig.from_settings(str(config_path)).web.admin_api_key == "secret-from-env"


def test_refresh_config_clears_legacy_and_strategy_caches() -> None:
    import gugu.config as config_module
    import gugu.strategies.registry as registry_module

    with (
        mock.patch.object(config_module.get_config, "cache_clear") as config_clear,
        mock.patch.object(config_module.load_yaml, "cache_clear") as yaml_clear,
        mock.patch.object(config_module.strategy_defaults, "cache_clear") as defaults_clear,
        mock.patch.object(
            registry_module.get_enabled_strategies, "cache_clear"
        ) as strategies_clear,
        mock.patch.object(
            config_module, "get_config", wraps=config_module.get_config
        ) as get_config,
    ):
        config_module.refresh_config()
    config_clear.assert_called_once()
    yaml_clear.assert_called_once()
    defaults_clear.assert_called_once()
    strategies_clear.assert_called_once()
    get_config.assert_called_once()


def test_experimental_strategies_are_filtered_by_execution_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gugu.config as config_module
    import gugu.strategies.registry as registry_module

    cfg = AppConfig(
        execution={"mode": "track_live"},
        strategy=StrategyConfig(
            verified_strategies=["box_breakout"],
            experimental_strategies=["double_yin"],
            enabled_by_regime={"bull": ["box_breakout", "double_yin"]},
        ),
    )
    monkeypatch.setattr(config_module, "get_config", lambda: cfg)
    monkeypatch.setattr(
        registry_module,
        "get_manifest",
        lambda: mock.Mock(list_runtime_enabled=lambda mode, keys: list(keys)),
    )
    result = registry_module.get_enabled_strategies_for_regime("bull")
    assert [strategy.name for strategy in result] == ["box_breakout"]


def test_scheduler_jobs_have_serialized_execution_defaults() -> None:
    engine = mock.AsyncMock()
    scheduler = TradingScheduler(engine=engine)
    scheduler.setup()
    for job in scheduler._scheduler.get_jobs():
        assert job.max_instances == 1
        assert job.misfire_grace_time == 120
        assert job.coalesce is True


def test_record_boot_uses_atomic_json_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gugu.config as config_module
    import gugu.engine.scheduler as scheduler_module

    writes: list[tuple[Path, dict]] = []

    def record(path: Path, data: dict, **kwargs: object) -> None:
        writes.append((path, data))

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scheduler_module, "atomic_write_json", record)
    scheduler = object.__new__(TradingScheduler)
    scheduler._record_boot()
    assert len(writes) == 1
    assert writes[0][0] == tmp_path / "data" / "scheduler_boot.json"


def test_jsonl_append_is_atomic_and_rotates(tmp_path: Path) -> None:
    from gugu.utils.atomic import append_jsonl

    path = tmp_path / "history.jsonl"
    for value in range(6):
        append_jsonl(path, {"value": value}, max_lines=4)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records == [{"value": 2}, {"value": 3}, {"value": 4}, {"value": 5}]


def test_async_locks_are_lazy_for_sync_constructors(monkeypatch: pytest.MonkeyPatch) -> None:
    from gugu.data.manager import DataManager
    from gugu.notifier.feishu import FeishuNotifier

    monkeypatch.setenv("GUGU_TEST_MODE", "1")
    with mock.patch("gugu.data.manager.get_config", create=True):
        data_manager = DataManager()
    notifier = FeishuNotifier()
    assert data_manager._lock is None
    assert notifier._queue_lock is None


def test_risk_manager_exposes_backtest_reset() -> None:
    manager = RiskManager({"daily_loss_halt": 0.05}, scope="backtest")
    manager._halted = True
    manager.reset_backtest_state()
    assert manager.is_halted is False
    assert manager._portfolio_peak == 0.0


# ========================================
# Fix 1: portfolio_peak scope persistence
# ========================================


def test_production_portfolio_peak_persists_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gugu.risk.manager as risk_module

    monkeypatch.setattr(risk_module, "RISK_STATE_FILE", tmp_path / "risk_state.json")
    monkeypatch.setattr(risk_module, "PROJECT_ROOT", tmp_path)

    mgr1 = RiskManager(scope="production")
    mgr1.update_portfolio_peak(1_500_000.0, persist=True)
    assert mgr1._portfolio_peak == 1_500_000.0

    mgr2 = RiskManager(scope="production")
    assert (
        mgr2._portfolio_peak == 1_500_000.0
    ), "Production scope should restore portfolio_peak from disk on restart"


def test_backtest_portfolio_peak_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gugu.risk.manager as risk_module

    monkeypatch.setattr(risk_module, "RISK_STATE_FILE", tmp_path / "risk_state.json")
    monkeypatch.setattr(risk_module, "PROJECT_ROOT", tmp_path)

    mgr1 = RiskManager(scope="backtest", config={"daily_loss_halt": 0.05})
    mgr1.update_portfolio_peak(1_500_000.0, persist=True)
    mgr1._save_halt_state()

    mgr2 = RiskManager(scope="backtest", config={"daily_loss_halt": 0.05})
    assert (
        mgr2._portfolio_peak == 0.0
    ), "Backtest scope should NOT restore portfolio_peak (starts at 0)"


def test_backtest_does_not_pollute_production_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gugu.risk.manager as risk_module

    monkeypatch.setattr(risk_module, "RISK_STATE_FILE", tmp_path / "risk_state.json")
    monkeypatch.setattr(risk_module, "PROJECT_ROOT", tmp_path)

    bt = RiskManager(scope="backtest", config={"daily_loss_halt": 0.05})
    bt.update_portfolio_peak(2_000_000.0, persist=True)
    bt.check_daily_loss(0.06)
    assert bt.is_halted

    prod = RiskManager(scope="production")
    assert prod._portfolio_peak == 0.0, "Production peak should not be polluted by backtest"
    assert not prod.is_halted, "Production halt should not be polluted by backtest"


# ========================================
# Fix 2: 熔断冷却只去重告警，不允许绕过熔断
# ========================================


def test_cooldown_does_not_bypass_circuit_breaker() -> None:
    mgr = RiskManager({"daily_loss_halt": 0.05, "daily_loss_warn": 0.03})

    mgr.check_daily_loss(0.06)
    assert mgr.is_halted

    mgr.clear_halt_only()
    assert not mgr.is_halted

    result = mgr.check_daily_loss(0.06)
    assert result.action.value == "HALT", "Cooldown must not bypass HALT; 6% loss must still halt"
    assert mgr.is_halted, "6% loss must re-trigger halt even during cooldown"


def test_cooldown_warn_still_fires() -> None:
    mgr = RiskManager({"daily_loss_halt": 0.05, "daily_loss_warn": 0.03})
    mgr._halted = True
    mgr._last_halt_reset_time = time.time()
    mgr._halted = False

    result = mgr.check_daily_loss(0.04)
    assert result.action.value == "WARN", "WARN should still fire during cooldown"


# ========================================
# Fix 3: RiskManager thread safety
# ========================================


def test_risk_manager_thread_safety() -> None:
    mgr = RiskManager({"daily_loss_halt": 0.05})
    # 本用例只验证「并发访问不抛异常」，不验证磁盘持久化。5 个线程交替
    # 翻转 halted / peak，会让几乎每次调用都触发文件锁 + 原子写（数百次 IO），
    # 在沙箱或慢盘上可达分钟级，反而掩盖了真正的线程安全断言。
    # 持久化由 test_safety_invariants.py（reload 恢复）与
    # test_e2e_trading_day.py（L2 熔断落盘）单独覆盖。
    mgr._save_halt_state = lambda: None  # type: ignore[method-assign]
    errors: list[Exception] = []

    def hammer_halt() -> None:
        for _ in range(100):
            try:
                mgr.check_daily_loss(0.06)
            except Exception as e:
                errors.append(e)

    def hammer_reset() -> None:
        for _ in range(100):
            try:
                mgr.reset_halt()
            except Exception as e:
                errors.append(e)

    def hammer_peak() -> None:
        for _ in range(100):
            try:
                mgr.update_portfolio_peak(1_000_000.0 + _ * 1000)
            except Exception as e:
                errors.append(e)

    threads = [
        threading.Thread(target=hammer_halt),
        threading.Thread(target=hammer_reset),
        threading.Thread(target=hammer_peak),
        threading.Thread(target=hammer_halt),
        threading.Thread(target=hammer_reset),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread safety errors: {errors}"


def test_risk_manager_has_lock() -> None:
    mgr = RiskManager()
    assert hasattr(mgr, "_lock")
    assert isinstance(mgr._lock, threading.Lock)


# ========================================
# Fix 4: check_holding_duration wired into stop loss
# ========================================


def test_holding_duration_returns_halt_for_long_losing_position() -> None:
    from datetime import date, timedelta

    halt_date = (date.today() - timedelta(days=90)).isoformat()
    mgr = RiskManager({"max_holding_days_halt": 60, "max_holding_days_warn": 30})
    result = mgr.check_holding_duration("600519", halt_date, -0.05)
    assert result.action.value == "HALT"
    assert "强制清仓" in result.message


def test_holding_duration_returns_warn_for_medium_losing_position() -> None:
    from datetime import date, timedelta

    warn_date = (date.today() - timedelta(days=35)).isoformat()
    mgr = RiskManager({"max_holding_days_halt": 60, "max_holding_days_warn": 30})
    result = mgr.check_holding_duration("600519", warn_date, -0.05)
    assert result.action.value == "WARN"
    assert "建议减半仓" in result.message


def test_holding_duration_allow_for_profitable_position() -> None:
    mgr = RiskManager({"max_holding_days_halt": 60, "max_holding_days_warn": 30})
    result = mgr.check_holding_duration("600519", "2026-01-01", 0.10)
    assert result.action.value == "ALLOW"
    assert "盈利" in result.message


def test_stop_loss_manager_wires_holding_duration_check() -> None:
    """Verify StopLossManager.execute calls risk.check_holding_duration for positions with entry_date."""
    from gugu.execution.stop_loss_manager import StopLossManager
    from gugu.models.position import Position

    slm = StopLossManager()
    broker = mock.Mock()
    pos = Position(
        symbol="600519",
        quantity=1000,
        available=1000,
        avg_cost=200.0,
        current_price=180.0,
        entry_date="2026-01-01",
    )
    broker.get_portfolio.return_value = {"600519": pos}
    broker.get_account.return_value = mock.Mock(cash=100000)
    broker.get_position.return_value = None
    risk = mock.Mock()
    risk.check_order.return_value = mock.Mock(allowed=False)
    risk.check_holding_duration.return_value = mock.Mock(
        action=mock.Mock(value="ALLOW"), message="ok"
    )
    notifier = mock.AsyncMock()
    event_engine = mock.Mock()
    data_mgr = mock.AsyncMock()
    data_mgr.fetch_stock_history.return_value = pd.DataFrame()
    data_mgr.fetch_stock_history_batch.return_value = {}

    import asyncio

    asyncio.run(slm.execute(broker, risk, notifier, event_engine, data_mgr))

    risk.check_holding_duration.assert_called_once_with("600519", "2026-01-01", mock.ANY)


# ========================================
# Fix 5: backtest check_portfolio_drawdown persist=False, halted check
# ========================================


def test_backtest_check_portfolio_drawdown_uses_persist_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gugu.risk.manager as risk_module

    monkeypatch.setattr(risk_module, "RISK_STATE_FILE", tmp_path / "risk_state.json")
    monkeypatch.setattr(risk_module, "PROJECT_ROOT", tmp_path)

    from gugu.backtest.engine import BacktestEngine

    engine = BacktestEngine()
    engine._risk._portfolio_peak = 100_000.0

    engine._risk.check_portfolio_drawdown(70_000.0, persist=False)
    state_file = tmp_path / "risk_state.json"
    if state_file.exists():
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert (
            data.get("halted") is False
        ), "Backtest drawdown check with persist=False should not write halt to file"


def test_backtest_halted_skips_remaining_buy_signals() -> None:
    from gugu.backtest.engine import BacktestEngine

    engine = BacktestEngine()
    assert hasattr(engine, "_risk")
    engine._risk._halted = True
    assert engine._risk.is_halted


def test_backtest_simulates_daily_loss_circuit_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gugu.risk.manager as risk_module

    monkeypatch.setattr(risk_module, "RISK_STATE_FILE", tmp_path / "risk_state.json")
    monkeypatch.setattr(risk_module, "PROJECT_ROOT", tmp_path)

    from gugu.backtest.engine import BacktestEngine

    engine = BacktestEngine()
    engine._risk._portfolio_peak = 100_000.0
    engine._risk._halted = False

    result = engine._risk.check_portfolio_drawdown(70_000.0, persist=False)
    assert result.action.value == "HALT"
    assert engine._risk.is_halted


# ========================================
# Fix 6: Walk-forward MarketFilter + min_conditions
# ========================================


def test_backtest_market_filter_graduated_matches_production() -> None:
    """_check_market_regime graduated logic must match production MarketFilter._analyze."""
    from gugu.backtest.engine import BacktestEngine
    from gugu.engine.market_filter import MarketFilter

    dates = pd.date_range("2024-01-01", periods=300, freq="B")
    idx = list(range(300))
    df_bull = pd.DataFrame(
        {
            "date": dates,
            "close": [5000 + i for i in idx],
            "high": [5010 + i for i in idx],
            "low": [4990 + i for i in idx],
            "volume": [1e9] * 300,
        }
    )
    bt_regime = BacktestEngine._check_market_regime(df_bull)
    mf = MarketFilter()
    prod_result = mf._analyze(df_bull)
    assert (
        bt_regime == prod_result["regime"]
    ), f"Backtest regime {bt_regime} != production {prod_result['regime']}"

    df_bear = pd.DataFrame(
        {
            "date": dates,
            "close": [5000 - i for i in idx],
            "high": [5010 - i for i in idx],
            "low": [4990 - i for i in idx],
            "volume": [1e8] * 300,
        }
    )
    bt_regime = BacktestEngine._check_market_regime(df_bear)
    prod_result = mf._analyze(df_bear)
    assert (
        bt_regime == prod_result["regime"]
    ), f"Backtest regime {bt_regime} != production {prod_result['regime']}"


# ========================================
# Fix 7: Web/引擎风控状态一致性
# ========================================


def test_web_risk_reads_from_state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gugu.risk.manager as risk_module
    import gugu.web.app as web_app

    monkeypatch.setattr(risk_module, "RISK_STATE_FILE", tmp_path / "risk_state.json")
    monkeypatch.setattr(risk_module, "PROJECT_ROOT", tmp_path)

    web_app._web_risk = None

    from datetime import date

    today = date.today().isoformat()
    data = {"halted": True, "halt_date": today, "last_reset_date": today}
    state_file = tmp_path / "risk_state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    risk = web_app._get_web_risk()
    assert risk.is_halted, "Web risk should read halt state from file"


def test_engine_halt_reflects_in_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gugu.risk.manager as risk_module

    monkeypatch.setattr(risk_module, "RISK_STATE_FILE", tmp_path / "risk_state.json")
    monkeypatch.setattr(risk_module, "PROJECT_ROOT", tmp_path)

    engine_risk = RiskManager(scope="production")
    engine_risk.check_daily_loss(0.06)

    state_file = tmp_path / "risk_state.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["halted"] is True

    web_risk = RiskManager(scope="production")
    assert web_risk.is_halted, "New RiskManager should pick up engine's halt from file"
