# gugu 技术文档

> 自动化 A 股交易系统 · 技术架构与开发指南

---

## 一、技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 语言 | Python 3.12+ | venv 使用 3.12.13 |
| 依赖管理 | uv | pyproject.toml + uv.lock |
| 数据源 | akshare（主）+ 新浪（降级） | 免费，使用不同底层 API 确保冗余 |
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
│   │   ├── models.py            # AppConfig + 10 section BaseModel
│   │   └── _legacy_config.py    # 旧 settings() 函数
│   ├── data/                    # 数据层
│   │   ├── manager.py           # DataManager（统一入口，async + 并发降级）
│   │   ├── quality.py           # 链式校验（6 个 ValidationRule 类）
│   │   ├── cache.py             # 内存缓存（2 分钟 TTL）
│   │   └── collectors/
│   │       ├── base.py          # BaseCollector 抽象
│   │       ├── akshare_collector.py  # 主源（东方财富 API）
│   │       └── fallback.py      # 降级源（新浪 API）
│   ├── models/
│   │   ├── position.py          # Position 统一模型
│   │   └── signal.py            # Signal 强类型数据类 + OrderResult
│   ├── strategies/              # 策略层
│   │   ├── base.py              # Strategy 抽象基类
│   │   ├── trend.py             # 趋势（Turtle/DualMA/MACD/SAR）
│   │   ├── mean_revert.py       # 均值回归（RSI/KDJ/Bollinger）
│   │   ├── breakout.py          # 突破（Box/DualThrust）
│   │   ├── generator.py         # LLM 策略生成器
│   │   └── registry.py          # 策略注册表
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
│   │   └── formatter.py         # 卡片格式化（类型安全）
│   ├── selector/                # 选股层
│   │   └── stock_selector.py    # 资金流 + 策略筛选
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
│   │   ├── execution_optimizer.py # 执行优化
│   │   ├── stage_detector.py   # P0 四阶段判断器（牛皮/升势/疯狂/最后）
│   │   ├── trailing_stop.py    # P0 浪谷递进移动止损引擎
│   │   ├── danger_signal.py    # P0 五大危险信号检测器
│   │   └── no_average_down.py  # P0 向下摊平检查器
│   ├── wisdom/                  # 决策层
│   │   ├── advisor.py           # WisdomAdvisor（LLM 决策 + 多视角路由 + fallback）
│   │   ├── book_router.py       # P0 BookPerspectiveRouter（28 本书多视角路由）
│   │   └── skills/              # 认知 SKILL
│   │       ├── (炒股的智慧 6 个 skill)
│   │       └── books/           # 仓颉蒸馏的 28 本股书 SKILL.md
│   ├── engine/                  # 主引擎
│   │   ├── main.py              # TradingEngine（核心编排）
│   │   ├── scheduler.py         # APScheduler 调度（4 次/日扫描）
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
    ├── ITERATION_1.md ~ ITERATION_5.md  # 迭代记录
```

## 三、核心架构

### 3.1 分层架构

```
数据层(data/)        采集器(akshare主+新浪降级) → 链式质量校验 → 内存缓存(2min TTL)
       ↓
策略层(strategies/)  8个内置策略 + LLM自然语言生成器
       ↓
规则引擎层(analysis/)  四阶段判断 → 危险信号检测 → 向下摊平检查（不可绕过）
       ↓
过滤层(filters/)     基本面 → 资金流 → 行业约束（仅买入信号）
       ↓
决策层(wisdom/)      BookPerspectiveRouter（28本书多视角）→ LLM决策 → fallback
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
   - 每个 symbol：策略路由 → SignalPipeline.process()（5 层过滤链）
   - Wisdom 决策增强（LLM 或 fallback）
7. `_process_signal()`：逐信号 → wisdom 过滤 → 风控检查 → 下单 → 通知 → 信号历史持久化
8. `_check_daily_loss()`：L2 预警/熔断（含事件推送）
9. `_write_heartbeat()`：心跳文件 + 历史记录追加

**事件驱动**：循环中 7 个关键节点通过 EventEngine.put() 产生事件（cycle_start/end、market_regime、signal、order_filled、risk_alert、stop_loss、daily_loss_warn/halt）。

### 3.3 信号过滤流水线

`SignalPipeline`（[engine/signal_pipeline.py](src/gugu/engine/signal_pipeline.py)）：

```
策略信号 → 路由 → L3 元数据注入(prev_close/is_st/is_suspended)
                    ↓
         [步骤0]  四阶段判断（StageDetector）
                    ↓
         [步骤0.5] 危险信号检测（DangerSignalDetector，仅买入，medium+ 过滤）
                    ↓
              1. 基本面过滤（仅买入）
                    ↓
              2. 资金流过滤（仅买入）
                    ↓
              3. 行业约束（仅买入 + 新建仓位）
                    ↓
         [步骤2.5] 向下摊平检查（NoAverageDownChecker，仅买入）
                    ↓
              4. 市场状态仓位修正（budget 已体现）
                    ↓
              5. Wisdom 决策层（LLM 或 fallback）
                    ↓
         [步骤3.5] 四阶段入场过滤（疯狂/最后阶段不入场）
                    ↓
              完整信号输出
```

