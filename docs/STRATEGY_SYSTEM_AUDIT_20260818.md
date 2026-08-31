# gugu 策略系统深度审计与优化计划

> 审计日期：2026-08-18
> 范围：策略注册/禁用、准入门禁、回测与 Walk-Forward、前视偏差、信号流水线、数据故障降级、生产执行安全
> 结论性质：代码与项目内日志审计；未将本次结论表述为“策略未来盈利保证”。

## 1. 首屏结论

### 1.1 当前系统是否靠谱

**执行安全性：中等偏高；策略研究可靠性：中等偏低；生产盈利准入：不合格。**

- 安全边界目前是可信的：`config/settings.yaml:250-269` 将执行模式设为 `track_live`，`src/gugu/engine/engine_mixin.py:432-478` 明确只通知、不自动下单；已有测试也禁止默认 `live` 模式。
- 研究流程已经具备正确方向：连续持仓回测、`shift(1)`、多股 Walk-Forward、前视偏差测试、manifest 证伪档案和数据证据链都已存在。
- 但当前不能说“策略系统已验证可靠”：`box_breakout`、`rsi_reversal`、`double_yin` 均缺少满足 gate-v1 且证据完整的生产准入依据；当前配置已将它们放入 experimental，生产 `track_live` 不加载策略信号，仅跟踪真实持仓。历史准入门槛和 paper/live 数据缺失语义仍需持续审计。
- 当前策略被禁用较多，不是系统失灵，而是连续持仓审计正在揭露事件研究和小样本回测的高估。这个方向是正确的，但状态机还没有彻底把“研究、观察、条件通过、核心生产”隔离开。

### 1.2 为什么好多策略被禁用

根因按重要性排序：

1. **样本外失效或负期望**：如 `rebound_elite`、`hot_money`、`chenxuezhang`、`death_valley_rebound`、`bollinger_oversold_rebound`、`industry_capital_momentum`，项目内记录了验证期负超额、负 Sharpe 或持仓回测亏损。
2. **事件研究与连续持仓口径不一致**：`config/strategy_manifest.yaml:845-861` 对 `chenxuezhang` 明确记录事件研究正收益，但连续持仓回测均值收益 -0.22%、Sharpe -0.24；`1149-1197` 对超跌反弹类策略记录了事件研究正收益而持仓回测转负。这说明只看“信号后 N 日收益”会忽略信号重叠、资金锁定、出场、重复持仓、交易成本和资金路径。
3. **小样本、集中单股或集中板块**：`hot_money` 的历史信号依赖单股占比过高；`rebound_elite` 的正结果来自 16 个小样本；`price_volume_signal` 的验证结果依赖科创板和单股集中，故仍是 in_review。
4. **市场状态错配**：趋势策略在震荡/下跌样本中被验证，均值回归/反弹策略在弱市中又可能接飞刀。很多策略的“适用环境”写在文档里，但不等于已经完成 regime 条件下的独立样本外验证。
5. **实现漂移和参数错误**：manifest 已记录 `ma5_momentum` 曾因代码参数名不匹配而产生无效回测；`linjudaye` 曾只有买入规则、没有可用卖出端，导致资金锁死。说明“策略能运行”不等于“回测语义正确”。
6. **数据源故障和字段/单位问题**：2026-08-14 日志中出现 akshare/eastmoney 资金流连续失败、宏观接口失败、通知降级、新闻解析错误等。数据不可用会直接改变过滤结果和 regime 判断，不能把这种运行状态当作策略本身的表现。

## 2. 策略状态盘点

由 `config/strategy_manifest.yaml` 统计：

| 项目 | 数量 | 含义 |
|---|---:|---|
| signal_strategies 总数 | 40 | 包含研究、待验证、证伪和启用档案 |
| manifest `enabled=true` | 6 | `box_breakout`、`rsi_reversal`、`double_yin`、`linjudaye`、`price_volume_signal`、`regime_cycle_four` |
| manifest `enabled=false` | 34 | 证伪、待验证、死配置或历史研究 |
| `backtest_failed` | 19 | 明确不应进入信号融合 |
| `pending` | 9 | 尚无足够验证证据 |
| `conditional_pass` | 9 | 只能观察/小范围验证，不应被当作正式核心策略 |
| `in_review` | 2 | 证据不足，尚未完成准入 |
| `failed` | 1 | 明确失败 |

当前 regime 启用集合来自 `config/settings.yaml:60-100`：

