# gugu 产品文档

> 自动化 A 股交易系统 · 产品说明

---

## 一、产品定位

**gugu = 给开发者自用的自动化 A 股交易系统**

| 维度 | 说明 |
|------|------|
| 核心功能 | 免费行情采集 → 策略选股 → 自动买卖 → 飞书实时通知 |
| 目标用户 | 个人投资者（开发者本人），非通用 SaaS |
| 交付形态 | Python 后端服务 + 飞书卡片消息（无 GUI）+ Web 看板 |

## 二、解决的痛点

| 痛点 | gugu 的解决方案 |
|------|----------------|
| 没时间盯盘、执行交易纪律 | APScheduler 定时调度（4 次/日扫描），自动采集行情、执行信号、下单 |
| 人工选股易被情绪左右 | 8 个内置量化策略 + 多策略信号融合 + LLM 决策层（炒股的智慧），纪律性执行 |
| 缺统一风控 | 三级风控（L1 仓位 / L2 日亏 / L3 系统规则），铁律不可绕过，熔断持久化防重启绕过 |
| 信号/持仓/盈亏信息分散 | 飞书卡片消息统一推送（信号 / 日报 / 告警 / 回测 / 异常） |
| 策略验证成本高 | 自研回测引擎（含涨跌停检查 + wisdom 可选项）+ 模拟盘，零资金风险验证策略 |
| 数据源单点故障 | 四级冗余：主源 sina → 降级 eastmoney → 兜底 akshare → 终极兜底 tencent；大市数据三通道（akshare→新浪K线→东方财富） |

## 三、五阶段架构（渐进式）

| 阶段 | 名称 | 状态 | 说明 |
|------|------|------|------|
| 1 | 策略固化 | ✅ 已完成 | 自然语言 → Python 策略代码（LLM 生成 + 校验 + 注册） |
| 2 | 回测 | ✅ 已完成 | 历史数据验证策略有效性，输出收益/夏普/回撤/胜率，含涨跌停检查和 wisdom 可选 |
| 3 | 模拟盘 | ✅ 已完成 | 真实行情 + 模拟资金 + 飞书通知，零资金风险。状态持久化，进程重启自动恢复 |
| 4 | 小额实盘 | ⏳ 待启用 | QMT 接口 + 小号账户 + 人工确认机制 |
| 5 | 正式实盘 | ⏳ 未来 | 多数据源备份 + 监控告警 + 灾备 + 审计日志 |

## 四、核心功能

### 4.1 策略层

**核心策略分组**：

| 分组 | 策略 | 说明 |
|------|------|------|
| 已验证 | box_breakout | 达尔文箱体突破（Walk-Forward 通过，样本外最稳） |
| 已验证 | double_yin | 双阴钱袋子（主力洗盘结束+倍量反转，收益稳定为正） |
| 已验证 | rsi_reversal | RSI 超买超卖反弹（震荡市专用） |
| 实验性 | hot_money | 游资三数交易法（7 日内涨停 + 缩量买入/放量卖出） |
| 实验性 | ma5_momentum | MA5 动量突破（待 Walk-Forward 验证） |
| 实验性 | chenxuezhang | 陈学长不吃面规则策略（6 大高胜率规则组合） |
| 实验性 | linjudaye | 邻居大爷规则策略（8 大量价规则组合） |
| 实验性 | ml_gradient_boost | ML GradientBoosting 5 日收益预测（需预训练模型） |
| 历史/观察 | Turtle / DualMA / MACD | 趋势跟踪基础策略（当前未启用，保留代码） |
| 历史/观察 | KDJ / Bollinger / PullbackLow | 均值回归/低吸策略（当前未启用，保留代码） |
| 历史/观察 | NorthForceLong | 趋势向上+缩量回踩 MA20（回测未通过，持续调参） |
| 历史/观察 | DragonTigerShort | 涨停断板反包（回测观察通过，仅在 paper 观察） |
| 已验证 | rebound_elite | 断板反包精英追涨（基于事件研究验证的有效子集：高活跃妖股+放量反包追涨，5日+4.08%/10日+9.99%） |
| 实验性 | price_volume_signal | 换手率+量比尾盘黄金信号（四信号框架，5日收益+13.85%胜率82% bootstrap显著） |
| 已禁用 | IndustryCapitalMomentum | 行业资金强度+动量确认 v1.2（回测 -15.38%，待重构） |
| 已验证 | regime_cycle_four | 公众号《八月，来了~~》四周期模型（银山谷+MACD金叉潜伏期买/死亡谷+死叉幻灭期卖，事件研究验证 10日+2.19%） |
| 已验证 | death_valley_rebound | 幻灭期修复反弹（空头排列+20日跌>10%超跌+3日不创新低+KDJ金叉+站上5日线，验证期 10日+3.02% p=0.0012） |
| 已验证 | bollinger_oversold_rebound | 布林下轨超跌反弹（20日跌>12%+跌破下轨抢反弹，中轨/持有N日卖出，验证期 10日+3.65% p=0.0001） |

**当前启用策略（按市场状态分流）**：
- `bull/rally`：3 个已验证策略（box_breakout / double_yin / rsi_reversal）+ 5 个实验性策略（hot_money / ma5_momentum / chenxuezhang / linjudaye / ml_gradient_boost）
- `sideways`：3 个已验证策略（box_breakout / double_yin / rsi_reversal）
- `bear/crash`：空仓，不启用任何买入策略
- 实验性策略在 `signal_only/track_live` 模式下仅作为观察信号（卡片标注警告），仅在 `paper` 模式下参与融合

