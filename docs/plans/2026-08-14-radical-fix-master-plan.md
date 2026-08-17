# gugu 根治方案：从"修 bug"到"消灭 bug 家族"

> 日期：2026-08-14
> 触发：2026-08-14 多专家评审（代码×业务×数据×风控）发现 6 类问题，用户要求"彻底解决、从根上解决、全量优化、不越改越乱"
> 原则：第一性原理（消灭根因族而非个案补丁）+ 对抗性审查（每步有验收门禁）

---

## 0. 本次审计发现的 6 类问题 → 4 个结构性根因

| # | 现象（实证） | 根因映射 |
|---|---|---|
| 1 | 止损告警"重置→升级"振荡：`record_stop_loss_triggered` 每次破位把 escalation_level 重置 0，同日 40+ 次尝试全升 level1/2，永远到不了 level3 | **根因 B：状态机无单一事实源**（内存状态 vs 文件状态 vs 事件流各自为政） |
| 2 | 告警卡 dedup 吞掉且 `return True` 假装发送成功：512480（亏 -20.6%）的严重升级卡全天未送达，调用方无感、重试队列 0 条 | **根因 D：可观测性缺失**（发送结果无审计、无"去重率异常"告警） |
| 3 | `strategy_manifest.yaml` 内 double_yin：`validation="Walk-Forward 未通过"` vs `validation_gate.status=conditional_pass` 自相矛盾，且该策略正发着今天全部信号 | **根因 B：状态多源复制**（validation 文本 / gate / registry / signal_router 硬编码 / __init__.py 导出，改一处忘一处） |
| 4 | 模拟盘 100万 → 33 笔买入、0 笔卖出，8/10 后停摆；配置仍在（initial_capital=100万） | **根因 B：僵尸态无消亡机制**（配置在、代码在、没在跑、无检测） |
| 5 | 选股验证连续 4 天期望 -1.28%（t=-2.50 显著为负），系统仍每天照推 | **根因 C：价值闭环无反馈闸门**（负期望无熔断、无自动降级） |
| 6 | LLM 402 欠费降级 fallback 无显式标记；数据源全挂时 macro 日报"推送成功"但行业 0 个 | **根因 A：假设输入干净**（空数据照常走完整管道） |

**四根因定义**（源自 quant-system-health-audit 2026-08-07 复盘）：
- **A 假设输入干净，防御点状分散** → 解法：数据入口统一 sanitize 层
- **B 状态多源复制，无单一事实源** → 解法：注册表只从配置生成 + 启动 fail-fast 自检
- **C 测试与生产鸿沟** → 解法：真实异常 fixture 进回归 + E2E 进发布门槛
- **D 迭代速度 > 可观测性** → 解法：可观测性先行，静默失效要有告警

---

## 1. 五大工程支柱（根因的根治，不是症状的修补）

### 支柱 ① 单一事实源（SSOT）——治根因 B
- **S1 策略注册表只从 manifest 生成**：删除 `registry.py` 手写 dict、`signal_router.py` 硬编码集合、`strategies/__init__.py` 手动导出。运行时唯一权威 = `config/strategy_manifest.yaml`。
- **S2 启动 fail-fast 一致性校验**：enabled 策略必满足——(a) `validation_gate.status` 存在且非 `backtest_failed`/`failed`/`pending`；(b) `validation` 文本与 `gate.status` 语义不矛盾；(c) 三个文件（manifest/settings/运行中注册集）交叉一致。任一违反 → 拒绝启动（不像 double_yin 跑两周才发现）。
- **S3 通知状态统一状态机**：止损事件四态（initial → urgent → severe → critical，ack 终止，clear 移除）。状态唯一写入方 = `stop_loss_escalator`；`record_stop_loss_triggered` 不再重置 level（改只更新 last_alert_ts）。
- **S4 去重键语义化**：去重 key = `{事件类型}_{symbol}_{date}`（告警类不走内容指纹）；通用卡片指纹只用于信号卡。`send_card` 去重命中**返回 False 且写审计行**，废除"假装成功"。
- **S5 僵尸态清理**：模拟盘二选一（恢复卖出闭环 or 从 manifest/配置移除）；每周一致性扫描报告"配置在但未运行的模块"。

