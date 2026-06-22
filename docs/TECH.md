# gugu 技术文档

> 自动化 A 股交易系统 · 技术架构与开发指南

---

## 一、技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 语言 | Python 3.11+ | 使用 3.12 运行 |
| 依赖管理 | uv | pyproject.toml |
| 数据源 | akshare（主）+ 新浪（降级） | 免费，使用不同底层 API 确保冗余 |
| 数据处理 | pandas / numpy | |
| 调度 | APScheduler | CronTrigger |
| 通知 | 飞书开放平台 API | 卡片消息 |
| Web | FastAPI + uvicorn | 仪表盘 API + 静态页面 |
| 实盘 | QMT（迅投）Python API | 阶段四启用 |
| HTTP | httpx | 异步客户端 |
| 配置 | pydantic-settings + YAML | |
| 日志 | loguru | |
| 测试 | pytest + pytest-asyncio + coverage | |
| Lint | ruff | |
| 类型检查 | mypy | |

## 二、目录结构

```
gugu/
├── CLAUDE.md                    # 项目工作规则（约束先行）
├── pyproject.toml               # 依赖与工具配置
├── .env.example                 # 环境变量模板
├── config/
│   ├── settings.yaml            # 主配置
│   └── strategy_defaults.yaml   # 策略默认参数
├── src/gugu/
│   ├── __init__.py
│   ├── config.py                # 配置加载（YAML + env）
│   ├── data/                    # 数据层
│   │   ├── manager.py           # DataManager（统一数据入口）
│   │   ├── quality.py           # 数据质量校验
│   │   ├── cache.py             # 内存缓存（2 分钟 TTL）
│   │   └── collectors/
│   │       ├── base.py          # BaseCollector 抽象
│   │       ├── akshare_collector.py  # 主源（东方财富 API）
│   │       └── fallback.py      # 降级源（新浪 API，与主源不同底层）
│   ├── models/
│   │   └── position.py          # Position 统一模型
│   ├── strategies/             # 策略层
│   │   ├── base.py              # Strategy 抽象基类
│   │   ├── trend.py             # 趋势（Turtle/DualMA/MACD/SAR）
│   │   ├── mean_revert.py       # 均值回归（RSI/KDJ/Bollinger）
│   │   ├── breakout.py          # 突破（Box/DualThrust）
│   │   ├── generator.py         # LLM 策略生成器
│   │   └── registry.py          # 策略注册表
│   ├── backtest/               # 回测引擎
│   │   ├── engine.py            # 回测引擎
│   │   ├── metrics.py           # 收益/夏普/回撤/胜率
│   │   └── report.py            # 回测报告格式化
│   ├── risk/                    # 风控
│   │   ├── manager.py           # 三级风控管理器
│   │   └── rules.py             # 枚举定义
│   ├── execution/              # 执行层
│   │   ├── base.py              # BaseBroker 抽象
│   │   ├── paper.py             # 模拟盘
│   │   └── qmt.py               # QMT 实盘（阶段四）
│   ├── notifier/               # 通知层
│   │   ├── feishu.py            # 飞书推送
│   │   └── formatter.py         # 消息格式化
│   ├── selector/               # 选股层
│   │   └── stock_selector.py    # 资金流 + 策略筛选
│   ├── wisdom/                  # 决策层
│   │   ├── advisor.py           # WisdomAdvisor（加载 skill + 增强信号）
│   │   └── skills/              # 炒股的智慧 8 个 skill（项目内置，随仓库提交）
│   ├── engine/                  # 主引擎
│   │   ├── main.py              # TradingEngine 主入口
│   │   ├── scheduler.py          # APScheduler 调度
│   │   └── signal_router.py      # 多策略信号融合
│   ├── web/                     # Web 仪表盘
│   │   ├── app.py               # FastAPI 应用
│   │   └── static/index.html    # 单页仪表盘
│   └── utils/
│       ├── log.py               # loguru 日志
│       └── calendar.py          # 交易日历
├── tests/
│   ├── conftest.py              # pytest fixture
│   └── unit/                    # 单元测试
├── scripts/
│   ├── run_backtest.py          # 回测入口
│   ├── run_paper.py             # 模拟盘入口
│   ├── run_web.py               # Web 仪表盘入口
│   ├── show_portfolio.py        # 持仓查看入口
│   └── run_live.py              # 实盘入口（阶段四）
└── docs/
    ├── PRODUCT.md               # 产品文档
    ├── TECH.md                  # 本文件
    ├── ITERATION_1.md           # 迭代1计划
    └── ITERATION_1_REVIEW.md    # 迭代1评审
```

## 三、核心架构

### 3.1 分层架构

```
数据层(data/)        采集器(akshare主+新浪降级，不同底层API) → 质量校验 → 内存缓存
       ↓
策略层(strategies/)  8个内置策略 + LLM自然语言生成器
       ↓
引擎层(engine/)      信号路由(多策略融合) → 主引擎 → 调度器
       ↓
风控层(risk/)        L1单股仓位 / L2日亏 / L3 T+1+涨跌停+停牌
       ↓
执行层(execution/)   模拟盘(PaperBroker) / QMT实盘(阶段四)
       ↓
通知层(notifier/)    飞书卡片(信号/日报/告警/回测/异常)
```

### 3.2 主业务流转

