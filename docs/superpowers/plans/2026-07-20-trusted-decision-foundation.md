# 可信决策底座实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 gugu 的信号处理收敛为可信、可审计的决策合同，严格阻断数据不足和视角不通过的买入，并修复盈利反馈口径。

**Architecture:** 在现有 `Signal` 和 `PerspectiveAnalysisResult` 上增加兼容字段，不立即重构全部模块。由一个统一的决策门禁函数负责生成 `approved/blocked` 状态和阻断原因；信号流水线、通知和历史反馈只消费该结果。保留旧字段读取能力，但新路径不再使用订单成功推断盈利，也不允许无视角结果默认通过。

**Tech Stack:** Python 3.11+, dataclasses, pytest, pytest-asyncio, pandas；沿用现有配置、日志和序列化模式。

## Global Constraints

- 第一阶段不启用自动实盘下单。
- 数据状态为 `fresh` 才允许买入门禁；`stale`、`partial`、`invalid`、`unavailable` 只能研究/通知，不能买入。
- 十大视角必须同时满足综合分、最低分、通过数量、无致命项、无异常和无核心数据缺失。
- 订单成功、买入方向不得作为盈利标签。
- 旧 JSONL/字典数据必须兼容读取，但缺少真实结果时不得进入交易盈利反馈。
- 每项行为先补测试，再写最小实现。
- 修改代码后同步更新 `docs/PRODUCT.md`、`docs/TECH.md`、`docs/ARCHITECTURE_DECISIONS.md`。

---

### Task 1: 建立决策合同和统一状态判断

**Files:**
- Modify: `src/gugu/models/signal.py:12-132`
- Create: `src/gugu/models/decision.py`
- Modify: `src/gugu/models/__init__.py`
- Test: `tests/unit/test_signal.py`
- Test: `tests/unit/test_decision_contract.py`

**Interfaces:**
- Produces `DataQuality`, `DecisionStatus`, `DecisionAction` literal aliases.
- Produces `DecisionContract` dataclass with `to_dict()` and `from_dict()`.
- Produces `evaluate_entry_gate(...) -> DecisionContract` for later pipeline use.

- [ ] **Step 1: Write failing contract tests**

Add tests for:

```python
from gugu.models.decision import (
    DataQuality,
    DecisionAction,
    DecisionContract,
    DecisionStatus,
)


def test_contract_defaults_to_candidate_observation():
    decision = DecisionContract(symbol="600519", decision_type="entry")
    assert decision.action == "observe"
    assert decision.status == "candidate"
    assert decision.data_quality == "unavailable"
    assert decision.allowed_to_trade is False


def test_contract_round_trips_legacy_safe_fields():
    decision = DecisionContract(
        symbol="600519",
        action="blocked",
        status="blocked",
        data_quality="partial",
        block_reasons=["核心数据缺失"],
    )
    restored = DecisionContract.from_dict(decision.to_dict())
    assert restored.symbol == "600519"
    assert restored.status == "blocked"
    assert restored.block_reasons == ["核心数据缺失"]
```

Extend `Signal.to_dict()` and `Signal.from_dict()` tests to preserve `decision` data without breaking old dictionaries.

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```bash
pytest tests/unit/test_decision_contract.py tests/unit/test_signal.py -q
```

Expected: FAIL because the contract and fields do not exist yet.

- [ ] **Step 3: Implement the minimal contract**

Create `DecisionContract` with these fields:

```python
@dataclass
class DecisionContract:
    symbol: str = ""
    decision_type: Literal["entry", "exit", "observe"] = "entry"
    action: Literal["buy", "sell", "hold", "observe", "blocked"] = "observe"
    status: Literal[
        "candidate", "blocked", "approved", "notified",
        "executed", "expired", "closed", "evaluated"
    ] = "candidate"
    data_quality: DataQuality = "unavailable"
    data_timestamp: str = ""
    data_sources: list[str] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)
    entry_condition: str = ""
    invalidation_condition: str = ""
    expires_at: str = ""
    execution_status: str = "pending"
    outcome_status: str = "pending"
    outcome_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed_to_trade(self) -> bool:
        return self.status == "approved" and self.action in {"buy", "sell"}
```

Add `decision: dict[str, Any]` to `Signal`, serialize it, and read it with `{}` for legacy records. Export the new types from `gugu.models`.

- [ ] **Step 4: Run focused tests and confirm pass**

Run:

```bash
pytest tests/unit/test_decision_contract.py tests/unit/test_signal.py -q
```

Expected: PASS.

---

### Task 2: Make perspective aggregation strict and consistent

**Files:**
- Modify: `src/gugu/analysis/perspectives/data_types.py:156-217`
- Modify: `src/gugu/analysis/perspectives/engine.py:51-175`
- Modify: `src/gugu/analysis/perspectives/integrators.py:269-305`
- Test: `tests/unit/test_perspectives.py`

