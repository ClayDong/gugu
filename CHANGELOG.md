# Changelog

## v0.6.0 (2026-07-07) — 全面优化与对抗性审查修复

### 概述
基于第一性原理和对抗性审查原则，从 10 个专家视角对项目进行全面审查，发现并修复 32 项问题。测试用例从 981 增至 1326（+345），覆盖率从 64% 提升至 67%。

### P0 级修复

#### Bug 修复
- **StageDetector._find_swings**：修复最后一个点重复计数问题
- **TradingEngine._scan_signals**：修复 `scan_list` 为空时 `UnboundLocalError`
- **资金流数据源不可用告警升级**：灰色状态卡片 → 红色告警卡片，标题明确标注"数据源不可用"

#### 测试增强
- **signal_pipeline 测试**：新增 7 个测试类覆盖 signal_only 模式、危险信号过滤、量价背离、四阶段入场过滤、向下摊平、价格校验、Wisdom 超时降级
- **flow_report 测试**：新增 7 个测试类覆盖行业轮动分析、重点行业提取、概念热点、强流入个股、市场全局分析、卡片构建、决策指导格式化

### P1 级优化

#### 算法优化
- **MA 斜率计算**：`np.polyfit` 线性回归替代双点差分，抗噪声能力更强
- **RSI 实现修复**：Wilder 平滑中 gain/loss 全为 0 时正确返回 RSI=100 或 RSI=0

#### 配置化
- **资金流背离阈值**：硬编码阈值提取为模块级常量 `DIVERGENCE_MAIN_THRESHOLD` / `DIVERGENCE_PRICE_THRESHOLD`
- **调度任务可配置化**：`settings.yaml` 新增 `scheduler.tasks` 区块（20 个开关），`SchedulerTasksConfig` 强类型模型

#### 功能增强
- **被过滤信号摘要推送**：收盘报告展示被过滤信号及原因，解决"沉默拒绝"
- **signal_only 免责声明**：仅通知模式卡片标注"不构成交易建议"
- **StockRanker 并发评分**：串行 for 循环改为 `asyncio.gather` 并发
- **盘中扫描频率动态调整**：熊市/暴跌市自动跳过 10:30 和 13:05 扫描
- **信号卡片信息密度优化**：30 秒决策摘要优先（止损止盈 → 绩效 → 决策链路 → 筹码 → 智慧参考）

#### 边界保护
- **KellyCalculator**：`avg_loss` 为 0 时不再除零，盈亏比上限 10
- **PositionController**：单股上限不低于 5%
- **_collect_fallback_market_data**：深证涨跌幅除数为 `prev_close` 而非硬编码 1

### P2 级优化

#### 性能优化
- **batch_history 并发预取**：串行 await 改为 `asyncio.gather` 并发
- **technical.py 指标扩展**：新增 `rsi()` / `macd()` / `bollinger()` / `obv()` 四个指标

#### 运维优化
- **通知重试指数退避**：根据 `retry_count` 动态跳过重试，避免加剧限流
- **clear_halt_only 二次确认**：熔断未激活时直接 return，日志升级为 ⚠️ 级别
- **配置管理弃用标记**：`_legacy_config.py` 添加弃用警告
- **策略参数热加载**：`refresh_config()` + `config_has_changed()`，scheduler 每 5 分钟检测文件变更自动重载
- **数据源健康监控仪表盘**：`/api/health` 端点返回三通道状态 + 资金流状态

#### 功能增强
- **尾盘选股器接入十大视角评分**：`ScreeningResult` 新增 `perspective_score`，排序按视角评分降序
- **已启用策略独立回测脚本**：新增 `scripts/verify_enabled_strategies.py`

#### 测试增强
- **十大视角评分函数测试**：新增 10 个视角详细测试类
- **新增 test_money_management.py**：19 个测试用例
- **新增 test_position_controller.py**：13 个测试用例
- **test_technical.py 增强**：新增 15 个测试用例

### 审查评级
- **总体评分**: B+ → **A-**（32 项问题全部修复）
- **架构设计**: A- | **策略实现**: B+ | **风控安全**: A | **数据鲁棒性**: A-
- **业务逻辑**: B+ | **测试覆盖**: B+ → A- | **认知引擎**: B+ | **用户体验**: B → B+

---

## v0.5.4 (2026-07-06)

### 变更
- 飞书通知重试机制增强（send_card 即时重试 + retry_queued 强制刷新 token + 队列间隔 10min→2min）
- 资金流报告数据源容错（HTTP 重试+UA 池 + 降级条件修复 + 量价推算 fallback）
- 震荡市观察列表接入资金流筛选（主力净流出>1亿/占比<-5% 排除，按资金强度排序）
- 循环导入修复（order_manager/stop_loss_manager 延迟导入 engine 模块）

---

## v0.5.0 (2026-07-01)

### 新增
- 十大视角分析系统（10 位投资大师认知框架量化评分）
- 盘中实时盯盘（IntradayMonitor，按性能分级频率）
- 筹码集中度过滤器（股东户数季度慢变量）
- 财叔动态止盈减仓引擎
- 信号绩效追踪器（SignalTracker）

### 优化
- 信号过滤流水线重构为独立 SignalPipeline 类
- 数据源三级冗余（sina + akshare + eastmoney）
- 熔断状态持久化到磁盘（防重启绕过）
- 测试隔离（4 个 monkeypatch 重定向到 tmp_path）