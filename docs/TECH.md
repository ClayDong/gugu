# gugu 技术文档

> 自动化 A 股交易系统 · 技术架构与开发指南

---

## 一、技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 语言 | Python 3.12+ | venv 推荐 3.12+，当前已验证 3.14 |
| 依赖管理 | uv | pyproject.toml + uv.lock |
| 数据源 | sina（主）+ eastmoney（降级）+ akshare（兜底）+ tencent（终极兜底） | 免费，四级冗余确保高可用 |
| 数据处理 | pandas / numpy / pyarrow | |
| 调度 | APScheduler | CronTrigger |
| 通知 | 飞书开放平台 API | 卡片消息（interactive card） |
| 实盘 | QMT（迅投）Python API | 阶段四启用 |
| HTTP | httpx | 异步客户端 |
| 配置 | pydantic-settings + pydantic BaseModel | settings.yaml + .env |
| 日志 | loguru | 10MB 轮转，30 天保留 |
| 测试 | pytest + pytest-asyncio + pytest-cov | |
| Lint | ruff | line-length=100 |
| 类型检查 | mypy | strict 模式 |

## 二、目录结构

```
gugu/
├── CLAUDE.md                    # 项目工作规则（约束先行）
├── pyproject.toml               # 依赖与工具配置
├── .env.example                 # 环境变量模板
├── config/
│   ├── settings.yaml            # 主配置（YAML）
│   └── strategy_defaults.yaml   # 策略默认参数
├── src/gugu/
│   ├── __init__.py
│   ├── config/                  # 配置层
│   │   ├── __init__.py          # 导出 + 向后兼容
│   │   └── models.py            # AppConfig + 10 section BaseModel
│   ├── _legacy_config.py        # 旧 settings() 函数（不在 config/ 内）
│   ├── data/                    # 数据层
│   │   ├── manager.py           # DataManager（统一入口，async + 并发降级）
│   │   ├── quality.py           # 链式校验（6 个 ValidationRule 类）
│   │   ├── cache.py             # 内存缓存（2 分钟 TTL）
│   │   ├── source_health.py     # 数据源健康监控（事件驱动，持久化健康摘要）
│   │   └── collectors/
│   │       ├── base.py          # BaseCollector 抽象
│   │       ├── akshare_collector.py  # 兜底源（东方财富 akshare 库）
│   │       ├── eastmoney_collector.py # 降级源（东方财富 push2 直连 HTTP）
│   │       ├── fallback.py      # 主源（新浪/网易 直连 HTTP）
│   │       └── tencent_collector.py  # 终极兜底源（腾讯 ifzq.gtimg.cn 直连，qfq 日 K）
│   ├── models/
│   │   ├── position.py          # Position 统一模型
│   │   └── signal.py            # Signal 强类型数据类 + OrderResult
│   ├── strategies/              # 策略层
│   │   ├── base.py              # Strategy 抽象基类
│   │   ├── trend.py             # 趋势（Turtle/DualMA/MACD）
│   │   ├── mean_revert.py       # 均值回归（RSI/KDJ/Bollinger）
│   │   ├── breakout.py          # 突破（Box/DualThrust）
│   │   ├── generator.py         # LLM 策略生成器
│   │   ├── registry.py          # 策略注册表
│   │   └── manifest.py         # 策略注册中心（统一清单加载器）
│   ├── backtest/                # 回测引擎
│   │   ├── engine.py            # 回测引擎（含 L3 涨跌停 + wisdom 可选）
│   │   ├── metrics.py           # 收益/夏普/回撤/胜率
│   │   └── report.py            # 回测报告格式化
│   ├── risk/                    # 风控
│   │   ├── manager.py           # 三级风控管理器（AppConfig 三路输入）
│   │   └── rules.py             # 枚举定义
│   ├── execution/               # 执行层
│   │   ├── base.py              # BaseBroker 抽象
│   │   ├── paper.py             # 模拟盘（原子写入 + 状态持久化）
│   │   └── qmt.py               # QMT 实盘（阶段四骨架）
│   ├── notifier/                # 通知层
│   │   ├── feishu.py            # 飞书推送（含 tenacity 重试）
│   │   ├── formatter.py         # 信号卡片（核心 + 子模块 re-export）
│   │   ├── formatter_reports.py # 日报/绩效/风控/回测/验证卡片
│   │   ├── formatter_special.py # 云策略/游资/冲突/概念/止盈卡片
│   │   ├── fund_monitor.py      # 基金净值监控
│   │   ├── flow_report.py       # 资金流日报 + 14:30 盘中决策报告
│   │   ├── flow_collector.py    # 资金流数据采集与分析引擎
│   │   ├── flow_formatter.py    # 资金流日报格式化/卡片构建
│   │   └── perspective_report.py # 十大视角分析飞书报告（v0.5.0）
│   ├── macro/                   # 宏观五维模块
│   ├── selector/                # 选股层
│   │   └── stock_selector.py    # 资金流 + 策略筛选
│   ├── screener/                # 尾盘选股器（P2-1 低吸逻辑 + 自动调优）
│   │   ├── terminal_screener.py # 14:30 全市场扫描（缩量回踩 MA20）
│   │   ├── params.py            # ScreenerParams 动态参数与硬边界
│   │   ├── auto_tune.py         # 基于验证历史的参数自动调优
│   │   ├── validator.py         # T+1 选股有效性验证
│   │   └── report.py            # 飞书选股报告卡片
│   ├── filters/                 # 过滤层
│   │   ├── fundamental.py       # 基本面过滤
│   │   ├── money_flow.py        # 资金流过滤
│   │   ├── industry_constraint.py # 行业分散约束
│   │   └── market_regime.py     # 市场状态识别
│   ├── analysis/                # 高级分析
│   │   ├── alpha_factory.py     # 26 个 Alpha 因子计算
│   │   ├── regime_detector.py   # 多周期择时
│   │   ├── position_controller.py # 仓位总控
│   │   ├── position_manager.py  # 持仓管理
│   │   ├── performance.py      # 绩效归因
│   │   ├── strategy_pool.py    # 策略池管理
│   │   ├── stock_ranker.py     # 个股综合评分
│   │   ├── sector_rotation.py  # 板块轮动
│   │   ├── param_optimizer.py  # 参数优化
│   │   ├── heuristic_optimizer.py # 启发式参数优化器（GA/PSO）
│   │   ├── optimization_pools.py  # 策略专用优化股票池（按策略类型匹配）
│   │   ├── signal_tracker.py      # 信号绩效追踪器（前瞻追踪 T+1/3/5/10 收益）
│   │   ├── execution_optimizer.py # 执行优化
│   │   ├── stage_detector.py   # P0 四阶段判断器（牛皮/升势/疯狂/最后）
│   │   ├── trailing_stop.py    # P0 浪谷递进移动止损引擎
│   │   ├── danger_signal.py    # P0 五大危险信号检测器
│   │   ├── no_average_down.py  # P0 向下摊平检查器
│   │   ├── dynamic_take_profit.py # 财叔动态止盈减仓引擎
│   │   └── perspectives/       # 十大视角分析引擎（v0.5.0）
│   │       ├── data_types.py    # StockDataContext + 评分结果模型
│   │       ├── scorer.py       # 10个投资大师视角评分函数
│   │       ├── engine.py       # PerspectiveEngine（单股/批量分析）
│   │       └── integrators.py  # 与现有模块的集成桥接
│   ├── wisdom/                  # 决策层
│   │   ├── advisor.py           # WisdomAdvisor（LLM 决策 + 多视角路由 + fallback）
│   │   ├── book_router.py       # P0 BookPerspectiveRouter（28 本书多视角路由）
│   │   └── skills/              # 认知 SKILL
│   │       ├── (炒股的智慧 6 个 skill)
│   │       └── books/           # 仓颉蒸馏的 28 本股书 SKILL.md
│   ├── engine/                  # 主引擎
│   │   ├── main.py              # TradingEngine（核心编排，EngineOpsMixin 混入）
│   │   ├── engine_mixin.py      # 信号扫描/处理/去重/心跳（从 main.py 提取）
│   │   ├── scheduler.py         # TradingScheduler（APScheduler 调度）
│   │   ├── scheduler_jobs.py    # 各定时任务安全执行方法（从 scheduler.py 提取）
│   │   ├── signal_router.py     # 多策略信号融合（any/majority/unanimous）
│   │   ├── signal_pipeline.py   # 过滤流水线（独立可测试）
│   │   └── event_engine.py      # 事件驱动引擎（10 事件类型）
│   ├── web/                     # Web 看板
│   │   ├── app.py               # FastAPI 应用
│   │   └── static/
│   │       └── index.html       # 前端页面
│   └── utils/
│       ├── log.py               # loguru 日志
│       └── calendar.py          # 交易日历（chinese_calendar + 内置表）
├── tests/
│   ├── conftest.py              # 全局 fixture（路径隔离）
│   ├── fixtures/                # 测试数据
│   │   ├── __init__.py
│   │   └── benchmark_600519.csv # 贵州茅台 300 行基准数据
│   ├── unit/                    # 单元测试（~40 个文件）
│   └── integration/             # 集成测试
│       └── test_e2e_pipeline.py # 端到端链路测试
├── scripts/
│   ├── run_backtest.py          # 回测入口
│   ├── run_paper.py             # 模拟盘入口
│   ├── show_portfolio.py        # 持仓查看
│   ├── run_web.py               # Web 看板
│   ├── run_live.py              # 实盘入口（阶段四）
│   ├── scan_stocks.py           # 选股扫描
│   └── compare_wisdom.py        # wisdom 效果对比
└── docs/
    ├── PRODUCT.md               # 产品文档
    ├── TECH.md                  # 本文件
    ├── ITERATION_1.md ~ ITERATION_6.md  # 迭代记录
```