**策略池动态权重（v0.7.10+）**：`StrategyPool` 根据近 20 笔交易表现动态调整策略权重，连续亏损 5 次大幅降权但保留策略（不删除），连续盈利 3 次逐步恢复；权重持久化到 `data/strategy_pool_weights.json`，进程重启不丢失。

**买入成交量确认（v0.7.10+）**：`SignalRouter` 对买入信号应用成交量折扣，最新量能低于 20 日均量时按档位降低置信度（0.85 / 0.7 / 0.5），过滤缩量诱多/假突破。
**LLM 自然语言策略生成**：描述策略逻辑 → LLM 生成 Python 代码 → AST 校验 → 自动注册。

**策略注册中心**（v0.3.5+）：所有策略（信号生成/风控择时/止盈止损/Skill）统一登记到 `config/strategy_manifest.yaml`，支持：
- `python scripts/verify_strategies.py list` — 列出全量策略
- `python scripts/verify_strategies.py check` — 代码与清单一致性校验
- `python scripts/verify_strategies.py dashboard` — 推送策略看板到飞书
- `python scripts/verify_strategies.py source "炒股的智慧"` — 按出处查询
- `python scripts/verify_strategies.py params trailing_stop` — 查看策略参数

**财叔动态止盈减仓策略**（v0.3.5+）：+30% 减 1/3 → 每 +15% 减 10% → 大跌减半，与移动止损互补。

### 4.2 决策层（Wisdom）

融合 28 本股书智慧 + 4 个规则引擎的双层决策架构：

#### 认知注入层（LLM prompt 增强）

| 组件 | 说明 |
|------|------|
| BookPerspectiveRouter | 从 28 本股书蒸馏 SKILL 中按交易场景智能路由 2-4 个最相关视角 |
| WisdomAdvisor（LLM 决策） | 信号上下文 + 多视角认知 + SKILL.md → LLM → 结构化 JSON 决策 |
| 硬编码 fallback | LLM 不可用时降级到分层下注规则（首次 20%，加码 40%，止损 8%） |

#### 规则引擎层（可量化、不可绕过）

| 组件 | 功能 | 核心规则 |
|------|------|----------|
| StageDetector（四阶段判断器） | 判断牛皮市/正常升势/疯狂/最后阶段 | 疯狂/最后阶段禁止入场 |
| TrailingStopEngine（移动止损引擎） | 浪谷递进动态止损 | 跌破浪谷 1 次警惕/2 次警告/3 次清仓 |
| DangerSignalDetector（危险信号检测器） | 五大危险信号检测 | 量增价不涨/两天转头/坏消息/量价背离/长上影线 |
| NoAverageDownChecker（向下摊平检查器） | 禁止亏损仓位加码 | 亏损 >2% 时拦截加仓 |

**28 本书分类**：价值投资(5) + 成长选股(2) + 技术分析(4) + 交易系统(6) + 宏观经济(5) + 投资哲学(6)

### 4.3 事件驱动架构

交易循环的关键节点推送事件，可注册处理器实现扩展：

| 事件 | 触发时机 | 数据 |
|------|----------|------|
| cycle_start / cycle_end | 循环开始/结束 | 时间戳/状态/持仓数 |
| market_regime_change | 市场状态检测后 | 状态/置信度/仓位上限 |
| signal_generated | 策略信号产生 | 股票/方向/仓位/wisdom 决策 |
| order_submitted / order_filled | 订单提交/成交 | 股票/数量/价格/佣金 |
| risk_alert | 风控告警 | 级别/消息/建议 |
| stop_loss_triggered | 止损触发 | 股票/价格/数量 |
| daily_loss_warning / daily_loss_halted | 日亏预警/熔断 | 亏损比例 |

### 4.4 风控层（三级铁律）

| 级别 | 规则 | 动作 |
|------|------|------|
| L1 单股 | 单只仓位 ≤ 30% | 超限拒绝加仓 |
| L2 日亏 | ≥ 3% 预警，≥ 5% 熔断 | 预警飞书通知，熔断停止交易（持久化到文件，进程重启自动恢复）|
| L3 系统 | T+1 / 涨跌停 / 停牌 | 自动过滤 |

### 4.5 通知层（飞书卡片）

| 类型 | 触发时机 | 内容 | 来源 |
|------|----------|------|------|
| 买卖信号 | 策略产生信号 | 股票名称+代码/方向/策略/置信度/建议仓位/wisdom/历史胜率 | `formatter.py` |
| 信号汇总 | 09:10 / 11:35 / 15:10 | 市场概览/今日信号/持仓概况/绩效验证 | `formatter.py` |
| 风控告警 | 触发风控规则 | 级别/详情/建议动作 | `formatter.py` |
| 回测报告 | 回测完成 | 收益/夏普/回撤/胜率 | `formatter.py` |
| 系统异常 | 采集失败/引擎崩溃 | 异常详情/恢复建议（密钥自动脱敏） | `formatter.py` |
| 基金监控 | 15:35（交易日） | 基金净值/涨跌幅/波动率/回撤/定投建议 | `fund_monitor.py` |
| 资金流复盘 | 08:00（交易日） | 大盘资金流/行业TOP5/轮动强度/背离预警/概念热点/强流入个股 | `flow_report.py` |
| 盘中资金流决策报告 | 14:30（交易日） | 大盘资金面/热点行业/强流入个股/背离预警/操作原则 | `flow_report.py:build_afternoon_card()` |
| 资金流日报 | 15:10（交易日） | 当日大盘/行业TOP/轮动/背离/强流入个股/热门概念 | `flow_report.py` |
| 持仓卖出告警 | 收盘后（15:10 日报附送） | 检查持仓股卖出信号，主动推送告警 | `main.py:_check_holdings_sell_signals()` |
|| 宏观简报 | 08:00/09:15/11:40/15:15 | 金/油/汇/债/G 五维全球宏观数据 + 大师兄解读 | `macro/report.py` |
|| 十大视角日报 | 08:30（交易日） | 自选股池10视角综合分析（10位投资大师视角评分+综合判定+操作建议+价位） | `notifier/perspective_report.py` |

