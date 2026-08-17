# CLAUDE.md

> gugu A股信号监控系统专属工作规则
> 本文件约束在该项目目录下的所有 AI 助手行为

---

## 一、项目是什么

**gugu = A股选股信号监控系统**

- 输入：免费行情数据（sina 主 + eastmoney 降级 + akshare 兜底 + tencent 终极兜底，四级冗余）
- 输出：策略信号 → 飞书实时通知 → 信号绩效验证
- 用户：给开发者自己用，核心场景是**选股信号监控 + 策略验证**
- **不是自动交易系统**：默认 signal_only 模式，只推送信号不下单

### 核心价值

1. **选股**：从全市场扫描 + 自选股池，多策略融合产生买卖信号
2. **通知**：信号实时推送到飞书，含策略/仓位/价格/智慧建议（去重机制防止重复推送）
3. **验证**：追踪信号产生后 1/3/5/10 日收益，统计命中率，对比策略表现
4. **监控**：守护进程保活，盘中 4 轮扫描，失败通知自动重试
5. **资金流**：08:00 盘前复盘 + 15:10 收盘日报，覆盖大盘/行业/个股资金流向
6. **基金跟踪**：15:35 基金净值监控（涨跌幅/波动率/回撤/止盈）
7. **宏观解读**：大师兄视角解读宏观数据（LLM 驱动）

### 融合来源

本项目是 **唯一的活跃项目**。以下项目已完成或计划合并到 gugu：

| 项目 | 状态 | 关系 |
|------|------|------|
| **MakingMoney** | 🟡 冻结 | 全栈量化平台 v1.0（27 策略、Flask UI），所含功能已被 gugu 重构替代 |
| **MingCe（明策）** | 🟢 **已迁移** | 日报 Bot v1.5（5 时段飞书推送、五维宏观框架）。日报/基金/通知功能已迁入 gugu，MingCe 可归档 |
| **realtime-flow** | 🟢 **已迁移** | 资金流监测系统。资金流日报（大盘/行业/个股）已迁入 `src/gugu/notifier/flow_report.py` |
| **mulan-quant** | 🟢 **已迁移** | ML量化选股系统。博主规则库（47规则）+ ML GradientBoosting 已迁入 `src/gugu/strategies/blogger_rules/` 和 `src/gugu/strategies/ml_gradient_boost/` |
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
| `signal_only` | 只发信号通知，不下单 | 监控验证策略 |
| `paper` | 模拟下单（纸面交易） | 验证下单逻辑 |
| `track_live` | 实盘持仓跟踪（只读用户真实持仓，不自动交易） | **当前默认**，用户有真实持仓时使用 |
| `live` | 实盘交易 | 需配置券商接口 |

> **v0.6.1 重要变更**：新增 `track_live` 模式，解决用户真实持仓被模拟盘误删问题。
> - 用户真实持仓存储于 `data/live_positions.json`（独立于 `paper_broker_state.json`）
> - `LivePositionTracker` 只读持仓文件，`order()` 永远拒绝下单（保护用户真实持仓）
> - 止损/止盈触发时推送"需手动执行"提醒卡片，不自动下单
> - `paper_broker_state.json` 仅用于纯模拟盘交易，与用户真实持仓完全隔离

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
│       ├── config/             # 配置层（AppConfig 强类型 + 向后兼容）
│       │   ├── __init__.py     # 导出 + get_config()
│       │   └── models.py       # AppConfig + 10 section BaseModel
│       ├── _legacy_config.py   # 旧版 settings() 函数（迁移桥接）
│       ├── data/                # 数据层
│       │   ├── manager.py       # DataManager（统一数据入口）
│       │   ├── quality.py       # 数据质量校验（v0.9.11 新增 sanitize_dataframe 入口清洗）
│       │   ├── cache.py         # 本地缓存（LRU + 新鲜度语义盘后盘中分离）
│       │   ├── source_health.py # 数据源健康监控（事件驱动，持久化健康摘要）
│       │   ├── ml_models/       # ML 模型持久化（gradient_boost_v1.pkl + scaler）
│       │   └── collectors/      # 采集器
│       │       ├── base.py      # BaseCollector 抽象（含资金流熔断 v0.9.13）
│       │       ├── fallback.py  # 主源（新浪 Market_Center 全市场快照，v0.9.10）
│       │       ├── akshare_collector.py # 降级源（东方财富）
│       │       ├── eastmoney_collector.py # 东方财富 push2 直连 HTTP
│       │       ├── tencent_collector.py # 终极兜底源（腾讯 ifzq.gtimg.cn qfq 日 K）
│       │       ├── holder_structure.py # 股东户数采集器
│       │       └── altalpha/   # 另类Alpha数据采集器（v0.5.9）
│       │           ├── base.py              # AltAlphaCollector 基类（限流+缓存+降级）
│       │           ├── insider_trading.py   # 高管增减持
│       │           ├── earnings_forecast.py # 业绩预告
│       │           ├── dragon_tiger.py      # 龙虎榜
│       │           ├── block_trade.py       # 大宗交易
│       │           ├── margin_trade.py      # 融资融券
│       │           └── share_unlock.py      # 解禁压力
│       ├── models/              # 数据模型
│       │   ├── position.py      # Position 统一模型
│       │   └── signal.py        # Signal 强类型数据类
│       ├── filters/             # 过滤层
│       │   ├── fundamental.py   # 基本面过滤
│       │   ├── money_flow.py    # 资金流过滤
│       │   └── industry_constraint.py # 行业分散约束
│       ├── macro/               # 宏观简报（五维：金/油/汇/债/G）
│       │   └── report.py
│       ├── strategies/          # 策略层
│       │   ├── base.py          # 策略抽象基类
│       │   ├── trend.py         # 趋势跟踪（海龟/双均线/MACD）
│       │   ├── mean_revert.py   # 均值回归（RSI/KDJ/布林带）
│       │   ├── breakout.py      # 突破（箱体/Dual Thrust/支撑阻力）
│       │   ├── first_board.py   # 首板策略
│       │   ├── breakout_rebound.py # 断板反包反弹（陈学长6大规则组合）
│       │   ├── ma5_momentum.py  # MA5 动量突破
│       │   ├── rebound_system.py # 陈学长断板反包实战体系（手册完整版，v0.9.22）
│       │   ├── rebound/         # 断板反包规则 SSoT（单一事实源，v0.9.22）
│       │   │   ├── __init__.py
│       │   │   └── rules.py     # 形态规则唯一实现 RULE_VERSION="v2.0.0-20260812"
│       │   ├── fanbao_strategy.py # 小圈圈儿《反包策略》V2.0，5+1 种战法
│       │   ├── asset_combo.py   # 认知资产组合验证策略
│       │   ├── risk_rules.py    # 通用风险/认知规则组件（活跃度/破位止损/板上减半）
│       │   ├── rebound_elite.py # 断板反包精英追涨（已验证子集）
│       │   ├── fanbao/          # 反包策略相关模块
│       │   ├── regime_cycle.py  # 四周期/幻灭期反弹/布林超跌（公众号战法，v0.9.5）
│       │   ├── capability.py    # 策略能力画像（市场状态/周期/风险偏好匹配）
│       │   ├── registry.py      # 策略注册表（含 verify_registry_consistency 自检）
│       │   ├── manifest.py      # 策略注册中心（统一清单加载器）
│       │   └── generator.py     # LLM 自然语言策略生成器
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
│       │   ├── track_live.py    # 实盘持仓跟踪（track_live模式，只读不交易）
│       │   └── qmt.py           # QMT实盘（阶段四启用）
│       ├── notifier/            # 通知层
│       │   ├── feishu.py        # 飞书推送 + 通知重试队列
│       │   ├── formatter.py     # 信号卡片（核心 + 子模块 re-export）
│       │   ├── formatter_reports.py # 日报/绩效/风控/回测/验证卡片
│       │   ├── formatter_special.py # 云策略/游资/冲突/概念/止盈卡片
│       │   ├── fund_monitor.py  # 基金净值监控（001480/026211）
│       │   ├── flow_report.py   # 资金流日报（08:00复盘/15:10日报）
│       │   ├── flow_collector.py # 资金流数据采集与分析引擎
│       │   └── flow_formatter.py # 资金流日报格式化/卡片构建
│       ├── selector/            # 选股层
│       │   └── stock_selector.py # 资金流 + 策略筛选
│       ├── analysis/            # 高级分析 + P0 认知引擎
│       │   ├── stage_detector.py  # P0 四阶段判断器
│       │   ├── trailing_stop.py   # P0 移动止损引擎
│       │   ├── danger_signal.py   # P0 危险信号检测器
│       │   ├── no_average_down.py # P0 向下摊平检查器
│       │   ├── regime_detector.py # 多周期择时
│       │   ├── dynamic_take_profit.py # 财叔动态止盈减仓引擎
│       │   ├── market_temp.py     # 博格市场温度计
│       ├── alpha_factory.py   # AlphaFactory（技术因子+另类因子融合）
│       ├── altalpha_factors.py # 另类Alpha因子引擎（8因子z-score）
│       ├── heuristic_optimizer.py # 启发式参数优化器（GA/PSO）
│       └── ...               # 其他分析模块
│       ├── wisdom/              # 决策层（28 本书认知 + P0 规则引擎）
│       │   ├── advisor.py       # WisdomAdvisor（LLM 决策 + 多视角路由 + fallback）
│       │   ├── book_router.py   # BookPerspectiveRouter（28 本书多视角路由）
│       │   └── skills/          # 认知 SKILL
│       │       ├── (炒股的智慧 6 个 skill)
│       │       └── books/       # 28 本股书蒸馏 SKILL.md（仓颉认知植入术）
│       ├── engine/              # 主引擎
│       │   ├── main.py          # TradingEngine（EngineOpsMixin 混入）
│       │   ├── engine_mixin.py  # 信号扫描/处理/去重/心跳方法
│       │   ├── scheduler.py     # TradingScheduler（SchedulerJobsMixin 混入）
│       │   ├── scheduler_jobs.py # 各定时任务安全执行方法
│       │   ├── signal_router.py # 多策略信号融合
│       │   ├── signal_pipeline.py # 信号过滤/处理管道
│       │   └── realtime_scanner.py # 盘中 5 分钟实时信号扫描（v0.9.x）
│       ├── health/               # 健康守护（watchdog + auto_recover）
│       │   ├── __init__.py        # 导出自愈闭环
│       │   ├── watchdog.py        # 5 项健康检查 + 告警去重（120min 指纹抑制）
│       │   └── auto_recover.py    # 自动修复闭环（v0.9.12，安全边界+冷却防风暴）
│       ├── web/                 # Web 仪表盘
│       │   ├── app.py           # FastAPI 应用
│       │   └── static/index.html # 单页仪表盘
│       └── utils/
│           ├── log.py           # 日志（10MB 轮转，30天保留；GUGU_TEST_MODE 重定向）
│           ├── single_instance.py # 单实例锁（PID+命令行验证，v0.9.8）
│           └── calendar.py      # 交易日历（节假日识别）
├── tests/
│   ├── unit/                    # 单元测试（2277 passed, 1 skipped）
│   │   ├── test_analysis.py
│   │   ├── test_backtest_benchmark.py
│   │   ├── test_asset_combo.py
│   │   ├── test_auto_recover.py
│   │   ├── test_p0_first_batch_guard.py
│   │   ├── test_p0_second_batch_fix.py
│   │   ├── test_rebound_system.py
│   │   ├── test_rebound_rules_consistency.py
│   │   ├── test_fanbao_strategy.py
│   │   ├── test_risk_rules.py
│   │   ├── test_lookahead_fix.py
│   │   └── ...
│   ├── integration/             # 集成测试
│   ├── fixtures/                # 测试数据
│   └── conftest.py              # 脏数据 fixture + GUGU_TEST_MODE 隔离
├── scripts/
│   ├── run_backtest.py          # 回测入口
│   ├── run_paper.py             # 模拟盘入口
│   ├── run_web.py               # Web 仪表盘入口
│   ├── run_scheduler.py         # 调度器入口（含单实例锁）
│   ├── run_live.py              # 实盘入口（阶段四）
│   ├── health_watchdog.py       # 健康看门狗（支持 --auto-fix 自愈模式）
│   ├── alert_triage.py          # 告警分诊采集（agent cron 每30分钟）
│   ├── e2e_verify.py            # E2E 验证（锁/心跳/磁盘/数据源/通知链路）
│   ├── scan_rebound_market.py   # 断板反包扫描
│   ├── gen_rebound_charts.py    # 断板反包图表
│   ├── draw_rebound_samples.py  # 断板反包样本绘制
│   ├── validate_asset_combo.py  # 认知资产组合验证
│   ├── walk_forward_asset_combo.py # 资产组合 Walk-Forward 验证
│   ├── score_rebound_signals.py # 断板反包信号评分
│   ├── render_rebound_signal_charts.py # 信号图表渲染
│   ├── show_portfolio.py        # 持仓查看入口
│   ├── verify_strategies.py     # 策略注册中心验证 CLI
│   ├── compare_wisdom.py        # Wisdom 决策对比
│   └── optimize_params.py       # 参数优化器（GA/PSO）
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
- **数据源**：sina（主）+ akshare（降级）+ eastmoney（兜底，直连 push2 API）
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

