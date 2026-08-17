# 代码审查与优化需求文档

> 审查日期：2026-08-15
> 审查范围：全项目（策略层 / 回测引擎 / 数据层 / 风控组合层 / 配置一致性）
> 审查视角：量化策略专家、回测工程专家、金融风控专家、软件工程专家
> 审查结论：**策略系统目前"半靠谱"——风控骨架扎实、测试体系完善，但存在 3 个会算错钱/风控失效的 P0 缺陷，且启用策略的真实胜率存疑（7 个 enabled 策略中 0 个达到严格 verified，3 个负夏普仍启用）。**

---

## 一、执行摘要

### 1.1 总体判断

| 维度 | 评分 | 说明 |
|------|------|------|
| 代码组织与规范 | ★★★★☆ | 模块分层清晰、命名规范、测试 2277+ 通过 |
| 风控设计意图 | ★★★★☆ | 三级风控 + 熔断持久化 + 铁律意识强 |
| 回测引擎正确性 | ★★★☆☆ | shift(1) 防前视方向正确，但熔断失效、成交价/幸存者偏差未解决 |
| 策略系统可信度 | ★★☆☆☆ | 0 个严格 verified，多个负夏普策略仍启用融合 |
| 配置一致性 | ★★☆☆☆ | manifest 与 settings 存在 P0 漂移，CI 门禁会红 |
| fanbao V2.0 | ★★☆☆☆ | 战法覆盖完整，但存在前视偏差 + 回测架构不兼容，无法验证 |

### 1.2 核心结论

**策略系统是否靠谱？** —— **当前不可直接用于实盘决策。** 理由：

1. **7 个 enabled 策略中，0 个达到严格上线标准（verified/status=passed）**。其中：
   - `double_yin`：Sharpe **-0.02**、param_stability **0.4**，权重却达 0.35（第二高）
   - `chenxuezhang`：持仓回测 Sharpe **-0.24**/收益 -0.22%，next_action 已写"建议降级"，仍 enabled
   - `price_volume_signal`：`in_review`，样本外 p=0.193 不显著，仍 enabled
   - `regime_cycle_four`：`in_review`，val_p=0.305 不显著，仍 enabled
   - `rsi_reversal`：manifest 自称"唯一严格达标"，但 validation_gate 无任何量化回测字段（0 回测记录）

2. **2 个已证伪策略泄漏进信号融合**（`death_valley_rebound` Sharpe -5.33、`bollinger_oversold_rebound` Sharpe -1.78，已于 2026-08-15 在 manifest 降级 disabled，但 settings.yaml 三处引用未清理）。

3. **回测引擎熔断逻辑是死代码**，回撤 ≥15% 触发的 HALT 不会阻止任何后续买入。

4. **刚重写的 fanbao V2.0 无法在现有回测引擎下验证**（架构不兼容 + 当日 close 前视）。

---

## 二、P0 级问题（阻断级，必须立即修复）

### P0-1 回测引擎熔断逻辑死代码，回撤熔断完全失效

- **位置**：`src/gugu/backtest/engine.py:588-593`
- **问题**：
  ```python
  # 循环末尾才执行，此时当日买卖已完成
  equity = cash + position_qty * close
  self._risk.update_portfolio_peak(equity, persist=False)
  dd_result = self._risk.check_portfolio_drawdown(equity, persist=False)
  if dd_result.action == RiskAction.HALT and position_qty == 0:
      signal = 0   # ① 循环末尾赋值，不影响当日；② 下一轮循环 signal 被重新读取覆盖
  ```
  ① `signal = 0` 位于每日循环末尾，当日买入/卖出已执行完毕，赋值无意义；② 下一轮循环开头 `signal = int(signal_arr[i])` 重新从策略信号数组读取，上一轮末尾的赋值被覆盖。且 `RiskManager._halted` 与 `is_tradable()` 均不检查熔断状态。
- **影响**：组合回撤 ≥15% 触发 HALT 后，**不会阻止任何后续买入**。回测中的组合回撤熔断形同虚设，生产环境风险敞口失控。
- **修复建议**：在每日循环**开头**（买入分支之前）判断熔断状态，熔断时跳过买入只允许卖出；或让 `is_tradable(direction="buy")` 检查 `_halted`。

### P0-2 配置漂移：已证伪策略泄漏进信号融合

