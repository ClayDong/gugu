# 迭代 8：多股聚合优化 + 测试隔离 + 自优化闭环

> 2026-07-04

## 1. 背景

迭代 7 完成了数据质量与通知优化，达到 818 测试 / 70% 覆盖率。但在多股聚合 Walk-Forward 验证过程中发现 6 个关键问题：

1. **单股 Walk-Forward 严重过拟合**：训练 Sharpe 5.01 vs 测试 Sharpe -6.28
2. **df.iloc 逐行访问性能瓶颈**：turtle 多股聚合 575s（cProfile 显示 df.iloc 占 57%）
3. **测试隔离漏洞**：PaperBroker 实例化即读写生产文件，污染 data/paper_broker_state.json
4. **SinaCollector 双源失败不抛异常**：主源无法触发降级， DataManager._on_failure_locked 永不触发
5. **money_flow_follow 完全失效**：33392 次回测 trades=0（5 AND 过严）
6. **调度器无优化任务**：自优化机制形同虚设，参数永不会自动更新
7. **状态文件非原子写入**：崩溃可能导致 heartbeat.json / take_profit_state.json 截断
8. **配置权威源分裂**：leopard_three_factors 在 settings.yaml 启用但 manifest.yaml 缺失

本次迭代核心目标：**修复多股聚合验证发现的所有问题，建立自优化闭环，保障测试隔离与数据一致性**。

## 2. 本次迭代目标

| 目标 | 说明 |
|------|------|
| 性能修复 | df.iloc 逐行访问 → numpy 预提取，4.5x 加速 |
| 测试隔离 | 4 个 monkeypatch 防止污染生产文件 |
| 数据源降级 | SinaCollector 双源失败抛异常，触发主源降级 |
| 状态原子写入 | heartbeat + take_profit_state 改用 atomic_write_text |
| 自优化调度 | 周日 03:00 自动跑 Walk-Forward 多股聚合 |
| 策略重设计 | money_flow_follow 混合模式（hard filter + soft score） |
| 配置一致性 | leopard_three_factors 补入 manifest + 一致性测试 |
| 测试验证 | 910 测试全部通过，无回归 |

## 3. 改动范围

### 3.1 修改文件

| 文件 | 修改要点 |
|------|----------|
| `src/gugu/backtest/engine.py` | P1-9: df.iloc 性能修复，numpy 预提取列数组 |
| `tests/conftest.py` | P1-2: 4 个 monkeypatch 修复测试隔离漏洞 |
| `src/gugu/data/collectors/fallback.py` | P1-10: SinaCollector 双源失败抛 RuntimeError |
| `src/gugu/strategies/money_flow_follow.py` | P1-11: 重设计为混合模式（hard filter + soft score） |
| `config/strategy_defaults.yaml` | 移除 money_flow_follow 的 buy_score_threshold 参数 |
| `src/gugu/engine/main.py` | P1-5: 心跳写入改用 atomic_write_text |
| `src/gugu/analysis/dynamic_take_profit.py` | P1-5: 止盈状态写入改用 atomic_write_text |
| `src/gugu/engine/scheduler.py` | P1-7: 新增 weekly_optimize_0300 任务 |
| `config/strategy_manifest.yaml` | P1-8: 补全 leopard_three_factors 元数据 |
| `tests/unit/test_manifest.py` | P1-8: 新增 TestManifestSettingsConsistency 测试类 |
| `tests/unit/test_scheduler.py` | 更新 job 数期望值 19 → 20 |

## 4. 架构演进

### 4.1 df.iloc 性能修复（P1-9）

```
迭代 7（df.iloc 逐行访问）:
  for i in range(n_rows):
      close = float(df.iloc[i]["close"])  # 每次触发 ExtensionArray + 200+ 次 isinstance
  ↓ cProfile 显示：329s/575s = 57% 耗时
  ↓ 根因：pandas 内部 ExtensionArray 重新评估 typing

迭代 8（numpy 预提取）:
  close_arr = df["close"].to_numpy(dtype=float)  # 循环外一次性提取
  date_arr = df["date"].to_numpy()
  signal_arr = signals_df["signal"].to_numpy(dtype=int)

  for i in range(n_rows):
      close = float(close_arr[i])  # 单次 ~1μs（原 ~0.4ms）
  ↓ cProfile 验证：575s → 129s（4.5x 加速）
```