可独立于 TradingEngine 测试，通过依赖注入替换各过滤组件。

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

- 主源 `AkshareCollector`：东方财富 API
- 降级源 `SinaCollector`：新浪 API（不同底层，真实冗余）
- 连续失败 3 次 → 降级，冷却 5 分钟后切回
- **并发降级**：主源失败后 `asyncio.wait(FIRST_COMPLETED)` 并发尝试所有降级源
- 所有源共享数据质量校验（DataValidator 链）

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
- `majority`：多数策略同方向才输出（默认）
- `unanimous`：全部策略同方向才输出

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
    execution: ExecutionConfig # mode, paper.capital, live.confirm_required, ...
    feishu: FeishuConfig       # enabled, notify_signal, report_times, ...
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
├── unit/                          # 单元测试（~40 个文件）
│   ├── test_analysis.py           # 8 个测试类覆盖全部 analysis 模块
│   ├── test_filters.py            # 4 个测试类覆盖全部 filter 模块
│   ├── test_alpha_factory.py      # 16 个因子计算测试
│   ├── test_stock_ranker.py       # 7 个排名测试
│   ├── test_signal_pipeline.py    # 12 个过滤链测试
│   ├── test_event_engine.py       # 14 个事件引擎测试
│   ├── test_backtest_benchmark.py # 10 个基准回测测试
│   ├── test_data_manager_extended.py # 16 个数据管理器测试
│   ├── test_strategies.py         # 策略测试
│   ├── test_strategies_benchmark.py # 30 个策略基准测试（所有7策略）
│   └── ...
└── integration/
    └── test_e2e_pipeline.py       # 端到端集成测试（~50 个）
```

### 7.2 覆盖率

当前覆盖率：**47%**（506 测试，全部通过）

> 注：迭代 6 新增 5 个 P0 模块（1,338 行新代码），覆盖率暂时下降。
> 新模块自身有 33 个专项测试覆盖，但整体行数增长导致百分比下降。

| 模块 | 覆盖率 | 说明 |
|------|--------|------|
| models/ | 94-100% | Signal 数据类 + Position 模型 |
| strategies/ | 95-100% | 7 个策略全覆盖 + 基准 CSV 断言 |
| risk/ | 86-95% | L1/L2/L3 全覆盖，含熔断持久化 |
| backtest/ | 84-94% | 含 enable_wisdom、L3 涨跌停 |
| engine/main.py | 76% | 核心编排，含 wisdom/事件推送 |
| engine/signal_pipeline.py | 69% | 独立测试覆盖过滤链全路径 |
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
| analysis/stage_detector.py | 43% | P0 四阶段判断器（新增） |
| analysis/trailing_stop.py | 43% | P0 移动止损引擎（新增） |
| analysis/danger_signal.py | — | P0 危险信号检测器（新增） |
| analysis/no_average_down.py | — | P0 向下摊平检查器（新增） |
| wisdom/book_router.py | 79% | P0 28 本书多视角路由器（新增） |

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
| 8 | AppConfig 尚未全面替换 settings() | 双模式共存 | 下一轮 |
| 9 | P0 规则引擎参数未经验证 | 止损比例/浪谷窗口/收紧比例均为经验值 | 需回测验证 |
| 10 | 移动止损未集成到回测引擎 | 无法回测验证止损效果 | 下一迭代 |
| 11 | Python 版本更新 | 文档标 3.14，实际 venv 使用 3.12.13 | 2026-06 已修 |
| 12 | BookPerspectiveRouter 静态路由 | 不考虑市场环境变化 | 需动态路由 |
| 13 | 危险信号"坏消息"依赖人工 | 无法自动检测利空消息 | 需新闻 API |
| 14 | SignalPipeline 新增 4 个检查点未独立测试 | 过滤链集成测试覆盖不足 | 下一轮 |

## 九、可观测性数据文件

| 文件 | 格式 | 写入时机 | 用途 |
|------|------|----------|------|
| `data/heartbeat.json` | JSON | 每次循环结束 | 最新状态，外部监控检查 |
| `data/heartbeat_history.jsonl` | JSONL | 每次循环追加 | 历史心跳，回溯崩溃时间点 |
| `data/signals_history.jsonl` | JSONL | 每次信号处理追加 | 信号决策全链路，回溯分析 |
| `data/paper_broker_state.json` | JSON | 每次下单后 | 持仓与交易持久化 |
| `data/paper_broker_state.json.bak` | JSON | 每次保存前备份 | 原子写入保护，崩溃回滚 |
| `data/risk_state.json` | JSON | L2 熔断触发/解除时 | 熔断状态持久化，防重启绕过 |
| `logs/gugu_*.log` | 文本 | 实时 | 详细运行日志（10MB 轮转，30 天保留） |