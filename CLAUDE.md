# CLAUDE.md

> gugu 交易系统专属工作规则
> 本文件约束在该项目目录下的所有 AI 助手行为

---

## 一、项目是什么

**gugu = 自动化A股交易系统**

- 输入：免费行情数据（akshare + 东方财富）
- 输出：按策略自动选股 → 买 → 卖 → 飞书实时通知
- 用户：给开发者自己用，核心场景是自动化交易 + 风险控制

### 融合来源

本项目融合三个参考项目的精华（参考代码在 `_refs/`，不提交）：

| 来源 | 复用能力 | 转化到 gugu 的模块 |
|------|---------|-------------------|
| realtime-flow | 资金流采集 + 信号 + 回测 | `src/gugu/data/` |
| MingCe | 18策略 + 三级风控 + 飞书 + 模拟盘 | `src/gugu/strategies/` `risk/` `notifier/` `execution/paper.py` |
| books2skill | "炒股的智慧"交易决策 skill | `src/gugu/wisdom/` |

### 五阶段架构（渐进式，不可跳级）

1. **策略固化**：自然语言 → Python 策略代码，存入 `strategies/`
2. **回测**：历史数据验证策略有效性
3. **模拟盘**：真实行情 + 模拟资金 + 飞书通知，零资金风险
4. **小额实盘**：QMT 接口 + 小号账户 + 人工确认机制
5. **正式实盘**：多数据源备份 + 监控告警 + 灾备 + 审计日志

**当前阶段：阶段一（策略固化）+ 阶段二（回测）+ 阶段三（模拟盘）并行搭建骨架**

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
│       │   ├── feishu.py        # 飞书推送
│       │   └── formatter.py     # 信号/日报/告警消息格式化
│       ├── selector/            # 选股层
│       │   └── stock_selector.py # 资金流 + 策略筛选
│       ├── wisdom/              # 决策层（books2skill skill 调用）
│       │   ├── advisor.py       # WisdomAdvisor（加载 skill + 增强信号）
│       │   └── skills/          # 炒股的智慧 8 个 skill（项目内置，随仓库提交）
│       ├── engine/              # 主引擎
│       │   ├── main.py          # 主引擎入口
│       │   ├── scheduler.py     # APScheduler 调度
│       │   └── signal_router.py # 多策略信号融合
│       ├── web/                 # Web 仪表盘
│       │   ├── app.py           # FastAPI 应用
│       │   └── static/index.html # 单页仪表盘
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
│   ├── run_web.py               # Web 仪表盘入口
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
- 版本：v0.1.0
- 阶段：阶段一（策略固化）+ 骨架搭建