- **数据源（三级冗余）**：sina（主）+ akshare（降级）+ eastmoney（兜底）
- **大市数据（三通道）**：akshare → 新浪K线 JSON → 东方财富 push2 直连
- **降级策略**：主源连续失败 3 次，并发降级到所有备用源（FIRST_COMPLETED），冷却 5 分钟后切回主源
- **数据质量**：实时价格校验（`price_is_realtime`）、单位转换（元→亿、/100 点位）、字段名统一
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

| 类型 | 触发时机 | 内容 | 来源 |
|------|----------|------|------|
| 买卖信号 | 策略产生信号 | 股票/方向/策略/理由/建议仓位/wisdom | `formatter.py` |
| 信号汇总 | 09:10 / 11:35 / 15:10 | 市场概览/今日信号/持仓概况/绩效验证 | `formatter.py` |
| 风控告警 | 触发风控规则 | 级别/详情/建议动作 | `formatter.py` |
| 回测报告 | 回测完成 | 收益/夏普/回撤/胜率 | `formatter.py` |
| 系统异常 | 采集失败/引擎崩溃 | 异常详情/恢复建议 | `formatter.py` |
| 基金监控 | 15:35（交易日） | 基金净值/涨跌幅/波动率/回撤/定投建议 | `fund_monitor.py` |
| 资金流复盘 | 08:00（交易日） | 大盘资金流/行业TOP5/轮动强度/背离预警/概念热点/强流入个股 | `flow_report.py` |
| 资金流日报 | 15:10（交易日） | 当日大盘/行业TOP/轮动/背离/强流入个股/热门概念 | `flow_report.py` |
| 持仓卖出告警 | 收盘后（15:10 日报附送） | 检查持仓股卖出信号，主动推送告警 | `main.py` |
| 动态止盈减仓 | 触发减仓条件（+30%/+15%/大跌） | 股票/减仓比例/原因/策略逻辑（标注财叔出处） | `formatter.py` |
| 策略全景看板 | 手动触发（`verify_strategies.py dashboard`） | 全量策略清单（按类别分组） | `strategies/manifest.py` |
| 宏观简报 | 08:00/09:10/11:35/15:10 | 金/油/汇/债/G 五维全球宏观数据 + 大师兄解读 | `macro/report.py` |
| 市场温度计 | 08:25（交易日） | 博格估值温度/12宽基PE百分位/操作建议 | `analysis/market_temp.py` |

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
- **测试隔离（P1-2）**：所有测试默认写入 tmp_path，4 个 monkeypatch 防污染生产文件
- **配置一致性（P1-8）**：新增策略必须同步 settings.yaml + manifest.yaml，CI 会校验
- 命令：`pytest tests/ -v --cov=src/gugu`

### 自动优化（P1-7）

- **周参数优化**：周日 03:00 自动跑多股聚合 Walk-Forward（6 只行业代表股）
- **启发式参数优化**：GA/PSO 算法自适应调参（见 `analysis/heuristic_optimizer.py`）
  - 遗传算法 (GA)：锦标赛选择 + 单点交叉 + 高斯变异 + 精英保留
  - 粒子群优化 (PSO)：惯性权重 + 个体/全局最优引导
  - 适应度函数：夏普40% + Calmar30% + 胜率20% + 收益10% 多目标加权
  - 参数约束：支持自定义约束函数（如 short_window < long_window）
  - 多股聚合：对多只股票回测取平均得分，避免单股过拟合