**配置开关**：通过 `config/settings.yaml` 中 `feishu` 段的开关控制：

```yaml
feishu:
  enabled: true              # 全局开关，false 禁用所有通知
  signal: true               # 信号通知
  daily_report: true         # 日报
  risk_alert: true           # 风控告警
  backtest: true             # 回测报告
  system_error: true         # 系统异常
```

**可靠性保证**：
- 失败通知自动持久化到 `data/notify_queue.jsonl`（原子写入 tmp + rename）
- 每 10 分钟重试队列中的失败通知，最多重试 5 次
- 所有通知类型共享同一个重试队列，包括宏观推送
- 密钥脱敏：错误消息中的 app_secret / api_key / token / Bearer 自动替换为 `***REDACTED***`

### 4.6 回测引擎

- 输入：历史 OHLCV 数据 + 策略 + 初始资金
- 交易成本：佣金 0.025% / 印花税 0.05%（卖出）/ 滑点 0.2%
- 输出：收益曲线、夏普比率、最大回撤、胜率、盈亏比
- 风控集成：L3 涨跌停检查（涨停不可买入、跌停不可卖出）
- Wisdom 可选：`enable_wisdom=True` 启用决策层增强

### 4.7 模拟盘

- T+1 结算（买入当日不可卖）
- 佣金 0.025% / 印花税 0.05%（卖出）/
- **流动性感知滑点模型**（P3 优化）：按市值分层（1000亿→10亿），滑点 0.05%→0.6%
- **部分成交模拟**（P3 优化）：小盘股部分成交概率 30%，大盘股 2%
- **涨跌停成交概率**：涨停买入/跌停卖出各 30% 成交概率
- **状态持久化**：JSON 文件存储（原子写入 + 备份），进程重启自动恢复持仓和交易记录
- 持仓盈亏实时更新
- CLI 持仓查看：`uv run python scripts/show_portfolio.py --all`

### 4.8 信号绩效验证闭环（v0.8.1）

信号发出后的实际收益是验证策略有效性的唯一标准。gugu 构建了完整的三阶段验证闭环：

| 阶段 | 时机 | 内容 | 工具 |
|------|------|------|------|
| 前瞻追踪 | 信号产生后 T+1/T+3/T+5/T+10 | `SignalTracker` 追踪信号后 N 日收益，标记"前瞻命中/未命中"，写入 `signals_history.jsonl` | `src/gugu/analysis/signal_tracker.py` |
| 次日验证 | T+1 09:35 盘中 / 15:05 收盘 | 尾盘选股候选的次日开盘溢价、盘中最高溢价、收盘收益、最大回撤、止损命中率，验证结果写入 `data/screener_validation.jsonl` | `screener/validator.py` |
| 绩效周报 | 每周一 08:00（自动推送飞书） | 汇总近 30 日命中率、策略收益对比、最佳/最差信号排行，含超额收益 vs 沪深 300 | `SignalTracker.generate_report(days=30)` + 飞书卡片 |

**绩效指标口径**（禁止互相替代或混用）：

| 指标 | 含义 | 使用边界 |
|------|------|----------|
| `execution_success_rate` | 订单执行成功率 | 只衡量订单执行，不代表盈利 |
| `forward_5d_signal_hit_rate` | 信号发出后五个交易日方向命中率 | 前向信号验证，不等同平仓盈利率 |
| `closed_trade_win_rate` | 已完成平仓交易的盈利占比 | 仅纳入完整平仓结果 |
| `realized_pnl` | 已实现盈亏 | 只统计已平仓结果 |
| `estimated_signal_pnl` | 按假设仓位估算的信号收益 | 估算值，不作为实际账户盈亏 |

### 4.9 数据源四级冗余机制

| 层级 | 源 | API 接口 | 能力 |
|------|----|----------|------|
| 主源 | SinaCollector | 新浪 `hq.sinajs.cn` + 网易 `quotes.money.163.com`（直连 HTTP） | 仅行情，无资金流 |
| 降级 1 | EastmoneyCollector | 东方财富 `push2.eastmoney.com` 直连（不依赖 akshare 库） | 历史行情 + 实时行情 + 行业/个股资金流 |
| 兜底 2 | AkshareCollector | 东方财富 `ak.stock_zh_a_hist()` + `ak.stock_zh_a_spot_em()` | 历史行情 + 实时行情 + 资金流 |
| 终极兜底 3 | TencentCollector | 腾讯财经 `ifzq.gtimg.cn` 直连（仅 qfq 日 K） | 前复权日 K 线（仅历史行情） |

- `fallback.py`（SinaCollector）不依赖 `akshare`，通过 `httpx` 直连新浪/网易原生 HTTP 接口
- `eastmoney_collector.py` 不依赖 `akshare`，通过 `httpx` 直连东方财富 push2 API
- `tencent_collector.py` 通过 urllib 直连腾讯 ifzq.gtimg.cn，提供最稳定的 qfq 日 K 兜底
- 主源连续失败 3 次 → 并发降级到所有备用源（FIRST_COMPLETED 模式取最快响应）
- 冷却 5 分钟后尝试切回主源
- 降级源也做数据质量校验

#### 大市数据三通道冗余（market_filter.py）