```text
box_breakout
rsi_reversal
double_yin
linjudaye
price_volume_signal
regime_cycle_four
```

问题不在“禁用太多”，而在于 **manifest 的 enabled、validation_gate.status、settings 的 verified/experimental、enabled_by_regime 四套状态还没有成为一个不可矛盾的状态机**。

## 3. 已证实问题

### P0：准入门槛存在多套口径（历史问题，部分已修复）

证据：

- `scripts/validate_strategy_gate.py:19-29`：当前已统一读取 `GatePolicy gate-v1`，但历史报告仍可能保留旧门槛字段，需按报告版本审计。
- `scripts/optimize_params.py:722-792`：参数稳定性 `>=80%`、样本外交易次数 `>=30`。
- `src/gugu/strategy_consistency.py:132-137`：同样采用 80% 和 30 笔。
- `config/settings.yaml` 和 `scripts/walk_forward_offline.py` 当前已同步为 gate-v1 的 80% 与样本外交易次数门槛。

影响：同一策略可能被一个脚本判定通过，被另一个脚本判定失败；审计记录无法复现；生产准入依赖调用者选择了哪条命令。

修复：建立单一 `StrategyGatePolicy`，所有脚本、测试、报告和启动自检只读取同一套版本化门槛；门槛变更必须带版本号和生效日期。

### P0：`conditional_pass` 仍被当作 verified 或运行时启用

证据：

- 历史版本的 `config/settings.yaml` 曾将 `box_breakout`、`rsi_reversal` 放入 `verified_strategies`，但 manifest 均为 `conditional_pass`；当前配置已将二者移入 experimental。
- `double_yin` 已放入 experimental；`enabled_by_regime` 保留它只供 paper/观察模式使用，生产模式由 verified 白名单过滤。
- `src/gugu/config/models.py:157-189` 的 regime 过滤只按列表与 verified/experimental 集合求交，没有检查 manifest 的 `validation_gate.status`。
- `src/gugu/engine/signal_pipeline.py:225-232` 负责标注能力和验证等级，但不是强制准入门禁。

影响：配置语义容易产生“文档说观察、代码却参与扫描”的错觉；后续维护者只看 `enabled_by_regime` 就可能误把实验策略当作生产策略。

修复：运行时只允许 `gate.status=enabled` 进入核心融合；`conditional_pass` 只能进入独立 paper/observation 通道；`experimental` 不能改变核心策略权重；`backtest_failed/pending/in_review` 不得进入交易信号路径。

### P0：参数优化存在 `--force` 绕过准入写回（已修复，需回归防止复发）

证据：`scripts/walk_forward_offline.py:267-300` 和 `scripts/optimize_params.py` 的同名逻辑允许 `--write --force` 在解锁失败时写回 `strategy_defaults.yaml`。

影响（修复前）：即使样本外 Sharpe、参数稳定性、收益或交易次数失败，仍可把参数污染到后续回测和生产默认值。当前两个优化入口均只在 GatePolicy 通过时写回，`--force` 仅保留兼容性。

修复：删除生产路径中的 `--force`；如确需研究写回，写入隔离的 `data/research_overrides/`，必须带 `research_only=true`、操作者、原因、过期时间，且配置加载器拒绝将其用于 `track_live/live`。

### P0：paper/live 数据缺失时存在 fail-open

证据：`src/gugu/engine/signal_pipeline.py:779-785` 明确写“数据缺失时默认放行”；基本面分支 `814-825` 对 `data_missing` 放行；资金流/另类因子分支 `827-835`、`896-919` 也没有统一的质量状态门禁。

影响：线上数据源被限流、字段异常或缓存过期时，系统可能仍把信号送到执行层。当前 `track_live` 不自动下单，风险主要是错误建议和模拟盘污染；若未来切到 `live`，则是实盘风险。

修复：区分三类状态：`fresh_valid`、`stale_degraded`、`unavailable`。对于交易执行：`unavailable` 必须阻断买入，`stale_degraded` 只能进入观察，不得进入 live；卖出/止损链路可以独立保守运行。所有信号记录必须写明 data quality。

### P1：多股 Walk-Forward 的时间对齐可能不严谨

证据：`src/gugu/backtest/engine.py:1040-1061` 先按各股票最短行数对齐，再使用位置切片；没有按真实日期做交集/时间戳对齐。