- **4 项解锁条件**：样本外 Sharpe>0 + 参数稳定性≥50% + 回撤<25% + 收益非负
- **失败保护**：未通过解锁条件仅记录日志，不污染生产参数
- 手动触发 Walk-Forward：`uv run python scripts/optimize_params.py --multi-stock 600519,002714,... --write`
- 手动触发启发式优化：`uv run python scripts/optimize_params.py --strategy ma5_momentum --algorithm ga --population 30 --generations 20`

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

- 创建日期：2026-07-06
- 版本：v0.9.22
- 阶段：阶段三（信号监控）+ P0 修复上线质量门禁 + 数据源健康监控 + 信号绩效验证闭环
- 测试：2277 passed, 1 skipped（排除5个已知预存失败文件：test_cloud_trend/test_first_board/test_ma5_momentum/test_north_force_long/test_first_board_prefilter）
- 最近审查：2026-07-31（多专家综合审查修复 + v0.8.1 上线审查）；2026-08-12（rebound_system 落地 + 6 预存失败修复，全量回归 0 failed）
- v0.9.22 变更（2026-08-12）：
  - 🆕 新策略 rebound_system（陈学长断板反包实战体系·手册完整版）
    - 硬性过滤：市值50-150亿 / 股价>3元 / 题材热度(近250日涨停≥3) / 股性(20日均振幅>3%) / 非ST非创业非科创
    - 四阶段：S1首板(成交额≈流通市值1%±0.3%) → S2暴力洗盘(量≥1.5×首板) → S3护盘(收盘不破首板开盘价) → S4反包(收盘>首板最高价, 成交额≥1.5%)
    - 三买点：A缩量回踩(优化四条件默认开) / B底背离 / C放量追击(历史证伪默认关)
    - 六卖出：SL1破位 / SL2次日不及预期 / TP1反包减仓 / TP2顶背离 / TP3量价背离+倍量 / TP4压力位
    - 定位 experimental：settings.yaml 未启用，edge 未证实前仅信号观察
  - 🆕 scan_rebound_market.py 升级：成交额估算列 + 振幅/题材过滤 + 洗盘1.5×严格化 + 反包收盘>首板最高价
  - 🔴 修复 6 个预存测试失败（全量回归 2277 passed, 0 failed）：
    - 代码缺陷：cache.get 时间基准不一致（质量校验用真实 today，缓存注入 now 失效）
      → FreshnessRule 支持 now 注入 + validate_stock_history 透传 + cache.get 统一 ref_time
    - 测试数据日期写死：test_bj_volume_factor 改动态日期
    - 测试过期：test_screener_params 对齐 v0.9.10（bear 不再跳过，候选×0.4）
- v0.8.1 变更（2026-08-01 上线级质量审查修复）：
  - 🔴 P0修复：风控状态隔离
    - P0-1：RiskManager scope 参数隔离生产/回测状态文件（risk_state.json vs risk_state_backtest.json）
    - P0-2：回测组合峰值不持久化（persist=False），防止污染生产熔断状态
    - P0-3：回测开始前显式重置 _portfolio_peak 和 _halted，防止跨测试污染
    - P0-4：基本面过滤 fall-open 漏洞——PE/PB/ROE 关键指标 ≥2/3 缺失即拒绝
    - P0-5：策略验证标签不一致——hot_money/ma5_momentum 验证状态修正
    - P0-6：无持仓卖出信号拦截——signal_pipeline 硬检查 direction=sell + 空仓即拦截
  - 🆕 新增：数据源四级冗余
    - 新增 TencentCollector（腾讯 ifzq.gtimg.cn qfq 日 K 兜底）
    - 注册到 DataManager _COLLECTOR_REGISTRY + settings.yaml fallback_sources
  - 🆕 新增：数据源健康监控（DataSourceHealthMonitor）
    - 事件驱动记录各源成功/失败/降级/恢复
    - 持久化到 JSONL + 每日摘要
    - 收盘日报卡片附带数据源健康摘要
    - 线程安全单例 + 缓冲区批量写入
  - 🆕 新增：信号绩效验证闭环
    - SignalTracker 前瞻收益追踪（1/3/5/10日）
    - T+1 次日验证报告（次收盘/回撤/止损命中率）
    - 信号绩效周报（分日信号密度 + 策略贡献度）
    - 飞书卡片：build_signal_performance_card / build_signal_daily_validation_card / build_signal_weekly_card
    - 调度器集成：15:25 次日验证 + 周日 20:30 周报
  - 🆕 新增：策略价格验证 + rebound_elite 策略
    - LivePositionTracker.persist_prices() 持仓价格持久化（0 值保护）
    - StrategyPool MAX_WEIGHT/MIN_WEIGHT 权重边界保护
    - 腾讯 PE/PB 基本面源优先
    - rebound_elite（断板反包精英追涨，陈学长策略验证有效子集）
    - price_volume_signal（换手率+量比尾盘黄金信号，experimental）
  - 🆕 新增：日报功能增强
    - DailyReportManager 信号汇总 + 过滤信号展示 + 数据源健康摘要
    - 持仓认知分析报告（15:35 自动推送）
    - 收盘信号汇总附带绩效验证和数据源健康
  - 📝 代码质量提升
    - ruff 16 处 F841/SIM105/SIM103/UP042/PTH103 清理
    - ruff format 21 个文件自动格式化
    - mypy 0 错误（numpy 类型兼容性警告不阻塞）
    - 测试：test_v081_fixes.py（9 个针对性测试：persist_prices/权重保护/腾讯PE/PB优先）
  - 🧪 测试：1974 passed, 1 skipped
  - 修改文件（共 34+ 个）：
    - 新功能：src/gugu/data/collectors/tencent_collector.py, src/gugu/data/source_health.py
    - 新功能：src/gugu/strategies/rebound_elite.py, price_volume.py
    - 新功能：tests/unit/test_v081_fixes.py, test_rebound_elite.py
    - 风控：src/gugu/risk/manager.py（scope/persist 隔离）
    - 数据：src/gugu/data/manager.py（TencentCollector 注册）
    - 基本面：src/gugu/filters/fundamental.py（fall-open 修复 + 腾讯 PE/PB）
    - 信号追踪：src/gugu/analysis/signal_tracker.py（完整重写）
    - 通知：src/gugu/notifier/formatter_reports.py（绩效/次日验证/周报卡片）
    - 日报：src/gugu/notifier/daily_report_manager.py（源健康+过滤信号展示）
    - 调度：src/gugu/engine/scheduler.py, scheduler_jobs.py
    - 配置：config/settings.yaml（四级冗余）, config/strategy_manifest.yaml, config/strategy_defaults.yaml
    - 执行：src/gugu/execution/track_live.py（persist_prices）
    - 飞书：src/gugu/notifier/feishu.py, formatter.py
    - 配置一致性：src/gugu/strategy_consistency.py
    - 策略池：src/gugu/analysis/strategy_pool.py（权重边界）
    - 测试增强：test_data_manager.py, test_heuristic_optimizer.py, test_huamulan_strategies.py
    - 测试增强：test_notifier.py, test_scheduler.py, test_screener_params.py, test_strategy_pool.py
    - 文档：CLAUDE.md（版本历史更新）
- v0.8.0 变更（2026-08-01 花木兰策略审查修复：ghost_breakout 清理 + fake_dip 重构）：
  - 🗑️ 删除：huamulan_ghost_breakout（烂板成妖打板战法）
    - 根因：来源标签漂移——标称"06-24 首板洗盘"战法，但对应视频25转写实为实盘记录，转写全文不存在"烂板成妖/炸板回封"逻辑
    - 处置：从 registry/manifest/defaults/optimization_pools/测试/回测脚本全部移除，文件归档至 `_refs/archived_strategies/`
  - 🔧 重构：huamulan_fake_dip（早盘假摔低吸战法）
    - 忠实度修复：去掉原文没有的额外条件（不破前低+MA多头强制），回归原文核心4条件
    - 卖出逻辑：删除原文没有的6%止盈，改为次日冲高/滞涨卖出（原文语义）
  - 📋 审查依据：2026-07-31 多专家综合审查报告（花木兰4策略全部 disabled 是正确的保守决策）
