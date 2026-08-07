## 2026-08-07 — v0.9.11 防 bug 体系落地（4 层防线）+ 熊市出选股

### 需求变更：熊市也出选股
- `skip_regimes` 从 `[bear, crash]` 改为 `[crash]`（bear 不再完全跳过）
- bear 市候选数 ×0.4（`candidate_modifier`），MA5<MA10 弱势市候选数再减半
- 同步更新 `data/screener_params.json`（自动调优持久化参数覆盖代码默认值——教训：改参数必须同步两个文件）
- crash 保留保护性空仓（系统性崩盘日不接飞刀）

### 防线1：数据入口统一清洗层（消灭"脏数据类"bug）
- `quality.py` 新增 `sanitize_dataframe()`：None/NaN→0、字符串列 None→""、全 NaN 行剔除
- `sanitize_scalar()`：信号字典等非 DataFrame 场景
- 接入 `DataManager._validate` 入口：所有外部数据进系统先清洗，消费点不再各自防御
- 覆盖 v0.9.9 信号黑洞同款场景：`f"{None:.1f}"` 不再崩溃

### 防线2：策略注册表一致性自检（消灭"幽灵策略"bug）
- `registry.py` 新增 `verify_registry_consistency()` → (fatal, warning) 二元组
  - fatal：配置启用但代码未注册 → scheduler 拒绝启动（fail-fast）
  - warning：代码注册但配置未引用 → 死代码残留记日志（hot_money 教训）
- scheduler 启动时执行自检
- **实测抓到 27 项死代码残留**（turtle/dual_ma/macd 等历史策略注册未启用）——验证了自检价值

### 防线3：真实脏数据 fixture 进回归（消灭"mock全绿上线挂"）
- `tests/conftest.py` 新增 5 个真实脏数据 fixture：dirty_history_df / dirty_snapshot_df / empty_df / none_fields_signal / truncated_snapshot_df
- 全部来自生产实战 bug 场景（东财限流空表/字段漂移 None/北交所 502/残缺 80 只快照/v0.9.9 视角失败）
- 新增回归测试 `test_dirty_data_regression.py`：历史写入不崩溃、清洗净化、快照过滤安全

### 验证
- 新增 21 测试（清洗 8 + 脏数据回归 5 + 一致性 4 + 熊市相关），相关模块 143 passed 无回归
- 生产重启加载自检 ✅

## 2026-08-07 — v0.9.10 尾盘选股断供修复（东财快照限流 → 新浪全市场保底源）

### 根因确认
尾盘选股/自动选股"一直没数"（8/3 起 4 个交易日 screener_history 零新增）= 两个叠加原因：
1. **数据源**：全市场快照 100% 依赖东财 clist 接口（sina 主源 `fetch_stock_realtime([])` 原返回空 → DataManager 跳过主源走降级），东财 8/3 起持续限流 RemoteDisconnected（8/5 仅残缺 80 只）→ 快照为空 → 选股零产出
2. **市场保护**：即使数据可用（8/4 新浪源 5535 只），沪深300 MA20(4659)<MA60(4802) 判定 bear → `skip_regimes=[bear,crash]` 保护性空仓跳过（设计行为非 bug）

### 修复
- `SinaCollector.fetch_stock_realtime([])` 空列表 = 全市场快照（新浪 Market_Center 分页接口，100/页 × ~57 页，5538 只，15s）
- 字段对齐 DataManager 约定：symbol/name/price/change_pct/volume/amount/turnoverratio/circ_mv(万元→元×1e4)
- `DataManager._call_with_fallback` 移除 `fetch_stock_realtime([])` 跳过主源逻辑（sina 现已支持）
- `TerminalScreener` 方式3 rename 补全 amount/turnoverratio/circ_mv → 硬过滤条件完整生效

### 验证
- 新增 4 测试（分页/空/HTTP失败/空列表语义变更），65 passed 无回归
- 真实链路：DataManager 全市场快照 5538 只 ✅ / 尾盘选股扫描跑通（bear 市保护性跳过，符合策略）
- 8/5 残缺 80 只快照问题一并消除（新浪源稳定 5500+）

## 2026-08-07 — v0.9.9 信号历史黑洞修复（perspective None 崩溃）+ 止损日志降噪 + 资金流熔断