**Interfaces:**
- `PerspectiveScore` gains `status: Literal["ok", "error"]`.
- `PerspectiveAnalysisResult` gains `error_count` and `missing_core_fields`.
- `PerspectiveAnalysisResult.passed` is the only authoritative gate property.
- `filter_by_perspective(..., require_pass=True)` rejects absent scores and failed scores.

- [ ] **Step 1: Add failing tests for strict gate behavior**

Add tests covering:

```python
def test_empty_perspective_result_does_not_pass():
    result = PerspectiveAnalysisResult(symbol="600519", name="测试", price=100)
    assert result.passed is False


def test_perspective_error_blocks_result(monkeypatch):
    def broken_scorer(ctx):
        raise RuntimeError("broken scorer")

    monkeypatch.setitem(SCORERS, ALL_PERSPECTIVES[0], broken_scorer)
    result = PerspectiveEngine().analyze(make_excellent_context())
    assert result.error_count >= 1
    assert result.passed is False
    assert "异常" in result.overall_verdict or "阻断" in result.overall_verdict


def test_filter_does_not_keep_signal_without_perspective_result():
    signals = [{"symbol": "600519", "direction": "buy"}]
    assert filter_by_perspective(signals, require_pass=True) == []
```

Add a test for a score above 7 but `passed_count < 7`, asserting `passed is False` and the verdict is not a buy recommendation.

- [ ] **Step 2: Run perspective tests and confirm failure**

Run:

```bash
pytest tests/unit/test_perspectives.py -q
```

Expected: FAIL on absent-score retention, error handling, or verdict mismatch.

- [ ] **Step 3: Implement strict result semantics**

Change `PerspectiveScore` exception construction to `status="error"`; do not represent exceptions as ordinary score 5.0. Add `error_count` to the result and include it in `to_dict()`.

Make `passed` require:

```python
return (
    bool(self.scores)
    and self.error_count == 0
    and self.fatal_count == 0
    and self.composite_score >= COMPOSITE_PASS_THRESHOLD
    and self.min_score >= MIN_PERSPECTIVE_SCORE
    and self.passed_count >= 7
)
```

Make `_aggregate()` derive the verdict from `result.passed` conditions, so no branch can output a buy recommendation when `passed` is false. Use `"观察/阻断"` for failed entry analysis. Update `filter_by_perspective()` so `require_pass=True` requires a nonzero score, an explicit pass flag, and no blocked status; remove the legacy “score == 0 means keep” branch for strict mode.

- [ ] **Step 4: Run tests and inspect all perspective assertions**

Run:

```bash
pytest tests/unit/test_perspectives.py tests/unit/test_multi_perspective_signal.py -q
```

Expected: PASS. Update only assertions that depended on the old unsafe keep behavior.

---

### Task 3: Add conservative data-quality gate to signal integration

**Files:**
- Modify: `src/gugu/analysis/perspectives/data_types.py:63-153`
- Modify: `src/gugu/analysis/perspectives/integrators.py:1-250`
- Modify: `src/gugu/engine/signal_pipeline.py:360-520`
- Modify: `src/gugu/config/models.py` only if existing config types require the gate flag
- Test: `tests/unit/test_signal_pipeline.py`
- Test: `tests/unit/test_perspectives.py`

**Interfaces:**
- `StockDataContext` gains `data_quality`, `data_timestamp`, `data_sources`, and `missing_core_fields`.
- `build_context_from_signal()` preserves missing-vs-zero information.
- `attach_perspective_to_signal()` writes a serialized decision contract.
- Pipeline calls one strict gate for all modes before a buy can continue.

- [ ] **Step 1: Write failing data-quality and pipeline tests**

Add tests for:

```python
def test_missing_core_fields_are_recorded():
    ctx = build_context_from_signal({"symbol": "600519", "price": 100})
    assert ctx.data_quality in {"partial", "unavailable"}
    assert ctx.missing_core_fields


def test_stale_signal_is_blocked_before_buy_execution(pipeline, ...):
    # construct a buy signal with decision data_quality="stale"
    # run the pipeline and assert no executable buy remains
    assert result[0]["decision"]["status"] == "blocked"
    assert result[0]["decision"]["action"] == "observe"
```

Add tests for missing perspective result and failed perspective result. Both must yield `blocked`, with non-empty `block_reasons`.

- [ ] **Step 2: Run focused pipeline tests and confirm failure**

Run:

```bash
pytest tests/unit/test_signal_pipeline.py tests/unit/test_perspectives.py -q
```

Expected: FAIL because pipeline currently allows missing/legacy perspective data and has no unified decision field.