- **位置**：`config/settings.yaml:70-71,85-86,96-97,119-120,139-140`
- **问题**：`death_valley_rebound`（持仓回测 Sharpe -5.33）与 `bollinger_oversold_rebound`（Sharpe -1.78）已于 2026-08-15 在 `strategy_manifest.yaml:1149,1174` 降级为 `enabled: false`，但 settings.yaml 仍保留三处引用：
  - `enabled_by_regime.bull/rally` 仍含二者（70-71, 85-86）
  - `enabled_by_regime.sideways` 仍含二者（96-97）
  - `experimental_strategies` 仍含二者（119-120）
  - `experimental_weights` 仍残留 0.08+0.08（139-140）
- **影响**：
  1. `tests/unit/test_config_consistency.py` 断言必然失败（CI 门禁已红）
  2. paper 模式下这两个已证伪策略仍会泄漏进信号融合，污染信号质量
- **修复建议**：从 settings.yaml 三处同步移除二者，保持 manifest 与 settings 单一事实来源。

### P0-3 负夏普/不显著策略仍 enabled=true 参与融合（信任根基问题）

- **位置**：
  - `config/strategy_manifest.yaml:383,408` — `double_yin` enabled:true，avg_sharpe **-0.02**，param_stability 0.4，权重 0.35
  - `config/strategy_manifest.yaml:830,845-856` — `chenxuezhang` enabled:true，持仓回测 Sharpe **-0.24**，next_action 写"建议降级 disabled"
  - `config/strategy_manifest.yaml:1091,1115-1126` — `price_volume_signal` enabled:true，status `in_review`，p=0.193 不显著
  - `config/strategy_manifest.yaml:1132,1136-1143` — `regime_cycle_four` enabled:true，status `in_review`，val_p=0.305 不显著
  - `config/strategy_manifest.yaml:171-181` — `rsi_reversal` enabled:true，validation_gate 无量化回测字段（0 回测记录），与 settings.yaml:112 注释"唯一严格达标者"自相矛盾
- **影响**：这是"策略系统是否靠谱"的核心问题——启用策略中无一只严格验证有效，负夏普策略被赋予高权重，信号融合的期望收益为负。
- **修复建议**：
  1. 将 `chenxuezhang` 降级 disabled（next_action 已明确建议）
  2. `double_yin`、`price_volume_signal`、`regime_cycle_four` 降级或移出权重分配，等待 paper 前向验证 30+ 信号后再评估
  3. 补 `rsi_reversal` 的真实量化回测记录，否则不能声称"严格达标"

### P0-4 fanbao V2.0 存在前视偏差 + 与回测引擎架构不兼容

- **位置**：
  - `src/gugu/strategies/fanbao/rules.py:240-246`（红线5 用当日 close）、`:348-352`（`_check_position` 用 `close[-1]`）、`:297-303`（情绪周期用当日 HS300 close）、`:686-687`（一字板 `pos_ok` 用 `closes[last]`）、`:909`（跳空 `is_limit_up[last]` 需当日收盘是否涨停）
  - `src/gugu/strategies/fanbao_strategy.py:162-179`（只在 `last = n-1` 写 signal）
- **问题**：
  1. **前视偏差**：战法声称"竞价高开确认后 9:30 前买入"（`rules.py:489-493`），但位置/红线/情绪/跳空判定都依赖**当日收盘/量能**——实盘开盘时根本不知道。回测中这些判定看似成立，实盘必然失真（虚高）。
  2. **回测架构不兼容**：`_generate_signals_impl` 只在最后一根 K 线写 signal；而 `backtest/engine.py:246-247` 对整段信号做 `shift(1)` 后次日收盘成交。对完整历史 df 一次性调用时，fanbao 只产生末一根信号，shift 后落到末尾之外 → **回测几乎零信号，该策略无法被现有引擎验证**。
- **影响**：fanbao V2.0 虽 `enabled: false`（待回测），但当前状态**既无法回测、又有前视**，等于重写尚未闭环。
- **修复建议**：
  1. 把"今日"信号统一改成只用截至昨日数据判定，今日仅用 `open` 做高开门槛（`shift(1)` 语义）
  2. 让 `_generate_signals_impl` 逐日生成信号（而不是只在 last 写），或为 fanbao 单独实现逐日滚动切片回测

---

## 三、P1 级问题（重要，高风险/逻辑错误）

### 风控一致性