影响：不同股票停牌、上市日期、缺失交易日或数据源缺口会让同一窗口的“第 N 行”对应不同自然日期；跨股票聚合可能混合不同市场阶段，导致训练/测试边界含义不一致。

修复：先标准化时区和交易日，再按真实日期构造全局窗口；每个 fold 输出 `train_start/train_end/test_start/test_end`；对缺失日、停牌和新上市股票明确处理，不允许静默按行号对齐。

### P1：优化目标只看平均 Sharpe，容易掩盖最差标的和集中风险

证据：`src/gugu/backtest/engine.py:1066-1129` 按所有股票平均训练 Sharpe 选参数；此前测试结果只返回平均测试指标，虽然有 `test_details`，但没有把最差股票、负收益股票比例、收益集中度纳入审计。

影响：一只强趋势股或少数股票可能拉高平均值；策略对大多数股票无效时仍可能“平均通过”。

阶段性修复：`walk_forward_optimize_multi_stock()` 现在在保持原有 `test_avg_*` 字段兼容的同时，输出 `test_min_sharpe`、`test_min_return`、`test_worst_max_dd`、`test_negative_return_ratio`、`test_max_trade_share` 和 `risk_flags`，并在所有窗口中标记全局最差窗口。离线验证报告同步展示这些风险指标。

当前边界：`test_max_trade_share` 仅衡量交易笔数集中度，不等同于资金或收益贡献集中度；后续必须增加成交金额、已实现 PnL 和行业映射后，才能实现 `max_symbol_contribution`、`max_sector_contribution`。这些指标目前是审计输出，不自动改变生产准入门禁。

### P1：缺少完整的多重检验/研究次数记录

证据：当前报告记录最优参数和窗口结果，但没有统一记录尝试过的策略变体、参数组合总数、窗口方案数量、数据清洗版本和研究迭代次数。

影响：即使 OOS 通过，也无法判断是否是大量试验后偶然筛出的赢家；项目内已有多次策略重构和口径切换，选择偏差不可忽略。

修复：每次研究生成不可变 `research_run_id`，记录 git commit、数据快照 hash、参数网格、候选策略数、目标函数、随机种子、过滤规则版本、结果是否被看过。将最终 holdout 或 prospective paper period 作为一次性揭示。

### P1：策略状态字段不是单一真值源

证据：同一策略同时存在 manifest `enabled`、`validation` 文本、`validation_gate.status`、settings `verified_strategies`、settings `experimental_strategies`、`enabled_by_regime`、registry 是否存在等字段。

影响：状态容易漂移。`strategy_consistency.py` 目前只检查部分重叠、权重和 manifest 禁用，不会把所有 gate 状态与 runtime role 做硬性校验。

修复：引入显式状态机：`research -> pending -> backtest_failed | conditional_pass -> paper -> enabled | retired`；运行时角色由状态机派生，禁止手工同时维护多个角色列表。

## 4. 已确认的正确方向

1. `BacktestEngine` 使用 `signals_df["signal"].shift(1)`：`src/gugu/backtest/engine.py:245-265`，至少解决了“用当日收盘生成并在同日成交”的主要风险。
2. 盘中只在最后一根 bar 日期等于当天时剔除：`tests/unit/test_lookahead_fix.py:35-94`，避免把昨日完整 bar 错误删除。
3. 多股 Walk-Forward 返回逐股票明细：`src/gugu/backtest/engine.py:1146-1191`，为后续最差标的审计提供了基础。
4. 已证伪策略保留 manifest 档案、从 registry 清除：`tests/unit/test_registry_filtering.py:17-35`，这是比“代码残留但不调用”更安全的做法。
5. `track_live` 当前明确拒绝自动下单：`src/gugu/engine/engine_mixin.py:432-478`，与用户项目要求的“不自动下单”一致。
6. 数据基础设施已经开始建立 evidence metadata 和 provider 隔离，`docs/DATA_INFRASTRUCTURE_OPTIMIZATION_PLAN.md:176-203` 明确记录了未通过的网络门禁，没有把一次真实采集失败伪装成上线成功。

## 5. 按专家视角的判断

### 5.1 量化研究员

核心问题是研究流程已从“单次回测”升级到“多股 Walk-Forward”，但还没有升级到“不可污染的研究账本”。必须防止反复改规则后仍引用旧 OOS 结果。

### 5.2 统计学/因果推断视角