- [ ] **Step 3: Implement data-quality metadata and gate integration**

Add explicit metadata to `StockDataContext`; when building from a signal, collect absent required fields instead of silently treating all values as known zero. Required fields should include at minimum `symbol`, positive `price`, `timestamp`, and the fields required by enabled strategy/perspective inputs.

Implement `evaluate_entry_gate(signal, perspective_result=None) -> DecisionContract` in `src/gugu/models/decision.py` with this order:

1. Reject non-buy entry candidates as not applicable.
2. Reject `data_quality != "fresh"`.
3. Reject absent perspective result.
4. Reject perspective errors or `not passed`.
5. Otherwise set `action="buy"`, `status="approved"`, `allowed_to_trade=True`.

Use stable Chinese block reasons, for example:

```python
"数据状态不是 fresh"
"缺少十大视角评分"
"十大视角未通过统一门禁"
"存在视角评分异常"
```

In `attach_perspective_to_signal()`, write both legacy perspective fields and `decision`. In `signal_pipeline.py`, invoke the gate after perspective analysis and before any buy execution path. All modes must consume the same gate result; notification-only modes may display blocked candidates but must not call an execution method for them.

- [ ] **Step 4: Run the full focused suite**

Run:

```bash
pytest tests/unit/test_signal_pipeline.py tests/unit/test_perspectives.py tests/unit/test_execution.py -q
```

Expected: PASS with blocked signals represented as observations, not executable buys.

---

### Task 4: Remove false profitability feedback and separate outcome metrics

**Files:**
- Modify: `src/gugu/wisdom/advisor.py:117-141`
- Modify: `src/gugu/analysis/attribution.py:180-240`
- Modify: `src/gugu/analysis/signal_tracker.py` where metric names are emitted
- Modify: `src/gugu/engine/main.py` where performance fields are attached
- Test: `tests/unit/test_wisdom.py`
- Test: `tests/unit/test_p0_wisdom.py`
- Test: `tests/unit/test_analysis.py`

**Interfaces:**
- `_is_profitable_signal(record) -> bool` returns true only for an explicit closed-trade profitable outcome or explicit realized PnL > 0.
- Forward five-day metrics use `forward_5d_signal_hit_rate` naming.
- Execution success remains separate and never populates profitability feedback.

- [ ] **Step 1: Add failing outcome-label tests**

Add tests:

```python
def test_order_success_is_not_profit():
    assert _is_profitable_signal({
        "direction": "buy",
        "order_success": True,
        "execution_status": "filled",
    }) is False


def test_closed_trade_profit_is_profit():
    assert _is_profitable_signal({
        "direction": "buy",
        "outcome_status": "closed",
        "realized_pnl": 12.5,
    }) is True


def test_open_trade_is_not_profit_even_with_positive_mark_to_market():
    assert _is_profitable_signal({
        "direction": "buy",
        "execution_status": "filled",
        "outcome_status": "open",
        "unrealized_pnl": 12.5,
    }) is False
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run:

```bash
pytest tests/unit/test_wisdom.py tests/unit/test_p0_wisdom.py tests/unit/test_analysis.py -q
```

Expected: the old implementation incorrectly marks the first case as profitable.

- [ ] **Step 3: Implement explicit outcome semantics**

Replace the old direction/order-success shortcut with:

```python
def _is_profitable_signal(signal_record: dict) -> bool:
    if signal_record.get("outcome_status") != "closed":
        return False
    realized_pnl = signal_record.get("realized_pnl")
    if realized_pnl is None:
        realized_pnl = signal_record.get("outcome_metrics", {}).get("realized_pnl")
    try:
        return float(realized_pnl) > 0
    except (TypeError, ValueError):
        return False
```

Update callers so execution success is emitted as `execution_success_rate` or per-record `execution_status`; five-day forward returns are emitted under `forward_5d_signal_hit_rate`; no caller uses these fields as closed-trade profit.

- [ ] **Step 4: Run outcome and regression tests**

Run:

```bash
pytest tests/unit/test_wisdom.py tests/unit/test_p0_wisdom.py tests/unit/test_analysis.py tests/unit/test_signal_pipeline.py -q
```

Expected: PASS; no open or merely filled order contributes to Wisdom profitability feedback.

---

### Task 5: Make notifications reflect blocked versus approved decisions

**Files:**
- Modify: `src/gugu/notifier/formatter.py:260-430`
- Modify: `src/gugu/notifier/perspective_report.py:70-220`
- Test: `tests/unit/test_notifier.py`
- Test: `tests/unit/test_perspectives.py`

**Interfaces:**
- Formatter consumes `signal["decision"]` when available.
- Blocked candidate renders as `观察信号` and includes `block_reasons`.
- Approved signal renders allowed action and data quality.

- [ ] **Step 1: Write failing notification tests**

Add tests asserting:

```python
def test_blocked_decision_is_observation_not_buy():
    text = format_signal({
        "symbol": "600519",
        "direction": "buy",
        "decision": {
            "action": "observe",
            "status": "blocked",
            "allowed_to_trade": False,
            "block_reasons": ["十大视角未通过统一门禁"],
        },
    })
    assert "观察" in text
    assert "十大视角未通过统一门禁" in text
    assert "推荐买入" not in text