**P1-1 ST 买入拦截不在风控铁律层 + Web 下单路径绕过**
- `src/gugu/risk/manager.py:304-385`：`check_order` 全程无 ST 买入 HALT，`is_st` 仅用于涨跌停比例计算
- `src/gugu/web/app.py:497-505`：Web 手动下单 `check_order(...)` 未传 `is_st`/`is_suspended`
- 后果：ST/停牌拦截只靠 `signal_pipeline.py:382-388` 的软标记，铁律层形同虚设
- 修复：`check_order` 买入分支加 `if is_st: HALT`，停牌对所有方向 HALT；Web 下单补传元数据

**P1-2 仓位 fallback 绕过总仓位预算**
- `src/gugu/execution/order_manager.py:141-142`：`suggested_ratio <= 0` 时回退 `max_position_ratio * 0.8`，覆盖 PositionController 给出的 0 预算（总仓位满/市场禁买/熔断）
- 修复：0 预算直接跳过下单，不 fallback

**P1-3 熔断状态文件损坏时 fail-safe 方向错误**
- `src/gugu/risk/manager.py:260-265`：加载异常"保守恢复为非熔断状态"，对风控而言"保守"应为 `halted=True`（阻断）
- 修复：损坏时置 halted 并告警

**P1-4 单股仓位下限突破总仓位预算**
- `src/gugu/analysis/position_controller.py:135-136`：`max(single_limit, 0.05)` 使熊市 2% 总预算下单股却给 5%
- 修复：`single_limit = min(single_limit, total_limit)`

**P1-5 模拟盘涨跌停未处理 ST + 无停牌检查**
- `src/gugu/execution/paper.py:185-202`：ST 按 10% 处理；`_order_impl` 无 `is_suspended` 阻断
- 修复：传入 is_st 用 5%，下单前检查停牌

### 回测正确性

**P1-6 加仓可突破 L1 单股仓位上限**
- `src/gugu/backtest/engine.py:518,538-558`：加仓分支 `buy_cash = cash`，`_execute_buy` 的 `max_value = min(cash, equity*position_ratio)` **未扣除已有持仓市值**，连续加仓后单股总仓位远超上限
- 修复：加仓时 `max_value` 扣除已有持仓市值

**P1-7 成交价用 close 而非 open + 一字涨停无法识别**
- `src/gugu/backtest/engine.py:275,528,571`：信号 shift(1) 后 T 日开盘即可成交，但用 T 日 close（仅 ±0.2% 滑点）；涨跌停判定 `close vs prev_close` 无法识别"开盘一字涨停、盘中打开、收盘未涨停"
- 修复：执行价改用 T 日 open，或至少用 open 判定可成交性

**P1-8 幸存者偏差（接近 P0）**
- `src/gugu/analysis/optimization_pools.py:24-84,87-130`：股票池全为当前存活白马/龙头，无退市股；引擎无退市强制平仓，标的退市后持仓悬空、损失不落地
- 修复：引入退市股样本 + 退市强制平仓逻辑

**P1-9 TrailingStop 回测日期用真实 now，3 次跌破浪谷清仓失效**
- `src/gugu/analysis/trailing_stop.py:455`（`today = pd.Timestamp.now()`）、`:317,206`；调用点 `engine.py:331-333` 未传回测日期
- 后果：`last_break_date` 首次跌破后被设为真实今天，之后所有历史 K 线跌破因 `last_break_date == today` 恒成立不再计数，`valley_break_count` 最多到 1，3 次清仓永不触发
- 修复：引擎把历史 bar 日期传入 `update/evaluate_signal`

**P1-10 Sortino 下行偏差公式错误**
- `src/gugu/backtest/metrics.py:219-222`：`downside = returns[returns < 0]; downside_std = downside.std()` 只取负收益子集
- 正确应为全样本 `sqrt(mean(min(r - rf, 0)^2))`
- 修复：修正公式

**P1-11 T+1 加仓当天可卖出**
- `src/gugu/backtest/engine.py:562,543`：`buy_date` 仅在首次买入时赋值，加仓股当日仍按旧 `buy_date` 放行卖出
- 修复：加仓时更新 `buy_date`

### 策略逻辑

**P1-12 `is_yizi` 涨停价计算错误，一字板严重误判**
- `src/gugu/strategies/fanbao/rules.py:114-119`：`limit_price_col = close.shift(1) * 1.095`，主板涨停价应为 `round(prev_close*1.10, 2)`，再叠加 `*0.99` 容差 → 阈值约 `prev_close*1.084`。普通涨停（开 +8.4% 且全天不破）会被误判为一字板
- 修复：改用 `round(prev_close*1.10, 2)`，容差改为固定分值（如 -0.01 元）