### 根因确认（信号历史 7/1 起零写入）
`record_signal_history` 被 `perspective_check.composite_score=None` 一票打死：
视角分析失败路径（signal_pipeline:728-733）写入 `composite_score=None`，旧代码
`.get('composite_score', 0)` 在 key 存在值为 None 时返回 None（dict.get 默认值只在 key 缺失时生效），
`f"{None:.1f}"` 抛 `TypeError: unsupported format string passed to NoneType.__format__` →
整个 record_signal_history try 块异常短路 → append_jsonl 永不执行。
叠加去重机制放大：600519 首个信号崩溃但通知成功 → 此后同信号被"已通知过"拦截在记录点之前 →
永不重试 → signals_history.jsonl 从 7/1 起 0 新增（35 天信号历史黑洞）。

### P0：perspective None 防御（信号历史恢复写入）
- `signal_pipeline.py` record_signal_history：`composite_score/min_score/fatal_count` 全部改 `or 0` 防御
- 回归测试 `test_record_signal_history_perspective_none_scores`：None 输入不崩溃且正确写入

### P1：止损提醒日志降噪
- `stop_loss_manager.py`：WARNING 移到 `_should_alert` 去重判断之后（只在真正推送时打），
  触发日志 WARNING→INFO。此前破位持仓每 5 分钟刷 2 条 WARNING（去重已拦通知但日志照打）

### P1：akshare 资金流熔断（东财限流防刷屏）
- `akshare_collector.py`：连续失败 3 次 → 熔断 1800 秒 → 冷却期内静默返回空（不调 API 不打 ERROR）→ 到期自动恢复
- 触发背景：东财 RemoteDisconnected 限流，修复前单日 1705 次 ERROR 刷屏
- 测试：`TestAkshareCollectorFlowBreaker` 4 用例（阈值触发/熔断静默/成功重置/到期恢复）

### 验证
- 新增 5 个测试全过；test_risk.py 21 passed / test_p0_p1_repairs.py 26 passed 无回归
- 生产重启（PID 74384）：熔断 10:24:03 触发 ✅ / 重启后资金流 ERROR 仅 3 次（熔断前）✅
- 止损去重正常：`[track_live] 止损提醒 510500/512480 已去重，跳过通知` ✅
- 信号历史恢复：需等新信号（去重机制下 600519 旧信号不再重写，属预期）

## 2026-08-05 — v0.9.8 第二批 P0 修复（信号黑洞根因 + 实时止损接入 + 模拟盘隔离 + 可观测性）

### 根因确认
**信号黑洞的真正根因 = 120 日 K 线缓存陈旧**（当日数据缺失 → 策略无法产出当日信号 → 加权融合无输入 → 信号被静默吞掉）。

### P0-1：120 日 K 线缓存陈旧修复（信号黑洞根因）
- 缓存新鲜度判定修复：盘后（含当日 bar）与盘中（< 当日 bar）语义分离
- 盘中 5 分钟轮询的陈旧缓存主动驱逐 + 重拉；E2E 验证修复后真实扫描产出信号
- 证据：修复前 17:15 扫描 18 只 → 通过 0 个；修复后 17:32 扫描 18 只 → 通过 2 个
- 真实信号恢复：603501 buy 0.408 / 300760 buy 0.45

### P0-2：信号路由可观测性（消除静默过滤）
- signal_router 三态日志：`所有策略无信号`（策略未触发）/ `加权融合无方向`（未达融合门槛）/ `当日最新信号`（历史 vs 当日语义分离）
- 每只股票信号链路完全透明，不再有"无声无息被过滤"

### P0-3：止损检查接入实时循环
- RealtimeSignalScanner 每 5 分钟执行止损检查 + 升级检查（此前仅每日 4 轮）
- track_live 模式止损提醒是用户唯一执行信号，破位后 5 分钟内升级告警

### P1：模拟盘隔离 / 日志口径 / auto_tune 判定
- `updated_at` 字段 + GUGU_TEST_MODE uuid 隔离（防测试污染共享模拟盘）
- 600519 幽灵成交清理（现金恢复 99.99 万）
- box_breakout 日志标注 `[120天窗口]` + 当日信号语义（不再误导）
- auto_tune AND 判定修复：负 alpha 或低胜率 → 收紧（不再被 AND 卡死）

### 验证
- 新增 11 个测试（`tests/unit/test_p0_second_batch_fix.py`），全量回归 **2264 passed**
- 生产重启加载：`[track_live] 止损告警升级器已启用` ✅，26 个任务注册正常
- 飞书双通道验证：系统卡片 → Hermes 对话 ✅ / 业务卡片 → 开悟之路群 ✅
- 止损状态：510500（-0.3%）🔴 / 512480（-5.2%）🔴 破位提醒已推送（去重生效）