### 4.2 测试隔离修复（P1-2）

```
迭代 7（漏洞）:
  @pytest.fixture(autouse=True)
  def isolate_project_paths(tmp_path, monkeypatch):
      monkeypatch.setattr("gugu.engine.main.PROJECT_ROOT", tmp_path)
      # ↓ 漏洞1: paper.STATE_FILE 未重定向
      # ↓ 漏洞2: gugu.config.PROJECT_ROOT 是不同模块引用
      # ↓ 漏洞3: record_signal_history 未全局 mock
      # ↓ 漏洞4: _notified_signals_file 在 __init__ 即计算路径

迭代 8（4 个 monkeypatch）:
  monkeypatch.setattr("gugu.engine.main.PROJECT_ROOT", tmp_path)
  monkeypatch.setattr("gugu.config.PROJECT_ROOT", tmp_path)  # L2 修复
  monkeypatch.setattr("gugu.risk.manager.RISK_STATE_FILE", tmp_path / "risk_state.json")
  monkeypatch.setattr("gugu.execution.paper.STATE_FILE", tmp_path / "paper_broker_state.json")  # L1 修复
  monkeypatch.setattr("gugu.engine.main.record_signal_history", lambda *a, **kw: None)  # L3 修复
  monkeypatch.setenv("GUGU_TEST_MODE", "1")  # L4 修复
```

### 4.3 money_flow_follow 三次迭代的对抗性审查（P1-11）

```
原版（5 AND 过严）:
  ma_aligned_4 & cond_pullback_hold & cond_volume_confirm & cond_not_abnormal & cond_hot
  ↓ 33392 次回测 trades=0

第 1 次修改（纯评分制）:
  score >= 0.5 触发买入
  ↓ 震荡市误触发 22 个买入信号（应 0）

第 2 次修改（3 均线多头 hard filter）:
  ma_aligned_3 & vol_expanding & cond_above_ma20 & cond_not_abnormal
  ↓ 仍有 6 个误触发

第 3 次修改（基于 /tmp/analyze_flat_market.py 分析）:
  ↓ 发现所有 6 个误触发都是 ma20>ma60=False
  ↓ 恢复 4 均线多头排列 + 新增 MA20 上升趋势

  ma_aligned_4 & ma20_rising & vol_expanding & cond_above_ma20 & cond_not_abnormal
  ↓ 震荡市 0 买入信号，13/13 测试通过
```

### 4.4 自优化调度闭环（P1-7）

```
迭代 7（无优化任务）:
  调度器仅注册交易循环任务，参数永不会自动更新
  ↓ 自优化机制形同虚设

迭代 8（周参数优化）:
  self._scheduler.add_job(
      self._safe_optimize_params,
      CronTrigger(hour=3, minute=0, day_of_week="sun"),
      id="weekly_optimize_0300",
  )
  ↓ 周日 03:00 自动跑多股聚合 Walk-Forward
  ↓ 6 只行业代表股：白酒/养殖/电池/CXO/安防/金融
  ↓ --write 模式：通过 4 项解锁条件才写回参数
  ↓ 1h 超时保护，失败仅日志不影响下周一交易
```

### 4.5 配置一致性校验（P1-8）

```
迭代 7（分裂）:
  settings.yaml: enabled: [leopard_three_factors]
  manifest.yaml: 缺失
  ↓ 策略看板缺失该条目

迭代 8（补全 + 防御性测试）:
  - manifest.yaml 补全 leopard_three_factors 元数据
  - TestManifestSettingsConsistency 新增 2 个测试:
    * settings 启用的策略必须在 manifest 中有元数据
    * manifest enabled=true 的 signal 策略应在 settings 启用列表中
  ↓ 未来再添加策略时若忘了同步 manifest，CI 会失败
```

## 5. 关键设计决策

### 5.1 numpy 预提取而非改为向量化

**决策**：在循环外用 `df["col"].to_numpy()` 提取列，循环内用数组索引替代 df.iloc。

