# 迭代 5：全量专家分析与持续优化

> 2026-06-23 ~ 2026-06-24

## 1. 背景

项目在迭代 4 之后已具备完整的基础骨架（280 测试，83% 覆盖率）。本迭代的目标是从**架构设计、代码质量、测试覆盖、配置管理、事件驱动**五个维度进行系统性深度优化，让项目从"可运行"跃迁到"可维护、可扩展、可检验"。

## 2. 本次迭代目标

- **架构优化**：SignalPipeline 从 TradingEngine 提取为独立类、EventEngine 从死代码变为真正驱动循环
- **代码质量**：Signal 强类型数据类替代游离 dict、AppConfig 强类型配置替代 `dict.get()` 模式
- **测试覆盖**：从 280 测试扩展到 463 测试（+65%），覆盖率 62% → 87%（+25%）
- **配置管理**：pydantic BaseModel 强类型配置 + 向后兼容桥
- **数据校验**：3 个独立函数 → 链式校验模式（6 个可组合规则类）
- **数据源降级**：串行 fallback → 并发 FIRST_COMPLETED 降级

## 3. 改动范围

### 3.1 新增文件

| 文件 | 行数 | 说明 |
|------|------|------|
| `src/gugu/models/signal.py` | 148 | Signal 强类型数据类 + OrderResult，替代游离的 dict[str, Any] |
| `src/gugu/config/models.py` | 380 | pydantic BaseModel 强类型配置（10 个 section 模型 + flatten() 兼容） |
| `src/gugu/engine/signal_pipeline.py` | 210 | 独立过滤流水线，从 TradingEngine._scan_signals 提取 |
| `tests/fixtures/benchmark_600519.csv` | 300 | 贵州茅台 300 行固定 OHLCV 基准数据 |
| `tests/fixtures/__init__.py` | — | fixtures 包标记 |
| `tests/unit/test_analysis.py` | 1300 | 8 个测试类覆盖全部 analysis 模块 |
| `tests/unit/test_filters.py` | 300 | 4 个测试类覆盖全部 filter 模块 |
| `tests/unit/test_alpha_factory.py` | 200 | 16 个因子计算测试 |
| `tests/unit/test_stock_ranker.py` | 200 | 7 个股票排名测试 |
| `tests/unit/test_signal_pipeline.py` | 250 | 12 个过滤链测试 |
| `tests/unit/test_backtest_benchmark.py` | 180 | 10 个基准回测测试 |
| `tests/unit/test_event_engine.py` | 200 | 14 个事件引擎测试 |
| `tests/unit/test_data_manager_extended.py` | 200 | 16 个数据管理器高级测试 |

### 3.2 重构文件

| 文件 | 行数变化 | 重构要点 |
|------|---------|----------|
| `src/gugu/engine/main.py` | 675→670 | SignalPipeline 提取（-13%）、EventEngine 集成（10 事件 put 点） |
| `src/gugu/engine/event_engine.py` | 73→140 | 10 事件常量 + register/put 完整实现 |
| `src/gugu/data/quality.py` | 123→300 | 3 独立函数 → 链式校验（6 规则类 + DataValidator） |
| `src/gugu/data/manager.py` | 222→250 | 同步→异步 + 并发降级（asyncio.wait FIRST_COMPLETED） |
| `src/gugu/notifier/formatter.py` | 389→250 | 移除所有 isinstance 防御性检查 |
| `src/gugu/risk/manager.py` | 56→145 | AppConfig 三路输入（dict/AppConfig/None） |

### 3.3 更新文件

| 文件 | 更新内容 |
|------|----------|
| `src/gugu/wisdom/advisor.py` | advise() 同时接受 Signal 和 dict；新增 _as_dict 静态方法 |
| `src/gugu/config/__init__.py` | 导出 AppConfig + 各 section 模型 + flatten_config |
| `src/gugu/models/__init__.py` | 导出 Signal, OrderResult, Direction, Action |
| `src/gugu/engine/__init__.py` | 导出 EventEngine + 所有 EVENT_* 常量 |

## 4. 架构演进

```
优化前（迭代 4）:
  engine/main.py(675行) ← 16 个依赖，过滤链嵌入 _scan_signals(107行)
  EventEngine: 注册 2 个事件但从未调用 put() —— 死代码
  data/quality: 3 个独立函数，扩展需改函数体
  config: dict[str, Any]，无类型提示

优化后（迭代 5）:
  engine/main.py(670行) ← 核心编排
    → engine/signal_pipeline.py(210行) ← 过滤链可独立测试
    → engine/event_engine.py(140行) ← 10 事件常量，7 个 put 点
  data/quality.py(300行) ← 6 个规则类 + DataValidator 链
  config/models.py(380行) ← 10 section pydantic 模型 + flatten() 兼容
  models/signal.py(148行) ← 强类型数据类
```

