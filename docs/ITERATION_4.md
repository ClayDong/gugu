# 迭代4 · 数据精准性与智慧决策修复

> 日期：2026-06-22
> 范围：数据采集器真降级 + wisdom 决策流程修复 + 数据质量增强
> 触发：实际运行体验中发现数据不精准、智慧决策被绕过

---

## 一、问题发现

通过实际运行项目（连接真实飞书 + 模拟盘交易），发现以下严重问题：

| # | 问题 | 严重性 | 根因 |
|---|------|--------|------|
| 1 | SinaCollector 实时行情调用 `ak.stock_zh_a_spot_em()`——与主源完全相同 | P1 | 降级源不是真降级，主源故障时降级源同样失败 |
| 2 | TencentCollector 与 SinaCollector 代码完全重复 | P1 | 零价值冗余，两个降级源调用同一 API |
| 3 | **智慧仓位调整被完全绕过** | P0 | `wisdom.advise()` 在 `suggested_position_ratio` 设置前调用，拿到 0.0 跳过调整 |
| 4 | **入场过滤信号仍被执行交易** | P0 | `_process_signal` 不检查 `wisdom_filtered` 标志 |
| 5 | 数据质量校验不检测零价/过期数据 | P1 | quality.py 仅检查负值/缺失/high<low |

## 二、修复清单

### P0-1: 智慧仓位调整被完全绕过

- **文件**: [src/gugu/engine/main.py](file:///d:/aispace/gugu/src/gugu/engine/main.py)
- **根因**: `_scan_signals()` 中 `wisdom.advise(signal)` 在 `suggested_position_ratio` 设置前调用，`original_ratio = signal.get("suggested_position_ratio", 0.0)` 拿到 0.0，`original_ratio > 0` 为 False，仓位调整被跳过。然后 `_process_signal()` 又用 `max_ratio * 0.8` 覆盖。
- **修复**:
  1. 在 `_scan_signals()` 中，**先设置** `signal["suggested_position_ratio"] = max_ratio * 0.8`，**再调用** `wisdom.advise(signal)`
  2. 在 `_process_signal()` 中，使用 `signal.get("suggested_position_ratio", 0.0)` 而非覆盖
- **验证**: 新增 `test_process_signal_respects_wisdom_position_ratio` 测试，验证下单数量基于 wisdom 调整后的 20% 而非默认 24%

### P0-2: 入场过滤信号仍被执行交易

- **文件**: [src/gugu/engine/main.py](file:///d:/aispace/gugu/src/gugu/engine/main.py)
- **根因**: wisdom 设置 `signal["wisdom_filtered"] = True` 后，`_process_signal()` 不检查此标志，仍执行下单
- **修复**: 在 `_process_signal()` 开头增加检查：`if signal.get("wisdom_filtered"): 仅通知不下单`
- **验证**: 新增 `test_process_signal_wisdom_filtered` 测试，验证被过滤的信号不调用 `broker.order` 但调用 `notifier.notify_signal`

### P1-1: SinaCollector 实时行情不是真降级

- **文件**: [src/gugu/data/collectors/fallback.py](file:///d:/aispace/gugu/src/gugu/data/collectors/fallback.py)
- **根因**: `SinaCollector.fetch_stock_realtime()` 调用 `ak.stock_zh_a_spot_em()`（东方财富接口），与 `AkshareCollector` 完全相同。主源因该接口故障时，降级源同样失败。
- **修复**: 改用 `ak.stock_zh_a_spot()`（新浪源接口），与主源使用不同的底层 API
- **验证**: 代码审查确认 API 调用不同

### P1-2: 移除无价值的 TencentCollector

- **文件**: [src/gugu/data/collectors/fallback.py](file:///d:/aispace/gugu/src/gugu/data/collectors/fallback.py) + [src/gugu/data/__init__.py](file:///d:/aispace/gugu/src/gugu/data/__init__.py) + [src/gugu/data/manager.py](file:///d:/aispace/gugu/src/gugu/data/manager.py) + [config/settings.yaml](file:///d:/aispace/gugu/config/settings.yaml)
- **根因**: TencentCollector 与 SinaCollector 调用完全相同的 `ak.stock_zh_a_daily()` API，提供零冗余价值
- **修复**: 删除 TencentCollector 类，从 DataManager fallbacks 列表移除，从 __init__.py 导出移除，从 settings.yaml 配置移除

### P1-3: 数据质量校验增强

- **文件**: [src/gugu/data/quality.py](file:///d:/aispace/gugu/src/gugu/data/quality.py)
- **根因**: 仅检查负值/缺失/high<low，不检查零价（停牌返回 0）和数据时效性（过期数据）
- **修复**:
  1. 新增零价检测：`close == 0` 的行被剔除并告警
  2. 新增数据时效性检查：最新数据日期超过 7 天则告警
- **验证**: 新增 `test_validate_stock_history_zero_price` 测试

### P1-4: 测试状态隔离修复

- **文件**: [tests/unit/test_execution.py](file:///d:/aispace/gugu/tests/unit/test_execution.py) + [tests/unit/test_engine.py](file:///d:/aispace/gugu/tests/unit/test_engine.py)
- **根因**: PaperBroker 新增 JSON 持久化后，测试 fixture 未隔离 STATE_FILE，导致加载手动测试时的持久化状态
- **修复**: 使用 `monkeypatch.setattr("gugu.execution.paper.STATE_FILE", tmp_path / "state.json")` 隔离每个测试

## 三、质量验证结果

```
ruff check src tests: All checks passed!
pytest: 153 passed in 4.58s
覆盖率: 77%
```

## 四、新增测试用例

| 测试文件 | 测试用例 | 覆盖点 |
|----------|----------|--------|
| test_data.py | test_validate_stock_history_zero_price | 零价数据被剔除 |
| test_engine.py | test_process_signal_wisdom_filtered | 入场过滤信号仅通知不下单 |
| test_engine.py | test_process_signal_respects_wisdom_position_ratio | wisdom 仓位调整被尊重 |
| test_execution.py | broker fixture 状态隔离 | 持久化不干扰测试 |

## 五、架构改进总结

### 修复前的数据流（有 bug）

```
_scan_signals:
  router.route() → signal (无 suggested_position_ratio)
  wisdom.advise(signal) → original_ratio=0.0 → 跳过仓位调整
_process_signal:
  signal["suggested_position_ratio"] = max_ratio * 0.8 → 覆盖（但 wisdom 根本没设过）
  → 下单（即使被入场过滤也下单）
```

### 修复后的数据流（正确）

```
_scan_signals:
  router.route() → signal
  signal["suggested_position_ratio"] = max_ratio * 0.8 → 先设基础比例
  wisdom.advise(signal) → original_ratio=0.24 → 调整为 0.20（试仓）+ 止损 + 入场过滤
_process_signal:
  if signal.get("wisdom_filtered"): → 仅通知不下单
  suggested_ratio = signal.get("suggested_position_ratio") → 使用 wisdom 调整后的 0.20
  → 基于 0.20 计算下单数量
```
