# 多专家 Review 与开发进度盘点（2026-09-22）

> 范围：基于 `fix/paper-lifecycle-20260909` 分支当前 HEAD（含本次提交 48fada9）对 `/Users/dong/workspace/gugu` 的代码、配置、运行态做对抗性复查。
> 结论先行：**生产边界仍然安全（无任何 verified 策略、track_live 强制只读），但全项目没有任何策略具备支持生产自动交易的完整盈利证据。**

---

## 一、真实运行态（动态 review）

| 项 | 实测值 | 判定 |
|---|---|---|
| 系统时间 | 2026-09-22 22:33 CST | 注入的"08-28"为过时值，以机器时钟为准 |
| scheduler 进程 | pid 692，`kill -0` 存活 | alive |
| boot / heartbeat | boot 2026-09-22T22:16:17；heartbeat last_cycle 22:32:32，status=alive，halted=false | 新鲜 |
| 账户快照 | total_value=1,000,000，cash=1,000,000，positions=0 | 无真实持仓、无真实资金变动 |
| 执行模式 | `execution.mode: paper`；`live` 才配置 `broker: qmt` + `confirm_required` | 当前不触达真实券商 |
| 生产白名单 | `strategy.verified_strategies: []` | 生产策略集恒为空（测试断言佐证） |

**核心安全链已逐一核验：**
- `LivePositionTracker.order()` → `success=False`，"track_live 模式不自动下单" —— **只读强制成立**。
- `get_runtime_strategy_names(mode in 生产)` 先按 `verified_strategies` 过滤（`[]`）→ 候选为空 → **生产运行时策略集恒为空**（由 `test_production_runtime_strategies_within_verified_whitelist` 断言）。
- manifest 全表 **无任何 `validation_gate.status: enabled`**（grep 确认）—— 不存在"伪装成生产就绪"的策略。
- `test_conditional_pass_is_paper_only` 断言 `conditional_pass` 只能进 paper、不能进 `track_live` —— **分层隔离成立**。

---

## 二、多专家视角 Review

### 架构专家
- **门禁单一真相源已落地**：`validation/gate_policy.py` 定义 `GATE_VERSION="gate-v2"`，是验证脚本、参数优化、启动一致性检查共用的唯一阈值来源。无 `gate-v1` 残留代码（grep 确认）。
- **manifest / settings / registry 三元组语义一致**：`registry.get_runtime_strategy_names` 以 `verified_strategies` 白名单 + `manifest.is_runtime_enabled` 双重过滤，生产模式最终落点为空。
- **调度器与引擎已解耦**：`engine/main.py` 注入 broker，`scheduler.py` 只编排任务；本次未提交改动把阻塞的 `subprocess.run` 改为 `asyncio.to_thread`，修复了事件循环被饿死、心跳停滞、watchdog 误判挂死重启的根因（实证 2026-09-15 04:15）。
- **潜伏缺陷（建议）**：`manifest.is_runtime_enabled` 调用 `GatePolicy.evaluate(metrics)` 时**未传 `category`**，导致 gate-v2 的分层回撤（短线 <15% / 中长线 <25%）在生产准入检查中不生效。当前无 `enabled` 策略故为潜伏问题；建议为信号策略补 `validation_gate.gate_category` 字段并接入。

### 产品专家
- 项目已从"策略集合"演进为具备：事件研究框架、paper 生命周期、统一回测/报告、Web 看板、飞书通知、认知引擎（多视角书籍研判）的较完整产品骨架。
- **诚实定位**：当前仍是"观察/模拟"产品态，没有任何策略通过 gate-v2 进入生产自动交易。这是正确且稳健的状态，不应为"看起来有收益"而放宽门禁。

### 策略专家
- 信号策略扩至 20+ 个，状态分布：`conditional_pass`（多数）/ `in_review`（price_volume_signal、regime_cycle_four）/ `pending`（yege 等）。**全部停留在生产就绪以下**。
- **数据诚信缺陷（已修复）**：`rsi_reversal` 的 `validation` 文本写"Walk-Forward 通过 2026-07-26"，但其 `metrics` 全为 0（total_trades=0, avg_sharpe=0）—— 陈旧文本与登记指标矛盾。本次已更正为诚实陈述，并要求"若确有结果须回填 metrics 后方可主张"。
- **结构性建议（未强制改）**：存在一批 `conditional_pass` 但 `metrics` 全 0 / `{}` 且文本明确"待 Walk-Forward 验证"的策略（sector_momentum、dragon_head、huamulan_ghost_breakout、sentiment_cycle、macd_zero_axis、binge_714/712/wave/trend）。其 `conditional_pass` 在项目语义中等价于"paper 可观察"，名称却暗示"已通过某道门禁"。建议将零证据者统一改 `pending`，或将 `conditional_pass` 重命名为 `paper_candidate` 以消除歧义。**此改动会影响 paper 观察池，需你确认后再做。**