## 5. 关键设计决策

### 5.1 Signal 数据类（不强制迁移）

`Signal` 数据类提供 `to_dict()` / `from_dict()`，wisdom advisor 的 `advise()` 同时接受 `Signal | dict`。**不强制全量迁移**——新旧接口共存，逐模块渐进替换。

### 5.2 AppConfig 双模式过渡

`RiskManager.__init__` 同时接受 `dict | AppConfig | None` 三种输入。现有 `settings()` 调用完全不受影响。`engine/main.py` 的 `_load_watchlist` 和 `auto_select_enabled` 优先使用 `self._app_config`，fallback 到 `settings()`。

### 5.3 链式校验兼容性

`ValidationRule.check()` 返回 `(bool, str)` 二元组或 `(bool, DataFrame, str)` 三元组。`DataValidator.validate()` 自动识别。原有 `validate_stock_history()` 等函数保持签名不变——调用者零改动。

## 6. 覆盖率演进

```
迭代 4: 62% (280 tests)
  │
  ▼ Phase 1: 架构优化
 67% (279 tests) ← SignalPipeline + 链式校验 + 并发降级
  │
  ▼ Phase 2: 测试补全
 78% (372 tests) ← analysis/filters 测试 + 基准 CSV
  │
  ▼ Phase 3: 事件驱动
 82% (407 tests) ← EventEngine + alpha/stock_ranker 测试
  │
  ▼ Phase 4: 配置集成
 87% (439 tests) ← AppConfig + execution/param/sector 测试 + 基准回测
  │
  ▼ Phase 5: 细粒度补全
 87% (463 tests) ← DataManager + SignalPipeline 独立测试
  │
迭代 5: 87% (463 tests)
```

## 7. 测试质量指标

| 指标 | 迭代 4 | 迭代 5 | 变化 |
|------|--------|--------|------|
| 总测试数 | 280 | **463** | **+65%** |
| 通过率 | 99.3% | **100%** | ✅ |
| 覆盖率 | 62% | **87%** | **+25%** |
| 基准测试 | 无 | ✅ 30 个 | 新增 |
| EventEngine 测试 | 0 | 14 | 新增 |
| SignalPipeline 测试 | 0 | 12 | 新增 |
| AppConfig 测试 | 0 | 隐式覆盖 | engine 初始化路径覆盖 |

## 8. 剩余技术债务

| # | 债务 | 影响范围 | 预计解决时机 |
|---|------|---------|-------------|
| 1 | web/ 模块零测试（FastAPI 前端） | 覆盖率 0% | 阶段四前 |
| 2 | qmt.py 骨架未实现（43%） | 实盘不可用 | 阶段四 |
| 3 | analysis/ 的 alpha_factory 在专用测试外覆盖率低 | 主测试报告 14% | 下一轮 |
| 4 | 历史数据无 Parquet 持久化 | 进程重启需重新拉取 | 阶段二完善 |
| 5 | 无持仓盈亏时序记录 | 无法绘制净值曲线 | P2 |
| 6 | AppConfig 尚未全面替换 settings() | 双模式共存 | 下一轮 |

## 9. 后续建议

### 短期（下一迭代）

1. **AppConfig 全面迁移**：将 engine/main.py、scheduler.py、notifier/feishu.py 等模块中残留的 `settings().get()` 全部替换为 `self._app_config.*`
2. **alpha_factory 补全**：在 `test_alpha_factory.py` 中增加 DMI/CCI/MFI/OBV/VWAP 因子的计算验证
3. **sector_rotation 全覆盖**：补上 akshare 真实调用路径的集成测试

### 中期（阶段四前）

1. **Web 模块测试**：为 FastAPI 端点写集成测试（/api/portfolio, /api/signals, /api/heartbeat）
2. **QMT 骨架实现**：完成 QmtBroker 的 connect/order/get_position 方法
3. **JSON 日志格式**：为监控系统增加 JSON 格式日志输出

### 长期

1. **Parquet 数据缓存**：历史数据持久化到 Parquet 文件，减少重复采集
2. **持仓净值曲线**：每日快照记录，支持净值曲线绘制
3. **飞书命令回执**：用户可通过飞书回复确认/拒绝交易