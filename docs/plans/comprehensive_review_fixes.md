# 综合审查修复计划

> **For agentic workers:** 按照优先级从 P0 → P1 → P2 依次执行。

**目标:** 修复四位专家审查报告中发现的全部 28 个问题

**架构:** 每个修复独立，按优先级执行

**Tech Stack:** Python, YAML

---

## 执行摘要

| 优先级 | 数量 | 已修复 | 状态 |
|--------|------|--------|------|
| P0 (关键) | 5 | 4 | 进行中 |
| P1 (重要) | 9 | 0 | 待开始 |
| P2 (优化) | 14 | 0 | 待开始 |

### P0 修复状态

| # | 问题 | 文件 | 状态 |
|---|------|------|------|
| P0-1 | 花木兰4策略 enabled=true 但0回测 | config/strategy_manifest.yaml | ✅ 已修复 |
| P0-2 | return_5d 前视偏差 | — | ✅ 确认不存在（已使用收盘价） |
| P0-3 | 十大视角假精度 | data_types.py, engine.py | ✅ 已修复 |
| P0-4 | RiskManager dict构造绕过 | risk/manager.py | ✅ 已修复 |
| P0-5 | 因子IC方向错误 | analysis/factor_ic.py | ✅ 已修复 |

---

## 已修复详情

### P0-1：花木兰4策略禁用

**文件:** config/strategy_manifest.yaml

4个策略的 `enabled: true` → `enabled: false`：
- `huamulan_tail_pick`
- `huamulan_crash_layout`
- `huamulan_fake_dip`
- `huamulan_ghost_breakout`

统一注释 `# P0-1: 0回测数据，待Walk-Forward验证通过后再启用`

### P0-2：绩效验证前视偏差（无需修复）