事件研究只能回答“信号后窗口收益是否不同”，不能直接回答“带资金约束、持仓状态、出场规则、成本和信号重叠时能否交易”。当前 chenxuezhang、超跌反弹类策略已经给出反例。以后事件研究只能作为发现线索，不能作为生产准入证据。

### 5.3 组合经理

平均收益和平均 Sharpe 不够。必须看最差标的、最差年份、最差 regime、暴露集中度、策略间相关性、回撤持续时间和组合层面的资金锁定。`max_total_position_ratio=70%` 和 `max_total_positions=5` 只是容量约束，不等于组合 alpha 已验证。

### 5.4 交易系统工程师

最大的工程风险不是某个指标写错，而是“状态和数据质量没有贯穿全链路”。每个信号必须能回答：哪个策略版本、哪个数据快照、哪个 regime、哪些过滤器通过/拒绝、是否使用 stale 数据、最终为什么只通知不下单。

### 5.5 风控负责人

数据不可用时买入应该 fail-closed；止损/卖出应与买入过滤分离。LLM、十大视角和资金流等分析模块不应拥有绕过硬门禁的权限。当前 `track_live` 是安全默认，但任何切换 `live` 前都必须额外确认数据质量和人工审批链。

### 5.6 反前视偏差审计

目前有 `shift(1)` 和盘中裁剪测试，但仍需验证：基本面/资金流披露时点、历史股票池 membership、复权口径、涨跌停与成交可得性、缺失数据的填充方式。仅靠静态搜索 `shift(-n)` 不足以证明没有隐性泄漏。

## 6. 优化目标

### 目标 A：把“禁用”变成可解释的证据状态

每个策略必须输出：

```text
strategy_id
strategy_version
runtime_role: core | paper | observation | research_only | retired
validation_status
validation_run_id
data_snapshot_id
train/test 时间范围
总样本数、OOS 交易数
OOS Sharpe、收益、回撤、胜率
最差 fold、最差股票、最差 regime
集中度和成本敏感性
禁用/恢复原因
下一步动作和复验条件
```

### 目标 B：统一准入门禁

建议 v2 门槛：

- OOS 结果必须来自连续持仓回测，不接受单纯事件研究作为最终证据；
- 至少 5 个真实时间 fold，且每个 fold 明确训练/测试日期；
- OOS 交易次数按策略频率设定，最低 30 只是预警线，不是充分条件；
- 参数稳定性、最差 fold、正收益股票比例和集中度同时约束；
- 经过 2 倍成本/滑点压力测试后不能从正变负；
- 通过后先 `paper`，积累 30+ 可交易信号并完成实盘前向复验，再进入 `enabled`；
- 任何策略重构、数据口径变更、出场逻辑变更都使旧准入结果失效。

### 目标 C：隔离策略研究与生产

建议明确四条运行轨道：

1. `core`: 仅 gate.status=`enabled`，参与核心融合。
2. `paper`: 只进入独立模拟盘，不影响核心信号和权重。
3. `observation`: 只记录原始信号，不进入通知或执行决策，除非用户主动查看。
4. `research_only`: 只用于回测和研究，不加载到 runtime registry。

## 7. 分阶段实施计划

### Phase 0：冻结和基线（P0，先做）

- 保持 `track_live`，禁止切换 `live`。
- 暂停所有 `--force` 参数写回生产默认值。
- 将 `box_breakout`、`rsi_reversal` 从“verified”改成“conditional/core-observation”中的一种明确语义；在连续持仓 OOS 未完成前不宣称正式 verified。
- `double_yin`、`linjudaye`、`price_volume_signal`、`regime_cycle_four` 全部进入独立 paper/observation，不参与核心权重。
- 记录现有 git 工作区改动，不覆盖未提交文件。

验收：启动自检能打印实际 runtime strategy role；不存在“manifest conditional 但 verified 融合”的矛盾。

### Phase 1：单一准入策略（P0）

新增一个门禁模块，例如 `src/gugu/validation/gate_policy.py`：

- 集中定义阈值和比较方向；
- 返回结构化 `GateDecision`；
- 提供 `evaluate_backtest_gate`、`evaluate_paper_gate`、`evaluate_runtime_gate`；
- 报告包含 policy version；
- `validate_strategy_gate.py`、`optimize_params.py`、`strategy_consistency.py` 全部调用它；
- 测试 50%/80%、缺失指标、负回撤符号、边界值和版本迁移。

验收：修改一处门槛，所有脚本和测试得到同一结果。