**原因**：
- 完全向量化会破坏信号生成的顺序逻辑（持仓状态、止损价追踪）
- numpy 预提取保留了循环结构，最小改动达到 4.5x 加速
- cProfile 精确定位瓶颈，避免凭直觉优化

**代价**：
- 多了 5 行预提取代码
- 需要处理 confidence 列可选的情况

### 5.2 money_flow_follow 混合模式

**决策**：hard filter（必要条件 AND）+ soft score（增强评分），而非纯评分制。

**原因**：
- 纯评分制在震荡市会因偶然的量价齐升误触发
- hard filter 确保趋势确认 + 资金进入的本质条件
- soft score 让置信度反映信号质量，影响通知优先级

**对抗性审查**：
- 第 1 次纯评分制失败（22 个误触发）→ 证明评分制不能完全替代 hard filter
- 第 2 次 3 均线多头仍失败（6 个误触发）→ 证明 MA60 长期均线不可省略
- 第 3 次 4 均线 + MA20 上升成功 → 实测验证假设

### 5.3 Walk-Forward 4 项解锁条件

**决策**：参数写回需同时满足 4 项条件，任一不满足则仅记录日志。

**原因**：
- 单股 Walk-Forward 严重过拟合（训练 Sharpe 5.01 vs 测试 -6.28）
- 多股聚合找跨股鲁棒参数，但仍需门控防止过拟合污染生产
- 4 项条件覆盖：收益性（Sharpe>0）+ 稳定性（参数一致性≥50%）+ 风险（回撤<25%）+ 正收益

**代价**：
- 大多数策略可能无法通过 4 项条件，参数不会更新
- 但这恰恰是正确的：宁可参数不变，不可污染生产

## 6. 测试验证

### 6.1 测试统计

| 指标 | 迭代 7 | 迭代 8 | 变化 |
|------|--------|--------|------|
| 总测试数 | 818 | **910** | **+92** |
| 通过率 | 100% | **100%** | ✅ |
| 覆盖率 | 70% | 62% | -8%（新增模块拉低） |

### 6.2 money_flow_follow 测试

| 测试场景 | 验证结果 |
|----------|----------|
| 震荡市（10±0.5） | 0 买入信号 ✅ |
| 看涨回踩（5 步条件） | 至少 1 个买入信号 ✅ |
| MA5 下穿 MA10 | 卖出信号 ✅ |
| 跌破 MA20 | 卖出信号置信度 0.8 ✅ |
| 历史天量 | 置信度惩罚 ≤0.75 ✅ |
| 无放量确认 | 不触发买入 ✅ |

### 6.3 配置一致性测试

| 测试场景 | 验证结果 |
|----------|----------|
| settings 启用的策略在 manifest 中存在 | ✅ |
| manifest enabled=true 的 signal 策略在 settings 启用列表中 | ✅ |

### 6.4 调度器测试

| 测试场景 | 验证结果 |
|----------|----------|
| setup() 创建 20 个 job | ✅（原 19，新增 weekly_optimize_0300） |
| weekly_optimize_0300 在 job_ids 中 | ✅ |

## 7. 多股聚合验证结果汇总

| 策略 | 回测行数 | trades>0 占比 | 最佳单股表现 | 评价 |
|---|---|---|---|---|
| bollinger | 1818/516 | 28% | 600519 +11.81% / Sharpe 3.07 | ⭐⭐⭐ |
| rsi_reversal | 882/470 | 53% | 600519 +7.22% / Sharpe 4.74 | ⭐⭐⭐ |
| dual_ma (P1修复后) | 522/202 | 39% | 601318 +2.78% / Sharpe 4.26 | ⭐⭐ |
| turtle | 12114/2929 | 24% | 601318 +5.88% / Sharpe 4.18 | ⭐⭐ |
| box_breakout | 378/39 | 10% | 601318 +2.90% / Sharpe 3.91 | ⭐ |
| money_flow_follow | 33392/0 → 修复后待重测 | — | — | ✅ 修复 |
| chen_jiangting | 41490 | 未汇总 | 未汇总 | ? 待汇总 |
| leopard_three_factors | 未跑 | — | — | ? 待跑 |