## 三、核心架构

### 3.1 分层架构

```
数据层(data/)        采集器(sina主+akshare降级+eastmoney兜底) → 链式质量校验 → 内存缓存(2min TTL)
       ↓
策略层(strategies/)  8个内置策略 + LLM自然语言生成器
       ↓
规则引擎层(analysis/)  四阶段判断 → 危险信号检测 → 向下摊平检查（不可绕过）
       ↓
过滤层(filters/)     基本面 → 资金流 → 行业约束（仅买入信号）
       ↓
决策层(wisdom/)      BookPerspectiveRouter（28本书多视角）→ LLM决策 → fallback
       ↓
视角层(perspectives/) 10个投资大师视角评分 → 加权综合 → 木桶效应判定（v0.5.0）
       ↓
移动止损(analysis/)  浪谷递进止损引擎（每日动态更新止损价）
       ↓
引擎层(engine/)      信号路由(多策略融合) → 过滤流水线 → 事件驱动引擎
       ↓
风控层(risk/)        L1单股仓位 / L2日亏持久化熔断 / L3 T+1+涨跌停+停牌
       ↓
执行层(execution/)   模拟盘(PaperBroker原子写入) / QMT实盘(阶段四)
       ↓
通知层(notifier/)    飞书卡片(信号/日报/告警/回测/异常)
```

### 3.2 主业务流转

`TradingEngine.run_daily_cycle()`（[engine/main.py](src/gugu/engine/main.py)）：

1. 重入保护 + L2 熔断检查 + 交易日判断 → 跳过逻辑
2. T+1 结算 + 日初净值重置（仅新交易日）
3. `_update_prices()`：采集行情，更新持仓现价
4. `_check_stop_loss()`：**先止损再扫描信号**，确保信号基于最新持仓
5. 止损后立即 `_check_daily_loss()`：预防止损造成的大亏
6. `_scan_signals()`：自选股 + 自动选股（当日候选不污染 watchlist）
   - 市场择时 → 仓位预算计算
   - 每个 symbol：策略路由 → SignalPipeline.process()（12 步过滤链）
   - Wisdom 决策增强（LLM 或 fallback）
7. `_process_signal()`：逐信号 → **去重检查** → wisdom 过滤 → 风控检查 → 下单 → 通知 → 信号历史持久化 → **标记已通知**
8. `_check_daily_loss()`：L2 预警/熔断（含事件推送）
9. `_write_heartbeat()`：心跳文件 + 历史记录追加

**信号去重机制**：
- 通过 `_notified_signals` 字典记录当日已通知信号（key: `symbol_direction_strategy`）
- 持久化到 `data/notified_signals.json`，引擎重启后自动恢复
- 同一交易日、同方向、同策略的信号只通知一次，避免重复推送

### 3.3 信号过滤流水线

`SignalPipeline`（[engine/signal_pipeline.py](src/gugu/engine/signal_pipeline.py)）：

```
策略信号 → 路由 → L3 元数据注入(prev_close/is_st/is_suspended)
                    ↓
         [步骤0]   四阶段判断（StageDetector）
                    ↓
         [步骤0.5] 危险信号检测（DangerSignalDetector，仅买入）
                    ↓
         [步骤0.7] 量价背离检测（DivergenceDetector，仅买入）
                    ↓
         [步骤1]   基本面过滤（FundamentalFilter，仅买入）
                    ↓
         [步骤2]   资金流过滤（MoneyFlowFilter，仅买入）
                    ↓
         [步骤2.5] 板块轮动检查（SectorRotation，仅买入）
                    ↓
         [步骤3]   筹码集中度（HolderConcentrationFilter，仅买入）
                    ↓
         [步骤3.5] 另类Alpha因子（AltAlphaFilter，仅买入）
                    ↓
         [步骤4]   向下摊平检查（NoAverageDownChecker，仅买入）
                    ↓
         [步骤4.5] 行业约束（IndustryConstraint，仅买入）
                    ↓
  signal_only 模式:
         [步骤5]   Wisdom 决策层（仅 fallback）
                    ↓
              完整信号输出（signal_only 仅通知不下单）
  paper/live 模式:
         [步骤5]   四阶段入场过滤（StageDetector）
                    ↓
         [步骤6]   Wisdom 决策层（LLM 或 fallback）
                    ↓
         [步骤7]   十大视角评估（PerspectiveEngine）
                    ↓
         [步骤8]   综合入场判定（evaluate_entry_gate）
                    ↓
              完整信号输出
```

### 3.3.1 移动止损引擎

`TrailingStopEngine`（[analysis/trailing_stop.py](src/gugu/analysis/trailing_stop.py)）：

```
买入时: init_stop(entry_price) → 初始止损价 = entry_price × (1 - 8%)
         → 状态存储到 Position.trailing_stop (dict)

每日 _check_stop_loss 时:
  1. 读取 Position.trailing_stop → TrailingStopState
  2. update(state, df, danger_signals):
     a. 更新 highest_price
     b. 识别浪谷（N日窗口局部低点）
     c. 止损上移至最近浪谷（只能上移）
     d. 危险信号收紧止损（收紧 30%）
     e. 最大回撤兜底（>15% 触发 EXIT）
  3. 评估信号: EXIT/WARNING/ALERT/TIGHTEN/HOLD
  4. EXIT → 执行卖出 → 清除 trailing_stop 状态
  5. 状态写回 Position.trailing_stop
```

### 3.3.2 BookPerspectiveRouter

`BookPerspectiveRouter`（[wisdom/book_router.py](src/gugu/wisdom/book_router.py)）：