| 通道 | 源 | 接口 | 适用场景 |
|------|------|------|----------|
| 1 | akshare | `stock_zh_index_daily_em`（东方财富后端） | 主通道 |
| 2 | 新浪 K 线 JSON | `quotes.sina.cn/...getKLineData`（scale=240 日K） | akshare 被限流时降级 |
| 3 | 东方财富 push2 | `push2.eastmoney.com/api/qt/stock/kline/get` | 前两级均失败时兜底 |

- 大市数据用于 MA200 趋势判断（沪深300指数），决定是否允许选股
- 三通道逐级降级，前一级失败自动尝试下一级
- 三通道全失败时返回空 DataFrame，fail-safe 拒绝选股（保守不进场）

#### 数据质量保障

| 保障项 | 机制 |
|--------|------|
| 实时价格校验 | `price_is_realtime` 标记，实时行情失败时拒绝发送信号（不回退历史价） |
| 信号去重 | `notified_signals.json` 持久化，引擎重启后恢复去重状态 |
| 单位转换 | 东方财富 API 返回的价格 ×100、涨跌幅 ×100、资金流单位元（自动转亿） |
| 北向资金 | 数据不可用时显示"获取不到"（非 0），避免误导 |
| A 股指数 | 东方财富 f43/f170 字段自动 /100 转换为点位/百分比 |
|| 字段名统一 | akshare "今日"前缀字段与 HTTP API 字段自动映射统一 |

### 4.9 十大视角分析系统（v0.5.0）

将10位投资大师的认知框架量化为可计算的评分维度，对个股进行独立评分（0-10分），加权汇总为综合评分。

**10个视角**：

| 视角 | 权重 | 适合场景 |
|------|------|----------|
| 巴菲特 | 0.15 | 价值投资、护城河、ROE |
| 芒格 | 0.12 | 多元思维、盈利质量 |
| 索罗斯 | 0.10 | 反身性、市场环境 |
| 林奇 | 0.10 | PEG、成长性 |
| 利弗莫尔 | 0.10 | 趋势、关键点 |
| 邱国鹭 | 0.10 | 行业格局、胜率 |
| 陈江挺 | 0.08 | 趋势方向、止损 |
| 技术分析 | 0.10 | 均线、RSI、MACD |
| 投资习惯 | 0.08 | 纪律性、风险控制 |
| 大师兄 | 0.07 | 五维框架（金油汇债G） |

**输出**：
- 综合评分（0-10分）+ 判定（通过/不通过）
- 各视角独立评分 + 推理链
- 操作建议 + 入场价/止损价/目标价
- 核心优势 + 核心风险

**调度**：08:30（交易日），对自选股池运行分析，推送飞书报告卡片。

### 4.10 实时信号扫描 + 健康守护（v0.9.x）

**盘中实时信号扫描**（`src/gugu/engine/realtime_scanner.py`）：`RealtimeSignalScanner` 盘中每 5 分钟扫描一次自选股池，实时行情驱动信号生成，突破传统"定时任务扫描"延迟：
- 实时价格从主数据源（sina）获取，降级链自动切换
- 信号生成后进入标准管道（过滤 → Wisdom → 通知），与定时扫描共用同一套治理
- 与 scheduler 扫描互补：盘中高频覆盖 + 收盘低频确认

**健康守护**（`src/gugu/health/watchdog.py` + `scripts/health_watchdog.py`）：系统级 P0 守护，检测"调度器在跑但输出停止"的静默失效：
- 心跳新鲜度检查：调度器心跳 > 阈值未更新 → 告警
- 信号历史失忆检查：信号历史 > 2 天未写入 → "系统失忆"告警
- 启动记录检测：无启动记录/首次安装不误报
- 部署：cron `gugu-health-watchdog`（no_agent，每 5 分钟，静默模式——健康零输出、异常才推送飞书）

**P0 静默失效防线**（v0.9.6）：信号历史写入失败时异步飞书告警（数据完整性守护），测试环境下自动短路不泄漏。

**策略能力画像**（`src/gugu/strategies/capability.py`）：每个策略声明其能力维度（适合市场状态、持有周期、风险偏好），供策略路由与市场状态匹配使用。

## 当前运行契约（2026-07-21）

- `signal_only` 与 `track_live` 都是监控模式：只生成信号、记录历史并发送通知，不自动提交订单。`track_live` 额外读取用户真实持仓，用于跟踪和止损检查，但不会由系统自动交易。
- 所有买入候选统一经过十大视角门禁。门禁未通过的候选只能作为观察/研究结果展示，不能进入买入路径。
- 数据质量采用 `fresh`、`stale`、`partial`、`invalid`、`unavailable` 状态；只有 `fresh` 允许进入买入决策，其余状态只能用于研究或通知。
- 订单执行成功只表示订单执行结果为成功，不等于交易盈利。未完成平仓的记录不能作为已实现盈利样本。

## 五、使用方式

### 5.1 环境准备

```bash
# 克隆项目
git clone https://github.com/ClayDong/gugu.git
cd gugu

# 安装依赖（推荐 uv）
uv sync

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入飞书 app_id / app_secret / chat_id
```

### 5.2 配置

编辑 `config/settings.yaml`：

```yaml
watchlist: ["600519", "000858"]  # 自选股
auto_select: false                # 自动选股（默认关闭）
risk:
  max_position_ratio: 0.30        # L1 单股上限
  daily_loss_warn: 0.03           # L2 预警
  daily_loss_halt: 0.05           # L2 熔断
perspective:
  enabled: true                  # 十大视角分析开关
  auto_notify: true               # 自动推送飞书报告
```

### 5.3 运行