**P1-13 暴力洗盘战法永不触发（死代码）**
- `src/gugu/strategies/fanbao/rules.py:1007` 要求 `is_hot_topic`，但 `detect_all`（`:1085`）与 `fanbao_strategy.py:122` 默认传 `False` → 该战法默认永远 return None
- 修复：明确 is_hot_topic 的数据来源与默认值语义

**P1-14 huamulan 三策略 has_entry 永不重置 → 虚假卖出信号**
- `src/gugu/strategies/huamulan_crash_layout.py:152,163`、`huamulan_fake_dip.py:108-127`、`huamulan_tail_pick.py:184-198`：`has_entry = buy_cond.cumsum()>0` 一旦买入永久为 True，`signal_low` 用 `ffill()` 永不回退，首笔平仓后仍发 `-1` 信号
- 修复：引入持仓状态机，平仓后重置

**P1-15 rebound_system 与 rebound/rules.py 规则漂移**
- `src/gugu/strategies/rebound_system.py:244-259` 首板判定仍用旧口径；`:328-331` 反包用 `close > first_high`，而 `rebound/rules.py:557` 已改为 `close > high[wash_i]`。同一次回测两套逻辑结论打架
- 修复：统一到 `rebound/rules.py` 单一事实来源

**P1-16 `use_gate_open=False` 参数无效**
- `src/gugu/strategies/fanbao_strategy.py:121,165`：所有 detector 内部都强制 `_check_today_gap`（高开>0），该开关关闭时也无法在"断板日"发信号，参数语义落空
- 修复：让 detector 的高开检查受参数控制

---

## 四、P2 级问题（优化空间）

### 双重移位 / 逻辑冗余

| 位置 | 问题 |
|------|------|
| `asset_combo.py:183` | `position_percentile` 内部已 `shift(1)`，此处再 `.shift(1)` → 双重移位 |
| `breakout.py:166` | DualThrust confidence 用 `range_val.shift(1)`（range_val 已 shift 过）→ 双重移位 |
| `cloud_trend.py:85-86` | `hh/ll` 未 `shift(1)`，趋势维度含当日 high/low |
| `first_board.py:162,191` | 强制要求 `amount` 列但从未使用；`prev_amount` 死代码 |

### 计算 / 常量问题

| 位置 | 问题 |
|------|------|
| `fanbao/rules.py:53-55` | `SCORE_MV_WEIGHT` 等三权重常量未使用，`calc_score` 硬编码 40/40/20，注释误导 |
| `fanbao/rules.py:706` | `detect_yizi_fanbao` reason 用 `last_yizi_idx`（索引）当"前N日"，应为 `days_since_yizi` |
| `fanbao/rules.py:134` | `_check_today_gap` 阈值 `>0` 过敏感；`TIAOKONG_GAP_MIN`/`LOOKBACK_DAYS` 等常量未使用 |
| `asset_combo.py:164-167` | `count_limit_ups(min_periods=250)` 使前 250 行全 NaN，活跃度过滤近乎失效 |
| `validation.py:93-97` | `excess_annual` 用 `(1+(s-b))**(1/years)`，应为 `(1+s)/(1+b)` 比值年化 |
| `validation.py:162-164` | 参数扰动默认 `perturbation_pcts` 不含 0，`pct==0` 分支永远走 fallback |
| `metrics.py:116-117` | 年化用 `len(equity)` 当交易日数，停牌/跳空数据会扭曲 |
| `metrics.py:122-123` | 胜率/盈亏比不计未平仓仓位 |

### 死代码 / 失效逻辑

| 位置 | 问题 |
|------|------|
| `validation.py:197-199` | `calc_liquidity_stress_test` 用 `trade.get(...)`，但引擎产出的是 `Trade` dataclass（无 `.get`），且 `calc_*` 四个函数全项目无调用点 |
| `analysis/money_management.py` | `KellyCalculator`/`pyramiding_add` 未接入任何下单路径（死代码） |
| `fanbao/rules.py:658-660,756-758,593-596` | "今日放量" `vol_ok` 计算后从不参与判定（仅写入 meta） |
| `fanbao/rules.py:460-462` | 跌停板战法 `day_before` 只做边界检查，后续完全未用，缺"涨停→断板→跌停"结构校验 |

### 数据层 / 质量

