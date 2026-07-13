# Watchlist Signal + Paper Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Monitor 恒瑞医药、豪威集团、双环传动、机器人ETF华夏 as configurable watchlist symbols, send Feishu buy/sell alerts, and optionally record paper trades without enabling live trading.

**Architecture:** Use `config/settings.yaml` as the single source of configurable watchlist symbols. Keep runtime in `paper` mode for simulation while preventing QMT/live execution. Harden Web `/api/order` so orders go through `RiskManager.check_order()` before `PaperBroker.order()` and return explicit rejection reasons.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, pytest, ruff, mypy, uv.

## Global Constraints

- Default monitored symbols: `600276`, `603501`, `002472`, `562500`.
- Default execution mode: `paper` for simulation; never use QMT/live in this change.
- Feishu remains the notification channel; missing credentials must not crash local tests.
- Web order API must use unified risk checking before paper order execution.
- CI must run with declared dependencies only.

---

### Task 1: Configure Watchlist and Paper Mode

**Files:**
- Modify: `config/settings.yaml`
- Modify: `.env.example`
- Test: `tests/unit/test_config_consistency.py`

**Interfaces:**
- Consumes: `get_config().watchlist`, `get_config().execution.mode`, `get_config().intraday_monitor.custom_watchlist`
- Produces: default watchlist and execution mode used by scheduler, signal router, intraday monitor, and Web dashboard.

- [x] Update `watchlist` to the four user symbols.
- [x] Set `intraday_monitor.custom_watchlist` to the same four symbols.
- [x] Set `execution.mode` to `paper`.
- [x] Update `.env.example` `RUN_MODE=paper` comment to clarify paper is simulation only.
- [x] Add tests asserting watchlist and intraday custom watchlist contain exactly these symbols.

### Task 2: Harden Web Paper Order API

**Files:**
- Modify: `src/gugu/web/app.py`
- Test: `tests/unit/test_web_api.py`

**Interfaces:**
- Consumes: `RiskManager.check_order(symbol, direction, quantity, price, portfolio, cash, prev_close=None, is_suspended=False, is_st=False)`
- Produces: `/api/order` response with `status`, `symbol`, `direction`, `quantity`, `price`, `message`.

- [ ] Add positive validation for `quantity > 0` and `price > 0` when price is supplied.
- [ ] Reject unsupported execution modes except `paper` with a clear message.
- [ ] For buy/sell, call `risk.check_order()` using broker portfolio and account cash before `broker.order()`.
- [ ] Return risk rejection message without placing an order.
- [ ] Keep sell path allowed during L2 halt only if `RiskManager.check_order()` allows it.
- [ ] Add tests for paper buy success, risk rejection, invalid quantity, and signal/live mode rejection.

### Task 3: Fix CI Dependency and Type-Check Configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: declared `pytest-timeout` dependency and CI mypy command compatible with project Python version.

- [ ] Add `pytest-timeout` to dev dependencies.
- [ ] Set mypy command to `uv run mypy --python-version 3.12 src/gugu --ignore-missing-imports`.
- [ ] Keep pytest `--timeout=30` unchanged after dependency is declared.

### Task 4: Sync Documentation and Verify

**Files:**
- Modify: `README.md`

**Interfaces:**
- Produces: user-facing docs matching actual test count, coverage, paper mode, and monitored symbols.

- [x] Update README test count and coverage after verification.
- [x] Add monitored symbols section under quick start.
- [x] Clarify `paper` means simulation and QMT live remains disabled.
- [x] Run `uv run ruff check src/gugu tests/ --output-format=concise`.
- [x] Run `uv run pytest tests/ -q --tb=line --no-header`.
- [x] Run `uv run mypy --python-version 3.12 src/gugu/config src/gugu/filters/money_flow.py src/gugu/web/app.py --ignore-missing-imports`.

### Task 5: Strategy Validation Workflow

**Files:**
- Modify: `scripts/backtest_all_strategies.py`
- Modify: `scripts/verify_enabled_strategies.py`
- Modify: `scripts/backtest_by_regime.py`
- Modify: `scripts/optimize_params.py`
- Create: `scripts/validate_strategy_gate.py`
- Modify: `config/strategy_manifest.yaml`
- Test: `tests/unit/test_strategy_validation_workflow.py`

**Interfaces:**
- Produces: verification scripts that read real enabled strategies from `settings.yaml`; `double_yin` parameter grid for Walk-Forward; structured strategy gate with 9 thresholds; manifest `validation_gate` fields for enabled strategies.

- [x] Fix `backtest_all_strategies.py` to read `get_config().strategy.get_all_enabled_unique()`.
- [x] Fix `verify_enabled_strategies.py` to read `get_config().strategy.get_all_enabled_unique()`.
- [x] Fix `backtest_by_regime.py` default strategies to read from config instead of hardcoded `box_breakout,turtle`.
- [x] Add `double_yin` parameter grid and constraints to `optimize_params.py`.
- [x] Create `validate_strategy_gate.py` with 9-threshold structured gate.
- [x] Add `validation_gate` fields to `box_breakout` and `double_yin` in `strategy_manifest.yaml`.
- [x] Add tests covering all of the above.