`TradingEngine.run_daily_cycle()`（[engine/main.py](file:///d:/aispace/gugu/src/gugu/engine/main.py)）：

1. 交易日判断 → 非交易日跳过
2. `broker.settle_t_plus_1()` + `risk.reset()`
3. `_update_prices()`：采集行情，更新持仓现价
4. `_scan_signals()`：自选股 + 自动选股 → 多策略信号融合 → wisdom 决策增强
5. `_check_stop_loss()`：遍历持仓，现价触及止损价则执行卖出
6. `_process_signal()`：逐信号 → 风控检查 → 下单 → 通知
7. `_check_daily_loss()`：L2 预警/熔断
8. `_write_heartbeat()`：心跳文件

### 3.3 数据源降级机制

`DataManager`（[data/manager.py](file:///d:/aispace/gugu/src/gugu/data/manager.py)）：

- 主源 `AkshareCollector`：使用 `ak.stock_zh_a_hist()`（东方财富 API）+ `ak.stock_zh_a_spot_em()`（东方财富实时）
- 降级源 `SinaCollector`：使用 `ak.stock_zh_a_daily()`（新浪 API）+ `ak.stock_zh_a_spot()`（新浪实时）
- **关键**：主源和降级源使用不同的底层 API，确保真正的数据冗余
- 主源连续失败 3 次 → 降级到 SinaCollector
- 冷却 5 分钟后尝试切回主源
- 降级源也做质量校验（`validate_stock_history`）

### 3.4 信号融合

`SignalRouter`（[engine/signal_router.py](file:///d:/aispace/gugu/src/gugu/engine/signal_router.py)）：

- `any`：任一策略产生信号即输出
- `majority`：多数策略同方向才输出
- `unanimous`：全部策略同方向才输出
- `hold` 策略不参与投票

## 四、开发指南

### 4.1 环境搭建

```bash
# 安装 uv（推荐）
pip install uv

# 同步依赖
uv sync

# 或用 pip
pip install -e ".[dev]"
```

### 4.2 质量检查命令

```bash
# Lint
uv run ruff check src tests scripts

# 类型检查
uv run mypy src/gugu

# 测试
uv run pytest tests/ -v --cov=src/gugu --cov-report=term-missing
```

### 4.3 添加新策略

1. 在 `src/gugu/strategies/` 下创建新文件
2. 继承 `Strategy` 基类，实现 `generate_signals(self, df)` 方法
3. 设置 `name` 类属性
4. 在 `strategies/registry.py` 注册

```python
from gugu.strategies.base import Strategy
import pandas as pd

class MyStrategy(Strategy):
    name = "my_strategy"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self.ensure_columns(df)
        df["signal"] = 0
        # 策略逻辑
        return df
```

### 4.4 配置说明

`config/settings.yaml`：

| 键 | 说明 | 默认值 |
|----|------|--------|
| watchlist | 自选股列表 | ["600519", "000858"] |
| auto_select | 自动选股 | false |
| initial_capital | 初始资金 | 1000000 |
| position_ratio | 单股仓位上限 | 0.3 |
| daily_loss_warn | 日亏预警 | 0.03 |
| daily_loss_halt | 日亏熔断 | 0.05 |

`.env`：

| 键 | 说明 |
|----|------|
| FEISHU_APP_ID | 飞书应用 ID |
| FEISHU_APP_SECRET | 飞书应用密钥 |
| FEISHU_CHAT_ID | 飞书群聊 ID |
| LLM_API_KEY | LLM API 密钥（策略生成用） |

## 五、测试

### 5.1 测试结构

```
tests/
├── conftest.py              # 公共 fixture（ohlcv_df）
├── unit/                    # 单元测试
│   ├── test_strategies.py   # 策略测试
│   ├── test_risk.py          # 风控测试
│   ├── test_execution.py     # 执行层测试
│   ├── test_engine.py        # 主引擎测试
│   ├── test_signal_router.py # 信号路由测试
│   ├── test_backtest.py      # 回测测试
│   ├── test_notifier.py      # 通知层测试
│   ├── test_data.py          # 数据层测试
│   ├── test_generator.py     # 策略生成器测试
│   ├── test_position.py      # Position 模型测试
│   └── test_utils.py         # 工具函数测试
└── integration/
    └── test_e2e_pipeline.py  # 端到端链路测试
```

### 5.2 覆盖率

当前覆盖率：**74%**（目标 ≥ 70%）

| 模块 | 覆盖率 | 说明 |
|------|--------|------|
| strategies/ | 95-100% | 充分覆盖 |
| risk/ | 95% | 充分覆盖 |
| execution/paper.py | 84% | 充分覆盖 |
| engine/main.py | 81% | 核心路径覆盖（含 wisdom 决策流程） |
| backtest/ | 84-94% | 充分覆盖 |
| data/quality.py | 92% | 零价/时效性检测已覆盖 |
| data/collectors/ | 28-33% | 依赖网络，待补充 mock 测试 |
| selector/ | 23% | 依赖网络，待补充 |
| wisdom/ | 80% | 决策逻辑已覆盖 |

## 六、已知限制与技术债务

| # | 限制 | 影响 | 计划 |
|---|------|------|------|
| 1 | 历史数据无 SQLite/Parquet 持久化 | 进程重启需重新拉取 | 阶段二完善 |
| 2 | 回测引擎未接入完整 RiskManager | 回测结果可能高估 | 阶段二完善 |
| 3 | 回测 O(n²) 信号生成 | 多股票多策略性能受限 | 后续向量化优化 |
| 4 | 交易日历降级仅覆盖当年 | 跨年运行可能误判 | 生成近 3 年节假日表 |
| 5 | 日志为纯文本 | 监控系统解析困难 | 后续增加 JSON 格式 |
| 6 | 降级源仅行情无资金流 | 主源故障时无法做资金流选股 | 后续实现独立资金流采集 |