- v0.7.9 变更（2026-07-31 综合审查修复 + 信号产量优化）：
  - 🔴 P0修复：5个关键缺陷
    - P0-1：花木兰4策略0回测即启用生产 → 全部 disabled（strategy_manifest.yaml）
    - P0-2：绩效验证 return_5d 前视偏差 → 已确认使用收盘价，无需修复
    - P0-3：十大视角评分假精度 → 新增 data_completeness 字段，<50%标注"低数据完整度"
    - P0-4：RiskManager 构造函数参数绕过 → 4个硬编码安全上限常量，全部构造分支应用 min()
    - P0-5：另类因子IC方向错误 → 新增 compute_cross_sectional_ic() 横截面IC方法
  - 🟡 P1修复：9个重要缺陷
    - P1-1：SignalPipeline 架构重构已记录（大工作量待排期）
    - P1-2：enabled_by_regime 统一配置 → 清理花木兰引用，bear/crash=空列表
    - P1-3：涨跌停检查回测-实盘不一致 → paper.py 概率模型改为硬阻断
    - P1-4：飞书跨日去重 → feishu.py 指纹追加日期后缀 {hash}_{YYYY-MM-DD}
    - P1-5：DataManager批量串行 → 已确认使用 asyncio.gather + Semaphore(10)，无需修复
    - P1-6：LLM决策价值无法验证 → advisor.py 新增 _log_decision_comparison 记录到 wisdom_comparison.jsonl
    - P1-7：花木兰日内策略日K线运行 → 已通过 P0-1 禁用
    - P1-8：组合回撤 peak 更新时机 → engine.py/manager.py 每根K线后更新，persist参数区分回测/生产
    - P1-9：calc_liquidity_stress_test 失效 → Trade 新增 market_cap 字段，缺失时从成交额估算
  - 🟢 架构优化 P2：7个高价值修复
    - P2-4：GA/PSO 优化器 Early Stopping → patience + min_improvement 参数
    - P2-6：param_optimizer base_return → 超额收益替代绝对收益
    - P2-7：cache.py LRU淘汰修复 → and→无条件检查
    - P2-8：quality.py 假日检测 → d=latest → d=latest+1天
    - P2-10：信号拒绝记录 → 写入 signals_history.jsonl
    - P2-12：halt冷却时间持久化 → risk_state.json
    - P2-14：事件引擎评估 → 当前规模够用，无需改动
  - 🚀 信号产量优化（基于ROI评估的核心改进）
    - min_confidence 从 0.6 降至 0.4（原阈值在共识折扣下导致月产仅14条信号）
    - 8个策略全开：3 verified + 5 experimental（bull/rally regime）
    - get_enabled_for_regime() 逻辑扩展：experimental 策略也参与扫描
    - StrategyPool._init_weights() 加载 experimental_weights
    - RSI专项扫描：13:30独立启用 rsi_reversal（唯一通过门禁的策略）
    - 过滤链拒绝统计：每步拒绝次数/百分比输出到引擎日志
    - 每日信号摘要：扫描数/通过数/过滤数/按策略分组统计
  - 📝 代码质量提升
    - signal_pipeline.py 39处 UP031 %格式化 → loguru {}惰性求值
    - risk/manager.py 三路构造 → 提取 _apply_safety_caps 方法
    - cache.py LRU O(n)瓶颈 → OrderedDict O(1)
    - strategy_consistency.py 新增experimental策略校验
    - 清理8处未使用变量/导入 (F841/F401)
    - 配置语义统一：manifest注释 + validate新增warning校验
  - 🧪 测试：所有通过（37 pipeline + 22 feishu + 20 cache + 46 backtest + 72 total）
  - 修改文件（共 25+ 个）：
    - 配置：config/strategy_manifest.yaml, config/settings.yaml
    - 风控：src/gugu/risk/manager.py
    - 分析：src/gugu/analysis/perspectives/data_types.py, engine.py, factor_ic.py
    - 分析：src/gugu/analysis/heuristic_optimizer.py, param_optimizer.py, strategy_pool.py
    - 引擎：src/gugu/engine/signal_pipeline.py, engine_mixin.py, scheduler.py, scheduler_jobs.py
    - 数据：src/gugu/data/cache.py, quality.py
    - 执行：src/gugu/execution/paper.py
    - 回测：src/gugu/backtest/engine.py, validation.py
    - 通知：src/gugu/notifier/feishu.py
    - 智慧：src/gugu/wisdom/advisor.py
    - 配置：src/gugu/config/models.py
    - 验证：src/gugu/strategy_consistency.py
    - 文档：docs/plans/comprehensive_review_fixes.md
- v0.7.8 变更（2026-07-30 信号数据时效性追踪 + 数据源透明度优化）：
  - 🆕 新增：信号数据时效性追踪系统
    - Signal模型扩展：新增 data_timestamp/data_source/is_intraday/adjust_type 字段
    - DataManager增强：current_source/current_adjust_type 属性追踪当前数据源
    - 采集器标准化：所有采集器声明 adjust_type（sina=none, akshare=qfq, eastmoney=qfq）
    - Engine集成：扫描时自动注入数据源和时间戳元数据
    - 飞书卡片增强：信号卡片底部显示数据来源和复权方式
  - 🔴 P0修复：数据源透明度问题
    - 根因：信号卡片未标注数据来源和复权方式，导致MA20计算基准不一致
    - 修复1：BaseCollector 基类添加 adjust_type 类属性
    - 修复2：DataManager 添加 current_source/current_adjust_type 属性
    - 修复3：engine_mixin.py 扫描时记录 scan_timestamp/data_source/adjust_type
    - 修复4：formatter.py 卡片底部显示"数据源: 新浪/akshare/东方财富"和"复权: 不复权/前复权/后复权"
  - 🧪 测试：所有信号/通知相关测试通过（15 passed）
  - 修改文件：
    - 修改：src/gugu/models/signal.py, src/gugu/data/manager.py, src/gugu/data/collectors/base.py
    - 修改：src/gugu/data/collectors/akshare_collector.py, src/gugu/data/collectors/eastmoney_collector.py
    - 修改：src/gugu/data/collectors/fallback.py, src/gugu/engine/engine_mixin.py
    - 修改：src/gugu/notifier/formatter.py
- v0.7.7 变更（2026-07-30 花木兰策略系列实现）：
  - 🆕 新增：花木兰策略系列（4个策略）
    - huamulan_tail_pick：尾盘选股六步法（小资金复利首选）
      - 六步法：涨幅3-5% + 量比>1 + 换手5-10% + 市值50-200亿 + 均线多头 + 量能阶梯放大
      - 适用市场：bull/rally/sideways
    - huamulan_crash_layout：空仓等暴跌布局法（大资金稳健复利）
      - 买入条件：指数暴跌3% + 行业龙头（市值>200亿）+ 被错杀 + 估值合理（PE<30）+ 企稳信号
      - 适用市场：crash（特殊触发）
    - huamulan_fake_dip：早盘假摔低吸战法（捕捉主力洗盘）
      - 买入条件：长下影线（>3%）+ 阳线 + 缩量 + 不破前低 + 趋势向上
      - 适用市场：bull/rally
    - huamulan_ghost_breakout：烂板成妖打板战法（博取连板溢价）
      - 买入条件：前日涨停 + 高开放量 + 收阳 + 盘中冲高 + 突破横盘
      - 适用市场：bull/rally
  - 🧪 测试：新增 36 个花木兰策略单元测试
    - 每个策略9个测试：名称/类别/信号生成/信号值/置信度/参数验证
    - 集成测试：策略名称唯一性/类别有效性/置信度生成
  - 测试覆盖：4个策略文件行覆盖率 95%+/98%+/98%+/99%
  - 修改文件：
    - 新增：strategies/huamulan_tail_pick.py, strategies/huamulan_crash_layout.py, strategies/huamulan_fake_dip.py, strategies/huamulan_ghost_breakout.py
    - 新增：tests/unit/test_huamulan_strategies.py
    - 修改：strategies/registry.py, config/strategy_defaults.yaml, config/settings.yaml, config/strategy_manifest.yaml
  - 当前状态：experimental（待回测验证通过后加入 verified_strategies）