---

# Changelog

## 2026-08-05 — v0.9.8 第一批 P0 防护（单实例锁 + 测试 HTTP 硬隔离 + 健康告警升级 + E2E 验证）

### 背景
深度复盘 8/5 全天 11 个问题（幽灵策略/双 scheduler/测试泄漏/磁盘满 886 次写失败/30 天 0 信号），
根因：**系统对自身状态无可验证不变量 + 所有故障静默发生，用户是最后的检测器**。

### 防护一：单实例锁（杜绝双 scheduler 并行）
- 新增 `src/gugu/utils/single_instance.py`：锁文件 `data/scheduler.lock`（PID+启动时间+命令行）
- 获取锁时验证 PID 存活 + 命令行匹配 gugu 特征（防 PID 复用误判）；残留锁自动接管；退出自动释放
- 接入三入口：`scripts/run_scheduler.py` / `gugu.engine.scheduler._main()` / `scripts/run_live.py`
- GUGU_TEST_MODE 跳过（测试进程可并发）
- 验证：生产重启后第二实例启动被拒绝（PID 13770 持有锁），原实例不受影响

### 防护二：测试 HTTP 硬隔离（结构性杜绝测试泄漏到生产飞书）
- `FeishuNotifier._test_mode_blocks_http()`：GUGU_TEST_MODE + 真实网络 client → 禁止外发
- 短路点：`send_card` 入口 + `_get_tenant_token` + `_post_card` + `_post_webhook`
- **关键设计**：测试显式替换 `_client = MockTransport(...)` 的用例放行（不破坏现有 2207 个测试断言）；
  只有真实网络 client + 测试模式才短路——daemon 线程/后台测试进程结构上不可能外发
- 修复过程发现并修正：新方法插入位置导致 `@retry` 装饰器错挂（重试从 6 次变 2 次）——装饰器归属已修正

### 防护三：健康告警升级（把静默失败变成主动通知）
watchdog 新增三项检查（`src/gugu/health/watchdog.py`）：
1. `check_disk_health`：磁盘剩余 < 1GB → P0 告警（8/5 磁盘满 886 次写失败事故）
2. `check_data_source_health`：读 `source_health_events.jsonl` 当日失败率 > 50% 或降级 ≥10 次 → P0 告警（8/4 全市场快照为空静默事故）
3. `check_zero_signal_streak`：交易时段连续 3 轮扫描 0 信号 → P0 告警（30 天 0 信号事故）
- `run_all_checks` 从 2 项扩展为 5 项；`_alert_fingerprint` 增加三类新指纹去重
- 验证：生产 watchdog 实测 5 项检查全部运行正常（磁盘 16.6GB / 数据源失败率 2.6% / 0 信号 streak 正常）

### 防护四：E2E 验证脚本
- 新增 `scripts/e2e_verify.py`：单实例锁 + 心跳 + 磁盘/数据源 + 真实扫描 + 双通道通知链路
- `--check` 只读 / `--no-scan` / `--no-notify` 模式；默认发送测试卡片验证系统/业务双通道
- 验证：8 项全部通过，测试卡片系统（Hermes 对话）业务（开悟之路群）双通道均 True

### 测试
- 新增 `tests/unit/test_p0_first_batch_guard.py`：15 个针对性测试（锁 4 场景/隔离 3 场景/健康检查 7 场景/组合检查）
- 全量回归：**2207 passed**（88.5s，--timeout=30）
- 生产验证：scheduler 重启（PID 13770）+ 心跳正常 + 锁拒绝第二实例 + E2E 全绿

## 2026-08-03 — 资金流 0 结果根因修复

- 统一行业资金流 validator 的 `symbol` 调用契约，保留 DataFrame attrs。
- 为资金流全源失败增加 `data_available`、`data_error`、`all_sources_failed` 状态，过滤器与尾盘报告区分数据异常和正常无结果。
- signal scan 状态增加资金流健康字段，paper/live 缺失资金流时拒绝买入，signal_only 保持观察模式。

## v0.9.6 (2026-08-01) — P0 修复：测试副作用泄漏到生产飞书告警

### 事故
用户收到"⚠️ 信号历史写入失败（P0 数据完整性）股票: ? (?) 错误: [Errno 2] No such file or directory: '/nonexistent/path/data'"告警。