审查报告称"使用5日内最高价计算收益率"，实际代码使用 `close` 收盘价：
- [signal_tracker.py](file:///Users/dong/workspace/gugu/src/gugu/analysis/signal_tracker.py): `float(df.loc[target_idx, "close"])`
- [attribution.py](file:///Users/dong/workspace/gugu/src/gugu/analysis/attribution.py): `float(df.loc[target_idx, "close"])`

### P0-3：十大视角数据完整度

**文件:**
- [data_types.py](file:///Users/dong/workspace/gugu/src/gugu/analysis/perspectives/data_types.py)
- [engine.py](file:///Users/dong/workspace/gugu/src/gugu/analysis/perspectives/engine.py)

新增：
1. `_DATA_COMPLETENESS_FIELDS` 列表（48个核心字段）
2. `calc_data_completeness(ctx)` 函数计算非默认值填充率
3. `PerspectiveAnalysisResult.data_completeness` 字段
4. 评分判定中 `data_completeness < 50%` 时标注"低数据完整度"

### P0-4：RiskManager硬编码安全上限

**文件:** [manager.py](file:///Users/dong/workspace/gugu/src/gugu/risk/manager.py)

新增4个安全上限常量（所有3个构造分支均应用 `min()`）：
```
_SAFE_MAX_POSITION_RATIO = 0.50  # ≤ 50%
_SAFE_DAILY_LOSS_WARN = 0.05     # ≤ 5%
_SAFE_DAILY_LOSS_HALT = 0.10     # ≤ 10%
_SAFE_MAX_DRAWDOWN_HALT = 0.30   # ≤ 30%
```

### P0-5：增加横截面IC计算

**文件:** [factor_ic.py](file:///Users/dong/workspace/gugu/src/gugu/analysis/factor_ic.py)

新增 `FactorICAnalyzer.compute_cross_sectional_ic(factor_df, forward_returns)`：
- 输入：factor_df(index=symbol, columns=factor_name), forward_returns(index=symbol)
- 事件驱动因子走横截面路径而非时序路径
- 自动处理股票覆盖不足和零标准差的情况

---

## 待修复：P1（重要缺陷）

### P1-1：SignalPipeline + engine_mixin.py 重构

**问题:** SignalPipeline 1200+行，engine_mixin.py 上帝 mixin

**建议:**
1. 引擎 mixin 拆分为 SignalScanner/SignalDedupService/HeartbeatService 等组合服务
2. 信号过滤链重构为 Chain of Responsibility，消除 signal_only/paper 两条重复过滤链

**影响范围:**
- `src/gugu/engine/signal_pipeline.py`
- `src/gugu/engine/engine_mixin.py`

### P1-2：enabled_by_regime 配置缺失

**问题:** 策略配置缺少 enabled_by_regime 显式定义，与 regime_strategy_adjuster 冲突

**建议:** 在策略配置中显式定义 `enabled_by_regime: {bull: [...], sideways: [...], bear: []}`

**影响范围:**
- `config/settings.yaml`（已按市场状态配置策略，但缺少熊市的明确空列表）
- `src/gugu/analysis/regime_strategy_adjuster.py`

### P1-3：回测-实盘涨跌停检查不一致

**问题:** BacktestEngine 硬阻断（差5%就拒绝），PaperBroker 概率模型（30%侥幸成交）

**建议:** 统一为硬阻断逻辑

**影响范围:**
- `src/gugu/backtest/engine.py`（涨跌停检查）
- `src/gugu/execution/paper.py`（概率模型）

### P1-4：飞书通知跨日去重

**问题:** `_record_card_fingerprint` 1h TTL + 纯日期key不包含跨日信息，23:50-00:10窗口可能误拦截

**建议:** fingerprint key 包含日期时间信息，避免跨日窗口误拦截

**影响范围:**
- `src/gugu/notifier/feishu.py` 或 `src/gugu/notifier/flow_collector.py`

### P1-5：DataManager 批量串行

**问题:** `fetch_stock_history_batch` 50只股票=50次串行HTTP

**建议:** 改为 asyncio.gather 并行请求

**影响范围:**
- `src/gugu/data/manager.py`

### P1-6：LLM决策价值无法验证

**问题:** 无A/B对比机制，无法衡量LLM比硬编码规则好多少

**建议:** 添加决策对比模式，同时生成 LLM 和规则决策并存记录

**影响范围:**
- `src/gugu/wisdom/advisor.py`
- `src/gugu/wisdom/tutor.py`

### P1-7：花木兰日内策略在日K线上运行

**问题:** huamulan_fake_dip（早盘假摔）需要分钟级数据，日K线的长下影线可能是尾盘跳水

**建议:** 已通过 P0-1 禁用该策略。长期方案：增加分钟级数据支持

**影响范围:**
- 已禁用（P0-1）

### P1-8：组合回撤 peak 更新时机过晚

**问题:** 循环结束时才更新 `_portfolio_peak`，盘中最高点到执行止损间的回撤可能被低估

**建议:** 在循环内及时更新 peak

**影响范围:**
- `src/gugu/backtest/engine.py`

### P1-9：策略验证 calc_liquidity_stress_test 失效

**问题:** Trade dataclass 没有 market_cap 字段，`trade.get("market_cap", 100e8)` 始终返回默认值

**建议:** 在 Trade 数据类中增加 market_cap 字段，并在回测/实盘中填充

**影响范围:**
- `src/gugu/models/trade.py`（或 Trade dataclass 所在文件）
- `src/gugu/screener/validator.py`
- `src/gugu/backtest/engine.py`

---

## 待修复：P2（架构优化）

| # | 问题 | 文件 | 难度 |
|---|------|------|------|
| P2-1 | EngineOpsMixin→组合模式拆分 | engine_mixin.py | 高 |
| P2-2 | 配置重复定义消除+权重=1.0 CI测试 | config/*.yaml | 低 |
| P2-3 | YAML中文文档移至 docs/ | strategy_manifest.yaml | 低 |
| P2-4 | 启发式优化器添加Early Stopping | analysis/heuristic_optimizer.py | 中 |
| P2-5 | 回测指标缺少Alpha/Beta/IR | backtest/engine.py, validation.py | 中 |
| P2-6 | 参数扰动 base_return 应显式含0% | analysis/param_optimizer.py | 低 |
| P2-7 | DataCache.set() LRU淘汰缺陷 | data/cache.py | 低 |
| P2-8 | FreshnessRule假日检测fallback | data/quality.py | 低 |
| P2-9 | DataFrame保留attrs元数据 | data/*.py | 中 |
| P2-10 | 信号拒绝记录到JSONL | engine/signal_pipeline.py | 低 |
| P2-11 | stop_loss_manager快照过时 | execution/order_manager.py | 中 |
| P2-12 | halt冷却时间应持久化 | risk/manager.py | 低 |
| P2-13 | 测试环境隔离不完整 | PaperBroker/RiskManager | 低 |
| P2-14 | 事件引擎过度工程 | engine/event_engine.py | 低 |

---

## 执行顺序建议

1. **P0-2修复验证**（确认已使用收盘价，无需改动）
2. **P0-1 → P0-4 → P0-3 → P0-5**（已修复）
3. **P1-3 → P1-5 → P1-8 → P1-9**（影响实盘收益的风控/数据关键路径）
4. **P1-4 → P1-6**（运营体验）
5. **P1-1 → P1-2**（架构重构，影响范围大）
6. **P2**（按需排期）