- 加载 28 本股书蒸馏 SKILL.md
- 按交易场景（entry/stop_loss/position_sizing 等）路由到对应书籍类别
- 每个场景最多返回 4 个视角，提取核心认知（信念/绝不做的事/思维习惯）
- 买入时补充陈江挺视角（交易纪律），卖出时补充利弗莫尔视角（止损铁律）
- 构建多视角认知上下文注入 LLM prompt

### 3.4 事件引擎

`EventEngine`（[engine/event_engine.py](src/gugu/engine/event_engine.py)）：

- 10 个预定义事件类型常量（EVENT_CYCLE_START/END、EVENT_MARKET_REGIME、EVENT_SIGNAL、EVENT_ORDER_SUBMITTED/FILLED、EVENT_RISK_ALERT、EVENT_STOP_LOSS、EVENT_DAILY_LOSS_WARN/HALT）
- 同步注册/分发，per-handler 异常隔离
- 交易循环中 7 个节点插入 put() 调用
- 当前注册 3 个事件处理器：risk_alert、stop_loss、order_filled

### 3.5 数据源降级机制

`DataManager`（[data/manager.py](src/gugu/data/manager.py)）：

- 主源 `SinaCollector`：新浪 API
- 降级源 `EastmoneyCollector`：东方财富 push2 直连
- 兜底源 `AkshareCollector`：东方财富 akshare 库
- 终极兜底 `TencentCollector`：腾讯财经 ifzq.gtimg.cn（仅 qfq 日 K，不依赖第三方库）
- 连续失败 3 次 → 降级，冷却 5 分钟后切回
- **并发降级**：主源失败后 `asyncio.wait(FIRST_COMPLETED)` 并发尝试所有降级源
- 所有源共享数据质量校验（DataValidator 链）

### 3.5.1 数据源健康监控（v0.8.1）

`DataSourceHealthMonitor`（[data/source_health.py](src/gugu/data/source_health.py)）事件驱动记录各数据源健康状态：

| 事件 | 触发时机 | 记录内容 |
|------|----------|----------|
| `success` | 某源请求成功 | source / latency_ms / symbol |
| `failure` | 某源请求失败 | source / error_type / error_msg |
| `degraded` | 从主源降级到备用源 | from_source → to_source / symbol |
| `recovered` | 冷却后切回主源成功 | source / downtime_seconds |

- **持久化**：`data/source_health_events.jsonl`（按日期滚动，最多 5000 行）
- **每日摘要**：`get_daily_summary()` 汇总各 source 的健康指标（成功率 / 平均延迟 / 降级次数 / 恢复次数）
- **静默优先**：降级/恢复事件只记录不主动推送，仅在收盘日报（15:10）附带健康摘要
- **线程安全**：`threading.Lock` 保护 JSONL 追加写入

### 3.6 数据质量校验链

`DataValidator` + `ValidationRule`（[data/quality.py](src/gugu/data/quality.py)）：

```
STOCK_HISTORY_VALIDATOR:
  RequiredColumnsRule → NonNegativeValuesRule → ZeroPriceRule
  → HighLowConsistencyRule → FreshnessRule → SortByDateRule

SECTOR_FLOW_VALIDATOR:
  RequiredColumnsRule → DeduplicateByColumnRule

STOCK_FLOW_VALIDATOR:
  RequiredColumnsRule → SortByDateRule
```

自定义校验链：`DataValidator().add_rule(rule1).add_rule(rule2).validate(df)`

### 3.7 信号融合

`SignalRouter`（[engine/signal_router.py](src/gugu/engine/signal_router.py)）：

- `any`：任一策略产生信号即输出
- `weighted`：加权评分融合（默认）
- `majority`：多数策略同方向才输出
- `unanimous`：全部策略同方向才输出

**买入成交量确认（v0.7.10+）**：
- 对 `buy` 方向信号，计算最新成交量 / 20 日均量比率
- 比率 ≥ 1.0：置信度不变
- 比率 0.8~1.0：置信度 × 0.85
- 比率 0.5~0.8：置信度 × 0.7
- 比率 < 0.5：置信度 × 0.5
- 卖出信号不应用此折扣（恐慌性抛售可能缩量）
- 目的：过滤缩量诱多、假突破，提高买入信号质量

### 3.8 策略池动态权重管理（v0.7.10+）

`StrategyPool`（[analysis/strategy_pool.py](src/gugu/analysis/strategy_pool.py)）：

- 维护各策略权重，支持 `weighted_fusion` 信号加权融合
- 根据近 20 笔交易收益动态调整权重：
  - 胜率 > 60%：权重 × 1.2
  - 胜率 < 30%：权重 × 0.7
  - 连续亏损 ≥ 5 次：以 `min(当前权重, 初始权重) × 0.05` 大幅降级，但不低于 `MIN_WEIGHT = 0.01`
  - 连续盈利 ≥ 3 次：权重 × 1.5，上限为初始权重的 2 倍
- 所有策略均保留启用状态，**不删除策略**，符合用户要求
- 权重持久化到 `data/strategy_pool_weights.json`（原子写入），进程重启自动恢复

### 3.9 信号可信度与结果指标契约（当前实现）

信号处理遵循“先判断是否可买，再区分是否执行”的原则：

1. `SignalPipeline` 为信号补充 `data_quality`，并对买入候选运行十大视角分析和统一入口门禁。
2. 只有十大视角门禁通过且 `data_quality == fresh` 的买入候选，才具备进入买入路径的资格；`stale`、`partial`、`invalid`、`unavailable` 只能保留为研究/通知结果。
3. `signal_only` 与 `track_live` 在引擎处理阶段只记录信号并通知，不提交订单。`track_live` 可读取真实持仓并执行相关检查，但不自动交易。
4. `paper`/`live` 才进入风控和执行分支；订单成功仅记录执行结果，不能直接推导盈利。

信号历史和绩效汇总必须区分以下指标：

| 字段 | 定义 | 数据来源/边界 |
|------|------|--------------|
| `execution_success_rate` | 订单执行成功率 | `execution_status`/订单结果；不作为盈利标签 |
| `forward_5d_signal_hit_rate` | 信号发出后五个交易日方向命中率 | 前向收益 `return_5d`；不要求订单成交 |
| `closed_trade_win_rate` | 完整平仓交易的盈利率 | 仅 `outcome_status == closed` 且有 `realized_pnl` |
| `realized_pnl` | 实际已实现盈亏 | 已平仓交易结果 |
| `estimated_signal_pnl` | 基于假设仓位的估算信号收益 | 研究/验证指标，不等同账户实际盈亏 |

因此，订单执行成功、前向信号命中和已实现盈利是三类独立事实，禁止互相替代或混用。

### 3.10 十大视角分析引擎（v0.5.0）

`PerspectiveEngine`（[analysis/perspectives/engine.py](src/gugu/analysis/perspectives/engine.py)）：

**核心理念**：将10位投资大师的认知框架量化为可计算的评分维度，每个视角独立评分（0-10分），加权汇总为综合评分。

**数据流**：
```
K线数据 / 信号字典 → build_context_from_kline() / build_context_from_signal()
    → StockDataContext（~50字段）
    → PerspectiveEngine.analyze()（同步，纯计算）
    → 10个评分函数独立评分
    → _aggregate() 加权汇总 + 木桶效应判定
    → PerspectiveAnalysisResult（含综合分/操作建议/价位）
```

**10个视角及权重**：

| 视角 | 权重 | 核心评估逻辑 |
|------|------|-------------|
| buffett | 0.15 | 护城河/ROE/PE/PB/低负债/内部人行为 |
| munger | 0.12 | 多元思维/盈利质量/现金流/资本配置 |
| soros | 0.10 | 反身性/市场环境/资金流向/趋势确认 |
| lynch | 0.10 | PEG/成长性/市值适配/分类（6类） |
| livermore | 0.10 | 趋势方向/关键点/成交量/仓位管理 |
| qiu_guo_lu | 0.10 | 行业格局/胜率/行业估值对比 |
| chen_jiang_ting | 0.08 | 趋势方向/止损/仓位/等待策略 |
| technical | 0.10 | 均线/RSI/MACD/布林带/52周位置 |
| investing_habits | 0.08 | 纪律性/风险控制/市场周期/耐心 |
| dashixiong | 0.07 | 五维框架（金油汇债G）/三层传导 |