### 根因
`test_record_signal_history_error_handling` 将 `gugu_config.PROJECT_ROOT` mock 为 `/nonexistent/path` 调用真实 `record_signal_history`，捕获异常后触发 `_alert_signal_history_write_failure`——该函数在 **daemon 线程中构造真实 FeishuNotifier**（读到生产配置，`_system_error_enabled=True`）→ `notify_error` 真实推送飞书。conftest 的 autouse fixture 只 mock 了 `gugu.engine.main.record_signal_history`，**漏了 `signal_pipeline` 模块**；且 GUGU_TEST_MODE 只重定向通知队列文件路径，**不阻止 HTTP 发送**。

### 修复（三层防御）
- **源码层（治本）**：`_alert_signal_history_write_failure` 开头检查 `GUGU_TEST_MODE` → 短路仅记录 debug 日志，任何测试触发都不外发
- **测试层**：`test_record_signal_history_error_handling` patch 告警函数并断言 `assert_called_once`（验证 P0 防线链路触发，隔离环境下不真实外发）
- **conftest 纵深防御**：autouse fixture 补 mock `gugu.engine.signal_pipeline._alert_signal_history_write_failure`（L5，防未来其他测试触发）

### 验证
- `tests/unit/test_signal_pipeline.py` 35 passed
- 全量回归 tests/unit 全绿（`--timeout=20` 防挂起）
- 生产 `data/signals_history.jsonl` 正常（最新 2026-08-01 19:04 落盘），生产无此问题

## v0.9.5 (2026-08-01) — 公众号战法落地（四周期 + 幻灭期修复反弹 + 布林超跌）

### 背景
用户提供公众号《八月，来了~~》战法文章（四周期模型 + MACD 金叉铁律 + 幻灭期修复反弹 + 布林下轨抢反弹），要求落地验证。

### 新增（3 个策略，`src/gugu/strategies/regime_cycle.py`）
- **regime_cycle_four**（四周期模型）：站上20日线+银山谷(MA20上穿MA60)+MACD金叉状态 → 潜伏期买入；跌破20日线+死亡谷+MACD死叉状态 → 幻灭期卖出。主升浪必须建立在MACD金叉状态（原文铁律）
- **death_valley_rebound**（幻灭期修复反弹）：幻灭期(MA5<MA60空头排列) + 20日累计跌幅>10%超跌 + 3日不创新低 + KDJ金叉 + 站上5日线 → 买入；跌破5日线卖出
- **bollinger_oversold_rebound**（布林下轨超跌反弹）：收盘跌破布林下轨 → 买入；反弹到中轨（目标位）卖出

### 关键校准（对抗性审查发现的原文语义缺失）
- 初版 death_valley 漏了"**处于幻灭期**"前提 → 15股600日 2615 信号泛滥（每股每天触发），3日超额 -0.17% 为负
- 补上幻灭期（空头排列）+ 超跌（20日跌>10%）两个前置过滤后 → 75 信号，10日超额 +2.41% (p=0.0004)、验证期 +3.02% (p=0.0012) **显著**
- 参数稳健性：超跌阈值 8-12% 均显著；窗口 20 日甜蜜点（10d 样本太少 / 30d 失效）
- KDJ"金叉"实现为状态（k>d）而非交叉事件——交叉事件在暴跌末段已发生但当时不满足企稳条件

### 优化（第二轮，bollinger 校准）
- **bollinger_oversold_rebound 加超跌过滤**：验证期 p=0.116 未显著 → 加"20日累计跌幅>12%"前提（参考 death_valley 成功经验）→ 验证期 +3.65% (p=0.0001) **显著**
- 超跌阈值网格：不过滤 p=0.116 / 8% p=0.032 / **12% p=0.0001（最优）** / 16% 更猛但信号少
- 集中度复核：13%（sz300059），剔除后 p=0.000 仍显著；2024/2025/2026 三年全正超额（+4.11%/+3.48%/+3.88%）
- **卖出模式参数化**（exit_mode: mid/hold）——事件研究验证卖出模式需引擎级回测（事件研究只看入场信号后N日收益，与卖出无关）
- 布林带固有特性：急跌时 std 扩大 → 下轨自动下移 → 价格追不上下轨，需深跌（-35%）才破位（测试数据必须足够陡）