### Phase 2：状态机和运行时硬门禁（P0）

- 从 manifest 派生 runtime role，不再手工维护 verified/experimental 与 enabled_by_regime 的重复真值。
- `backtest_failed/pending/in_review` 在 runtime 直接拒绝加载。
- `conditional_pass` 只能写入 paper ledger，不能进入核心 `SignalRouter`。
- 所有信号记录 `strategy_status`、`runtime_role`、`gate_policy_version`。

验收：故意把 `backtest_failed` 加到 regime 列表，启动自检失败；故意把 conditional 策略放入 core，测试失败。

### Phase 3：回测口径统一（P0/P1）

- 将连续持仓回测作为唯一生产准入回测。
- 事件研究保留为研究线索，输出中强制标记 `event_study_only`。
- 统一信号重叠、同股持仓、资金不足、T+1、涨跌停、停牌、交易成本、滑点和部分成交规则。
- 每次回测保存数据快照、代码 commit、参数、成本假设和运行 ID。
- 对同一策略至少输出事件研究 vs 连续持仓差异，并解释差异来源。

验收：chenxuezhang、death_valley_rebound、bollinger_oversold_rebound 等历史反例能在统一框架中复现“事件研究正、持仓回测负”的警告。

### Phase 4：Walk-Forward 加强（P1）

- 改为按真实日期对齐，不按行号截断。
- 采用 rolling 或 expanding 窗口前先固定设计，不根据结果挑窗口。
- 输出每个 fold 的日期、参数、OOS 交易明细、收益、Sharpe、回撤、最差股票和 regime。
- 目标函数从平均 Sharpe 改为多目标约束：平均、最差分位、正收益比例、集中度、成本敏感性。
- 对 ML 策略增加 purged/embargo 时间隔离和 point-in-time 特征检查。

验收：测试数据包含停牌、缺失日、不同上市日期时，fold 日期仍然正确；强趋势单股不能单独拉高整体准入。

### Phase 5：数据质量与 fail-closed（P0/P1）

- 统一 `fresh_valid/stale_degraded/unavailable`。
- 买入：`unavailable` 阻断；`stale_degraded` 只观察；卖出/止损独立运行。
- 信号卡片显示数据源、采集时间、统计周期、复权/单位和质量状态。
- 数据源连续失败、字段单位异常、日期倒退、重复列、空结果均进入质量告警。
- 对 `paper/live` 链路增加“数据不可用不进入执行”的回归测试。

验收：模拟 akshare/eastmoney 同时失败时，系统不产生可执行买入信号，仍能生成止损告警。

### Phase 6：纸面前向验证与退役（P1）

- 每个 conditional 策略单独维护 paper ledger，不与核心策略混合。
- 记录信号生成时刻、当时可得数据、理论成交价、滑点、持仓路径、退出原因和基准超额。
- 30+ 信号只是最低观察线；低频策略应使用更长时间窗口和置信区间。
- 设定失效规则：连续多个滚动窗口低于基线、OOS 超额转负、集中度失控、数据质量不达标时自动降级 observation。
- 退役策略保留可复现档案，但不保留 runtime 注册入口。

验收：策略降级、恢复和退役均能由证据报告触发，不靠手工改多个 YAML 列表。

## 8. 建议新增测试矩阵

### 配置/状态

- manifest status 与 runtime role 一一映射。
- `backtest_failed`、`pending`、`in_review` 不得进入核心 router。
- conditional 不得占用 verified 权重。
- regime 列表与 manifest 状态不一致时启动失败。

### 准入

- 所有脚本使用同一门槛。
- 缺失指标必须失败，不得默认 0 后误通过。
- 边界值测试：Sharpe=0、DD=25%、稳定性=80%、交易数=30。
- policy version 改变后旧报告不能自动复用。

### 回测

- 同日信号不能同日成交。
- 停牌、涨跌停、T+1、部分成交和成本压力测试。
- 事件研究与连续持仓的重复信号、资金锁定差异。
- 多股不同日期、停牌和上市日期对齐。
- 最差股票/最差 fold 不能被平均指标掩盖。

### 数据

- 主源空响应触发 fallback。
- stale cache 只可观察，不可进入买入执行。
- 单位错误、日期倒退、重复列、字段漂移直接失败。
- 所有信号带 evidence metadata。

### 执行安全