**判定规则**：
- 单项 ≥ 6分通过，< 4分致命缺陷
- 综合分 ≥ 7.0 + 最低分 ≥ 4.0 + 通过视角 ≥ 7 → 整体通过
- 最低分决定木桶效应（单一维度极差则整体不通过）

**集成点**：
- `integrators.py`：`attach_perspective_to_signal()` 将评分附加到信号字典
- `filter_by_perspective()`：按最低分过滤信号列表
- `perspective_report.py`：飞书卡片报告生成
- `scheduler.py`：08:30 每日分析任务

**配置**：
```yaml
perspective:
  enabled: true
  perspectives: [buffett, munger, soros, lynch, livermore, qiu_guo_lu, chen_jiang_ting, technical, investing_habits, dashixiong]
  min_composite_score: 7.0
  min_perspective_score: 4.0
  min_pass_count: 7
  auto_notify: true

## 3.10 尾盘选股与自动调优

### 3.10.1 尾盘选股器

`TerminalScreener`（[screener/terminal_screener.py](src/gugu/screener/terminal_screener.py)）在每个交易日 14:30 执行全市场扫描，采用陈江挺《炒股的智慧》中的**缩量回踩 MA20 低吸逻辑**。

**8 条选股条件**：

| # | 条件 | 说明 |
|---|------|------|
| 1 | 当日涨幅 -2% ~ +1% | 回踩而非追涨 |
| 2 | 前 5 日有涨幅 ≥ 5% | 确认强势股基因 |
| 3 | 量比 < 1.0 | 缩量回踩才健康 |
| 4 | 收盘价 ≥ MA20 | 趋势未破 |
| 5 | MA20 > MA60 | 中期趋势向上 |
| 6 | 换手率 3% ~ 15% | 活跃但非过度投机 |
| 7 | 流通市值 50亿 ~ 500亿 | 避免小盘操纵与大盘滞涨 |
| 8 | 非尾盘异常冲高 | 排除诱多形态 |

**性能优化**：
- 快照硬过滤：先用行情快照的 量比/换手率/流通市值 字段粗筛，6400+ 只股票降至 50~100 只进入 K 线验证
- 并发 K 线获取：`Semaphore(5)` + `asyncio.gather`，5 倍加速
- 总超时 20 分钟硬上限：超时返回已扫描部分结果，避免阻塞日报
- HTTP 失败熔断：连续 5 只股票失败时中止该批

**市场状态过滤**：
- 通过 `MarketRegimeDetector` 判断当前市场状态（bull/rally/sideways/bear/crash）
- `bear`/`crash` 状态下跳过正式选股，返回观察列表或空仓提示
- `sideways` 状态下候选数量乘以 0.6 的修正系数，降低曝光

### 3.10.2 次日验证

`ScreenerValidator`（[screener/validator.py](src/gugu/screener/validator.py)）在 T+1 日两次验证选股结果：

- **09:35 盘中验证**：读取昨日选股记录，通过东财 push2 批量实时接口获取开盘价与盘中最高价，计算：
  - `open_premium` = (T+1 开盘价 - T 收盘价) / T 收盘价
  - `intraday_high_premium` = (T+1 盘中最高价 - T 收盘价) / T 收盘价
  - `benchmark_open_change` = 沪深 300 开盘涨幅
  - 超额收益 = 个股开盘溢价 - 基准开盘涨幅

- **15:05 收盘验证**：补充收盘价收益、盘中最大回撤、止损命中率，覆盖真实可执行收益与风险

验证结果写入 `data/screener_validation.jsonl`，用于自动调优与人工复盘。

### 3.9.3 参数自动调优

`ScreenerParams` + `auto_tune.py`（[screener/params.py](src/gugu/screener/params.py)、[screener/auto_tune.py](src/gugu/screener/auto_tune.py)）构成尾盘选股的自动调优闭环。

**参数管理**：
- 运行时参数从 `data/screener_params.json` 加载，不依赖 settings.yaml
- 每个阈值都有硬边界（`_BOUNDS`），防止调优过程进入非理性区间
- `apply_adjustment()` 返回新实例，原实例不变，便于 A/B 对比

**调优触发条件**（交易日 15:10）：

| 触发条件 | 阈值 | 动作 |
|----------|------|------|
| 近 N 日平均开盘超额收益 | < -0.5% | 收紧选股条件 |
| 胜率 | < 30% | 收紧选股条件 |
| 止损命中率 | > 40% | 收紧选股条件 |
| 近 N 日平均开盘超额收益 | > 0.3% | 放宽选股条件 |
| 胜率 | > 45% | 放宽选股条件 |

**调优规则**：
- 效果差时：降低量比上限、提高换手率下限、收窄涨幅区间 → 提高信号质量
- 效果好时：放宽量比/换手率/涨幅区间 → 增加样本数量，避免过度拟合
- 每次调整幅度小（步进式），防止参数抖动
- 样本不足（< 5 个交易日）或自动调优关闭时跳过

**调度集成**：
- `screener_1430`：14:30 执行选股
- `screener_validate_0935`：次日 09:35 盘中验证
- `screener_validate_1505`：15:05 收盘验证
- `screener_auto_tune_1510`：15:10 基于收盘验证数据自动调优参数

### 3.10.4 使用与监控

```bash
# 手动触发尾盘选股
uv run python -m gugu.screener.terminal_screener

# 手动触发次日验证
uv run python -m gugu.screener.validator