```bash
# 回测
uv run python scripts/run_backtest.py

# 模拟盘
uv run python scripts/run_paper.py

# Web 看板
uv run python scripts/run_web.py

# 实盘（阶段四）
uv run python scripts/run_live.py
```

## 六、能力边界与已知限制

1. **自动选股默认关闭**：数据源稳定性待验证，建议先手动配置自选股
2. **降级源仅行情**：SinaCollector 不支持行业/个股资金流明细（主源故障时自动选股跳过）
3. **交易日历降级仅覆盖当年**：跨年运行需注意
4. **飞书熔断恢复仅 CLI**：`engine.reset_halt()` 手动调用，仅清除熔断标志不重置日初净值
5. **监控依赖本地文件**：通过 `data/heartbeat.json` + `data/signals_history.jsonl` 实现可观测
6. **Web 看板支持信号/持仓/书籍视角查看**：4 标签页（总览/信号历史/持仓详情/书籍视角），但不支持操作
7. **宏观数据未融入交易决策**：宏观模块（`src/gugu/macro/`）采集金/油/汇/债/G 五维数据并通过飞书推送，
   但**不参与引擎的交易决策循环**（买入/卖出/仓位/风控）。详见第十一章。
8. **策略缺乏系统回测验证**：8 个内置策略未经过全市场多周期的统一回测，历史表现数据不可考。
   回测引擎支持单策略单股回测，但尚未建立策略评估流水线。详见第十二章。

## 七、可观测性设计

项目无 GUI 与后台 Dashboard，通过以下机制实现可观测性：

| 数据文件 | 格式 | 内容 | 用途 |
|----------|------|------|------|
| `data/heartbeat.json` | JSON | 最新一次循环状态 | 外部监控（如 cron 检查文件修改时间） |
| `data/heartbeat_history.jsonl` | JSONL | 历史心跳记录 | 回溯崩溃时间点与历史状态 |
| `data/signals_history.jsonl` | JSONL | 信号决策全链路记录 | 回溯"某日某股为何被过滤/下单/拦截" |
| `data/paper_broker_state.json` | JSON | 模拟盘持仓与交易 | 持久化状态，进程重启恢复 |
| `data/paper_broker_state.json.bak` | JSON | 状态文件备份 | 原子写入保护，崩溃可回滚 |
| `data/risk_state.json` | JSON | L2 熔断状态 | 进程重启自动恢复熔断 |
| `logs/gugu_*.log` | 文本 | 详细运行日志 | 调试与排障 |

**监控建议**：
- 用 cron 检查 `heartbeat.json` 修改时间，超 30 分钟未更新则告警
- 用 `jq` 解析 `signals_history.jsonl` 做信号统计分析
- 飞书通知是便捷推送，非监控方案；监控应基于本地文件 + 外部告警

## 八、迭代记录