### 支柱 ② 输入防御统一层——治根因 A
- **D1 DataManager 出口 sanitize**：所有返回路径（collector→cache→fallback 链）统一清洗：NaN 行删除、全空表拦截、断连标记；配套 fixture 含"断连/空/None/字段漂移"样本。
- **D2 内容质量门**：macro/资金流日报数据完整度低于阈值（如 行业 0 个、维度 <4/6）→ 改发"⚠️ 数据源异常简报"并写 source_health 事件，**不冒充正常日报**。LLM 降级（402/超时）→ 报告显式标注"规则模式（LLM 不可用）"。

### 支柱 ③ 可观测性——治根因 D
- **O1 发送审计日志** `data/notification_audit.jsonl`（只追加）：每条发送记录 = 时间/通道/目标/事件类型/结果三态（sent|deduped|failed）/耗时。**所有 send_card/send_webhook 出口统一打点**。
- **O2 watchdog 第 6、7 项**：近 1 小时 dedup 拦截率 > 80% → P1 告警；发送失败数 > 0 → P1 告警。今天这种"告警被吞"1 小时内现形，而非事后审计才发现。
- **O3 一致性对账 cron**（每日收盘后 15:40）：registry 三方比对 + 验证标签矛盾扫描 + enabled 策略绩效快照 → 不一致即系统对话 P0 告警。
- **O4 `[DEGRADED]` 日志协议**：所有静默降级路径（LLM fallback / 数据源全挂 / 选股被门控 / 空报）必须带 `[DEGRADED]` 可 grep 标记。

### 支柱 ④ 测试-生产弥合——治根因 C
- **T1 契约测试（failed-first）**：每个已定位 bug 先写"证明 bug 存在"的测试，再改代码：
  - escalator：同日重复触发只升级一次（振荡回归）、ack 后终止、clear 后重建
  - send_card：dedup 命中 → 返回 False + 审计行；≠ 假装成功
  - registry：manifest 矛盾 → 启动失败
  - 质量门：空数据 → 不发正常日报
- **T2 生产场景 fixture**：把本次 6 个实证场景固化（510500 连续 40 轮破位、同指纹告警 2 小时、double_yin 双状态、402 fallback、行业 0 个空报、同日双信号卡）。
- **T3 发布门禁** `scripts/release_check.py`：全量回归 + 一致性校验 + 发送 dry-run + ruff/bandit → 全绿才允许重启生产调度器。

### 支柱 ⑤ 价值闭环自校正——治"核心价值为负"
- **V1 选股负期望熔断**：连续 10 个验证日期望 < 0 → 自动暂停选股推送 + 系统对话告警；只允许人工调参后显式重开。熔断状态持久化（screener_params.json 增 `auto_halt` 字段）。
- **V2 策略启用自动裁决**：周绩效看板（已有 performance_tracker）→ 策略连续 20 条真实信号期望为负且无改善 → 自动降级 disabled + 通知。enabled 不再靠手动维护。
- **V3 验证回写循环**：每日验证结果（sim_return_pct）→ auto_tune 优化目标（已接线，需加止损线）→ 形成"信号→验证→调参→再验证"自我修正闭环。
- **V4 模拟盘决策**：恢复"买入+卖出+止盈止损"完整闭环（优先），否则从项目移除所有引用（不留僵尸配置）。

---

## 2. 执行路线图：5 个 PR，每个独立可验收

> 铁律：一次只动一个 PR；每 PR = 契约测试(failed-first) → 改代码 → 全量回归(基线 2271 passed 不得新增失败) → release_check → 重启 → 观察 1 个交易日 → 再进下一个。

| PR | 范围 | 改动文件（预计） | 关键测试 | 验收标准 | 预估 |
|---|---|---|---|---|---|
| **PR1 告警链路重建**（P0 资金安全） | 止损升级状态机修复 + dedup 语义修正 + 发送审计打点 | `stop_loss_escalator.py` `feishu.py` `engine_mixin.py` | 振荡回归测试；dedup 返回 False 测试；审计行写入测试 | 破位日：每标的升级卡 ≤1 次/日且真实到达业务群（API 回读确认）；审计日志有 sent/deduped 记录 | 当天 |
| **PR2 策略注册表 SSOT** | 删硬编码 + 一致性校验脚本 + double_yin 裁决 | `registry.py` `signal_router.py` `strategies/__init__.py` `check_registry_consistency.py` `strategy_manifest.yaml` | manifest 矛盾 → 启动失败；去硬编码后注册集不变（冒烟） | 启动 0 矛盾；三方一致脚本全绿；无"伪造状态"策略 | 1 天 |
| **PR3 可观测性** | 发送审计 + watchdog 扩展 + 对账 cron | `feishu.py` `watchdog.py` `scheduler_jobs.py` | watchdog 新检查单测；审计格式单测 | 人为制造 dedup 风暴 → watchdog 1 小时内告警（模拟测试） | 1 天 |
| **PR4 输入防御** | DataManager sanitize + 日报质量门 + 异常 fixture | `data/manager.py` `fixures/` `flow_report.py` `macro/report.py` | 空表/NaN/断连 fixture 回归；质量门单测 | 数据源全挂时日报显示"异常简报"而非正常日报（[DEGRADED] 标记） | 1-2 天 |
| **PR5 价值闭环自校正** | 期望熔断 + 启用自动裁决 + 模拟盘二选一 | `screener_params.py` `performance_tracker` `manifest` `execution/paper.py` | 熔断逻辑单测；全链路 E2E | 连续 10 日负期望 → 选股自动暂停并告警（CI 验证） | 1-2 天 |