# 手动触发自动调优
uv run python -m gugu.screener.auto_tune
```

监控指标：
- `data/screener_validation.jsonl` 中的 `open_premium` / `intraday_high_premium` / `close_premium`
- `data/screener_params.json` 中的参数变化
- 飞书选股报告中的市场状态、候选数量、每只股票的通过条件

### 3.11 行业资金强度+动量确认策略（v1.2 已禁用）

`IndustryCapitalMomentumStrategy`（[strategies/industry_capital_momentum.py](src/gugu/strategies/industry_capital_momentum.py)）实现四层架构，将行业资金流向作为领先指标，结合动量确认过滤假突破。

> ⚠️ **v0.7.1 禁用**：回测表现最差（23个策略中排名末位，总收益-15.38%，夏普-0.92），禁用待重新设计。

**禁用原因分析**：

| 版本 | 总收益 | 夏普 | 问题 |
|------|--------|------|------|
| v1.0（行业级别） | 0笔交易 | - | 策略设计为行业级别，回测框架传入个股数据 |
| v1.1（个股适配） | -18.04% | - | 重构为个股时间序列，但逻辑失效 |
| v1.2（放宽退出） | -15.38% | -0.92 | 止损8%→12%，止盈20%→25%，仍是所有策略中最差的 |

**根因**：策略核心逻辑基于行业横截面排名（需多行业数据对比），改为个股时间序列后逻辑失效。

**四层架构**（设计思路，供后续重构参考）：

```
资金监控层 → 动量确认层 → 入场触发层 → 退出风控层
```

**1. 资金监控层**（识别资金聚集）：

| 指标 | 计算方式 | 权重/阈值 |
|------|----------|-----------|
| 资金强度排名 | 近5日净流入在行业中的滚动分位 | 排名前10%得分0.25 |
| 成交额占比变化 | 当前成交额占全市场比 - 20日均值 | 提升>3%得分0.15 |
| 连续流入天数 | 主力资金连续净流入交易日 | 3-5日更可靠 |

**2. 动量确认层**（过滤假流入）：

| 确认条件 | 标准 | 作用 |
|----------|------|------|
| MA5/MA20金叉 | MA5上穿MA20或已金叉状态 | 确认中期趋势转多 |
| 收盘价>MA5 | 站上5日均线 | 排除一日游行情 |
| 量能放大 | 当日成交量 > 20日均量 × 1.2 | 真实买盘支撑 |
| RSI合理区间 | RSI(14) 在 35-70 之间 | 避免超买追入 |
| MA5斜率向上 | MA5斜率 > -0.02 | 短期趋势健康 |

**3. 入场触发层**（7项评分体系，≥0.40分入场）：

```python
entry_score = (
    score_rank * 0.25 +        # 资金排名
    score_turnover * 0.15 +    # 成交额变化
    score_price_trend * 0.15 + # 价格趋势
    score_volume * 0.15 +      # 量能配合
    score_resonance * 0.10 +   # 个股共振
    score_rsi_zone * 0.10 +    # RSI区间
    score_not_hot * 0.10       # 非过热状态
)
```

**4. 退出风控层**（v1.2 放宽后）：

| 退出类型 | 触发条件 | 逻辑 |
|----------|----------|------|
| 趋势破坏 | 收盘价跌破MA10 | 短期趋势转弱 |
| 资金退潮 | 量能萎缩或价格连跌 | 资金开始撤离 |
| 过热止盈 | 20日涨幅>35%或RSI>78 | 情绪过热，获利了结 |
| 硬止损 | 从高点回撤12% | 单笔风险控制 |
| 止盈 | 20日涨幅>25% | 获利了结 |

**后续优化方向**：
1. 重新设计为纯行业级别策略（需单独的数据管道，获取全行业横截面数据）
2. 或完全重构入场逻辑，增加更多有效因子
3. 待市场环境变化后再验证

## 四、配置体系

### 4.1 配置分层

| 层次 | 方式 | 文件 | 用途 |
|------|------|------|------|
| 强类型模型 | AppConfig (pydantic BaseModel) | config/models.py | 新代码推荐：`cfg = AppConfig.from_settings()` |
| YAML 加载 | settings() 函数 | config/_legacy_config.py | 旧代码兼容：`settings().get("risk", {})` |
| 环境变量 | EnvSettings (pydantic-settings) | .env | 密钥/敏感配置 |
| 策略默认值 | strategy_defaults() | config/strategy_defaults.yaml | 各策略参数 |

### 4.2 AppConfig 模型（10 个 section）

```python
class AppConfig(BaseModel):
    watchlist: list[str]
    risk: RiskConfig           # max_position_ratio, daily_loss_warn/halt, ...
    data: DataConfig           # primary_source, fail_threshold, cache_ttl, ...
    strategy: StrategyConfig   # enabled, fusion_mode, min_confidence, ...
    execution: ExecutionConfig # mode, paper_capital, live_confirm_required, ...
    feishu: FeishuConfig       # enabled, notify_signal, report_times, ...
    perspective: PerspectiveConfig # enabled, perspectives, min_scores, ...
    scheduler: SchedulerConfig # timezone, scan_times, ...
    wisdom: WisdomConfig       # skill_dir, skill_names, ...
    fundamental: FundamentalConfig  # pe_min/max, pb_min/max, roe_min, ...
    log: LogConfig             # level, rotation, retention, ...
```

向后兼容：`cfg.flatten()` 返回旧 dict 格式，`settings()` 依然可用。

### 4.3 RiskManager 配置输入

```python
# 三种输入方式
RiskManager()                         # 自动加载 settings.yaml
RiskManager({"max_position_ratio": 0.2})  # dict 方式
RiskManager(AppConfig.from_settings())      # 强类型方式
```

## 五、数据模型

### 5.1 Signal 强类型数据类

```python
@dataclass
class Signal:
    symbol: str = ""
    direction: Literal["buy", "sell"] = "buy"
    price: float = 0.0
    name: str = ""
    strategy: str = ""
    strategies: list[str] = field(default_factory=list)
    reason: str = ""
    confidence: float = 1.0
    suggested_position_ratio: float = 0.0
    has_position: bool = False
    current_position_ratio: float = 0.0
    stop_loss_price: float | None = None
    prev_close: float = 0.0
    is_st: bool = False
    is_suspended: bool = False
    wisdom_filtered: bool = False
    filter_reason: str = ""
    wisdom: dict = field(default_factory=dict)
    wisdom_decision: dict = field(default_factory=dict)
    order_result: dict | None = None
    # ... 另有 market_context, fundamental, money_flow, industry_check

# 双向转换
signal = Signal.from_dict({"symbol": "600519", "direction": "buy"})
d = signal.to_dict()  # 兼容旧 dict 接口
```

Signal 在 `gugu.models` 导出。当前 wisdom advisor 的 `advise()` 同时接受 `Signal | dict`。逐步替换各模块中的 dict 为 Signal。

## 六、开发指南

### 6.1 环境搭建

```bash
# 安装 uv（推荐）
pip install uv

# 同步依赖
uv sync

# 或用 pip
pip install -e ".[dev]"
```

### 6.2 质量检查命令

```bash
# Lint
uv run ruff check src tests scripts

# 类型检查
uv run mypy src/gugu

# 测试（推荐）
uv run pytest tests/ -v --cov=src/gugu --cov-report=term-missing

# 仅运行新增/修改的测试
uv run pytest tests/unit/test_signal_pipeline.py -v
```

### 6.3 添加新策略

1. 在 `src/gugu/strategies/` 下创建新文件
2. 继承 `Strategy` 基类，实现 `generate_signals(self, df)` 方法
3. 设置 `name` 类属性
4. 在 `strategies/registry.py` 注册
5. 在 `config/strategy_defaults.yaml` 添加默认参数

```python
from gugu.strategies.base import Strategy
import pandas as pd

class MyStrategy(Strategy):
    name = "my_strategy"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._ensure_columns(df)
        df["signal"] = 0
        df["confidence"] = 0.0
        # 策略逻辑
        return df