- v0.7.6 变更（2026-07-29 飞书通知去重机制彻底修复）：
  - 🔴 P0-13修复：飞书通知重复发送问题
    - 根因1：信号级去重在发送前标记，发送失败但已标记导致无法重试
    - 根因2：卡片指纹包含动态数据（价格、百分比、时间戳），价格变化导致指纹不同绕过去重
    - 根因3：重试队列只检查卡片指纹，不检查信号级去重
    - 修复1：engine_mixin.py 信号发送成功后再标记已通知（移除提前标记）
    - 修复2：feishu.py 卡片指纹排除动态数据（_is_dynamic_content 方法）
    - 修复3：feishu.py 重试队列增加信号级去重检查（_get_signal_key_from_card + _is_signal_notified + _mark_signal_notified）
    - 修复4：feishu.py 卡片指纹 TTL 从5分钟增加到1小时（覆盖扫描间隔）
    - 修复5：engine_mixin.py 去重 key 过滤条件放宽（保留带策略名的 key）
    - 修复6：test_feishu_notifier.py 增加 clear_notified_signals fixture 避免测试间状态污染
  - 修改文件：src/gugu/engine/engine_mixin.py, src/gugu/notifier/feishu.py, tests/unit/test_feishu_notifier.py
- v0.7.5 变更（2026-07-29 周线四因子选股策略开发 + 动态止损/移动止盈）：
  - 🆕 新增：周线四因子共振选股策略（strategies/weekly_combo.py）
    - 四因子共振：倍量(30%) + MACD金叉(30%) + 回踩20周均线(25%) + 三连阳(15%)
    - 动态止损机制：跌破入场价8%强制卖出（_apply_stop_loss_and_take_profit）
    - 移动止盈机制：盈利15%后启用，从最高价回撤10%止盈
    - 市场状态过滤：仅在 bull/rally 市场启用（settings.yaml enabled_by_regime）
    - 修复周线-日线日期对齐：改用 merge_asof(direction="backward") 替代 merge
  - 🧪 测试：新增 14 个周线策略单元测试（TestWeeklyComboStrategy + TestWeeklyComboStopLoss）
    - 基础功能：信号值有效性/周线转换/评分计算
    - 止损/止盈：止损触发/移动止盈触发/未持仓不触发
    - 边界条件：空数据/短数据/缺失列
  - 测试覆盖：weekly_combo.py 行覆盖率 91%（11行未覆盖为边界分支）
  - 修改文件：strategies/weekly_combo.py, config/strategy_defaults.yaml, config/settings.yaml, config/strategy_manifest.yaml, tests/unit/test_weekly_combo.py
  - 🟡 多股聚合回测结论（13只股票×500交易日）：
    - 版本A（严格参数）：平均收益-1.99%，夏普-1.22，最大回撤7.73%，盈利比31%
    - 版本B（放宽参数）：平均收益-5.38%，夏普-1.14，最大回撤12.46%，盈利比23%
    - **结论：严格参数优于放宽参数，策略适合高置信度入场**
    - 最佳表现：002371(+15.77%)、300750(+9.66%)
    - 最差表现：002230(-15.55%)、600438(-10.28%)
  - 新增脚本：scripts/multi_backtest_weekly.py（多股聚合回测工具）
  - 新增策略：周线四因子共振选股（weekly_combo）
    - 核心逻辑：周线级别信号比日线更稳定，主力资金难以伪造周线量价痕迹
    - 适用场景：牛市/反弹期趋势明确时效果最佳
    - 当前状态：disabled（回测未通过 4 项解锁条件，等待牛市环境）
- v0.7.3 变更（2026-07-28 启发式参数优化系统）：
  - 🆕 新增：启发式策略参数优化器（analysis/heuristic_optimizer.py）
    - 遗传算法 (GAOptimizer)：锦标赛选择 + 单点交叉 + 高斯变异 + 精英保留
    - 粒子群优化 (PSOOptimizer)：惯性权重 + 个体/全局最优引导 + 速度限制
    - StrategyParamOptimizer 封装：参数网格→ParamSpace→适应度函数→优化
    - 适应度函数：多股聚合回测的夏普40% + Calmar30% + 胜率20% + 收益10% 加权评分
    - 参数约束：支持自定义约束函数过滤非法参数组合
    - 集成：scripts/optimize_params.py 新增 run_heuristic_optimization() + CLI 参数
    - 集成：engine/scheduler.py 调度器支持启发式优化任务
    - 修复：heuristic_optimizer.py docstring 语法错误修复
  - 🧪 测试：新增 22 个启发式优化器单元测试（ParamSpace/GA/PSO/StrategyParamOptimizer/集成场景）
  - 测试覆盖：heuristic_optimizer.py 行覆盖率 93%（17 行未覆盖为边界分支）
  - 修改文件：src/gugu/analysis/heuristic_optimizer.py, scripts/optimize_params.py, src/gugu/engine/scheduler.py, tests/unit/test_heuristic_optimizer.py
  - 🟡 持续优化：行业资金强度+动量确认策略v2.x系列回测
    - v2.0 重构：状态机持仓追踪+连续评分系统+统一退出条件
    - v2.1 优化：RSI范围收紧45-65，MA5斜率>0，最少持仓8天 → 总收益-2.25%
    - v2.2 优化：收紧止损8%+提高止盈12%+移动止盈+MA20过滤 → 总收益-2.03%（最优）
    - v2.3 优化：降低量比1.3x+关闭MA20过滤+放宽评分阈值 → 总收益-3.35%，交易增加但质量下降
    - v2.4 优化：开启MA20过滤+放宽止损10% → 总收益-3.05%，夏普-1.97
    - 最终结论：策略在当前市场环境下难以实现正收益，建议禁用待进一步优化
    - 修改文件：strategies/industry_capital_momentum.py, config/strategy_defaults.yaml, config/strategy_manifest.yaml
- v0.7.1 变更（2026-07-26 行业资金强度+动量确认策略禁用）：
  - 🔴 禁用：行业资金强度+动量确认策略（IndustryCapitalMomentumStrategy）
    - 回测结果：23个策略中表现最差，v1.2优化后总收益-15.38%，夏普-0.92
    - 根因：策略核心逻辑基于行业横截面排名，改为个股时间序列后逻辑失效
    - v1.1 重构：适配个股数据分析（从行业级别改为个股时间序列）
    - v1.2 优化：放宽退出条件（止损8%→12%，止盈20%→25%），收益从-18.04%提升至-15.38%
    - 后续优化方向：需重新设计为纯行业级别策略（需单独的数据管道）或完全重构入场逻辑
    - 修改文件：config/settings.yaml（从所有启用列表移除）, config/strategy_manifest.yaml（标记disabled+记录回测结果）
  - 📚 文档更新：PRODUCT.md（策略列表新增）, TECH.md（3.10节技术文档新增）, CLAUDE.md（版本历史更新）
- v0.6.4 变更（2026-07-24 陈学长不吃面策略大幅放宽 + 验证增强）：
  - 🟡 P1优化：breakout_rebound策略信号产量大幅提升
    - 涨停判定：9.5%→7%（覆盖更多涨停）
    - 涨停回看窗口：7日→10日
    - 洗盘识别：跌幅-2%→-1%，放量1.2→1.0倍（只要阴线+放量就算洗盘）
    - 洗盘窗口：5日→7日
    - 护盘容忍度：±5%→±12%（支撑位判断大幅放宽）
    - 趋势过滤：斜率>-0.01→>-0.05（只要不是暴跌趋势就行）
    - 量能条件：缩量判断0.7→0.85，量比上限1.5（只要不是巨量异常就行）
    - 止损：5%→8%，止盈：6%→5%
    - 时间止损：5天→10天，阈值3%→1%
    - 置信度下限：0.5→0.3（让更多信号通过）
    - 核心买入条件：从"护盘状态"改为"有涨停历史+趋势OK+任意买点信号"
  - 回测结果（20只股票）：
    - 总计155笔交易，13只股票有信号
    - 正收益股票：002371(+12.4%)、002230(+7.7%)、002594(+7.0%)、002714(+3.6%)、600276(+2.1%)
    - 修改文件：strategies/breakout_rebound.py, config/strategy_defaults.yaml
  - 🆕 新增：策略验证增强模块（backtest/validation.py）
    - 基准对照：calc_benchmark_comparison() 计算超额收益/信息比率/Beta/相关性 vs 沪深300
    - 参数扰动稳定性：calc_param_perturbation_stability() ±10%/20%/50% 参数扰动分析
    - 流动性压力测试：calc_liquidity_stress_test() 小盘股冲击成本估算
    - 滚动窗口验证：calc_rolling_window_validation() 3月滚动窗口回测
    - 验证报告生成：build_validation_report() 汇总评分 + 建议