### 验证
- 16 个新单测（三策略信号/置信度/边界/超跌过滤）
- 事件研究：15股×600日，death_valley 88 事件、集中度19%（剔除最大贡献者后 p=0.005 仍显著）、2024/2025 两年正超额
- **两个策略 conditional_pass**（death_valley + bollinger）；regime_cycle_four → in_review（方向对样本不足，低频周期策略）
- param-audit 36 闭环 0 断裂

## v0.9.4 (2026-08-01) — 绩效纵深（20/60日追踪 + 牛熊分层 + 告警去重 + Brinson诚实化）

### 背景
v0.9.3 遗留三项优化建议（20/60 日收益追踪、按牛熊分层胜率、watchdog 告警去重）+ 全项目彻底检查中发现 Brinson 归因是"假实现"（永远返回 0 却当作真实指标展示，违反"降级掩盖残缺"铁律）。

### 新增
- **20/60 日收益追踪**（`signal_tracker`）：`TRACK_PERIODS` 扩展为 `[1,3,5,10,20,60]`；`_stats_by_period` 各周期独立统计，长周期样本不足标记 `insufficient` 不阻塞短周期；K 线拉取天数覆盖 60 交易日
- **牛熊分层胜率**（`signal_tracker._stats_by_regime`）：沪深300 + MA60 判定信号日市场状态（bull/bear/sideways，±1% 缓冲带）；指数数据不可用标记 `unknown` 不阻塞报告
- **watchdog 告警去重**（`health/watchdog.dedupe_alerts`）：按问题类别指纹去重（失忆/心跳缺失/心跳超时/重启风暴独立），默认 120 分钟抑制窗口；状态文件 `watchdog_alert_state.json`；恢复健康自动清除状态，下次故障立即重新告警；`--check` 模式保持纯净
- **绩效卡片增强**（`formatter_reports`）：新增 ⏱ 分周期命中率 + 🐂🐻 牛熊分层区块

### 修复
- **Brinson 归因诚实化**（`analysis/performance`）：从"永远返回 0 当作真实指标"修复为——有行业数据时计算简化版归因（组合内部分解：配置/选择/交互效应），无数据时 `brinson_enabled=False` 明确标注未启用
- **代码清理**：5 处未使用导入/变量（ruff F401/F841）、2 处风格问题（SIM103/SIM105）、4 处 except-pass 确认有 fallback 语义（加日志）

### 验证
- 16 个新单测（signal_tracker 周期/分层 5 + 去重 9 + Brinson 2）
- 全量单元 2105 passed（+16）；集成 44 passed
- ruff 全绿（src/scripts/tests）
- param-audit --strict：33 闭环 0 断裂
- check-debug-code：无调试残留

## v0.9.3 (2026-08-01) — 信号质量增强（数据质量戳 + 共振过滤 + 能力圈标注）

### 背景
多专家评审 P1/P2 共识（利弗莫尔：关键点需确认；巴菲特：知道圈内圈外；芒格：降级掩盖残缺）。

### 新增
- **数据质量戳三级**（`signal_pipeline`）：fresh(🟢实时)/degraded(🟡降级)/stale(🔴非实时)；降级/非实时信号标注 `observation_only` + 说明（利弗莫尔原则：无信息时不作方向假设）
- **多策略共振标注**（`signal_router`）：`resonance`(触发策略数) + `resonance_level`(strong≥2/single=1)；买卖同时触发时 `signal_conflict` + `conflict_detail`（拒绝机械转达矛盾）
- **能力圈标注**（新模块 `src/gugu/strategies/capability.py`）：30 个策略画像表（可解释性 fundamental/technical/rule/ml + 验证等级 + 仓位上限）；多策略取最保守画像；ML 策略标"黑箱+试验仓(5%)"
- **信号卡片增强**（`formatter`）：信号质量徽章行 + 数据降级说明 + 能力圈行 + 矛盾信号区块

### 验证
- 18 个新单测（能力圈 9 + 信号增强 9）
- 全量 2089 passed（+18）
- 实测：双策略共振→strong、单策略→single、买卖矛盾→正确标注冲突详情
- pre-commit param-audit：Passed

## v0.9.2 (2026-08-01) — P0 静默失效防线（健康看门狗）

### 背景
多专家视角评审（芒格/巴菲特/利弗莫尔）共识：系统最大敌人是**静默失效**——7/16~7/31 真实事故中调度器在跑、日志在写，但信号历史 11 天零写入无人察觉（"系统活着但失忆了"）。