```

### 6.3.1 主力资金跟随策略

v0.6.2 新增两只主力跟随策略，均继承 `Strategy` 基类、在 `strategies/registry.py` 注册、参数入口为 `config/strategy_defaults.yaml`，并通过 `config/strategy_manifest.yaml` 记录验证状态。生产环境中由 `SignalPipeline` 的过滤器进行二次确认，track_live 模式下只推送信号不下单。

#### `north_force_long` — 长线主力吸筹跟随

- **来源**：实战派「主力资金跟随」战法，模拟北向/公募等长线主力在趋势股中的吸筹行为。
- **核心逻辑**：
  - 趋势向上：`ma_trend` > `ma_long` 且短期均线斜率为正。
  - 缩量回踩：收盘价位于 `ma_trend` 上方，且与均线偏离 ≤ `pullback_to_ma_pct`（默认 5%），避免追涨。
  - 资金关注：`recent_momentum_window` 内涨幅 ≥ `recent_momentum_pct`，或 `recent_spike_window` 内出现 ≥ `recent_spike_pct` 的放量阳线。
  - 量能健康：当日成交量不高于 `vol_ma_window` 日均量的 `max_vol_ratio` 倍，排除历史天量。
- **卖点（状态机精确持仓）**：
  - 跌破入场低点 × (1 - `stop_loss_pct`)。
  - 连续 2 日收盘跌破 `ma_trend`，过滤单日噪音。
  - `ma_trend` 下穿 `ma_long` 趋势反转。
  - 高位巨量滞涨（`stagnation_vol_ratio` + 长上影 `max_upper_shadow_pct`）。
  - 时间止损：持仓超过 `time_stop_window` 日且收益 < `time_stop_threshold`。
  - 大阳线止盈：`daily_return` ≥ `take_profit_pct` × 0.6。
- **生产增强**：`AltAlphaFilter`（高管增持、龙虎榜机构净买、融资情绪）+ `HolderConcentrationFilter` 二次过滤，近似实现“跟随主力”。
- **当前状态**：v0.6.2 58 股 × 300 天回测未通过（收益 +0.08%，夏普 -1.23），`enabled: false`，作为 Walk-Forward 自动调参靶策略持续优化。

#### `dragon_tiger_short` — 短线龙虎榜反包跟随

- **来源**：A股实战派「涨停断板反包」战法，模拟机构/游资短线主力行为。
- **核心逻辑**：
  - 近 `limit_up_lookback` 日内出现涨幅 ≥ `limit_up_pct`（默认 7%）的涨停板。
  - 涨停次日未继续涨停（断板），并在 `max_break_days` 个交易日内被阳线反包。
  - 反包日收盘价突破断板日最高价，且为阳线、`ma_trend` 斜率为正、成交量 ≤ `max_vol_ratio` 倍均量。
- **评分项**：断板日缩量 0.25 + 反包量能 0.20 + 强反包（突破涨停日高点）0.20 + 趋势向上 0.15 + 时效 0.10 + 阳线 0.10，总分 ≥ `min_confidence`（默认 0.40，v0.7.9 从0.6下调）才触发信号。
- **卖点（状态机精确持仓）**：跌破入场低点止损、连续 2 日跌破 `ma_trend`、高位巨量滞涨、时间止损、大阳线止盈。
- **生产增强**：`AltAlphaFilter` 对龙虎榜机构净买因子（`lhb_inst_net`）二次确认，剔除机构净卖出的假反包。
- **当前状态**：v0.6.2 58 股 × 300 天回测观察通过（牛市胜率 43.9%，单笔平均 +6646 元），`enabled: true` 但仅在 `settings.yaml` 的 `bull/rally`  regime 下启用，track_live 模式收集 30+ 实盘信号后再评估扩大启用。

#### 自动调参与启发式优化集成（v0.7.3）

- **参数网格**：`scripts/optimize_params.py` 中已为两只策略注册完整参数网格（`STRATEGY_PARAM_GRIDS`）、配置键（`STRATEGY_CONFIG_KEYS`）和语义约束（`STRATEGY_PARAM_CONSTRAINTS`）。
- **解锁条件**：优化结果写回 `strategy_defaults.yaml` 必须通过 4 项门控：样本外 Sharpe > 0、参数稳定性 ≥ 50%、最大回撤 < 25%、平均收益 ≥ 0。
- **策略专用股票池（v0.7.10+）**：
  - 新增 `analysis/optimization_pools.py` 集中管理策略→股票池映射
  - 突破类（box_breakout / dual_thrust）：活跃中小盘（603501, 002371, 002472, 300033, 002230）
  - 趋势类（turtle / dual_ma）：各行业趋势龙头（600519, 300750, 002594, 300760, 002371）
  - 均值回归（bollinger / rsi_reversal）：高波动趋势股（300750, 601012, 600438, 603259, 002594）
  - 游资/短线（hot_money / first_board）：高换手活跃股（002472, 300033, 002230, 603501）
  - 价值投资（zhen_dahu）：大市值白马（600519, 000858, 600276, 300760, 601318）
  - 用户可在 `config/strategy_defaults.yaml` 中通过 `default_stock_pool` 覆盖默认池
- **Walk-Forward 调度**：`engine/scheduler.py` 的 `weekly_optimize_0300`（周日 03:00）对每个启用策略按类型匹配股票池，分别运行 `scripts/optimize_params.py --strategy <name> --multi-stock <pool> --days 500 --splits 3 --write`，失败仅记录日志，不污染生产参数。
- **启发式优化（v0.7.3 新增）**：
  - `analysis/heuristic_optimizer.py` 实现 GA（遗传算法）和 PSO（粒子群优化）两种启发式算法
  - **GAOptimizer**：锦标赛选择（tournament size=3）+ 单点交叉（crossover rate=0.7）+ 高斯变异（mutation rate=0.1，noise σ=10% range）+ 精英保留（elite ratio=0.1）
  - **PSOOptimizer**：惯性权重 w=0.7 + 个体最优引导 c1=1.5 + 全局最优引导 c2=1.5，速度限制在参数范围 20% 以内
  - **适应度函数**：多股聚合回测的夏普 40% + Calmar 30% + 胜率 20% + 收益 10% 加权评分
  - **参数约束**：支持自定义约束函数（如 short_window < long_window），约束违反返回 -inf
  - **CLI 入口**：`scripts/optimize_params.py` 新增 `run_heuristic_optimization()` + `--algorithm ga/pso` / `--population` / `--generations` / `--seed` 参数
  - **测试**：22 个单元测试覆盖 ParamSpace/GA/PSO/StrategyParamOptimizer/集成场景，行覆盖率 93%

#### 相关测试

- `tests/unit/test_north_force_long.py`：参数校验、信号生成、卖出优先级、数据不足边界。
- `tests/unit/test_dragon_tiger_short.py`：参数校验、涨停/断板/反包形态、卖出信号、状态机持仓。
- `tests/unit/test_config_consistency.py`：manifest / settings / registry 一致性校验。

### 6.4 添加新的过滤规则

1. 继承 `ValidationRule` 基类，实现 `check(df, symbol) -> tuple`
2. 添加到预构建的 `DataValidator`：

```python
from gugu.data.quality import DataValidator, ValidationRule

class MyRule(ValidationRule):
    def check(self, df, symbol=""):
        # 校验逻辑
        return True, "ok"  # 或 (False, "原因")

VALIDATOR = DataValidator().add_rule(MyRule()).validate(df)
```

### 6.5 添加新的事件类型

1. 在 `event_engine.py` 定义 `EVENT_MY_TYPE = "my_type"` 常量
2. 在需要的位置调用 `self._event_engine.put(EVENT_MY_TYPE, data)`
3. 在 `__init__` 中注册处理器：`register(EVENT_MY_TYPE, handler)`

## 七、测试

### 7.1 测试结构

```
tests/
├── conftest.py                    # 公共 fixture（路径隔离、OHLCV 数据）
├── fixtures/
│   ├── __init__.py
│   └── benchmark_600519.csv       # 基准测试数据（300 行固定 OHLCV）
├── unit/                          # 单元测试（~24 个文件）
│   ├── test_analysis.py           # 8 个测试类覆盖全部 analysis 模块
│   ├── test_filters.py            # 4 个测试类覆盖全部 filter 模块
│   ├── test_alpha_factory.py      # 16 个因子计算测试
│   ├── test_stock_ranker.py       # 7 个排名测试
│   ├── test_signal_pipeline.py    # 12 个过滤链测试
│   ├── test_event_engine.py       # 14 个事件引擎测试
│   ├── test_backtest_benchmark.py # 10 个基准回测测试
│   ├── test_data_manager_extended.py # 16 个数据管理器测试
│   ├── test_strategies_benchmark.py # 30 个策略基准测试（所有7策略）
│   └── ...
└── integration/
    └── test_e2e_pipeline.py       # 端到端集成测试（~50 个）