```

Use the repository’s actual formatter function name and fixture structure when adding the test.

- [ ] **Step 2: Run notifier tests and confirm failure**

Run:

```bash
pytest tests/unit/test_notifier.py -q
```

Expected: FAIL because formatter currently derives action from legacy direction/wisdom fields.

- [ ] **Step 3: Implement decision-first rendering**

At the top of the signal card, render:

```text
当前动作：观察 / 买入 / 卖出
状态：阻断 / 已批准
是否允许交易：是 / 否
数据质量：fresh / stale / partial / invalid / unavailable
阻断原因：...
```

When `status == "blocked"` or `allowed_to_trade` is false, override recommendation wording to observation. Keep detailed perspective information below the decision summary.

- [ ] **Step 4: Run notifier and end-to-end formatting tests**

Run:

```bash
pytest tests/unit/test_notifier.py tests/unit/test_flow_report.py tests/unit/test_perspectives.py -q
```

Expected: PASS and blocked notifications never present themselves as executable buy recommendations.

---

### Task 6: Synchronize product, technical, and architecture documentation

**Files:**
- Modify: `docs/PRODUCT.md`
- Modify: `docs/TECH.md`
- Modify: `docs/ARCHITECTURE_DECISIONS.md`
- Test: `tests/unit/test_config_consistency.py` if documentation/config assertions exist

**Interfaces:**
- Documentation describes actual default behavior and unified metrics.

- [ ] **Step 1: Update product behavior statements**

In `docs/PRODUCT.md`, change the current default description to state that `track_live` and `signal_only` monitor and notify only; they do not automatically place orders. State that blocked candidates are observations and cannot enter the buy path.

- [ ] **Step 2: Update technical contract**

In `docs/TECH.md`, document:

```text
fresh is required for entry decisions
stale/partial/invalid/unavailable are non-trading states
perspective gate is mandatory for buy candidates
missing/error perspective results do not pass
```

Document all five metric names and their exact meanings.

- [ ] **Step 3: Add architecture decision record**

Append an ADR entry explaining why the system uses a decision contract, conservative data blocking, and explicit outcome labels. Include the rejected alternative of treating missing data as neutral and order success as profit.

- [ ] **Step 4: Verify documentation consistency**

Run:

```bash
pytest tests/unit/test_config_consistency.py -q
```

Expected: PASS.

---

### Task 7: Run complete verification and perform adversarial review

**Files:**
- Test: all affected test files from Tasks 1-6
- Modify: affected implementation files only if verification reveals a concrete defect

- [ ] **Step 1: Run targeted regression suite**

Run:

```bash
pytest tests/unit/test_decision_contract.py tests/unit/test_signal.py tests/unit/test_perspectives.py tests/unit/test_signal_pipeline.py tests/unit/test_wisdom.py tests/unit/test_p0_wisdom.py tests/unit/test_analysis.py tests/unit/test_notifier.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full unit suite**

Run:

```bash
pytest tests/unit -q
```

Expected: PASS, or every pre-existing unrelated failure is recorded with its exact test name and reason.

- [ ] **Step 3: Run integration suite**

Run:

```bash
pytest tests/integration -q
```

Expected: PASS without any automatic real-order side effect.

- [ ] **Step 4: Perform adversarial checks**

Manually exercise these cases through the public pipeline:

1. Filled buy order followed by negative realized PnL;
2. No close result;
3. Stale cached data;
4. Missing one core field;
5. Perspective scorer exception;
6. Seven scores passing but one fatal score;
7. Composite score above 7 but only six perspectives passing;
8. Missing perspective result;
9. Blocked signal in `track_live`;
10. Approved signal in `track_live` and confirm it still does not auto-submit.

- [ ] **Step 5: Record final verification evidence**

Capture command results and changed file list in the final response. Do not claim completion without passing command output or explicitly documented test gaps.

## Self-review checklist

- All design requirements map to Tasks 1-7.
- No task uses a placeholder or unspecified helper name.
- `DecisionContract` is introduced before pipeline integration.
- The strict perspective result is introduced before the pipeline consumes it.
- Outcome labels are changed independently from notification wording.
- Documentation is updated only after behavior is implemented.
- No automatic real trading is enabled by this plan.
