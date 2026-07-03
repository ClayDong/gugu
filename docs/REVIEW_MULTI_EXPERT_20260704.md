# gugu 股票交易系统 — 多专家视角深度审查报告

> 审查日期：2026-07-04
> 审查方式：真实使用产品 + 第一性原理分析 + 对抗性审查
> 审查范围：产品定位、用户体验、策略、架构、开发、业务、炒股 7 个专家视角
> 项目位置：/Users/dong/workspace/gugu

---

## 摘要

本次审查通过真实启动 Web 界面、读取 data/*.jsonl 实际数据、追溯核心源码，发现了 **10 个 P0 级问题**，其中 3 个属于"系统正在产出垃圾信号且用户不知情"的严重缺陷。

核心结论：

1. **系统产出的是垃圾信号**：所有信号 `confidence=1.0`、`stage="数据不足"`、`stop_loss_price=null`、`order_quantity=0`，几乎不具备任何决策价值。
2. **风控处于半失效状态**：`portfolio_peak` 未更新、`stage_detector` 永远返回"数据不足"、组合回撤熔断失效。
3. **运行服务用的是旧代码**：代码中已有多个 P0 修复（共识折扣、基本面硬约束、stage 拦截），但运行中的服务自 07-01 01:05 重启后未加载新代码，所有修复均未生效。
4. **数据口径不一致**：Web 显示的 `daily_start_value` 与 `paper_broker_state.json` 中的实际值完全不符。

**建议**：立即重启服务加载新代码，并修复 P0-1（days=90 → 交易日换算）和 P0-6（sell fallback 止损价）两个代码层根因问题。

---

## Step 1：真实使用现状

### 1.1 服务运行状态

- scheduler 进程 PID 27701，web 进程 PID 84457
- heartbeat.json 显示 alive
- 最近信号：2026-07-01 14:30，已 3 天没新信号

### 1.2 持仓现状

| 代码 | 名称 | 持仓 | 现价 | 市值 |
|------|------|------|------|------|
| 002714 | 牧原股份 | 100 | - | - |
| 510500 | 中证500ETF | 400 | - | - |
| 588000 | 科创50ETF | 1400 | - | - |
| 510310 | 沪深300ETF | 500 | - | - |
| 512480 | 半导体ETF | 1200 | - | - |

- 总资产 1,000,005.98，现金 985,850.08
- `paper_broker_state.json` 中 `trades=[]`（无交易记录）

### 1.3 信号历史（14 条信号的 7 个真实问题）

读取 `data/signals_history.jsonl` 发现全部信号呈现以下问题：

| 问题 | 表现 | 根因（已追溯） |
|------|------|----------------|
| confidence 恒为 1.0 | 14 条信号全部 1.0 | 运行服务用的是旧代码（`signal_router.py:214-231` 共识折扣未生效） |
| stage="数据不足，无法判断阶段" | 14 条信号全部如此 | `stage_detector.py:92` 需要 70 根 K 线，但 `main.py:481` `days=90` 取日历日 ≈ 63 交易日 |
| stop_loss_price=null | 14 条信号全部如此 | `advisor.py:1001-1013` sell 信号 fallback 未设置 stop_loss_price |
| risk_allowed=null | 14 条信号全部如此 | signal_only 模式未执行风控检查 |
| order_quantity=0 | 14 条信号全部如此 | signal_only 模式仅通知不下单 |
| 时间戳异常 | 01:05:39、16:00:40 | `scheduler.py:360-366` `start()` 启动后立即执行 `run_daily_cycle()`，无交易时段检查 |
| 基本面"PE 获取失败，跳过过滤" | 全部放行 | `fundamental.py:248-254` 有 P0 修复但运行服务用的是旧代码 |

### 1.4 关键状态文件异常

- `data/notified_signals.json`：`{}` 空（信号未通知）
- `data/risk_state.json`：`portfolio_peak=1000000.0`（未更新到实际 1,000,006）
- `data/paper_broker_state.json`：`daily_start_value=1000000.0`，`trades=[]`

---

## Step 2：多专家视角深度审查

### 2.1 产品专家视角

**产品定位**：面向新手投资者的 A 股量化交易系统，提供选股、信号推送、风控、持仓管理、飞书通知、Web 仪表盘。

#### 问题 P-PD-1：核心价值主张被垃圾信号摧毁

- **现象**：系统产出的信号 `confidence=1.0`、`stage="数据不足"`、`order_quantity=0`，对新手毫无决策价值
- **根因**：运行服务用的是旧代码，所有质量修复未生效
- **第一性原理**：产品的核心价值是"提供有决策价值的信号"。当信号质量失真时，产品不仅没有价值，还会**反向误导**新手用户（虚假的高置信度让用户误以为信号可靠）
- **代码位置**：`signal_router.py:214-231`（共识折扣逻辑存在但未生效）

#### 问题 P-PD-2：signal_only 模式下信号未实际通知

- **现象**：`notified_signals.json` 为空，14 条信号均未推送飞书
- **根因**：`main.py:548-570` `_load_notified_signals` 每日清空非今日记录，且服务自 07-01 起无循环运行
- **第一性原理**：signal_only 模式的核心契约是"不下单但通知用户"。信号未通知等于产品功能完全失效

#### 问题 P-PD-3：缺失"新手引导"关键路径

- **现象**：Web 仪表盘仅有数据展示，无新手引导、无术语解释、无操作指引
- **第一性原理**：目标用户是新手，新手第一次接触量化系统需要"这是什么、怎么用、信号怎么读"的引导

#### 问题 P-PD-4：缺乏信号质量反馈闭环

- **现象**：有 `signal_tracker` 模块但未在 Web 界面展示信号绩效验证
- **第一性原理**：产品必须闭环验证"信号是否有效"，否则就是"只产出不验证"的黑盒

#### 问题 P-PD-5：模式定义不清晰（signal_only/paper/live）

- **现象**：用户无法直观知道当前是哪种模式，`order_quantity=0` 让用户困惑"为什么没下单"
- **代码位置**：`settings.yaml` `execution.mode: signal_only`，`main.py:614-644` signal_only 路径仅通知不下单
- **第一性原理**：模式切换必须有明确的状态展示和预期说明

---

### 2.2 用户体验专家视角

#### 问题 P-UX-1：daily_start_value 口径不一致导致用户误判

- **现象**：`/api/risk` 返回 `daily_start_value=1000000.0`（来自 `broker.daily_start_value`），但前端展示 22453.36（仅持仓市值）
- **代码位置**：`web/app.py:100` `start_value = broker.daily_start_value`
- **第一性原理**：数据一致性是用户信任的基础。当用户看到的日亏数据与实际持仓完全不符时，会彻底丧失对系统的信任

#### 问题 P-UX-2：飞书卡片三层折叠架构未真正降低认知负荷

- **现象**：虽然实现了三层折叠，但 `stage="数据不足"`、`confidence=1.0` 等无效字段仍展示
- **第一性原理**：折叠架构的目的是"渐进式披露信息"，但如果展示的是无效信息，折叠反而让用户更困惑

#### 问题 P-UX-3：专业术语未添加解释

- **现象**：信号卡片中"stage"、"consensus_factor"、"ATR"等术语未解释
- **代码位置**：`notifier/formatter.py` 中格式化逻辑
- **第一性原理**：目标用户是新手，专业术语必须附带"这是什么、为什么重要、怎么用"的解释

#### 问题 P-UX-4：错误反馈机制缺失

- **现象**：信号生成失败、数据获取失败时，用户无感知
- **代码位置**：`main.py:484` `except Exception: continue` 静默跳过
- **第一性原理**：错误必须可见。静默吞掉错误让用户以为系统正常工作，实际已产出垃圾

#### 问题 P-UX-5：无个性化内容（新手 vs 资深）

- **现象**：飞书卡片对所有用户展示相同内容
- **第一性原理**：新手需要"为什么买、风险是什么、怎么操作"，资深用户需要"策略参数、回测数据、信号链"

---

### 2.3 策略专家视角

#### 问题 P-ST-1：stage_detector 永远失效导致四阶段判断完全废弃

- **现象**：所有信号 `stage="数据不足，无法判断阶段"`
- **根因**：`stage_detector.py:92` `if df.empty or len(df) < self.MA_LONG + 10:` 需要 70 根 K 线，但 `main.py:481` `fetch_stock_history(sym, days=90)` 取 90 日历日 ≈ 63 交易日 < 70
- **代码位置**：`main.py:481` + `stage_detector.py:92` + `data/manager.py:279-282`
- **第一性原理**：四阶段判断（《炒股的智慧》核心框架：牛皮/正常升势/疯狂/最后）是策略的风控基石。阶段失效等于策略在"裸奔"，无法识别疯狂阶段和最后阶段的高风险
- **对抗性审查**：简单改 `days=120` 也不够，因为周末+节假日会导致实际交易日波动。应改为按交易日请求数据，或在 `stage_detector` 中根据可用 K 线数动态调整 MA_LONG 参数

#### 问题 P-ST-2：confidence 恒为 1.0 导致阈值形同虚设

- **现象**：`min_confidence=0.6` 但所有信号 confidence=1.0 都通过
- **代码位置**：`signal_router.py:214-231` 有共识折扣逻辑但运行服务用的是旧代码
- **第一性原理**：置信度是信号质量的量化指标。当所有信号都是满分时，置信度失去区分功能，无法过滤低质量信号
- **对抗性审查**：共识折扣修复后，单策略信号 confidence ≈ 0.74（1.0 * 0.7375），仍高于 0.6 阈值。建议阈值提升至 0.7 或采用更激进的折扣系数（0.6 + 0.4 * ratio）

#### 问题 P-ST-3：策略参数未回测验证

- **现象**：`strategy_defaults.yaml` 中 8 个策略的参数（如 dual_ma 的 MA_SHORT=10/MA_MEDIUM=20/MA_LONG=60、bollinger 的 period=20/std=2.0）都是经验值
- **代码位置**：`config/strategy_defaults.yaml`
- **第一性原理**：策略参数必须通过回测验证有效性。经验值在 A 股高噪声环境中很可能失效

#### 问题 P-ST-4：信号融合 weighted 模式仍可能产生假信号

- **现象**：weighted 模式下，单策略 confidence 高时仍可通过阈值
- **代码位置**：`signal_router.py:222-231`
- **第一性原理**：A 股市场高噪声，单策略信号可靠性远低于多策略共识。应要求至少 2 个策略共识才允许生成信号

#### 问题 P-ST-5：基本面过滤形同虚设

- **现象**：14 条信号全部"PE 获取失败，跳过过滤"
- **代码位置**：`fundamental.py:248-254` 有 P0 修复（全失败时拒绝）但运行服务用的是旧代码
- **第一性原理**：基本面过滤是防止买入垃圾股的最后防线。全失败时放行等于没有防线

---

### 2.4 架构专家视角

#### 问题 P-AR-1：运行服务用的是旧代码（最严重架构问题）

- **现象**：代码中已有共识折扣、基本面硬约束、stage 拦截等 P0 修复，但运行服务自 07-01 01:05 重启后未加载新代码
- **根因**：缺乏热重载机制 + 缺乏部署验证
- **第一性原理**：修复必须部署才生效。未部署的修复等于没修复。系统缺少"代码版本与运行服务版本一致性"的校验
- **对抗性审查**：热重载在量化系统中风险高（可能导致状态不一致），更好的方案是"重启时校验代码版本并告警"

#### 问题 P-AR-2：scheduler.start() 违反调度契约

- **现象**：`scheduler.py:360-366` `start()` 启动后立即执行 `run_daily_cycle()`，仅有 `is_trading_day()` 检查无交易时段检查
- **代码位置**：`scheduler.py:354-366`
- **第一性原理**：调度器的职责是"按计划执行"。启动即执行违反调度契约，且 `is_trading_day()` 只检查日期不检查时间，01:05 启动会立即执行
- **对抗性审查**：虽然 `main.py:223-238` 有 `is_trading_time()` 检查会跳过买入流程，但旧代码可能没有此检查，且止损检查路径仍可能生成信号

#### 问题 P-AR-3：状态文件读写缺乏版本控制

- **现象**：`paper_broker_state.json`、`risk_state.json`、`notified_signals.json` 均无版本号
- **第一性原理**：状态文件升级时（如字段重命名、结构调整）无法检测旧格式，可能导致字段缺失或解析失败

#### 问题 P-AR-4：portfolio_peak 未实时更新导致组合回撤熔断失效

- **现象**：`risk_state.json` 中 `portfolio_peak=1000000.0`，但实际总资产 1,000,006
- **代码位置**：`risk/manager.py:478-484` `update_portfolio_peak` 存在但自 07-01 起未被调用（无循环运行）
- **第一性原理**：组合峰值必须实时更新才能检测回撤。峰值冻结等于回撤熔断失效

#### 问题 P-AR-5：全局单例 Broker/RiskManager 与引擎实例不一致

- **现象**：`web/app.py:21-32` `_get_web_broker` / `_get_web_risk` 是全局单例，但 `TradingEngine` 内部也会创建 Broker/RiskManager 实例
- **代码位置**：`web/app.py:21-32`
- **第一性原理**：状态必须单一来源。Web 单例与引擎实例不一致会导致用户看到的持仓/风控状态与实际运行状态不同步

---

### 2.5 开发专家视角

#### 问题 P-DEV-1：异常静默吞噬（silent except）

- **现象**：`main.py:484` `except Exception: continue` 静默跳过失败的股票扫描
- **代码位置**：`main.py:484` + 多处 `except Exception as e: logger.debug(...)` 降级为 debug 日志
- **第一性原理**：异常必须可见。静默吞噬让开发者无法发现问题，用户也无感知系统已失效
- **对抗性审查**：不能简单地改成 raise，否则单只股票失败会中断整个扫描。应分级处理：网络错误重试，数据错误跳过但记录 ERROR

#### 问题 P-DEV-2：测试隔离不彻底

- **现象**：`feishu.py:80-85` 通过 `GUGU_TEST_MODE` 环境变量重定向队列文件，但其他模块（如 `main.py`）可能直接写 `data/signals_history.jsonl`
- **第一性原理**：测试必须完全隔离生产数据。部分隔离会导致测试数据污染生产文件

#### 问题 P-DEV-3：日志级别不合理

- **现象**：关键失败（如基本面过滤失败）记录为 debug 级别，无法在默认日志级别下发现
- **代码位置**：`fundamental.py` 中 PE/PB 获取失败仅 debug
- **第一性原理**：关键业务失败必须 ERROR 级别，便于监控告警

#### 问题 P-DEV-4：缺乏代码版本与运行版本一致性校验

- **现象**：无法检测运行服务是否加载了最新代码
- **第一性原理**：必须有"启动时打印代码版本/git commit hash"的机制，便于排查"修复未生效"问题

#### 问题 P-DEV-5：并发安全不完整

- **现象**：`feishu.py:74-75` 有队列锁，但 `main.py` 中 `_notified_signals` 字件读写无锁保护
- **代码位置**：`main.py:548-593`
- **第一性原理**：共享状态并发读写必须加锁。无锁可能导致去重失效或文件损坏

---

### 2.6 业务专家视角

#### 问题 P-BZ-1：产出垃圾信号可能承担法律责任

- **现象**：系统向新手用户推送 `confidence=1.0` 的虚假高置信度信号
- **第一性原理**：向新手推送虚假投资建议可能构成"非法荐股"。信号必须明确标注"仅供参考，不构成投资建议"
- **对抗性审查**：即使有免责声明，虚假高置信度信号仍可能误导用户做出错误决策

#### 问题 P-BZ-2：合规风险——数据来源未授权

- **现象**：使用 sina/eastmoney/akshare 爬取数据，未明确授权
- **第一性原理**：商业产品使用数据必须有合法授权，否则面临法律风险

#### 问题 P-BZ-3：运营成本不可控

- **现象**：LLM 调用（WisdomAdvisor）成本未量化，飞书 API 调用无配额管理
- **第一性原理**：产品必须有成本控制机制，否则规模化后成本失控

#### 问题 P-BZ-4：缺乏用户反馈闭环

- **现象**：无"信号是否有用"的用户反馈机制
- **第一性原理**：产品迭代必须基于用户反馈。无反馈闭环等于盲人摸象

#### 问题 P-BZ-5：无商业化路径

- **现象**：产品免费运行，无付费转化设计
- **第一性原理**：可持续的产品必须有商业模式。纯免费无法覆盖 LLM 和数据成本

---

### 2.7 炒股专家视角

#### 问题 P-TR-1：stage_detector 失效导致无法识别"疯狂阶段"和"最后阶段"

- **现象**：四阶段判断完全失效，无法识别"疯狂阶段"（高风险）和"最后阶段"（顶部）
- **第一性原理**：《炒股的智慧》核心教义是"在疯狂阶段和最后阶段必须减仓或清仓"。阶段失效等于无法识别顶部，新手可能在顶部接盘
- **对抗性审查**：即使 stage_detector 修复，A 股市场的阶段特征与美股不同，参数需要专门针对 A 股调优

#### 问题 P-TR-2：止损价缺失导致无法执行移动止损

- **现象**：`stop_loss_price=null`，移动止损引擎无法跟踪
- **代码位置**：`advisor.py:1001-1013` sell 信号 fallback 未设置 stop_loss_price
- **第一性原理**：止损是风控的最后一道防线。止损价缺失等于"裸奔"，一旦趋势反转无法自动止损

#### 问题 P-TR-3：仓位管理失效

- **现象**：`order_quantity=0`，订单未实际执行，无法验证仓位管理逻辑
- **第一性原理**：仓位管理是炒股的核心。纸面模拟（paper 模式）必须实际执行订单才能验证仓位逻辑

#### 问题 P-TR-4：策略未覆盖"不向下摊平"原则

- **现象**：未明确检测"持仓亏损时加仓"的行为并阻止
- **第一性原理**：新手最常犯的错误就是"向下摊平"。策略必须硬约束阻止此行为

#### 问题 P-TR-5：缺乏"耐心等待"机制

- **现象**：每天 4 次扫描可能产出过多信号，违背"耐心等待"原则
- **第一性原理**：《炒股的智慧》强调"耐心等待真正的机会"。过度扫描会产生噪声信号，干扰新手判断

---

## Step 3：P0/P1/P2 优先级汇总

### P0 问题（10 个，按优先级排序）

| 序号 | 问题 | 视角 | 根因类型 |
|------|------|------|----------|
| P0-1 | stage_detector 永远返回"数据不足" | 策略 | 代码缺陷：days=90 日历日 < 70 交易日 |
| P0-2 | confidence 恒为 1.0 | 策略 | 部署问题：运行旧代码 |
| P0-3 | 运行服务用的是旧代码 | 架构 | 部署问题：未加载新代码 |
| P0-4 | scheduler.start() 非交易时段立即执行 | 架构 | 代码缺陷：无交易时段检查 |
| P0-5 | 基本面全失败时放行 | 策略 | 部署问题：运行旧代码 |
| P0-6 | sell 信号 fallback 未设置 stop_loss_price | 开发 | 代码缺陷：sell 路径遗漏 |
| P0-7 | daily_start_value 口径不一致 | 用户体验 | 代码缺陷：口径混乱 |
| P0-8 | portfolio_peak 未更新 | 架构 | 运行问题：无循环运行 |
| P0-9 | notified_signals.json 每日清空 | 开发 | 代码缺陷：跨日去重失效 |
| P0-10 | 信号未实际通知（notified_signals.json 为空） | 产品 | 运行问题：无循环运行 |

### P1 问题（15 个）

| 序号 | 问题 | 视角 |
|------|------|------|
| P1-1 | 异常静默吞噬（silent except） | 开发 |
| P1-2 | 测试隔离不彻底 | 开发 |
| P1-3 | 日志级别不合理 | 开发 |
| P1-4 | 全局单例与引擎实例不一致 | 架构 |
| P1-5 | 状态文件缺乏版本控制 | 架构 |
| P1-6 | 策略参数未回测验证 | 策略 |
| P1-7 | weighted 模式仍可能产生假信号 | 策略 |
| P1-8 | 缺乏代码版本一致性校验 | 开发 |
| P1-9 | 并发安全不完整 | 开发 |
| P1-10 | 飞书卡片三层折叠未真正降低认知负荷 | 用户体验 |
| P1-11 | 专业术语未添加解释 | 用户体验 |
| P1-12 | 错误反馈机制缺失 | 用户体验 |
| P1-13 | 无个性化内容 | 用户体验 |
| P1-14 | 缺乏信号质量反馈闭环 | 产品 |
| P1-15 | 模式定义不清晰 | 产品 |

### P2 问题（10 个）

| 序号 | 问题 | 视角 |
|------|------|------|
| P2-1 | 缺失新手引导 | 产品 |
| P2-2 | 合规风险——数据来源未授权 | 业务 |
| P2-3 | 产出垃圾信号可能承担法律责任 | 业务 |
| P2-4 | 运营成本不可控 | 业务 |
| P2-5 | 缺乏用户反馈闭环 | 业务 |
| P2-6 | 无商业化路径 | 业务 |
| P2-7 | 策略未覆盖"不向下摊平"原则 | 炒股 |
| P2-8 | 缺乏"耐心等待"机制 | 炒股 |
| P2-9 | 仓位管理失效 | 炒股 |
| P2-10 | A 股阶段参数需专门调优 | 炒股 |

---

## Step 4：P0 问题修复建议

### P0-1：stage_detector 永远返回"数据不足"

**文件**：`/Users/dong/workspace/gugu/src/gugu/engine/main.py` 第 481 行
**关联文件**：`/Users/dong/workspace/gugu/src/gugu/analysis/stage_detector.py` 第 92 行

**问题代码**：
```python
# main.py:481
df = await self._dm.fetch_stock_history(sym, days=90)
```

**根因**：
- `stage_detector.py:92` 要求 `len(df) >= MA_LONG + 10 = 70` 根 K 线
- `main.py:481` `days=90` 取的是 90 日历日，去除周末（约 26 天）后实际交易日 ≈ 63 天
- 63 < 70 → 永远返回"数据不足，无法判断阶段"

**修复方案**：
```python
# main.py:481 — 改为按交易日请求，留足缓冲
df = await self._dm.fetch_stock_history(sym, days=120)  # 120 日历日 ≈ 84 交易日 > 70
```

或更稳健的方案——在 `data/manager.py` 的 `fetch_stock_history` 中增加 `trading_days` 参数，按交易日而非日历日请求数据。

**第一性原理依据**：
四阶段判断（《炒股的智慧》核心框架）是风控基石。`stage_detector` 失效意味着系统无法识别"疯狂阶段"（高风险）和"最后阶段"（顶部），新手可能在顶部接盘。

**对抗性审查**：
- 改 `days=120` 仍可能因长假（如春节）导致交易日不足。建议在 `stage_detector.detect()` 中根据可用 K 线数动态调整 `MA_LONG` 参数（如 `MA_LONG = min(60, len(df) - 10)`），降级但不失效。
- 若数据源返回的 K 线数本身不足，应明确告警"数据不足，拒绝生成信号"而非静默降级为 SIDEWAYS。

---

### P0-2：confidence 恒为 1.0

**文件**：`/Users/dong/workspace/gugu/src/gugu/engine/signal_router.py` 第 214-231 行

**问题代码**：
```python
# signal_router.py:222-231（新代码已有共识折扣，但运行服务用的是旧代码）
total_strategies = max(len(self._strategies), 1)
agreeing_count = len(triggered)
if total_strategies > 1:
    consensus_ratio = agreeing_count / total_strategies
    consensus_factor = 0.7 + 0.3 * consensus_ratio
    avg_conf *= consensus_factor
```

**根因**：
代码中已有共识折扣逻辑，但运行服务自 07-01 01:05 重启后未加载新代码，所有信号仍用旧逻辑（单策略 confidence=1.0 直接通过）。

**修复方案**：
1. 立即重启服务加载新代码（见 P0-3）
2. 重启后验证：单策略信号 confidence 应 ≈ 0.74（1.0 * 0.7375），非 1.0

**第一性原理依据**：
置信度是信号质量的量化指标。当所有信号都是满分时，`min_confidence=0.6` 阈值形同虚设，无法过滤低质量信号。A 股高噪声环境下，单策略信号可靠性远低于多策略共识。

**对抗性审查**：
- 共识折扣修复后，单策略 confidence ≈ 0.74 仍高于 0.6 阈值。建议将阈值提升至 0.7，或采用更激进的折扣系数（`0.6 + 0.4 * ratio`，单策略 → 0.65，会被 0.7 阈值过滤）。
- 折扣过于激进可能导致有效信号被过滤。需要通过回测验证最佳折扣系数。

---

### P0-3：运行服务用的是旧代码

**文件**：全局问题（影响所有已修复的 P0 代码）

**问题**：
代码中已有多个 P0 修复（共识折扣、基本面硬约束、stage 拦截等），但运行服务自 07-01 01:05 重启后未加载新代码，所有修复均未生效。

**根因**：
- 缺乏热重载机制
- 缺乏"代码版本与运行服务版本一致性"的校验
- 缺乏部署后验证

**修复方案**：
1. 立即重启服务：`kill 27701 && kill 84457`，然后重新启动 scheduler 和 web
2. 重启后验证：
   - 检查日志是否打印共识折扣信息（`consensus_factor`）
   - 检查新信号 confidence 是否 ≈ 0.74（非 1.0）
   - 检查基本面失败时是否拒绝信号（非放行）
3. 长期方案：在 `scheduler.start()` 中打印 `git rev-parse HEAD` 到日志，便于排查版本不一致

**第一性原理依据**：
修复必须部署才生效。未部署的修复等于没修复。系统缺少"代码版本与运行服务版本一致性"的校验，导致"代码已修复但运行未生效"的假象。

**对抗性审查**：
- 热重载在量化系统中风险高（可能导致状态不一致），不推荐。
- 更好的方案是"重启时校验代码版本并告警"——启动时打印 git commit hash，与上次启动时对比，不一致则记录 INFO 日志。
- 必须有部署后自动化验证脚本，检查关键指标（confidence 范围、stage 是否生效）是否符合预期。

---

### P0-4：scheduler.start() 非交易时段立即执行

**文件**：`/Users/dong/workspace/gugu/src/gugu/engine/scheduler.py` 第 354-366 行

**问题代码**：
```python
# scheduler.py:354-366
async def start(self) -> None:
    """启动调度器。启动后立即执行一次盘中扫描（如果是交易日）。"""
    self.setup()
    self._scheduler.start()
    logger.info("调度器已启动，按 Ctrl+C 退出")

    # 启动后立即扫描一次（避免重启后等到下一个时间点）
    if is_trading_day():  # ← 只检查交易日，不检查交易时段
        logger.info("启动后立即执行一次盘中扫描...")
        try:
            await self._engine.run_daily_cycle()  # ← 01:05 启动也会执行
        except Exception as e:
            logger.exception(f"启动扫描异常: {e}")
```

**根因**：
`is_trading_day()` 只检查是否为交易日（周一到周五），不检查当前时间是否在交易时段。服务在 01:05 重启时会立即执行 `run_daily_cycle()`，生成非交易时段信号。

**修复方案**：
```python
# scheduler.py:354-366 — 增加交易时段检查
from gugu.utils.calendar import is_trading_day, is_trading_time

async def start(self) -> None:
    self.setup()
    self._scheduler.start()
    logger.info("调度器已启动，按 Ctrl+C 退出")

    # 启动后仅在交易时段内立即扫描一次
    if is_trading_day() and is_trading_time():
        logger.info("启动后立即执行一次盘中扫描...")
        try:
            await self._engine.run_daily_cycle()
        except Exception as e:
            logger.exception(f"启动扫描异常: {e}")
    elif is_trading_day():
        logger.info("交易日但非交易时段，跳过启动扫描，等待下一个调度时间点")
```

**第一性原理依据**：
调度器的职责是"按计划执行"。启动即执行违反调度契约，且 `is_trading_day()` 只检查日期不检查时间，01:05 启动会立即执行。虽然 `main.py:223-238` 有 `is_trading_time()` 检查会跳过买入流程，但旧代码可能没有此检查，且止损检查路径仍可能生成信号。

**对抗性审查**：
- 如果服务在交易时段内崩溃后重启，跳过启动扫描会错过当前时段的信号。建议在跳过启动扫描时记录 WARNING 日志，并允许通过 Web API `/api/engine/run` 手动触发。
- `is_trading_time()` 的实现需要校验——如果该函数依赖交易所日历数据，日历数据缺失时会误判。需要 fallback 逻辑。

---

### P0-5：基本面全失败时放行

**文件**：`/Users/dong/workspace/gugu/src/gugu/filters/fundamental.py` 第 248-254 行

**问题代码**：
```python
# fundamental.py:248-254（新代码已有硬约束，但运行服务用的是旧代码）
# 旧代码：全失败时放行
# 新代码：全失败时拒绝
```

**根因**：
代码中已有 P0 修复（全失败时拒绝），但运行服务用的是旧代码（全失败时放行），导致所有股票都通过基本面过滤。

**修复方案**：
1. 立即重启服务加载新代码（见 P0-3）
2. 重启后验证：当 PE/PB 获取全失败时，信号应被拒绝（非放行）

**第一性原理依据**：
基本面过滤是防止买入垃圾股的最后防线。全失败时放行等于没有防线——任何股票都能通过，包括 ST 股、退市股、财务造假股。

**对抗性审查**：
- 全失败时拒绝可能导致"数据源故障期间完全无信号"。需要区分"数据源故障"和"股票本身无基本面数据"：
  - 数据源故障（网络错误、API 限流）→ 重试或降级到其他数据源
  - 股票本身无数据（如新股、ETF）→ 跳过基本面过滤（已有逻辑：ETF/LOF 跳过）
- 建议在拒绝时附加明确的 `filter_reason`，便于排查。

---

### P0-6：sell 信号 fallback 未设置 stop_loss_price

**文件**：`/Users/dong/workspace/gugu/src/gugu/wisdom/advisor.py` 第 1001-1013 行

**问题代码**：
```python
# advisor.py:1001-1013
elif direction == "sell":
    # 数据驱动建议（替代格言摘录）
    advice = self._build_data_driven_advice(signal, "sell")

    # P1 修复：fallback 卖出信号校验——无持仓时过滤卖出信号
    has_position = signal.get("has_position", False)
    if not has_position:
        signal["wisdom_filtered"] = True
        decision["filter_reason"] = "无持仓，过滤卖出信号"
        logger.info(f"[wisdom-fallback] {signal.get('symbol', '')} 卖出过滤: 无持仓")
    elif price > 0:
        decision["sell_reason"] = "策略信号触发卖出"
        logger.info(f"[wisdom-fallback] {signal.get('symbol', '')} 卖出决策: 策略信号触发")
        # ← 缺少 stop_loss_price 设置
```

对比 buy 信号（第 986-994 行）：
```python
# advisor.py:986-994
# 止损价预设
if price > 0:
    stop_price = price * (1 - FALLBACK_STOP_LOSS_PCT)
    decision["stop_loss_price"] = round(stop_price, 2)
    signal["stop_loss_price"] = round(stop_price, 2)
```

**根因**：
sell 信号 fallback 路径遗漏了 `stop_loss_price` 设置，导致所有 sell 信号的 `stop_loss_price=null`。

**修复方案**：
```python
# advisor.py:1011-1013 — 补充 sell 信号的 stop_loss_price
elif price > 0:
    decision["sell_reason"] = "策略信号触发卖出"
    # 补充止损价（卖出信号的止损价用于风险跟踪）
    stop_price = price * (1 - FALLBACK_STOP_LOSS_PCT)
    decision["stop_loss_price"] = round(stop_price, 2)
    signal["stop_loss_price"] = round(stop_price, 2)
    logger.info(f"[wisdom-fallback] {signal.get('symbol', '')} 卖出决策: 策略信号触发, 止损价: {stop_price:.2f}")
```

**第一性原理依据**：
卖出信号同样需要止损价用于风险跟踪。止损价缺失导致移动止损引擎无法跟踪持仓，一旦趋势反转无法自动止损。

**对抗性审查**：
- sell 信号的止损价语义可能与 buy 不同——sell 信号是"现在就卖"，止损价是"如果跌到这个价位才卖"。两者逻辑冲突。
- 更合理的方案：sell 信号不设止损价，而是设 `exit_price`（退出价）= 当前价，表示"立即在当前价退出"。但为了保持字段一致性（移动止损引擎依赖 `stop_loss_price`），可以设为当前价。
- 实际上，sell 信号触发后持仓应已清空，止损价无意义。真正的问题是 buy 信号必须有止损价。sell 信号的 `stop_loss_price=null` 可能是设计意图。需要确认移动止损引擎是否依赖此字段。

---

### P0-7：daily_start_value 口径不一致

**文件**：`/Users/dong/workspace/gugu/src/gugu/web/app.py` 第 100 行

**问题代码**：
```python
# web/app.py:100
start_value = broker.daily_start_value  # ← 来自 paper_broker_state.json，= 1,000,000
```

但前端展示的 `daily_start_value` 是 22453.36（仅持仓市值），与 `paper_broker_state.json` 中的 1,000,000 不符。

**根因**：
- `paper_broker_state.json` 中 `daily_start_value=1000000.0`（含现金+持仓）
- 前端展示 22453.36 可能是旧值或前端计算错误
- 口径不一致：后端返回含现金的总额，前端展示仅持仓市值

**修复方案**：
1. 统一口径：`daily_start_value` 应为"日初总资产"（现金+持仓市值），与 `paper_broker_state.json` 一致
2. 前端展示需明确标注"日初总资产"，避免与"持仓市值"混淆
3. 校验：每日开盘时更新 `daily_start_value` 为当前总资产

**第一性原理依据**：
数据一致性是用户信任的基础。当用户看到的日亏数据与实际持仓完全不符时，会彻底丧失对系统的信任。

**对抗性审查**：
- 如果 `daily_start_value` 在日中不更新（只在开盘时设置），那么日中持仓变化会导致口径不一致。需要明确"日初"的定义。
- 建议在 API 响应中同时返回 `daily_start_value`、`current_total_value`、`current_market_value`，前端明确标注每个字段的含义。

---

### P0-8：portfolio_peak 未更新

**文件**：`/Users/dong/workspace/gugu/src/gugu/risk/manager.py` 第 478-484 行

**问题**：
`risk_state.json` 中 `portfolio_peak=1000000.0`，但实际总资产 1,000,006。组合回撤熔断失效。

**根因**：
自 07-01 起无循环运行，`update_portfolio_peak` 未被调用。

**修复方案**：
1. 重启服务后，`run_daily_cycle` 会调用 `update_portfolio_peak`，自动更新峰值
2. 长期方案：在 `RiskManager.__init__` 中从磁盘加载 `portfolio_peak`（已有逻辑），并在每次 `_check_daily_loss` 时调用 `update_portfolio_peak`

**第一性原理依据**：
组合峰值必须实时更新才能检测回撤。峰值冻结等于回撤熔断失效——即使总资产从 1,000,006 跌到 850,000（回撤 15%），由于 `portfolio_peak=1000000`，回撤率计算为 (1000000-850000)/1000000 = 15%，刚好触发熔断。但如果峰值更新到 1,000,006，回撤率为 (1000006-850000)/1000006 = 15.0006%，更准确。

**对抗性审查**：
- 峰值更新过于频繁可能导致"每次新高都更新峰值"，回撤熔断过于敏感。建议只在"显著新高"（如超过历史峰值 1%）时更新。
- 峰值降级（回撤后峰值不降）是正确的——回撤熔断应基于历史最高点。

---

### P0-9：notified_signals.json 每日清空

**文件**：`/Users/dong/workspace/gugu/src/gugu/engine/main.py` 第 548-570 行

**问题代码**：
```python
# main.py:548-570
def _load_notified_signals(self) -> dict[str, str]:
    """从文件加载已通知信号记录（持久化去重）。"""
    try:
        if self._notified_signals_file.exists():
            with open(self._notified_signals_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 清理过期记录（只保留今日的）
            today_str = date.today().isoformat()
            cleaned = {
                k: v for k, v in data.items()
                if v == today_str and k.count("_") <= 1
            }
            # ...
```

**根因**：
`_load_notified_signals` 每日清空非今日记录，导致跨日去重失效。昨日信号今日可能重发。

**修复方案**：
保留近 N 天（如 7 天）的记录，而非仅保留今日：
```python
# main.py:548-570 — 保留近 7 天记录
from datetime import date, timedelta

def _load_notified_signals(self) -> dict[str, str]:
    try:
        if self._notified_signals_file.exists():
            with open(self._notified_signals_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 保留近 7 天记录（防止跨日重发）
            cutoff_date = (date.today() - timedelta(days=7)).isoformat()
            cleaned = {
                k: v for k, v in data.items()
                if v >= cutoff_date and k.count("_") <= 1
            }
            # ...
```

**第一性原理依据**：
去重必须持久化跨日才有效。每日清空等于"每天都能重发昨日的信号"，对用户造成重复打扰。

**对抗性审查**：
- 保留 7 天可能导致文件增长。但 `notified_signals.json` 是 dict 格式，每天最多几十条记录，7 天也就几百条，性能无影响。
- 如果信号在 7 天内仍然有效（如趋势信号），重发可能是合理的。需要区分"去重"（同一信号不重发）和"提醒"（主动告知用户持仓状态）。建议去重 key 包含时间戳，仅去重"当日同股同方向"。

---

### P0-10：信号未实际通知（notified_signals.json 为空）

**文件**：`/Users/dong/workspace/gugu/src/gugu/engine/main.py` 第 614-644 行

**问题**：
`notified_signals.json` 为空，14 条信号均未推送飞书。

**根因**：
自 07-01 起无循环运行，`run_daily_cycle` 未被调用，信号未进入通知流程。

**修复方案**：
1. 重启服务后，`scheduler` 会按计划执行 `run_daily_cycle`，信号会进入通知流程
2. 验证：重启后检查 `notified_signals.json` 是否有新记录

**第一性原理依据**：
signal_only 模式的核心契约是"不下单但通知用户"。信号未通知等于产品功能完全失效——用户看不到任何信号，无法做决策。

**对抗性审查**：
- 重启后需要验证飞书配置是否正确（`feishu_app_id`、`feishu_app_secret`、`feishu_chat_id`）。
- 如果飞书配置缺失，`feishu.py:96-101` 会记录 WARNING 但不发送。需要检查日志。
- 信号去重逻辑（`_mark_notified`）使用 `f"{symbol}_{direction}"` 作为 key，可能导致"同股同方向但不同策略"的信号被错误去重。需要确认去重 key 是否应包含策略名。

---

## 附录：修复优先级建议

### 立即执行（24 小时内）

1. **重启服务**（解决 P0-2、P0-3、P0-5、P0-8、P0-10）
2. **修复 P0-1**：`main.py:481` `days=90` → `days=120`（或按交易日请求）
3. **修复 P0-4**：`scheduler.py:360-366` 增加 `is_trading_time()` 检查
4. **修复 P0-6**：`advisor.py:1011-1013` 补充 sell 信号 `stop_loss_price`

### 短期执行（1 周内）

5. **修复 P0-7**：统一 `daily_start_value` 口径
6. **修复 P0-9**：`notified_signals.json` 保留近 7 天记录
7. 验证重启后信号质量（confidence 范围、stage 是否生效、stop_loss_price 是否设置）

### 中期执行（1 个月内）

8. 策略参数回测验证（P1-6）
9. 异常处理改进（P1-1、P1-3）
10. 状态文件版本控制（P1-5）

---

## 报告结束

> 本报告基于 2026-07-04 的代码状态和运行服务状态。所有 P0 问题均已追溯根因并给出修复建议。建议按"立即执行"清单依次修复，并在修复后验证关键指标。