- `track_live` 与 `signal_only` 永不调用真实下单。
- `live` 必须同时满足人工确认、数据质量、策略状态和风险门禁。
- 通知失败不应标记为已通知。
- 止损告警在数据源部分失败时仍可独立触发。

## 9. 当前不应做的优化

- 不要为了减少禁用数量而下调门槛。
- 不要把事件研究正收益直接写进 verified。
- 不要把 LLM 或十大视角评分变成准入裁判。
- 不要用 `--force` 写回生产参数。
- 不要在数据源不稳定时扩大策略池或切换 live。
- 不要同时重写回测引擎、策略规则和数据层；应先固定口径，再逐条验证。

## 10. 最终判断

**策略被禁用多，主要是因为系统开始承认真实证据，而不是因为策略池管理失败。** 真正的问题是：还有少数条件通过/实验策略在配置上靠近生产路径，且准入标准和数据质量语义不统一。

当前最优先的不是“再发明更多策略”，而是完成三件事：

1. 用单一状态机把 `core/paper/observation/research_only/retired` 隔离；
2. 用单一门禁策略统一所有准入脚本，彻底关闭 `--force` 生产写回；
3. 用连续持仓 Walk-Forward + 数据质量 fail-closed 重新验证现存六个启用/观察策略。

在这三件事完成前，系统适合 `track_live + 独立 paper 验证 + 止损提醒`，不适合自动下单，也不应宣称策略已经具备稳定盈利能力。

## 11. 本轮已落地变更与重启说明

### 11.1 已落地变更

- 运行时策略列表统一经过 `get_runtime_strategy_names()`：候选配置、manifest 验证状态和 Python 注册表三重过滤，验证脚本不再直接把配置并集当作生产可运行策略。
- `conditional_pass` 仅允许进入 `paper`；`track_live`、`signal_only` 和 `live` 只接受 settings `verified_strategies` 白名单中的已注册策略。当前白名单为空，因此生产模式不加载策略信号。
- RSI 专项调度在当前执行模式未通过准入时直接跳过，不再通过 `get_strategy()` 旁路加载策略。
- 滚动验证改为按真实日期构造独立 `train`/`test` 区间，测试指标只来自 test；支持在 train 区间选择参数，并输出 `train_start/train_end/test_start/test_end`。
- 参数扰动稳定性强制包含 0% 原始参数基准；原始参数回测失败时稳定性为 0，不再用扰动均值冒充基准。
- 买入数据质量、模拟盘前收和执行层二次 fail-closed 防线已落地；卖出/止损不被买入数据门禁阻断。

### 11.2 是否需要重启

| 变更类型 | 是否需要重启 | 说明 |
|---|---|---|
| 修改 Python 代码 | **需要** | 已运行的 scheduler/服务进程不会自动加载新代码；应先停止旧进程，再启动新进程。 |
| 修改 `config/settings.yaml` | 通常**不需要完整重启** | 项目支持 `refresh_config()` 热加载，但必须同时清理策略缓存；若 scheduler 未调用刷新入口，仍需重启。 |
| 修改 `config/strategy_manifest.yaml` | **建议重启** | `get_manifest()` 使用单例缓存；除非显式清理 manifest 缓存，否则运行进程仍使用旧清单。最稳妥做法是重启。 |
| 修改策略默认参数或数据质量规则 | **需要重启** | 这些属于 Python/配置加载链路变更，必须确保新进程和新配置同时生效。 |
| 仅修改 `docs/`、`CHANGELOG.md` | 不需要 | 文档变更不影响运行时。 |

本轮修改包含 Python 代码、策略准入逻辑、验证逻辑和 manifest 相关内容，因此：**如果当前 scheduler 正在运行，需要重启一次使代码和策略清单全部生效**。重启前后应确认：

```text
1. execution.mode 仍为 track_live；
2. 启动自检没有加载 conditional_pass/in_review/backtest_failed 策略；
3. scheduler 心跳恢复；
4. 没有调用 live broker 或自动下单接口；
5. 首次运行只检查日志、信号和飞书通知，不把通知当作成交证据。
```

推荐操作顺序：

```bash
# 先停止现有 scheduler，再启动新代码
.venv/bin/python scripts/run_scheduler.py

# 只读质量检查
.venv/bin/python -m pytest tests/unit/test_registry_filtering.py tests/unit/test_strategy_validation_workflow.py tests/unit/test_backtest_validation.py -q
```

当前提交不会改变 `execution.mode: track_live`，不会执行真实下单。