```

### 7.2 覆盖率

当前覆盖率：**70%**（818 测试，全部通过，v0.3.5）

| 模块 | 覆盖率 | 说明 |
|------|--------|------|
| models/ | 94-100% | Signal 数据类 + Position 模型 |
| strategies/ | 95-100% | 7 个策略全覆盖 + 基准 CSV 断言 |
| risk/ | 86-95% | L1/L2/L3 全覆盖，含熔断持久化 |
| backtest/ | 84-94% | 含 enable_wisdom、L3 涨跌停、认知引擎 |
| engine/main.py | 76% | 核心编排，含 wisdom/事件推送 |
| engine/signal_pipeline.py | 81% | 独立测试覆盖过滤链全路径 |
| engine/event_engine.py | 66% | 14 个独立测试覆盖 |
| engine/scheduler.py | 98% | 充分覆盖 |
| data/quality.py | 91% | 链式校验全路径 |
| data/manager.py | 67% | async 并发降级覆盖 |
| execution/paper.py | 89% | 原子写入 + T+1 覆盖 |
| notifier/ | 79-82% | 飞书推送 + 格式化覆盖 |
| selector/ | 92% | StockSelector 全覆盖 |
| wisdom/advisor.py | 85% | LLM + fallback 双模式 |
| filters/ | 71-88% | 4 个过滤器全覆盖 |
| analysis/ | 76-97% | 7/10 模块 90%+，3 个 70%+ |
| web/ | 0% | FastAPI 前端（阶段四前） |
| execution/qmt.py | 43% | QMT 骨架（阶段四实现） |

## 八、已知限制与技术债务

| # | 限制 | 影响 | 状态 |
|---|------|------|------|
| 1 | 历史数据无 Parquet 持久化 | 进程重启需重新拉取 | 未修复 |
| 2 | 交易日历降级仅覆盖当年 | 跨年运行可能误判 | 未修复 |
| 3 | 无持仓盈亏时序记录 | 无法绘制净值曲线 | 未修复（P2） |
| 4 | 飞书通知无回执机制 | 用户无法反馈"已知晓" | 未修复（P2） |
| 5 | 降级源仅行情无资金流 | 主源故障时自动选股跳过 | 未修复 |
| 6 | Web 前端零测试 | 覆盖率 0% | 阶段四前 |
| 7 | 日志为纯文本 | 监控系统解析困难 | 未修复 |
| 8 | AppConfig 尚未全面替换 settings() | 双模式共存 | ✅ 已修复（v0.3.0） |
| 9 | P0 规则引擎参数未经验证 | 止损比例/浪谷窗口/收紧比例均为经验值 | 需回测验证 |
| 10 | 移动止损未集成到回测引擎 | 无法回测验证止损效果 | ✅ 已修复（v0.3.0） |
| 11 | Python 版本更新 | 文档标 3.14，实际 venv 使用 3.12.13 | 2026-06 已修 |
| 12 | BookPerspectiveRouter 静态路由 | 不考虑市场环境变化 | 需动态路由 |
| 13 | 危险信号"坏消息"依赖人工 | 无法自动检测利空消息 | 需新闻 API |
| 14 | SignalPipeline 新增 4 个检查点未独立测试 | 过滤链集成测试覆盖不足 | ✅ 已覆盖（v0.3.0） |

### 八之一、v0.3.0 新增修复

| # | 修复 | 涉及文件 |
|---|------|----------|
| 15 | 配置双轨制消除：13 模块迁移到 `get_config()` 单例 | `config/__init__.py`, `main.py`, `risk/manager.py`, `data/manager.py`, `engine/*.py`, `execution/paper.py`, `strategies/registry.py`, `backtest/engine.py`, `wisdom/advisor.py`, `filters/*.py`, `analysis/position_controller.py`, `analysis/strategy_pool.py`, `analysis/sector_rotation.py` |
| 16 | `_STOCK_NAMES` 三份重复去重 | 新增 `data/stock_names.py`，`main.py`/`signal_pipeline.py`/`manager.py` 统一引用 |
| 17 | ATR 自适应止损优先 + wisdom 止损上限补齐 | `main.py:_process_signal` |
| 18 | 熔断跨日同步：`last_reset_date` 持久化 | `risk/manager.py`, `main.py:run_daily_cycle` |
| 19 | 止损检查批量拉取 `fetch_stock_history_batch` | `data/manager.py`, `main.py:_check_stop_loss` |
| 20 | 板块轮动缓存 TTL 3600s | `engine/signal_pipeline.py:_check_sector_rotation` |
| 21 | PlausiblePriceRule 成交量辅助校验 | `data/quality.py` |
| 22 | 认知引擎异常不再静默吞（`logger.debug`） | `backtest/engine.py:268,303` |
| 23 | 周线 resample 前视偏差（`closed="left"`） | `analysis/regime_detector.py:86` |
| 24 | 仓位基准改为当前净值 | `backtest/engine.py:_execute_buy` |
| 25 | 信号值 NaN/浮点校验到 `{-1,0,1}` | `backtest/engine.py:193` |
| 26 | 资金流默认保守拒绝 | `filters/money_flow.py:57` |
| 27 | 选股器/筛股器 per-stock 超时 | `selector/stock_selector.py`, `screener/terminal_screener.py` |
| 28 | `is_tradable` 传入方向参数 | `selector/stock_selector.py:94` |
| 29 | 错误消息密钥脱敏纵深防御 | `notifier/formatter.py`, `notifier/feishu.py` |
| 30 | `formatter.py` `_STOCK_NAMES` 去重 | `notifier/formatter.py` |

### 八之二、v0.3.0 产品化（迭代 22）

| # | 功能 | 涉及文件 |
|---|------|----------|
| 31 | 信号卡片 30 秒摘要行（置信度/仓位/止损/止盈） | `notifier/formatter.py` |
| 32 | ATR 波动率动态仓位调整 | `engine/main.py:_process_signal` |
| 33 | 止盈双目标方案 + ATR 自适应校准 | `wisdom/advisor.py:_compute_take_profit` |
| 34 | 信号融合 majority→weighted | `config/settings.yaml` |
| 35 | 5 策略启用（Bollinger+RSI 加入） | `config/settings.yaml` |
| 36 | 组合相关性监控（>85% 告警） | `risk/manager.py:check_correlation` |
| 37 | 组合最大回撤熔断（15%） | `risk/manager.py:check_portfolio_drawdown` |
| 38 | Walk-forward 参数优化 + 网格扫描 | `backtest/engine.py:walk_forward_optimize` |
| 39 | 仓位分层递进（试仓→加仓→确认加仓） | `wisdom/advisor.py:_advise_fallback` |
| 40 | Web 信号绩效 `/api/signals/performance` | `web/app.py` |
| 41 | Web 相关性检查 `/api/portfolio/correlation` | `web/app.py` |

### 八之三、仍待实施（不依赖付费数据）

| # | 功能 | 优先级 | 状态 |
|---|------|--------|------|
| 1 | 实盘模拟验证（signal_only→paper 模式，积累 3 个月交易记录） | P0 | ⏳ |
| 2 | 信号绩效自动推送（飞书卡片附历史胜率） | P1 | ✅ v0.3.1 |
| 3 | 策略参数敏感性分析（回测优化止损/窗口参数） | P1 | ⏳ |
| 4 | 交易日志 + 归因分析（每笔完成交易后自动分析盈亏原因） | P1 | ⏳ |
| 5 | 盘前预案推送（跳空高开/低开应对策略） | P2 | ⏳ |
| 6 | 流动性仓位约束（基于日均成交额限制单笔仓位） | P2 | ⏳ |

### 八之四、v0.3.1 修复（迭代 23）

| # | 修复 | 涉及文件 |
|---|------|----------|
| 42 | RiskManager.check_correlation async 化（修复 FastAPI 死循环） | `risk/manager.py` |
| 43 | 卡片 subtitle→element 渲染（修复 Feishu 静默丢弃） | `notifier/formatter.py:_card` |
| 44 | Web /api/halt/reset 使用 _web_risk 单例 | `web/app.py` |
| 45 | _portfolio_peak 类变量→实例变量 | `risk/manager.py` |
| 46 | 信号死代码移除（signal_tracker.py:190） | `analysis/signal_tracker.py` |
| 47 | walk_forward_optimize 空网格 best_sharpe 兜底 | `backtest/engine.py` |
| 48 | 信号历史胜率注入飞书卡片（_inject_performance_summary） | `engine/main.py` |

### 八之五、v0.3.7 修复（迭代 8：多股聚合优化 + 自优化闭环）

| # | 修复 | 涉及文件 |
|---|------|----------|
| 49 | P1-9: df.iloc 逐行访问性能瓶颈（cProfile 575s→129s，4.5x 加速） | `backtest/engine.py` |
| 50 | P1-2: 测试隔离 4 个 monkeypatch 漏洞（paper.STATE_FILE / config.PROJECT_ROOT / record_signal_history / _notified_signals_file） | `tests/conftest.py` |
| 51 | P1-5: 状态文件原子写入（heartbeat + take_profit_state 改用 atomic_write_text） | `engine/main.py`, `analysis/dynamic_take_profit.py` |
| 52 | P1-7: 调度器新增 weekly_optimize_0300 任务（周日 03:00 多股聚合 Walk-Forward，6 只行业代表股，4 项解锁条件门控） | `engine/scheduler.py` |
| 53 | P1-10: SinaCollector 双源失败不抛异常 → 主源无法触发降级 | `data/collectors/fallback.py` |
| 54 | P1-11: money_flow_follow 重设计混合模式（hard filter + soft score，震荡市 0 误触发） | `strategies/money_flow_follow.py` |
| 55 | P1-8: leopard_three_factors 补入 manifest + 配置一致性测试（防 settings/manifest 分裂） | `config/strategy_manifest.yaml`, `tests/unit/test_manifest.py` |

### 八之十、v0.7.3 新增（启发式参数优化系统）

| # | 功能 | 涉及文件 |
|---|------|----------|
| 81 | 遗传算法优化器（锦标赛选择+单点交叉+高斯变异+精英保留） | `analysis/heuristic_optimizer.py` |
| 82 | 粒子群优化器（惯性权重+个体/全局最优引导+速度限制） | `analysis/heuristic_optimizer.py` |
| 83 | StrategyParamOptimizer 封装（参数网格→ParamSpace→适应度函数） | `analysis/heuristic_optimizer.py` |
| 84 | 多股聚合适应度函数（夏普40%+Calmar30%+胜率20%+收益10%） | `analysis/heuristic_optimizer.py` |
| 85 | 参数约束函数支持（自定义语义约束过滤） | `analysis/heuristic_optimizer.py` |
| 86 | CLI 入口（run_heuristic_optimization + --algorithm/--population/--generations） | `scripts/optimize_params.py` |
| 87 | 调度器支持启发式优化任务 | `engine/scheduler.py` |
| 88 | 22 个启发式优化器单元测试 | `tests/unit/test_heuristic_optimizer.py` |

### 八之六、v0.3.8 修复（迭代 9：炒股专家审查）

| # | 修复 | 涉及文件 |
|---|------|----------|
| 56 | P0-1: 尾盘选股器从"涨幅 3-5% 追涨"改为"缩量回踩 MA20 低吸"（8 条件低吸逻辑） | `screener/terminal_screener.py` |
| 57 | P0-2: 移动止损去掉递减逻辑 + 仅创新高重置 + 放量跌破浪谷直接 EXIT | `analysis/trailing_stop.py` |
| 58 | P0-3: 双均线金叉加成交量确认（量比≥1.5）+ 趋势强度过滤（MA20>MA60+斜率>0），可配置关闭 | `strategies/trend.py` |
| 59 | P1-4: 资金流过滤器数据缺失时默认拒绝（paper/live 模式 pass=False） | `filters/money_flow.py` |
| 60 | P1-5: 风控增加持仓时间风控（30 天未盈利警告减半仓，60 天未盈利强制清仓） | `risk/manager.py` |
| 61 | P1-7: 大市过滤增加中证 500 + 创业板指（三级冗余，任一破 MA200 则中小盘禁止买入） | `engine/market_filter.py` |
| 62 | P2-8: 周日 20:00 周复盘推送（汇总本周信号统计推送飞书） | `engine/scheduler.py` |

### 八之八、v0.5.0 新增（十大视角分析系统）

| # | 功能 | 涉及文件 |
|---|------|----------|
| 64 | 十大视角分析引擎核心模块（data_types/scorer/engine/integrators） | `analysis/perspectives/*.py` |
| 65 | 飞书报告卡片生成器 | `notifier/perspective_report.py` |
| 66 | 配置集成（PerspectiveConfig + settings.yaml） | `config/models.py`, `config/settings.yaml` |
| 67 | 调度器集成（08:30 十大视角日报） | `engine/scheduler.py` |
| 68 | 105个全面单元测试 | `tests/unit/test_perspectives.py` |

### 八之八、v0.5.1 新增（视角信号流融合 + 信号质量控制）

| # | 功能 | 涉及文件 |
|---|------|----------|
| 69 | 十大视角接入信号过滤流水线（SignalPipeline 步骤9） | `engine/signal_pipeline.py` |
| 70 | signal_only 附加视角评分 + paper/live 硬过滤 | `engine/signal_pipeline.py` |
| 71 | 信号去重内存管理（保存前清理7天过期） | `engine/main.py` |
| 72 | SignalAggregationConfig（min_interval/max_daily/batch） | `config/models.py` |
| 73 | E2E 集成测试骨架（20+模块导入验证） | `tests/integration/test_e2e_pipeline.py` |
| 74 | POST /api/stop_loss/{symbol} Web API | `web/app.py` |

### 八之九、v0.5.2 新增（宏观增强 + 引擎拆分）

| # | 功能 | 涉及文件 |
|---|------|----------|
| 75 | MacroRegimeEnhancer（金/油/汇/债/G→仓位上限调整） | `analysis/macro_regime_enhancer.py` |
| 76 | detect_with_macro() 便捷方法 | `analysis/regime_detector.py` |
| 77 | DailyReportManager 提取（send_daily_report → 独立类） | `notifier/daily_report_manager.py` |
| 78 | OrderManager + StopLossManager 提取到 execution/ | `execution/order_manager.py`, `execution/stop_loss_manager.py` |
| 79 | main.py 1142→999 行（拆出 ~180 行） | `engine/main.py` |
| 80 | 宏观增强 5 个单元测试 | `tests/unit/test_macro_regime_enhancer.py` |

### 八之七、v0.3.9 修复（迭代 10：行业景气度评分）

| # | 修复 | 涉及文件 |
|---|------|----------|
| 63 | P1-6: 基本面增加行业景气度评分（涨跌幅40%+主力净流入30%+换手率15%+量比15%，<10 硬拦截行业衰退，<30 软警告，日内缓存复用） | `filters/fundamental.py` |

## 九、可观测性数据文件

| 文件 | 格式 | 写入时机 | 用途 |
|------|------|----------|------|
| `data/heartbeat.json` | JSON | 每次循环结束（原子写入） | 最新状态，外部监控检查 |
| `data/heartbeat_history.jsonl` | JSONL | 每次循环追加 | 历史心跳，回溯崩溃时间点 |
| `data/signals_history.jsonl` | JSONL | 每次信号处理追加 | 信号决策全链路，回溯分析 |
| `data/paper_broker_state.json` | JSON | 每次下单后 | 持仓与交易持久化 |
| `data/paper_broker_state.json.bak` | JSON | 每次保存前备份 | 原子写入保护，崩溃回滚 |
| `data/risk_state.json` | JSON | L2 熔断触发/解除时 | 熔断状态持久化，防重启绕过 |
| `data/notified_signals.json` | JSON | 每次信号通知后 | 信号去重记录（symbol_direction_strategy → date），防重复推送 |
| `data/take_profit_state.json` | JSON | 止盈状态变更时（原子写入） | 财叔动态止盈状态持久化 |
| `logs/gugu_*.log` | 文本 | 实时 | 详细运行日志（10MB 轮转，30 天保留） |