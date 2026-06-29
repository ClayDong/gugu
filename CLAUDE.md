# CLAUDE.md

> gugu A股信号监控系统专属工作规则
> 本文件约束在该项目目录下的所有 AI 助手行为

---

## 一、项目是什么

**gugu = A股选股信号监控系统**

- 输入：免费行情数据（akshare + 东方财富）
- 输出：策略信号 → 飞书实时通知 → 信号绩效验证
- 用户：给开发者自己用，核心场景是**选股信号监控 + 策略验证**
- **不是自动交易系统**：默认 signal_only 模式，只推送信号不下单

### 核心价值

1. **选股**：从全市场扫描 + 自选股池，多策略融合产生买卖信号
2. **通知**：信号实时推送到飞书，含策略/仓位/价格/智慧建议
3. **验证**：追踪信号产生后 1/3/5/10 日收益，统计命中率，对比策略表现
4. **监控**：守护进程保活，盘中 4 轮扫描，失败通知自动重试
5. **资金流**：08:00 盘前复盘 + 15:10 收盘日报，覆盖大盘/行业/个股资金流向
6. **基金跟踪**：15:35 基金净值监控（涨跌幅/波动率/回撤/止盈）

### 融合来源

本项目是 **唯一的活跃项目**。以下项目已完成或计划合并到 gugu：

| 项目 | 状态 | 关系 |
|------|------|------|
| **MakingMoney** | 🟡 冻结 | 全栈量化平台 v1.0（27 策略、Flask UI），所含功能已被 gugu 重构替代 |
| **MingCe（明策）** | 🟢 **已迁移** | 日报 Bot v1.5（5 时段飞书推送、五维宏观框架）。日报/基金/通知功能已迁入 gugu，MingCe 可归档 |
| **realtime-flow** | 🟢 **已迁移** | 资金流监测系统。资金流日报（大盘/行业/个股）已迁入 `src/gugu/notifier/flow_report.py` |
| **gugu** | 🟢 **唯一活跃** | 未来所有迭代只在此进行 |

> **决策依据**：详见 `docs/PROJECT_LANDSCAPE_ANALYSIS.md`（第一性原理 + 对抗性审查报告）

参考代码在 `_refs/`（仅查阅，不修改，不提交）：

| 来源 | 复用能力 | 转化到 gugu 的模块 |
|------|---------|-------------------|
| realtime-flow | 资金流采集 + 行业轮动分析 + 背离预警 | `src/gugu/notifier/flow_report.py` |
| MingCe | 五维宏观框架 + 定时推送 + LLM解读 | `src/gugu/macro/` + `src/gugu/notifier/fund_monitor.py` |
| MingCe | 18策略 + 三级风控 + 飞书 + 模拟盘 | `src/gugu/strategies/` `risk/` `notifier/` `execution/paper.py` |
| books2skill | "炒股的智慧"交易决策 skill | `src/gugu/wisdom/` |
| cangjie-skill/books | 28 本股书蒸馏 SKILL（仓颉认知植入术） | `src/gugu/wisdom/skills/books/` |

### 运行模式（execution.mode）

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `signal_only` | 只发信号通知，不下单 | **默认**，监控验证策略 |
| `paper` | 模拟下单（纸面交易） | 验证下单逻辑 |
| `live` | 实盘交易 | 需配置券商接口 |

### 五阶段架构（渐进式，不可跳级）

1. **策略固化**：自然语言 → Python 策略代码，存入 `strategies/`
2. **回测**：历史数据验证策略有效性
3. **信号监控**：真实行情 + 信号通知 + 绩效验证 ← **当前阶段**
4. **模拟盘**：PaperBroker 模拟下单 + 飞书通知
5. **实盘**：QMT 接口 + 人工确认机制

- **当前阶段：阶段三（信号监控 + 绩效验证）+ P0 认知引擎已落地**

---

## 二、目录结构约定