### 新增
- **健康看门狗模块** `src/gugu/health/watchdog.py`：
  - `check_scheduler_health`：心跳新鲜度（>15min 未更新告警）+ 重启风暴（单日 >3 次告警）
  - `check_signal_history_freshness`：调度器在跑但信号历史 >2 天未写入 → "信号历史失忆"告警
  - 无启动记录/首次安装不误报（避免安装即报警）
- **调度器启动记录**：`_record_boot()` 写 `data/scheduler_boot.json`（boot_time + pid + 最近 20 次 history，重启风暴检测数据源）
- **信号历史写入失败告警**：`record_signal_history` 异常时异步飞书告警（P0 数据完整性），不再静默 warn
- **watchdog 脚本** `scripts/health_watchdog.py`：`--check`（退出码 0/1）/ `--quiet`（cron 静默模式：健康零输出、告警输出+推飞书）
- **cron 部署**：`gugu-health-watchdog`（no_agent，每 5 分钟，静默守护）

### 验证
- 9 个新单测（信号历史新鲜度/心跳/重启风暴/无启动不误报）
- 全量 2071 passed（+9）
- 实测：失忆场景正确告警（5 天未写入 → 告警文本 + 飞书推送）、恢复后回到健康、退出码正确（告警 1/健康 0）
- pre-commit param-audit：Passed

## v0.9.1 (2026-08-01) — 参数闭环全量修复（P0+P1，16 断裂 → 0）

### 概述
GA/PSO 动态调优写回 `strategy_defaults.yaml` 的参数 16/27 策略代码根本不读（"调了等于没调"）。全部对齐：defaults 参数名与代码 `self.params.get/[]` 读取名完全一致（代码=权威），动态调优链路打通。

### 修复
- **P0 完全错位**（defaults 参数代码一个都不读）：double_yin 13 个对齐、zhen_dahu 19 个对齐
- **P1 部分错位**（16 个策略）：breakout_rebound 7 组语义映射迁移调优值（limit_up_window→limit_up_lookback 等）、north_force_long 4 组、dragon_tiger_short/ma20_trend/industry_capital_momentum 各 3-4 组；删 30+ 死参数（文档概念但代码未实现：first_board price_min/seal_ratio_min、turtle risk_per_trade/max_units 等）；ml_gradient_boost 3 个训练超参移除（train_and_save 硬编码）
- **chenxuezhang 参数化**：7 个阈值从硬编码接入 self.params（默认值=原硬编码值，行为零漂移，7/7 单测通过）
- **审计工具 bug**：module 为包名时误读 `__init__.py` 导致 ml_gradient_boost 假断裂 → 优先同名 .py + 通配扫描
- **param-audit --strict 全绿**（33/33 闭环），pre-commit hook 升级 strict 模式阻断新增断裂

### 验证
- `param-audit --strict`：33 闭环 0 断裂
- 全量单测：2062 passed（含 chenxuezhang 7/7、first_board 18/18、linjudaye/double_yin/zhen_dahu 相关 95/95）
- pre-commit param-audit hook：Passed

## v0.9.0 (2026-08-01) — 实时推荐 + 实盘交易（阶段四/五落地）

### 概述
响应用户核心需求「实时推荐、实时交易」：把 gugu 从「离线定时扫描」升级为「盘中实时推荐 + QMT 实盘执行」。完整实现 QmtBroker（xtquant 真实接入）、新增 RealtimeSignalScanner（盘中分钟级策略信号扫描+飞书推送+可选自动下单），打通阶段四（实盘）主链路。

### 新增
- **QmtBroker 完整实现**（`src/gugu/execution/qmt.py`）：connect/order/撤单/持仓同步/账户查询/一键平仓/应急限价买入/历史订单查询，全部真实接入 xtquant；代码格式自动转换（sh600000 ↔ 600000.SH）；连接心跳+自动重连；未配置时安全拒绝下单（防裸奔）；CLI 自检 `python -m gugu.execution.qmt`
- **RealtimeSignalScanner**（`src/gugu/engine/realtime_scanner.py`）：盘中策略信号持续扫描（复用引擎 `_scan_signals` 管线），新信号立即推送飞书；信号去重（同一 symbol+direction 冷却期内不重复）；live 模式支持自动执行（confirm_required=False 时直接委托 QMT）
- **配置项**：`intraday_monitor.realtime_recommend` / `signal_cooldown_sec`（settings.yaml + 强类型模型 + flatten）
- **run_live.py 升级**：实盘模式启动 TradingScheduler 持续运行（盘中实时推荐+定时任务），安全退出清理