**double_yin 裁决（PR2 前置，需用户拍板）**：
- 事实：`validation="Walk-Forward 未通过 2026-07-26 样本外 Sharpe≤0"`；`gate=conditional_pass(80笔, avg 2.8%, Sharpe -0.02, 胜率 48%)`；8/7-8/14 它在发 21/35 条信号且今日 2/2
- 选项 A（推荐）：按更严口径——标记 `conditional_pass` 但验证文本保留"Sharpe≤0 需 paper 观察"，并挂"观察期"；PR1 后由验证闭环裁决（V2 自动裁决会接住）
- 选项 B：直接禁用至 paper 验证 30+ 信号
- 要在 PR1 完成后 1 个交易日内定案，避免"矛盾状态"继续存在

---

## 3. 防"越改越多"五条铁律（每条都有执行检查点）

1. **一次一链路**：5 个 PR 严格串行，PR 内改动原子化；每 PR 全量回归一次（`pytest tests/ -q --timeout=30`，基线 2271 passed/6 预存 failed 不得增加）
2. **失败优先（TDD）**：先写契约测试证明 bug 存在（红），再改代码（绿）。没有失败测试的问题 == 没修
3. **改前锁基线**：开工前记录全量回归基线数字；每 PR 结束对比，新增失败 ≠ 0 则 PR 不验收
4. **隔离运行**：测试全部走 GUGU_TEST_MODE + MockTransport（结构性禁 HTTP 外发，v0.9.8 已建）；后台 pytest 绝不与生产 scheduler 并存；生产进程只允许在用户授权 + 收盘后 + release_check 全绿时重启
5. **观察再前进**：每 PR 上线后次日检查 `notification_audit.jsonl` + 业务群实际送达 + 无新告警 → 才允许动下一个 PR

## 4. 已知坑位（gugu-test-hygiene 沉淀，改代码前必读）
- 测试断言日志用 loguru StringIO sink（caplog 抓不到 loguru）
- 函数内局部 `from X import Y` → patch `X.Y` 而非调用方模块
- 测试共享状态文件必须 per-instance uuid 隔离
- 缓存新鲜度/质量校验时间基准必须注入一致（`now` 贯穿所有层）
- YAML 用 Python 直读直写验证可解析，别用带行号的 read_file 内容重建
- 跑回归前 `pgrep -fl pytest` 确认无残留进程（防自杀式 kill 陷阱）

## 5. 预期终态（全部 PR 完成后）
- 本次 6 类问题家族全部有契约测试锁死，回归即红
- 静默失效发现时间：周级（事后审计）→ 小时级（watchdog+审计日志）
- 选股负期望：10 日自动熔断，不会再"亏一个月才被发现"
- 策略启用：自动裁决，状态单一事实源，无自相矛盾
- 用户每天收到的每条消息：可追溯（审计）、可验证（业务群回读）、有质量保证（空报降级）

## 6. 风险与应对
| 风险 | 应对 |
|---|---|
| PR1 改 dedup 语义导致信号重复推送 | 信号卡指纹去重保留（只改告警类 key）；PR1 验收含"同日重复信号 ≤1 次" |
| 重启调度器影响盘中扫描 | 只在收盘后（>15:30）或用户授权时重启；launchd KeepAlive 自动拉起 |
| 全量回归耗时（~110s） | 每 PR 一次，接受；不并行 |
| 模拟盘恢复卖出闭环工作量大 | V4 允许选"移除"路线（配置+代码引用清理），不留僵尸态 |