| 轮次 | 日期 | 主要变更 |
|------|------|----------|
| 1-11 | 2026-06-22 ~ 2026-06-23 | 骨架搭建、策略固化、回测引擎、模拟盘、wisdom 决策层 |
| 12 | 2026-06-23 | **架构治理**：SignalPipeline 提取、链式校验、并发降级、强类型 Signal + AppConfig |
| 13 | 2026-06-23 | **测试覆盖**：analysis/filters 全覆盖（60+ 测试）、基准 CSV 数据、策略基准测试 |
| 14 | 2026-06-23 | **事件驱动**：EventEngine 完善（10 事件 + 7 put 点）、alpha/stock_ranker 测试 |
| 15 | 2026-06-24 | **配置集成**：AppConfig 三路输入（RiskManager + engine）、analysis 剩余模块测试、基准回测验证 |
| 16 | 2026-06-24 | **细粒度补全**：DataManager 扩展测试、SignalPipeline 独立测试、文档同步 |
|| 17 | 2026-06-24 | **仓颉深度融合 + P0 认知引擎**：28 本书多视角路由、四阶段判断器、移动止损引擎、危险信号检测器、向下摊平检查器、RegimeDetector bug 修复、Web 4 标签页 |
|| 18 | 2026-06-29 | **数据源修复 + 测试完善**：PROJECT_ROOT 路径修复（48 个测试一次性修复）、akshare 连接被封锁 → 切换 sina 为主源、异步测试 mock ranker 消除网络 IO、调度器 __main__ 入口、Cron 盘中自动扫描任务 |
|| 19 | 2026-06-29 | **宏观五维模块 + 多时段日报 + 项目全景分析**：新增 `src/gugu/macro/`（5 维度数据采集 + 飞书卡片 + 4 时段调度）、`docs/PROJECT_LANDSCAPE_ANALYSIS.md`（第一性原理+对抗性审查报告）、策略迁移评估表（18→8→+5）、Cron 更新（12 个定时任务含宏观）、PRODUCT.md 第九章合并计划、CLAUDE.md 项目状态表 |
| 20 | 2026-06-30 | **架构治理 v2**：配置双轨制消除（13 模块迁移到 AppConfig 单例，`settings()` 委托至 `get_config()`）、`_STOCK_NAMES` 三份重复去重到 `data/stock_names.py`、`TrailingStopEngine` ATR 自适应止损优先、熔断状态跨日同步（新增 `last_reset_date` 持久化）、`_check_stop_loss` 批量拉取优化、`PlausiblePriceRule` 低波动股防误判、Ruff 规则清理（255→0） |
| 21 | 2026-06-30 | **对抗性审查修复**：背测引擎认知模块异常不再静默吞（`logger.debug`）、周线 resample 消除前视偏差（`closed="left"`）、仓位基准改用当前净值（`current_equity` 替代 `initial_capital`）、资金流默认保守拒绝（`pass: False`）、选股器/筛股器 per-stock 超时保护、`is_tradable` 传入方向参数、错误消息密钥脱敏纵深防御、`formatter.py` `_STOCK_NAMES` 去重 |
| 22 | 2026-06-30 | **产品化 v1**：信号卡片 30 秒决策摘要行（置信度/仓位/止损/止盈一目了然）、ATR 波动率动态调整仓位（高波减 30%/低波加 30%）、止盈双目标方案（+10%/+20% + ATR 自适应校准）、信号融合切换为 weighted 模式、Bollinger+RSI 策略启用形成趋势/均值回归互补、`RiskManager` 新增组合相关性监控（>85%告警）和最大回撤熔断（15%）、`walk_forward_optimize()` 滚动前向验证+参数网格扫描、仓位分层递进（试仓 20%→盈利确认加码 60%→上限 30%）、Web 看板 `/api/signals/performance` 和 `/api/portfolio/correlation` 端点、文档同步更新 |
| 23 | 2026-06-30 | **Bug 修复 + 信号可信度闭环（v0.3.1）**：修复 `RiskManager.check_correlation` 在 FastAPI 中 `asyncio.run()` 死循环（改为 async）、修复 Feishu 卡片 subtitle 被静默丢弃（移至 card element 渲染）、修复 Web `/api/halt/reset` 操作错误的 RiskManager 实例（改用 `_web_risk` 单例）、修复 `_portfolio_peak` 跨实例类变量共享（移至实例属性）、移除 `signal_tracker.py` 死代码、修复 `walk_forward_optimize` 空参数网格时 `best_sharpe=-999`、新增 `_inject_performance_summary()` 将同类信号历史胜率注入飞书卡片（反馈闭环）、测试 541→550、覆盖率 70%→67%（新增 800 行） |
| 24 | 2026-06-30 | **产品完善 v2**：修复 SinaCollector 测试 mock 不兼容 Python 3.14 httpx 接口、修复 DataManager 测试 `_fresh_dm()` 实例化方式（`object.__new__` 规避配置读取）、统一数据源角色描述（sina 为主源、akshare 为降级源，含代码注释 + 配置默认值）、daemon 添加 watchdog 自动重启（崩溃自动拉起最多 10 次）、FeishuNotifier 接入配置开关（`enabled/notify_signal/daily_report/risk_alert/backtest/system_error` 全类型检查）、macro 通知支持外部 notifier 实例、PRODUCT.md 补充通知配置开关和重试队列说明、README.md 补充守护模式示例 |
| 25 | 2026-07-02 | **数据质量与通知优化（v0.3.3）**：修复飞书重复通知问题（信号去重机制持久化到 `data/notified_signals.json`，引擎重启后自动恢复）、新增大师兄解读功能（宏观日报添加 LLM 驱动的板块轮动分析视角）、强化价格数据校验（`price_is_realtime` 标记，实时行情获取失败时拒绝发送信号，防止历史收盘价污染通知）、测试 806 passed, 70% coverage |
| 26 | 2026-07-02 | **数据源健壮性增强（v0.3.4）**：新增东方财富直连采集器 `EastmoneyCollector`（第三备用源，不依赖 akshare 库，httpx 直连 push2 API）、大市数据三通道冗余（akshare→新浪K线→东方财富 push2，解决 akshare 被限流时选股阻塞）、资金流单位转换修复（元→亿）、A 股指数 /100 修正、北向资金不可用时显示"获取不到"、字段名统一（akshare "今日"前缀映射）、测试 818 passed |
| 27 | 2026-07-02 | **文档代码一致性修复 + Wisdom 异步（v0.3.5）**：12 项文档与实现歧义全部修复（market_filter fail-safe 注释、stock_selector 过滤范围、返回值语义、macro 大师兄异步补发、signal_pipeline Wisdom await+超时降级、batch 串行改并发、重试队列过期清理、定时任务错峰、DualThrust confirm 跳过说明、置信度归一化、PlausiblePrice 分级阈值、Parquet 缓存定期清理）、测试 818 passed |
| 28 | 2026-07-02 | **财叔动态止盈减仓策略（v0.3.5）**：新增 `DynamicTakeProfitEngine`（+30%减1/3→每+15%减10%→大跌减半），状态持久化到 `data/take_profit_state.json`，飞书卡片标注出处和策略逻辑，与移动止损互补（止损保本金、止盈锁利润）、测试 818 passed |
| 29 | 2026-07-02 | **策略注册中心（v0.3.5）**：新建 `config/strategy_manifest.yaml` 全量策略清单（26 个策略统一登记：8 信号生成+7 风控择时+1 止盈止损+9 Skill+1 动态生成器）、`strategies/manifest.py` 加载器（查询/统计/看板生成）、`scripts/verify_strategies.py` 验证脚本（list/check/dashboard/source/params 五个子命令）、飞书策略全景看板推送、测试 818 passed |
| 30 | 2026-07-02 | **产品文档完善（v0.3.6）**：新增第十一章(宏观数据未融入决策之分析)、第十二章(回测与策略验证必要性)、
    补充宏观模块当前定位与融合路线图、明确策略回测的必要性与步骤、能力边界补充 2 项已知限制、迭代记录更新 |
| 31 | 2026-07-17 | **主力资金跟随策略 v0.6.2**：新增 `NorthForceLongStrategy`（长线主力吸筹跟随）和 `DragonTigerShortStrategy`（短线龙虎榜反包跟随），均接入状态机精确持仓与连续 2 日跌破 MA20 止损；`dragon_tiger_short` 在 `bull/rally` 观察启用，`north_force_long` 回测未通过保持禁用；`scripts/optimize_params.py` 注册两只策略的参数网格、配置键和约束函数，`weekly_optimize_0300` 自动 Walk-Forward 调参；新增单元测试 20+，全量测试 1620 passed；同步更新 `PRODUCT.md` / `TECH.md` / `strategy_manifest.yaml` |