```
gugu/
├── CLAUDE.md                    # 本文件
├── .gitignore
├── .env.example                 # 环境变量模板（不提交 .env）
├── pyproject.toml               # 依赖管理（poetry/uv）
├── config/
│   ├── settings.yaml            # 主配置（数据源/飞书/风控参数）
│   └── strategy_defaults.yaml   # 策略默认参数
├── src/
│   └── gugu/
│       ├── __init__.py
│       ├── config.py            # 配置加载（YAML + env）
│       ├── data/                # 数据层
│       │   ├── manager.py       # DataManager（统一数据入口）
│       │   ├── quality.py       # 数据质量校验
│       │   ├── cache.py         # 本地缓存
│       │   └── collectors/      # 采集器
│       │       ├── base.py      # BaseCollector 抽象
│       │       ├── akshare_collector.py  # 主源
│       │       └── fallback.py   # 降级源（Sina/Tencent）
│       ├── models/              # 数据模型
│       │   └── position.py      # Position 统一模型
│       ├── strategies/          # 策略层
│       │   ├── base.py          # 策略抽象基类
│       │   ├── trend.py         # 趋势跟踪（海龟/双均线/MACD/SAR）
│       │   ├── mean_revert.py   # 均值回归（RSI/KDJ/布林带）
│       │   ├── breakout.py      # 突破（箱体/Dual Thrust/支撑阻力）
│       │   ├── generator.py     # LLM 自然语言策略生成器
│       │   └── registry.py      # 策略注册表
│       ├── backtest/            # 回测引擎
│       │   ├── engine.py
│       │   ├── metrics.py       # 收益/夏普/回撤/胜率
│       │   └── report.py
│       ├── risk/                # 风控
│       │   ├── manager.py       # 三级风控管理器
│       │   └── rules.py         # 单股上限/日亏预警/熔断
│       ├── execution/           # 执行层
│       │   ├── base.py          # 交易接口抽象
│       │   ├── paper.py         # 模拟盘
│       │   └── qmt.py           # QMT实盘（阶段四启用）
│       ├── notifier/            # 通知层
│       │   ├── feishu.py        # 飞书推送 + 通知重试队列
│       │   ├── formatter.py     # 信号/日报/风控/回测卡片
│       │   ├── fund_monitor.py  # 基金净值监控（001480/026211）
│       │   └── flow_report.py   # 资金流日报（08:00复盘/15:10日报）
│       ├── selector/            # 选股层
│       │   └── stock_selector.py # 资金流 + 策略筛选
│       ├── analysis/            # 高级分析 + P0 认知引擎
│       │   ├── stage_detector.py  # P0 四阶段判断器
│       │   ├── trailing_stop.py   # P0 移动止损引擎
│       │   ├── danger_signal.py   # P0 危险信号检测器
│       │   ├── no_average_down.py # P0 向下摊平检查器
│       │   ├── regime_detector.py # 多周期择时
│       │   └── ...               # 其他分析模块
│       ├── wisdom/              # 决策层（28 本书认知 + P0 规则引擎）
│       │   ├── advisor.py       # WisdomAdvisor（LLM 决策 + 多视角路由 + fallback）
│       │   ├── book_router.py   # BookPerspectiveRouter（28 本书多视角路由）
│       │   └── skills/          # 认知 SKILL
│       │       ├── (炒股的智慧 6 个 skill)
│       │       └── books/       # 28 本股书蒸馏 SKILL.md（仓颉认知植入术）
│       ├── engine/              # 主引擎
│       │   ├── main.py          # 主引擎入口
│       │   ├── scheduler.py     # APScheduler 调度
│       │   └── signal_router.py # 多策略信号融合
│       └── utils/
│           ├── log.py           # 日志
│           └── calendar.py      # 交易日历（节假日识别）
├── tests/
│   ├── unit/                    # 单元测试
│   ├── integration/             # 集成测试
│   ├── fixtures/                # 测试数据
│   └── conftest.py
├── scripts/
│   ├── run_backtest.py          # 回测入口
│   ├── run_paper.py             # 模拟盘入口
│   ├── show_portfolio.py        # 持仓查看入口
│   └── run_live.py              # 实盘入口（阶段四）
├── docs/                        # 文档
│   ├── PRODUCT.md               # 产品文档
│   ├── TECH.md                  # 技术文档
│   ├── ITERATION_1.md           # 迭代1计划
│   └── ITERATION_1_REVIEW.md    # 迭代1评审
└── _refs/                       # 参考项目（gitignore，不提交）
```

---

## 三、命名约定

- **包/模块**：小写下划线，如 `mean_revert.py`
- **类**：PascalCase，如 `TurtleStrategy` `FeishuNotifier`
- **函数/变量**：snake_case，如 `fetch_sector_flow` `signal_router`
- **常量**：UPPER_SNAKE，如 `MAX_POSITION_RATIO`
- **策略类**：`<方法>Strategy`，如 `TurtleStrategy` `BollingerStrategy`
- **配置键**：小写下划线，如 `feishu_app_id`