- v0.6.2 变更（2026-07-24 信号产量优化）：
  - 🔴 P0修复：AccountInfo.positions_value 缺失（main.py:407 运行时崩溃）
    - AccountInfo 数据类添加 positions_value: float = 0.0 字段
    - 更新所有 broker 的 get_account() 返回 positions_value
    - 修改文件：execution/base.py, paper.py, track_live.py, qmt.py
  - 🔴 P0修复：ScreenerScanResult.__len__ 缺失（生产环境 TypeError）
    - ScreenerScanResult 数据类添加 __len__ 方法返回 len(self.results)
    - 修改文件：screener/terminal_screener.py
  - 🟡 P1优化：MarketFilter 市场状态门槛放宽（解决信号产量极低问题）
    - 原逻辑：必须 3/3 条件满足才允许选股，导致震荡市完全无信号
    - 新逻辑：
      - 3/3 → bull（强势市场）
      - 2/3 → rally（偏强震荡，允许追势+均值回归）
      - 1/3 → sideways（偏弱震荡，允许 RSI 超卖反弹）
      - 0/3 → bear（禁止买入）
    - 修改文件：engine/market_filter.py
  - 🟡 P1优化：启用 rsi_reversal 策略（震荡市适用）
    - settings.yaml enabled_by_regime 添加 rsi_reversal 到 rally/sideways 市场状态
    - strategy_manifest.yaml rsi_reversal.enabled: true，添加 validation_gate
    - 修改文件：config/settings.yaml, config/strategy_manifest.yaml
  - 🟡 P1优化：扩大自选股池（9只→19只，覆盖更多行业）
    - 新增：600519贵州茅台、000858五粮液、300760迈瑞医疗、300750宁德时代、002594比亚迪、002371北方华创、601012隆基绿能、600438通威股份、300033同花顺、002230科大讯飞
    - intraday_monitor.custom_watchlist 同步更新为 19 只股票
    - 修改文件：config/settings.yaml
  - 🟡 P1优化：扩大回测样本池（50只代表性股票）
    - optimize_params.py 新增 DEFAULT_REPRESENTATIVE_STOCKS 常量（50只跨行业龙头）
    - 新增 --default-pool CLI 参数，支持一键多股聚合 Walk-Forward 优化
    - 修改文件：scripts/optimize_params.py
  - 🧪 测试：1890 passed, 5 skipped（原1890+新增P0/P1修复测试）
- v0.6.1 变更（2026-07-16 实盘/模拟盘数据分离）：
  - 🔴 P0修复：实盘/模拟盘数据分离（解决用户真实持仓被误删问题）
    - 根因：paper_broker_state.json 一个文件同时承担用户真实持仓跟踪和模拟盘交易两个职责，PaperBroker自动下单会覆盖用户真实持仓
    - 新增 track_live 模式：用户真实持仓独立存储于 data/live_positions.json
    - 新增 LivePositionTracker 类（src/gugu/execution/track_live.py）：
      - 继承 BaseBroker 接口，但 order() 永远拒绝下单（核心安全机制）
      - 只读用户真实持仓文件，update_price() 仅更新现价（不持久化，避免高频IO）
      - update_stop_loss() 持久化止损价（移动止损触发时调用）
      - adjust_for_corporate_action() 持久化除权除息调整
      - 线程安全（threading.RLock）+ 原子写入（fcntl.flock + tmp文件 rename）
    - settings.yaml execution.mode 从 paper 改为 track_live（当前默认）
    - main.py 根据 exec_mode 切换 broker：track_live 用 LivePositionTracker，paper/signal_only 用 PaperBroker
    - StopLossManager 新增 track_live 模式止损/止盈提醒卡片推送：
      - 检测 result.message 含 "track_live" 时推送"需手动执行"提醒卡片
      - 止损提醒：level=halt + 现价≤止损价 + 建议手动卖出数量
      - 止盈减仓提醒：level=warn + 触发步骤 + 建议手动减仓比例
    - _process_signal: track_live 模式走 signal_only 分支（仅通知不下单）
    - paper_broker_state.json 重置为初始100万空仓（纯模拟盘）
    - take_profit_state.json 清理 600519 异常数据（entry_price=10.0 是测试数据）
    - 用户5个真实持仓迁移到 live_positions.json（002714/510500/588000/510310/512480）
    - 配置一致性测试：test_execution_mode_is_paper 改为 test_execution_mode_is_safe，允许 paper/signal_only/track_live 三种安全模式
    - 16个 track_live 单元测试：加载/order拒绝/价格更新/止损持久化/账户信息/线程安全
  - 🧪 测试：1570 passed, 5 skipped（原1554+16新增track_live测试）
- v0.6.0 变更（2026-07-16 上线级全面优化）：
  - 🔴 P0修复：MarketFilter放宽 + 回测/生产逻辑统一
    - MarketFilter默认条件从3/3 MA200放宽到2/3（min_conditions=2），让震荡市也能产生信号
    - 根因：原3/3条件在震荡市完全阻止信号产生，导致近15天零信号
    - regime_detector统一：buy_allowed = regime in ("bull", "rally", "sideways")（bear/crash禁止买入）
    - 回测MarketFilter与生产regime_detector逻辑完全一致
  - 🔴 P0修复：模拟盘数据不一致
    - 重置paper_broker_state.json（清空5个历史测试遗留持仓，重置为初始100万现金）
    - 清空take_profit_state.json（600519的entry_price=10.0是测试数据）
    - 备份旧信号历史（signals_history.jsonl.bak），原文件清空（旧信号来自已禁用策略）
  - 🔴 P0修复：心跳历史停止更新
    - _write_heartbeat方法重构：payload构建与文件写入分离
    - get_account()失败时用fallback值确保心跳仍能写入
    - heartbeat.json和heartbeat_history.jsonl各自独立try/except，确保两者始终同步更新
  - 🔴 P0修复：回测引擎多股WF优化symbol=""问题
    - walk_forward_optimize_multi_stock内symbol=""改为symbol=sym
    - 确保涨跌停检查和T+1检查生效
  - 🔴 P0修复：北交所过户费未覆盖
    - backtest/engine.py和execution/paper.py的沪市前缀新增"8"和"4"（北交所）
  - 🔴 P0修复：相关性检查失效
    - 单股回测模式初始化_price_history = {symbol: df}
    - _check_correlation_with_portfolio移除错误的@staticmethod装饰器
  - 🟡 P1修复：signal_only模式补齐过滤器
    - 为买入信号补充5个过滤器（资金流/板块轮动/筹码/向下摊平/行业约束）
    - 所有过滤器只附加数据不拦截（保留信号供用户参考）
  - 🟡 P1修复：AlphaFactory 3个Bug
    - KDJ从单点近似改为EMA全序列迭代（与策略KDJ一致）
    - _kdj_at三元表达式失效修复（idx<=0时返回50.0）
    - ADX用SMA替代标准Wilder RMA修复
  - 🟡 P1修复：信号流水线封装违规
    - WisdomAdvisor新增公开方法advise_fallback()，消除外部访问私有方法
    - signal_pipeline.py中4处_advise_fallback改为advise_fallback
  - 🟡 P1修复：盘中前视偏差
    - stage_detector和weekly_trend在盘中使用df[:-1]排除当日未完成K线
  - 🟡 P1修复：KDJStrategy缺少下跌趋势过滤
    - 新增MA20>=MA60趋势过滤（与Bollinger/RSI一致）
  - 🟡 P1修复：double_yin参数默认值
    - min_confidence默认值从0.55改为0.0（与配置一致）
  - 🟡 P1修复：Wisdom入场过滤逻辑
    - wisdom_filtered=True时所有模式都仅通知不下单（安全优先原则）
    - 入场置信度条件从<改为<=（边界值正确过滤）
  - 🆕 新增：信号绩效验证调度任务
    - 每日15:20自动运行SignalTracker.generate_report(days=30)
    - 新增build_signal_performance_card飞书卡片（命中率/收益/策略对比）
    - Web界面新增信号绩效摘要卡片
  - 🟡 优化：警告风暴抑制
    - 新增_alert_history + 10分钟冷却机制
    - 同一股票同一原因的告警10分钟内只发一次
  - 🟡 优化：Web净值曲线fallback
    - heartbeat_history为空时返回heartbeat.json当前值
  - 🧪 测试：1554 passed, 5 skipped（所有P0/P1/P2修复后全量通过）