| 位置 | 问题 |
|------|------|
| `data/manager.py:398` + `quality.py:528` | `sanitize_dataframe` 把 NaN 填 0 后，`RequiredColumnsRule` 的 `null_count` 恒为 0、`ffill/bfill` 永不触发；open/high/low/volume 的 NaN 变 0 后无规则清理 |
| `quality.py:161-162,186-187` | `PlausiblePriceRule` 用全区间而非 60 日，长历史下"数据源封锁返回静态数据"检测基本失效 |
| `cache.py:59-63` | `>=` 触发且一次淘汰 10%，缓存上限在 90%~100% 间抖动 |
| `fanbao_strategy.py:67` | 硬编码 `data/parquet_cache/hs300_500d.parquet`，缺失时情绪周期静默退化 unknown，无告警 |

### 测试质量

| 位置 | 问题 |
|------|------|
| `tests/unit/test_fanbao_strategy.py:450-467` | `if open_pct > 0: assert ...`，open_pct<=0 时无有效断言静默通过（假绿） |
| `test_fanbao_strategy.py` 整体 | 仅覆盖形态检测与 signal 列存在性，未断言信号位置/置信度正确性 |

### 配置 / 架构一致性

| 位置 | 问题 |
|------|------|
| `strategy_manifest.yaml:943` | `industry_capital_momentum` 用 `failed`，其余 20+ 处用 `backtest_failed`，枚举不统一 |
| `strategy_consistency.py:132-138` | param_stability 阈值 0.80，但 settings.yaml:57 注释写"≥50%" |
| `registry.py:214-218` | `all_configured` 只并 experimental，未并 verified_strategies，verified 策略不在 enabled_by_regime 会被误报死代码 |
| `risk/manager.py:218-223` | 安全上限只做 min，不校验 warn≥halt 或负值 |
| `strategy_pool.py:139-171` | `MIN_WEIGHT(0.05)*count>1` 时 `remaining` 为负；`_init_weights` 分母只算 enabled |
| `signal_router.py:217-218` | weighted 融合把卖出投票者混入买入信号，`resonance` 虚高、consensus_ratio 恒为 1 |
| `risk/manager.py:272-278` | `reload()` 单向同步（只 set True 不 set False），A 进程 reset 后 B 进程仍 halted |
| `risk/manager.py:664-669` | 组合回撤 peak 未初始化时静默放行 ALLOW，而非 fail-safe 初始化 |
| `weekly_combo.py:170-192` | `resample("W")` 默认周日标签，周五用上周数据，滞后约一周 |

---

## 五、优化路线图（建议分阶段执行）

### 阶段一（立即，P0 修复，1-2 天）

1. 修复回测引擎熔断死代码（P0-1）
2. 清理 settings.yaml 中 death_valley_rebound / bollinger_oversold_rebound 三处残留（P0-2）
3. 降级 chenxuezhang（P0-3），double_yin 移出权重或降权重，补 rsi_reversal 回测记录
4. 修复 fanbao V2.0 前视 + 回测架构不兼容（P0-4），使 fanbao 能进入回测

### 阶段二（短期，P1 修复，3-5 天）

5. 风控一致性：ST/停牌拦截进铁律层 + Web 下单补传 + 仓位 fallback 修复（P1-1~P1-5）
6. 回测正确性：加仓上限、open 成交价、幸存者偏差、TrailingStop 日期、Sortino 公式（P1-6~P1-11）
7. 策略逻辑：is_yizi、暴力洗盘死代码、huamulan 状态机、rebound 规则统一（P1-12~P1-16）

### 阶段三（中期，P2 优化，持续）

8. 双重移位、死代码清理、常量统一、测试补强（假绿测试）
9. 配置枚举统一、registry 自检补全、权重归一化边界

### 阶段四（长期，架构级）

10. fanbao 逐日信号生成架构重构（支撑日内战法的正确回测）
11. 退市股样本库建设（消除幸存者偏差）
12. 启用策略的"滚动样本外验证"常态化（每月自动跑，负夏普自动降级）

---

## 附：验证记录

- P0-1 已人工复核 `engine.py:588-593` 确认死代码
- P0-2 已人工复核 settings.yaml 三处 + manifest 降级状态确认漂移
- P0-3 已人工复核 manifest double_yin(-0.02)/chenxuezhang(-0.24)/rsi_reversal(无回测字段) 确认
- P0-4 已人工复核 rules.py 前视调用点 + fanbao_strategy.py 只在 last 写 signal 确认
- P1-1/P1-2 已人工复核 risk/manager.py check_order 与 order_manager.py fallback 确认

（其余项来自 4 个专家 agent 的交叉审查，建议实施时逐项复核文件:行号。）