---

## 四、技术栈

- **语言**：Python 3.11+
- **依赖管理**：uv（优先）或 poetry
- **数据源**：akshare（主）+ 东方财富（主）+ 新浪/腾讯（降级）
- **回测**：自研轻量引擎（不引入 qlib 重依赖，除非必要）
- **调度**：APScheduler
- **通知**：飞书开放平台 API（卡片消息）
- **实盘**：QMT（迅投）Python API
- **数据存储**：SQLite（本地）+ Parquet（历史数据）
- **日志**：loguru
- **测试**：pytest + pytest-asyncio
- **配置**：pydantic-settings + YAML

---

## 五、数据源约定

- **主源**：akshare + 东方财富（免费）
- **降级源**：新浪财经 / 腾讯财经（仅行情，无资金流明细）
- **降级策略**：主源连续失败 3 次，跳过 5 分钟，降级源顶上，主源恢复后切回
- **数据质量**：采集后必须校验（缺失/异常值/时间戳），不合格数据不进系统
- **缓存**：实时数据本地缓存 2 分钟，历史数据落 SQLite/Parquet
- **交易日历**：优先 chinese_calendar 库，降级内置节假日表

---

## 六、风控规则（铁律，不可违反）

### 三级风控

| 级别 | 规则 | 动作 |
|------|------|------|
| L1 单股 | 单只股票仓位 ≤ 30% | 超限拒绝加仓 |
| L2 日亏 | 当日亏损 ≥ 3% 预警，≥ 5% 熔断 | 预警飞书通知，熔断停止交易 |
| L3 系统 | T+1 限制 / 涨跌停不交易 / 停牌不交易 | 系统自动过滤 |

### 实盘额外规则（阶段四起）

- API 权限最小化：只开交易，关闭存取款/转账
- IP 白名单：仅允许云服务器 IP
- 下单前人工确认：飞书回复"确认"后才执行
- 一键平仓应急脚本

---

## 七、飞书通知约定

### 消息类型

| 类型 | 触发 | 内容 |
|------|------|------|
| 信号通知 | 策略产生买卖信号 | 股票/方向/理由/建议仓位 |
| 每日日报 | 09:10 / 11:35 / 15:10 | 盘前/午盘/收盘总结 |
| 风控告警 | 触发风控规则 | 级别/详情/建议动作 |
| 回测报告 | 回测完成 | 收益/夏普/回撤/胜率 |
| 系统异常 | 采集失败/引擎崩溃 | 异常详情/恢复建议 |

### 消息格式

- 用飞书卡片消息（interactive card），不用纯文本
- 颜色语义：绿=买入/盈利，红=卖出/亏损，黄=预警，灰=信息
- 信号通知必须包含：股票代码/名称、方向、触发策略、触发理由、建议仓位、当前价

---

## 八、测试约定

- 每个模块必须有单元测试，覆盖率 ≥ 70%
- 策略必须有回测验证（历史数据）
- 实盘接口必须用模拟环境测试
- 测试数据放 `tests/fixtures/`，不依赖网络
- 命令：`pytest tests/ -v --cov=src/gugu`

---

## 九、Git 约定

- 仓库：`https://github.com/ClayDong/gugu.git`
- 默认分支：`main`
- **push 仅用于跨设备同步，等用户明确指令**
- commit message：英文，简洁描述变更意图（如 `feat: add turtle strategy` / `fix: feishu card format`）
- **不提交**：
  - `_refs/`（参考项目）
  - `.env`（密钥）
  - `__pycache__/` `*.pyc`
  - `.venv/` `venv/`
  - `data/` `logs/` `*.db`（本地数据）
  - `.pytest_cache/` `.coverage`

---

## 十、工作原则（来自全局）

1. **约束先行**：改任何约定前先改本文件
2. **UX 优先**：后端可以复杂，用户碰到的必须丝滑
3. **避免过度工程**：只做被请求的事，三行重复优于过早抽象
4. **避免向后兼容 hack**：直接改不要 shim
5. **风险控制优先**：风控规则不可被业务逻辑绕过
6. **第一性原理**：从问题本质出发，不因惯例照搬

---

## 元数据

- 创建日期：2026-06-22
- 版本：v0.2.0
- 阶段：阶段三（信号监控）+ P0 认知引擎落地（四阶段判断/移动止损/危险信号/禁止摊平/28 本书多视角）