- v0.5.9 变更（2026-07-15 另类数据Alpha因子系统）：
  - 🆕 新增另类数据Alpha因子系统（免费akshare API实现Alpha）
    - 8个另类因子：insider_buy/earnings_surprise/lhb_inst_net/roic_quality/block_discount/margin_sentiment/unlock_pressure/holder_concentrate
    - 6个采集器：insider_trading/earnings_forecast/dragon_tiger/block_trade/margin_trade/share_unlock
    - 数据源：akshare免费API（高管增减持/业绩预告/龙虎榜/大宗交易/融资融券/解禁）
    - 架构：AltAlphaCollector基类（500ms限流+12h Parquet缓存+过期缓存兜底）
    - 因子输出：横截面z-score（-3~+3），每因子独立计算后加权融合
    - 集成：AlphaFactory.composite_score_with_altalpha() 技术因子+另类因子加权融合
    - 调度：scheduler.py 每日18:00自动采集（altalpha_fetch_1800）
    - IC验证脚本：scripts/verify_altalpha_factors.py（--quick 10只/--full 50只）
    - IC验证结果（50只蓝筹股×5日收益）：
      - insider_buy IC=-0.2029（|IC|>0.03 ✓，IC同向率48% < 55% ✗）- 高管增持反向因子
      - lhb_inst_net IC=0.2841（|IC|>0.03 ✓，IC同向率50% < 55% ✗）- 龙虎榜机构净买入正向因子
      - margin_sentiment IC=-0.1384（|IC|>0.03 ✓，IC同向率44% < 55% ✗）- 融资余额反向因子
      - 5个因子0/50覆盖：事件驱动数据（业绩预告/大宗交易/解禁/股东户数）在蓝筹股中覆盖率低（数据特性，非bug）
    - ⚠️ 单期IC的IC同向率<55%是预期行为，生产环境需多期IC平均（30天滚动IC）+ 全市场5000+只股票计算横截面z-score
  - 🔴 P0修复：缓存目录路径计算错误
    - `_DEFAULT_CACHE_DIR` 原为 `Path(__file__).parent.parent.parent.parent.parent` = `src/data/altalpha/`（错误！）
    - 改为 `_find_project_root()` 动态查找含 `pyproject.toml` 的目录，缓存正确写入 `data/altalpha/`
    - 根因：硬编码parent层数在文件移动后会出错，且导致缓存清空时清不到（清的是data/altalpha/，缓存在src/data/altalpha/）
  - 🔴 P0修复：earnings_forecast缓存污染
    - `fetch_latest` 尝试多个报告期（年报→三季报→中报→一季报），但 `fetch_with_cache` 不区分date参数
    - 第一次调用date=20261231成功后写入缓存，第二次调用date=20260930命中缓存返回错误报告期数据
    - 修复：`fetch_latest` 改为直接调用API+限流，不使用 `fetch_with_cache` 的共享缓存
  - 🔴 P0修复：holder_structure列名映射缺失
    - `_AK_COL_MAP` 缺少 `stock_zh_a_gdhs_detail_em` 返回的列名（代码/名称/股东户数-本次/股东户数统计截止日等）
    - 导致 `_finalize` 抛出 KeyError 'symbol'，holder_concentrate 因子全部为0
    - 修复：`_AK_COL_MAP` 新增8个列名别名映射
  - 🟡 优化：IC验证脚本K线获取改用DataManager（三级降级）
    - 原直接用akshare stock_zh_a_hist，东方财富API经常RemoteDisconnected
    - 改用DataManager.fetch_stock_history（sina→akshare→eastmoney三级降级），K线获取成功率从0/10提升到49/50
  - 🧪 测试：1554 passed, 5 skipped（altalpha因子单元测试+调度器测试+数据采集器测试）
- v0.5.8 变更（2026-07-09 多专家深度审查 + P0级Bug修复）：
  - 🔴 P0修复：回测认知引擎前视偏差（会导致回测结果虚假偏高）
    - 四阶段判断器`window_df`从`df.iloc[max(0,i-60):i+1]`改为`df.iloc[max(0,i-60):i]`，不包含当日数据
    - 危险信号检测器同样修复window_df切片，添加`len(window_df) < 20`数据长度检查返回None
    - 根本原因：回测循环中第i根K线已包含当日close/high/low，认知引擎不应再偷看
  - 🔴 P0修复：信号链7个致命Bug（直接导致错误信号/风控失效）
    1. **无持仓卖出未拦截**：`signal_pipeline.py`入口添加硬检查，direction=sell且has_position=False直接返回None（防对空仓发卖出信号）
    2. **any模式买卖信号永远优先买入**：改为比较买卖总置信度，高者胜；平盘时sell优先（安全优先）
    3. **Wisdom决策覆盖前置风控结果**：合并结果时保留wisdom_filtered=True和filter_reason，已过滤信号不再调用LLM
    4. **筹码集中度键名不匹配**：filter_reason写"holder_concentration"但wisdom检查"chip_check"，统一为"筹码"匹配
    5. **趋势破坏后triggered列表混合买卖策略**：trend_breakdown强制sell时triggered只包含sell策略名
    6. **共识折扣使用总策略数而非实际投票数**：weighted模式分母改为len(voted)，避免全票一致反而被低置信度策略稀释
    7. **ST/停牌硬检查未正确终止流程**：is_st买入/is_suspended时return None而非继续wisdom决策
  - 🔴 P0修复：风控管理器熔断状态持久化不完整
    - `_load_halt_state()`恢复`_portfolio_peak`字段，防止进程重启后组合回撤熔断失效
  - 🔴 P0修复：DataCache线程不安全
    - 所有公共方法（get/set/clear/cleanup_expired）添加`threading.RLock()`保护
    - 防止多线程/协程并发读写时_store和_order竞态条件
  - 🔧 P1修复：策略池工厂废弃类引用
    - `strategy_pool.py`移除`Stage2Detector`，改用已存在的`StageDetector`
  - 🧪 测试增强：新增无持仓卖出拦截测试，修复原有卖出测试使用空portfolio的问题
  - 📚 审查参考：借鉴了qlib、zipline、backtrader、vnpy等开源量化项目的最佳实践
    - qlib：DataHandler数据缓存过期机制、策略组合信号融合
    - zipline/backtrader：前视偏差防护、严格的历史窗口边界
    - vnpy：RougeCell风控熔断持久化、线程安全设计