### 算法 / 量化专家
- gate-v2 相对 v1 的关键升级已落地：新增 `profit_factor ≥ 1.5`（弥补只看胜率不看盈亏比）、按策略类型分层回撤。方向正确。
- 回测引擎（T+1、涨跌停方向感知、停牌、ST、北交所 ±30%、滑点/佣金/印花税/过户费、部分成交、样本内/外隔离、Walk-Forward 跨窗口状态连续）按历史结论已具备 A 股真实性。
- 缺口仍在：通用 `PaperBroker` 尚未具备 CXBR ledger 同等级别的策略级已实现盈亏统计；paper ledger 尚未形成正式的策略级/日期级/市场状态级报告；多股 Walk-Forward 组合级资金连续模拟未成形。

### 股票专家
- A 股制度细节（T+1、涨跌停、北交所、ST、停牌拒单）在回测与实盘路径均有处理；`realtime_scanner` 在 `live` 模式要求 QMT 连接且 `confirm_required`，未连接则跳过自动执行 —— 实盘护栏成立。

### 风控专家
- `track_live` 只读、生产白名单为空、`paper` 走 `PaperBroker` 模拟（持久化到 `data/paper_broker_state.json`，与真实持仓隔离）—— 三层边界清晰。
- 历史告警（resource_tracker / Task exception was never retrieved / leopard_universe_filter）经历史审计确认为旧进程遗留噪声，最新日志（`gugu_2026xxxx.log`）无复现。

### 数据工程专家
- 数据源主源 sina + 降级 eastmoney/akshare/tencent；历史已修复东方财富历史行情嵌套重试（最坏 9→3 次）。
- 本次并入的 akshare 行业资金流失败 `ERROR→WARNING` 对齐了"备用源失败属设计内降级路径"的约定，避免瞬时限流污染 ERROR 统计。
- 异步资源生命周期（DataManager/collector Session 关闭、auto_recover 临时 DataManager 关闭）此前已补齐。

### 测试 / 可靠性专家
- 全量基线：**2207 passed, 143 skipped, 16 warnings**（较 8/28 的 2064 增长，项目持续扩展）。
- 剩余 16 warnings 均为良性：py_mini_racer 与 Python 弃用、Starlette/httpx 兼容、scikit-learn 特征名/单类别 ROC-AUC。
- 覆盖：策略准入、配置一致性、manifest 运行时门禁、异步、回测、ledger 均有专项测试。

---

## 三、开发进度盘点

### 已完成（自 8/28 以来重大演进）
- gate-v1 → **gate-v2**（profit_factor + 分层回撤）。
- 事件研究框架（enrich/统一 CLI/拒绝信号时间戳前的成交、长假期信号对齐、窗口外陈旧信号拒绝）。
- 舆情新闻/公告双源失效修复（akshare 接口误用 + pandas arrow 正则）。
- 尾盘验证行情字段防护 / 数据源失败返回 None 占位。
- 新策略：binge 714/wave/trend、macd_zero_axis、sector_momentum、dragon_head、huamulan_ghost_breakout、sentiment_cycle、two_b_reversal、yege 等。
- 调度器阻塞调用修复（to_thread）、akshare 降级日志级别修正。
- 统一回测成本口径、Walk-Forward 状态连续、CXBR paper ledger 对账。

### 进行中 / 未闭合（关键）
- **无任何 verified 生产策略**；所有收益证据均不充分。
- `rsi_reversal` 缺定量回测；`box_breakout` Walk-Forward 未通过；`double_yin`/`linjudaye` 样本外证据不足；`price_volume_signal`/`regime_cycle_four` 仅 in_review。
- 通用 PaperBroker 缺策略级已实现盈亏统计；paper ledger 未成正式报告。
- 多股 Walk-Forward 组合级资金连续模拟未成形。
- 大型文件拆分（历史 Task #10）仍 in_progress。
- 认知引擎毕业标准与价值评估（Task #14）pending。
- `is_runtime_enabled` 未传 `category`（gate-v2 分层回撤潜伏失效）。

### 本次新增修复（已提交 48fada9）
1. `rsi_reversal` 移除与指标矛盾的"Walk-Forward 通过"陈旧文本。
2. `manifest.py` / `registry.py` 残留 `gate-v1` 注释校准为 `gate-v2`。
3. 并入此前未提交的三诊修复（akshare 降级、scheduler to_thread、box_breakout 诚实标注）。
4. 验证：定向 71 passed；ruff All checks passed；compileall OK；pre-commit 钩子全过。

---

## 四、建议的后续动作（待你确认）
1. **（建议）** 零证据 `conditional_pass` 策略（8 个）统一改 `pending`，或将 `conditional_pass` 重命名为 `paper_candidate`。
2. **（建议）** 为信号策略补 `validation_gate.gate_category` 并接入 `is_runtime_enabled`，让 gate-v2 分层回撤在生产准入真正生效。
3. **（进行）** 继续按 gate-v2 重新验证 `rsi_reversal`/`box_breakout`/`double_yin`，为断板反包/CXBR 累积 ≥30 个跨日期、跨市场状态的独立 paper 样本。
4. **（保持）** 维持 `track_live` 只读、verified 为空的生产安全边界，不宣称稳定收益。