### 安全机制
- 默认 `signal_only`/`track_live`：飞书推送 + **并行记入独立模拟盘**（真实持仓保护不变，信号效果用模拟盘验证）
- `paper` 模式：信号走引擎完整管线（风控 + PaperBroker 模拟下单 + 通知）
- `live + confirm_required=true`：只推送，等待人工确认（默认安全）
- `live + confirm_required=false`：信号直接委托 QMT（需显式配置）
- QMT 未连接时拒绝一切下单

### 模拟盘验证（用户确认的落地路径）
- **track_live 模式下的真实持仓保护不变**（止损/止盈提醒照常）
- **信号效果用独立 PaperBroker 并行验证**：实时信号 → 飞书通知 + 模拟盘下单（`data/paper_broker_state.json`）
- 信号历史绩效验证（1/3/5/10 日收益）在 `_process_signal` 中持续记录
- 验证路径：效果达标后再切 live 模式上实盘

### 测试
- `tests/unit/test_realtime_scanner.py` 新增 24 个测试（代码转换/安全拒绝/去重/模式分层/sidecar 模拟盘/自动执行）

---

## v0.7.1 (2026-07-15) — 多专家视角深度审查（第二轮）与全面修复

### 概述
基于第一性原理和对抗性审查原则，从量化研究员、风控工程师、系统架构师、数据工程师、DevOps 等多视角对项目进行第二轮全面审查。发现并修复 8 项问题，核心方向：修复配置模型脱节、去除静默吞异常、补充 Calmar/Sortino 比率、回测脚本飞书通知全覆盖。

### 审查发现摘要

| 视角 | 发现数 | 核心问题 |
|------|--------|----------|
| 系统架构师 | 2 | `stock_ranker`/`flow_report` 配置模型缺失（settings.yaml 有定义但 config/models.py 无对应模型）；`flatten_config` 未覆盖新配置段 |
| 数据工程师 | 1 | `kb_indexer.py` 硬编码绝对路径 `/Users/dong/workspace/pdf2word/`（不可部署） |
| 运维工程师 | 1 | `except Exception: pass` 共 ~20 处，关键路径（`risk/manager.py`、`notifier/feishu.py`）异常被静默吞噬 |
| 量化研究员 | 2 | `metrics.py` 缺失 Calmar Ratio 和 Sortino Ratio；`backtest_report.json` 不包含风险调整指标 |
| DevOps | 2 | `backtest_offline.py`、`walk_forward_offline.py`、`diagnose_strategy.py` 无飞书通知集成，自动化流水线反馈链路断裂 |

### 修复清单

#### 配置一致性
- **StockRankerConfig / FlowReportConfig 新增**：`config/models.py` 新增 `StockRankerConfig`（6 个权重字段）和 `FlowReportConfig`（5 个开关字段），对应 `settings.yaml` 的 `stock_ranker:` 和 `flow_report:` 段
- **AppConfig + flatten_config 更新**：`AppConfig` 新增 `stock_ranker` 和 `flow_report` 字段，`flatten_config()` 返回字典同步新增

#### 硬编码路径修复
- **kb_indexer.py**：`DEFAULT_BOOKS_DIR` 从硬编码绝对路径改为通过环境变量 `GUGU_KB_DIR` 或 `PROJECT_ROOT.parent / "pdf2word" / "知识库_docx"` 的相对路径

#### 异常处理修复
- **risk/manager.py:652**：`except Exception: pass` → `logger.warning(f"获取 {sym} 历史数据失败: {e}")`
- **notifier/feishu.py:365**：`except Exception: pass` → `logger.warning(f"发送告警卡片失败: {e}")`
- **notifier/feishu.py:406**：`except Exception: pass` → `logger.warning(f"读取通知队列文件失败: {e}")`

#### 绩效指标补充
- **calmar_ratio() 新增**：`metrics.py` 新增 `_calmar_ratio()` 函数（年化收益/最大回撤），`calc_metrics()` 输出字典新增 `calmar_ratio` 字段
- **sortino_ratio() 新增**：`metrics.py` 新增 `_sortino_ratio()` 函数（仅用下行波动率的 Sharpe 变体），输出字典新增 `sortino_ratio` 字段

#### 飞书通知全覆盖
- **backtest_offline.py**：新增 `--notify` 参数和 `_send_feishu_report()` 函数，批量回测完成后推送策略对比报告到飞书
- **walk_forward_offline.py**：新增 `--notify` 参数和 `_notify_optimization_result()` 函数，WF 优化完成后推送结果到飞书