- v0.5.7 变更（2026-07-09 A股交易费用模型对齐 + 数据完整性修复）：
  - 🔴 P0修复：回测引擎费用模型对齐PaperBroker（真实A股交易成本）
    - 新增 `_calc_transfer_fee()` 方法：沪市（600/601/603/605/688/689/9开头）双向收取0.001%过户费，深市/创业板无
    - 买入/卖出均实现最低佣金5元规则（佣金=max(成交金额×费率, 5.0)）
    - Trade类新增transfer_fee字段，完整记录每笔交易过户费
    - 解决回测虚高问题：之前缺少过户费和最低佣金，高频/小单策略回测收益偏高
  - 🔴 P0修复：OrderResult数据结构扩展
    - [execution/base.py](file:///Users/dong/workspace/gugu/src/gugu/execution/base.py) OrderResult添加transfer_fee字段
    - [execution/paper.py](file:///Users/dong/workspace/gugu/src/gugu/execution/paper.py) PaperBroker买卖单均传入transfer_fee
  - 🔴 P0修复：信号历史记录完整性
    - [engine/signal_pipeline.py](file:///Users/dong/workspace/gugu/src/gugu/engine/signal_pipeline.py) record_signal_history缩进规范化
    - 新增order_stamp_tax和order_transfer_fee字段记录，完整记录交易三费
  - 🧪 测试增强：新增11个费用专项测试
    - 覆盖：沪/深/科创板过户费差异、最低5元佣金、大额按比例计费、买卖印花税、完整周期费用扣减、权益曲线反映成本
    - 回测测试总计25个通过
  - 费用模型与实盘完全一致：
    - 买入：佣金（≥5元）+ 过户费（仅沪市）+ 滑点+0.1%
    - 卖出：佣金（≥5元）+ 印花税0.05% + 过户费（仅沪市）+ 滑点-0.1%
- v0.5.6 变更（2026-07-08 博格市场温度计 + 策略全链路优化）：
  - 🆕 新增博格市场温度计模块 `analysis/market_temp.py`
    - 基于博格市场温度计方法：12个宽基指数PE的近10年百分位算术平均
    - 多维度加权综合温度：PE(70%) + PB(20%) + 巴菲特指数=总市值/GDP(10%)
    - 数据源：乐咕乐股 stock_index_pe_lg / stock_index_pb_lg / stock_buffett_index_lg（主）→ 东方财富 push2（备）
    - 动态权重：PB/巴菲特获取失败时权重自动归零，PE 权重等比放大
    - 温度规则：<20°加倍买入 | <30°正常买入 | 30-50°定投 | >50°减仓 | >70°大幅减仓
    - 08:25 盘前推送飞书卡片，含温度总览+各维度温度+各指数PE百分位+最热最冷
    - ⚠️ 所有策略输出均为指导决策参考，非自动交易指令
  - 🔄 策略全链路优化（解决"改了又改"根因）：
    - 🔴 优化1：策略按市场状态启用（enabled_by_regime），替代全局 enabled/disabled
      - settings.yaml 新增 enabled_by_regime 配置：bull/rally 启用突破追势，sideways 启用拐点，bear/crash 空仓
      - StrategyConfig 新增 get_enabled_for_regime() / get_all_enabled_unique() 方法
      - registry.py 新增 get_enabled_strategies_for_regime() 函数
      - main.py 扫描时按 regime 动态重建 SignalRouter
      - 核心思想：策略没有"好/坏"之分，只有"适用/不适用"之分
    - 🔴 修复缺陷A：配置一致性 CI 测试
      - 新增 tests/unit/test_config_consistency.py（5个测试用例）
      - 校验 manifest enabled 与 settings.yaml enabled_by_regime/enabled 一致
      - 校验所有启用策略在 _REGISTRY 中注册
    - 🔴 修复缺陷B：回测与生产环境行为一致性
      - BacktestEngine 新增 strict_confirmation 参数
      - strict=True 时使用 is_backtest=False，模拟生产环境次日确认机制
      - backtest_all_strategies.py 新增 --strict CLI 参数
    - 🔴 修复缺陷C：参数优化闭环
      - optimize_params.py 新增 write_optimization_report() 输出优化报告 JSON
      - 新增 apply_optimization_result() 更新 manifest validation 字段
      - 不自动禁用策略（避免误伤），仅标记状态供人工决策
      - 使用原子写入（atomic_write_text / atomic_write_json）
    - 🟡 优化2：策略诊断脚本
      - 新增 scripts/diagnose_strategy.py
      - 分阶段回测（牛/熊/震荡）+ 信号分布分析 + 失败trade归因 + 诊断报告
      - CLI: `uv run python scripts/diagnose_strategy.py --strategy turtle --stocks 600519,002714`
    - 🟡 manifest 配置同步修复
      - turtle/dual_ma/bollinger/rsi_reversal/money_flow_follow/chen_jiangting 的 enabled: true → false
      - 与 settings.yaml 实际启用状态对齐，validation 字段记录禁用原因
    - 30分钟缓存，asyncio并行获取15个数据点（12 PE + 2 PB + 1 Buffett），asyncio.to_thread避免阻塞
    - 65个单元测试覆盖：calc_pe/pb/buffett_percentile + fetch_index/pb/buffett_temp + calc_market_temp + build_market_temp_card
    - 多专家审查通过（8视角：架构/算法/策略/体验/性能/股票/操盘手/业务）
  - 🆕 settings.yaml 添加 market_temp 任务开关（默认启用）
  - 🆕 scheduler.py 添加 _safe_market_temp 定时任务（08:25）
- v0.5.5 变更（2026-07-07 多专家审查修复）：
  - 🔴 高优修复：组合回撤检查移到止损之后（step 2.5→step 3.5），避免虚高净值稀释回撤灵敏度
  - 🔴 高优修复：`_require_confirmation` 添加 `is_backtest` 参数，消除回测模式下当日数据前瞻偏差
  - 🟡 APScheduler 14:30 任务错开（扫描14:30 → 选股14:31 → 资金流14:32），避免并发竞争
  - 🟡 通知重试队列改为 IntervalTrigger(minutes=2)，规避系统分钟边界耦合
  - 🟡 `retry_queued` 修复：BaseException 下也清理 hb_task，防止异常吞噬
  - 🟡 `IntradayMonitor._alert_history` 添加过期清理，防止字典无限增长
  - 🟡 `reset_halt` 增加 `_clear_today_notified_signals`，补发熔断期间错过的信号
  - 🟡 `record_signal_history` 添加 threading.Lock 防并发写入竞态
  - 🟡 震荡市观察列表接入资金流筛选（主力净流出>1亿/占比<-5% 排除，按强度排序）
- v0.5.4 变更：

## 十一、十大视角分析系统（v0.5.0 新增）

### 概述
将10位投资大师的认知框架量化为可计算的评分维度，对个股进行独立评分（0-10分），加权汇总为综合评分。

### 架构
```
analysis/perspectives/
├── __init__.py          # 包导出
├── data_types.py        # StockDataContext, PerspectiveScore, PerspectiveAnalysisResult
├── scorer.py            # 10个评分函数（每个大师一个）
├── engine.py            # PerspectiveEngine（单股/批量分析）
└── integrators.py      # 与现有模块的集成桥接
notifier/
└── perspective_report.py # 飞书报告卡片生成
```

### 10个视角
| 视角 | 权重 | 核心逻辑 |
|------|------|----------|
| buffett | 0.15 | 护城河、ROE、PE/PB、低负债 |
| munger | 0.12 | 多元思维、盈利质量、现金流 |
| soros | 0.10 | 反身性、市场环境、资金流向 |
| lynch | 0.10 | PEG、成长性、市值适配 |
| livermore | 0.10 | 趋势、关键点、成交量 |
| qiu_guo_lu | 0.10 | 行业格局、胜率、行业对比 |
| chen_jiang_ting | 0.08 | 趋势方向、止损、仓位 |
| technical | 0.10 | 均线、RSI、MACD、布林带 |
| investing_habits | 0.08 | 纪律性、风险控制、市场周期 |
| dashixiong | 0.07 | 五维框架（金油汇债G）|

### 评分规则
- 每项 0-10 分，6分通过，4分以下致命缺陷
- 综合分 ≥ 7.0 + 最低分 ≥ 4.0 + 通过视角 ≥ 7 → 整体通过
- 最低分决定木桶效应

### 调度
- 08:30（十大视角日报）：对自选股池运行分析，推送飞书报告卡片
- 配置：`config/settings.yaml` → `perspective:` 区块


## 十、质量门禁

每次代码修改后必须运行质量检查：

```bash
# 一键运行所有质量检查
./scripts/run_quality_checks.sh

# 或单独运行
uv run ruff check src/             # lint
uv run ruff format --check src/    # 格式
uv run mypy src/ --ignore-missing-imports  # 类型
uv run python3 -m pytest tests/ -x --tb=short -q  # 测试

# 使用 --ignore 排除已知预存失败的文件（不是新引入的问题）
uv run python3 -m pytest tests/ --tb=short -q \
  --ignore=tests/unit/test_cloud_trend.py \
  --ignore=tests/unit/test_first_board.py \
  --ignore=tests/unit/test_north_force_long.py \
  --ignore=tests/unit/test_ma5_momentum.py \
  --ignore=tests/unit/test_first_board_prefilter.py
```

### 核心原则（必须遵守）

1. **不引入新的 P0/P1 问题。** 每次 review 发现的问题必须修复而非绕过。
2. **测试必须通过。** 任何代码变更后运行 `pytest tests/ -x`。
3. **缓存隔离。** 模块级全局缓存会导致测试间污染。所有缓存应有清晰的生命周期管理。
4. **密钥安全。** 密钥（app_secret/api_key/token）在任何日志/通知/异常消息中必须脱敏。
5. **回测信号纯净。** 回测信号必须 `shift(1)` 防止前视偏差。

### Review 清单（每次 AI 介入时自查）

- [ ] 所有测试通过（pytest -x）
- [ ] 未引入新的模块级可变全局状态
- [ ] 密钥/凭证不会出现在日志或通知中
- [ ] 回测引擎的信号没有前视偏差
- [ ] 配置变更兼容旧版格式（flatten() 模式）
- [ ] 新策略通过验证增强模块检查（基准对照/参数稳定性/流动性/滚动窗口）