## 九、项目关系与合并计划

**gugu 是唯一的活跃项目**。以下是与本系统相关的其他项目的关系：

| 项目 | 状态 | 关系 |
|------|------|------|
| **MakingMoney** (`~/workspace/MakingMoney/`) | 🟡 冻结 | 全栈量化平台 v1.0。所含功能已被 gugu 重构替代。不再修改 |
| **MingCe（明策）** (`~/workspace/MingCe/`) | 🟢 已迁移 | 日报 Bot v1.5。当前生产运行（端口 8000），计划将日报/宏观功能逐步迁移到 gugu 后归档 |
| **realtime-flow** | 🔴 已归档 | 资金流监测系统。核心逻辑已被 gugu 的 `src/gugu/analysis/` + `src/gugu/filters/` 吸收 |
| **gugu** | 🟢 **唯一活跃** | 未来所有迭代只在此进行 |

### 9.1 迁移优先级

| 优先 | 功能 | 来源 | 目标模块（gugu） |
|------|------|------|-----------------|
| P0 | 五维宏观框架（金油汇债G） | MingCe | `src/gugu/macro/` + 飞书宏观卡片 |
| P0 | 多时段日报（早间/午间/收盘/基金） | MingCe | `src/gugu/notifier/` 日报模板 |
| P1 | 数据质量评分系统 | MingCe | `src/gugu/data/quality.py` 扩展 |
| P2 | 交接生产（替换端口 8000/8765） | MingCe→gugu | launchd 切换 |
| P3 | MakingMoney 归档 | MakingMoney | 移出 workspace 或标注 DEPRECATED |

### 9.2 决策依据

详见 `docs/PROJECT_LANDSCAPE_ANALYSIS.md`（第一性原理 + 对抗性审查报告）

**当前状态**：818 tests passed with 70% coverage v0.3.6

---

## 十一、宏观数据模块分析

### 11.1 现状

`src/gugu/macro/` 模块采集 5 个维度的宏观数据：

| 维度 | 数据源 | 更新频率 |
|------|--------|----------|
| 黄金 | akshare | 每次推送 |
| 原油 | akshare | 每次推送 |
| 外汇（美元/人民币） | akshare | 每次推送 |
| 债券（国债收益率） | akshare | 每次推送 |
| A 股指数 | 三级冗余通道 | 每次推送 |

每日 4 次定时推送（08:00/09:15/11:40/15:15），附带大师兄解读（LLM 生成）。

### 11.2 当前定位

宏观模块是一个**独立的信息推送系统**，与交易决策引擎完全解耦：

```
macro/collectors.py → macro/report.py → Feishu (用户看)
                          ↑
                    决策引擎完全不依赖 macro 数据
```

### 11.3 未融合的原因

| 原因 | 说明 |
|------|------|
| **P0 优先** | 项目早期优先建设短线交易闭环（信号→决策→执行→风控） |
| **数据可靠性** | 宏观数据源（akshare）稳定性不如行情数据，作为决策依据风险较高 |
| **回测不可行** | 缺乏长周期宏观历史数据，无法验证宏观因子有效性 |
| **设计决策** | 目前作为信息推送，让用户自行判断；未来可逐步接入 |

### 11.4 融合路线图（未来）

| 阶段 | 内容 | 复杂度 |
|------|------|--------|
| P1 | 宏观因子注入 RegimeDetector：金油汇债 → 市场状态判断 | 中 |
| P2 | 宏观因子注入 Wisdom prompt：LLM 决策时看到当前宏观背景 | 低 |
| P3 | 宏观因子驱动风控：利率急剧上升时自动收紧仓位上限 | 高 |
| P4 | 宏观因子驱动策略权重：美元走强时降低周期股策略权重 | 高 |

---

## 十二、回测与策略验证

### 12.1 现有回测能力

项目内置了自研回测引擎（`src/gugu/backtest/`），支持：

| 能力 | 说明 |
|------|------|
| 单股单策略回测 | `python scripts/run_backtest.py --symbol 600519 --strategy turtle --days 120` |
| 交易成本 | 佣金 0.025% + 印花税 0.05%（卖出）+ 滑点 0.2% |
| 涨跌停检查 | L3 风控集成，涨停不可买入、跌停不可卖出 |
| Wisdom 可选 | `enable_wisdom=True` 启用决策层增强 |
| 认知引擎 | `enable_cognitive_engine=True` 启用四阶段/危险信号/移动止损 |
| 绩效指标 | 总收益、年化收益、夏普、最大回撤、胜率、盈亏比、持仓天数 |
| 飞书推送 | `--notify` 参数推送回测报告到飞书 |

### 12.2 缺失的回测能力

| 缺失项 | 影响 | 实现建议 |
|--------|------|----------|
| 全市场回测 | 无法评估策略在 5000+ 股票上的整体表现 | 扩展 `run_backtest.py` 支持批量回测 |
| 多周期回测 | 无法验证策略在不同市场状态下的稳定性 | 按牛/熊/震荡市分段评估 |
| 参数优化 | 策略参数靠猜测，缺乏最优参数选择 | ✅ v0.7.3 已完善：Walk-Forward 网格扫描 + GA/PSO 启发式优化，多股聚合评分（夏普40%+Calmar30%+胜率20%+收益10%），支持自定义参数约束（如 short<long），周日自动调参 + 4 项解锁条件门控 |
| 启发式优化 | 传统网格搜索效率低，不适用于大参数空间 | ✅ v0.7.3 已实现：`analysis/heuristic_optimizer.py` 遗传算法(GA)+粒子群优化(PSO)，锦标赛选择+单点交叉+高斯变异+精英保留，惯性权重+个体全局最优引导 |
| 过拟合检测 | 无法判断回测结果是否过拟合 | 引入蒙特卡洛模拟或交叉验证 |
| 策略对比 | 无法横向对比 8 个策略的优劣 | 统一回测 + 排序 |
| 实际收益跟踪 | 回测收益与模拟盘/实盘收益脱节 | 建立回测→模拟→实盘对照流水线 |