**关键发现**：A 股市场不适合传统趋势策略，bollinger/rsi_reversal 均值回归策略表现最佳。

## 8. 深度反思

### 8.1 从"凭直觉"到"cProfile 精确定位"

最初以为 typing 模块性能问题（macOS sample 显示 3453 samples 在 `_Py_union_from_tuple`），用 cProfile 精确分析后发现真相是 pandas `df.iloc[i]` 逐行访问占 57%，typing 只是 pandas 内部 isinstance 调用的副产物。

**核心原则**：性能优化必须基于 cProfile 精确分析，不可凭直觉猜测瓶颈。

### 8.2 从"过严"到"过松"再到"混合"

money_flow_follow 三次迭代的对抗性审查证明：
- 纯 AND 过严 → 零信号
- 纯评分制过松 → 震荡市误触发
- 混合模式（hard filter + soft score）→ 平衡

**核心原则**：策略设计不能走极端，hard filter 保证必要性，soft score 反映质量。

### 8.3 从"手动验证"到"自动闭环"

原优化脚本需手动运行，参数永不会更新。新增周日 03:00 自动任务后，系统具备自优化能力。

**核心原则**：关键运维流程必须自动化，避免依赖人工。

## 9. 后续优化路线图

### 短期（下一迭代）

1. 跑完 chen_jiangting + leopard_three_factors 多股聚合验证
2. 修复后的 money_flow_follow 重跑多股聚合验证
3. 周一开盘验证飞书卡片内容（置信度≠1.0、stage≠'数据不足'、stop_loss_price≠null）

### 中期

1. 扩大回测股票池（从 6 只到 20+ 只行业代表股）
2. Walk-Forward 优化结果持久化到 strategy_optimization_history.jsonl
3. 参数变化飞书通知（周优化完成后推送对比卡片）

### 长期

1. 全市场回测验证策略有效性
2. 策略表现衰减检测（参数自动失效告警）
3. 多策略组合优化（不仅单策略参数优化）

## 10. 迭代数据

```
迭代 7: 818 tests, 70% coverage, v0.3.6
  │
  ▼ Phase 1: P1-9 性能修复（df.iloc → numpy）
  + cProfile 575s → 129s（4.5x）
  + 14/14 单元测试通过
  │
  ▼ Phase 2: P1-2 测试隔离
  + 4 个 monkeypatch（paper.STATE_FILE / config.PROJECT_ROOT / record_signal_history / _notified_signals_file）
  │
  ▼ Phase 3: P1-10 数据源降级
  + SinaCollector 双源失败抛 RuntimeError
  │
  ▼ Phase 4: P1-5 状态原子写入
  + heartbeat.json + take_profit_state.json 改用 atomic_write_text
  │
  ▼ Phase 5: P1-7 自优化调度
  + weekly_optimize_0300 任务（周日 03:00）
  + 6 只行业代表股 + --write 门控
  │
  ▼ Phase 6: P1-11 策略重设计
  + money_flow_follow 混合模式（4 均线 + MA20 上升 + 放量 + soft score）
  + 13/13 单元测试通过
  │
  ▼ Phase 7: P1-8 配置一致性
  + leopard_three_factors 补入 manifest
  + 2 个一致性测试
  │
  ▼ Phase 8: 测试验证
  + 910 测试全部通过，无回归
  │
迭代 8: 910 tests, 62% coverage, v0.3.7
```

## 11. commit 记录

| Commit | 编号 | 修复点 |
|---|---|---|
| 12ca82d | P1-9 | df.iloc 逐行访问性能瓶颈 |
| 2a0793a | P1-2 | 测试隔离 4 个 monkeypatch 漏洞 |
| fcdf33d | P1-5 | 状态文件原子写入（心跳/止盈状态） |
| 6ce048c | P1-7 | 调度器新增周参数优化任务 |
| 630ee2d | P1-10 | SinaCollector 双源失败不抛异常 |
| 97d171f | P1-11 | money_flow_follow 重设计混合模式 |
| 8bc5830 | P1-8 | leopard_three_factors 补入 manifest + 一致性测试 |