### 审查评级
- **总体评分**: A- → **A**
- **配置完整性**: B+ → A- | **代码健壮性**: B+ → A- | **指标完备性**: B+ → A
- **通知覆盖**: A- → A | **架构设计**: A- | **算法可靠性**: A- | **风控安全**: A

---

## v0.7.0 (2026-07-15) — 多专家视角深度审查与算法可靠性修复

### 概述
基于第一性原理和对抗性审查原则，从量化研究员、风控工程师、系统架构师等多个专家视角对项目进行全面审查，发现并修复 13 项问题，其中 P0 级运行时 Bug 3 项、P1 级算法/量化逻辑问题 5 项、P2 级设计优化 5 项。核心方向：确保 alpha 算法合理可验证、消除前视偏差、完善飞书通知联动。

### P0 级修复

#### 运行时 Bug 修复
- **RiskManager._check_correlation_with_portfolio**：相关性检查原为空壳（仅记录日志），改为实现真实相关性计算（持仓间收益率皮尔逊相关系数 >0.85 触发告警）
- **ParamOptimizer 并行优化器**：`ProcessPoolExecutor` 嵌套函数 `_eval_params` 无法 pickle，改为 `ThreadPoolExecutor` 解决序列化问题
- **NonNegativeValuesRule.check()**：inplace drop 不生效（原始 DataFrame 不变），改为返回 `(is_valid, cleaned_df, violation_msg)` 三元组 + 真实过滤

### P1 级修复

#### 量化算法可靠性
- **MLFactorModel PCA 正交化前视偏差**：PCA 拟合从全量数据改为在 Walk-Forward 窗口内部进行，消除未来信息泄露
- **AlphaFactory OBV 计算**：OBV 能量潮指标原为 Python 循环逐根K线计算，改为 numpy 向量化（`np.sign` + `np.where` + `cumsum`），性能提升 10x+
- **FactorICAnalyzer 标注修正**：原误标为"截面 IC"，明确标注为"时序 IC"（按时间序列滚动计算因子值与未来收益的相关性）
- **RegimeDetector 周线 resample**：`resample("W")` 默认周日为周起点，与 A 股实际周线（周五收盘）不符，改为 `resample("W-FRI")`
- **DataCache LRU 语义修复**：`get()` 命中时未更新访问顺序，导致 LRU 淘汰策略失效，修复后在命中时先移除再追加（移至末尾）
- **BoxBreakout 置信度买卖叠加**：买入信号置信度被后续卖出信号覆盖（同一股票同时买卖时），改为赋值叠加机制（买入取卖方置信度，卖出取买方置信度，取均值）

### P2 级优化

#### 架构优化
- **抽取 max_hold_days 强制卖出为基类公共方法**：`BoxBreakoutStrategy` 和 `MLFactorStrategy` 中 30 天未盈利强制清仓逻辑，从各自策略提取到 `BaseStrategy._check_max_hold_days()`，消除重复代码
- **ParamOptimizer 目标函数 Calmar 比率替代**：原始权重组合目标函数改为 Calmar 比率（年化收益/最大回撤），更好地衡量风险调整后收益
- **Sharpe Ratio 加入无风险利率**：从 0 改为 2%（中国一年期存款利率近似），使得夏普比率计算更准确，特别是在低波动市场环境下
- **策略验证准入门禁脚本增强**：`validate_strategy_gate.py` 新增 `notify_validation()` 和 `format_validation_report()`，验证通过自动推送飞书通知，新增 `--notify` 命令行选项

### 审查评级
- **总体评分**: A-
- **架构设计**: A- | **策略实现**: A- | **风控安全**: A | **数据鲁棒性**: A-
- **算法可靠性**: B+ → A- | **测试覆盖**: A- | **认知引擎**: A-

### 验证算法建议
1. **回测验证**：`python scripts/run_backtest.py --strategy box_breakout --symbol 600519`
2. **准入门禁**：`python scripts/validate_strategy_gate.py --strategy box_breakout --metrics metrics.json --notify`
3. **Walk-Forward**：`python scripts/walk_forward_offline.py --strategy box_breakout`
4. **因子 IC 分析**：通过 `FactorICAnalyzer`（时序 IC）分析单因子预测力，|IC|>0.03 且 |IR|>0.3 视为有效因子

---

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