### 12.3 策略回测验证的必要性

当前 8 个策略的选用依据是经典投资书籍中的理论，但**理论有效 ≠ A 股市场上有效**：

| 策略 | 理论来源 | A 股市场特殊性 |
|------|----------|---------------|
| Turtle | 《海龟交易法则》美股市场 | A 股趋势性强但毛刺多，Turtle 可能频繁假突破 |
| Dual MA | 通用技术分析 | A 股震荡期长于美股，均线交叉可能反复磨损 |
| MACD | 通用技术分析 | MACD 在 A 股小盘股上信号滞后严重 |
| Bollinger | 布林格原创 | A 股波动率聚类现象可能导致频繁触发 |
| RSI/KDJ | 通用技术分析 | A 股超卖超买阈值可能需要调整 |

**建议**：在运行实盘前，至少完成以下验证：

1. **单策略回测**：对每个策略选取 5-10 只代表性股票，回测 1-3 年
2. **多策略对比**：在相同时间段、相同股票上对比所有策略的夏普比率
3. **参数敏感性**：测试关键参数（如 Turtle 的 20 日窗口）在不同取值下的表现
4. **市场状态分析**：分别回测牛市/熊市/震荡市的表现

### 12.4 使用建议

```
# 1. 回测单个策略
uv run python scripts/run_backtest.py --symbol 600519 --strategy turtle --days 365

# 2. 对比多个策略（手动）
for strategy in turtle dual_ma macd bollinger rsi kdj box_breakout; do
  uv run python scripts/run_backtest.py --symbol 600519 --strategy $strategy --days 365
done

# 3. 加入 Wisdom 决策层对比
uv run python scripts/run_backtest.py --symbol 600519 --strategy turtle --days 365 --enable-wisdom

# 查看历史信号回溯
cat data/signals_history.jsonl | jq '. | select(.symbol=="600519") | {date: .timestamp, direction, confidence, strategy}'
```

---

## 十三、迭代记录（续）

| 轮次 | 日期 | 主要变更 |
|------|------|----------|
| 31 | 2026-07-06 | **v0.5.1 十大视角接入信号流（P0-4）**：SignalPipeline 步骤3.6 新增视角质量门禁。signal_only 模式附加评分（通知卡片展示），paper/live 模式致命缺陷拦截。信号去重内存管理（P1-4）：保存前自动清理 7 天过期记录。信号告警疲劳控制（P1-3）：FeishuConfig 新增 `signal_aggregation`（min_interval, max_daily, batch_direction）。覆盖率 62%→63%，测试 1159 passed。 |
| 32 | 2026-07-06 | **v0.5.2 宏观增强 + 引擎拆分 + 集成测试**：新增 MacroRegimeEnhancer（P0-2），金/油/汇/债/G → RegimeDetector 仓位上限调整（黄金>3000 -10%, VIX>30 -15%, 美债倒挂 -20%）。提取 DailyReportManager（P0-3），main.py 1142→999 行。新增 E2E 集成测试骨架（P0-5），覆盖 20+ 模块导入。新增 POST /api/stop_loss/{symbol} Web API（P1-2）。OrderManager + StopLossManager 完成提取到 execution/。覆盖率 63%→65%，测试 1180 passed。 |
| 33 | 2026-07-15 | **v0.7.0 多专家视角深度审查与算法可靠性修复**：P0 修复相关性检查空壳/ProcessPoolExecutor pickle/NonNegativeValuesRule 返回值 3 项；P1 消除 PCA 前视偏差/OBV numpy 向量化/FactorIC 标注/RegimeDetector 周线/LRU 语义/BoxBreakout 置信度 6 项；P2 提取 max_hold_days 基类方法/Calmar 目标函数/Sharpe 无风险利率/validation --notify 联动 4 项。审查评级 A-。 |
| 34 | 2026-07-15 | **v0.7.1 多专家视角深度审查（第二轮）与全面修复**：新增 StockRankerConfig/FlowReportConfig 配置模型；修复 kb_indexer.py 硬编码绝对路径；修复 risk/manager.py + notifier/feishu.py 异常静默吞噬；metrics.py 补充 Calmar/Sortino 比率；backtest_offline.py + walk_forward_offline.py 新增 --notify 飞书通知。审查评级 A。 |
| 35 | 2026-07-28 | **v0.7.3 启发式参数优化系统**：新增 `analysis/heuristic_optimizer.py` 遗传算法(GA)+粒子群优化(PSO)双算法；适应度函数为多股聚合回测的夏普40%+Calmar30%+胜率20%+收益10%加权评分；`scripts/optimize_params.py` 新增 `run_heuristic_optimization()` + CLI 参数（--algorithm/--population/--generations）；`engine/scheduler.py` 调度器支持启发式优化任务；新增 22 个单元测试（ParamSpace/GA/PSO/StrategyParamOptimizer/集成场景），heuristic_optimizer.py 行覆盖率 93%；全量测试 1965 passed, 5 skipped；同步更新 CLAUDE.md/PRODUCT.md/TECH.md |