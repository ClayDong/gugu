# 主力资金跟随策略设计文档

> 基于散户"跟着主力进、跟着主力出"的操作说明书，落地为 gugu 可回测、可生产的两只 Strategy。
>
> **实现状态：已完成 ✅**（2026-07-17）
>
> - 两只策略已落地：`NorthForceLongStrategy`、`DragonTigerShortStrategy`。
> - 单元测试 13 个全部通过，全量测试 `1620 passed, 5 skipped`。
> - **v0.6.2 诊断后两只策略均已禁用**：`north_force_long` 6 蓝筹 × 200 天平均收益 -1.70%、胜率 0%；`dragon_tiger_short` 10 高波动股 × 200 天平均收益 -0.94%、信号稀少。二者均从 `settings.yaml` 的 `enabled_by_regime` 中移除，`strategy_manifest.yaml` 状态为 `backtest_failed`。
> - 策略层纯 OHLCV；另类 Alpha（龙虎榜/筹码/高管增减持）由 `SignalPipeline` 的 `AltAlphaFilter`/`HolderConcentrationFilter` 进行二次过滤。
> - 周日 03:00 Walk-Forward 自动调参仍会继续运行，但解锁条件通过前不会写回生产参数。

## 一、背景与目标

用户希望把一套从选股到买卖、仓位、风控的"主力跟随"流程，转化为可在 gugu 中自动运行的策略。核心要求：

1. 不猜主力成本，只用公开数据（龙虎榜、筹码、量价、宏观北向）判断大资金方向。
2. 右侧跟随：看到建仓/吸筹结果再跟进；看到趋势破坏再出场。
3. 用仓位 + 止损/止盈控制风险。
4. 做两只策略：一只长线、一只短线。
5. 后期支持自动调参/调优（本期预留接口，不实现完整闭环）。

## 二、关键约束与现实

- **北向个股实时数据已断**：2024年5月起港交所不再实时披露沪深股通个股数据，gugu 仅有全市场北向净流入（宏观层面）。
- **龙虎榜有滞后性**：收盘后公布，次日才能跟进；需连续多日+位置过滤。
- **筹码分布是估算值**：基于换手率模型，不能作为唯一证据。
- **现有能力复用**：gugu 已有 `AltAlphaFactorEngine`（龙虎榜/筹码/高管增持/融资情绪等）、`MoneyFlowFilter`（个股资金流过滤）、`HolderConcentrationFilter`（筹码过滤）、`StockRanker`（多维度评分）。

## 三、方案选择：A2（价量 + 另类 Alpha 因子）

### 3.1 选中方案（A2 落地形式）

- **策略层纯 OHLCV**：`NorthForceLongStrategy` / `DragonTigerShortStrategy` 内部只读取 K 线价量，保证回测可复现、无未来函数、数据覆盖率 100%。
- **另类 Alpha 二次过滤**：生产环境中由 `SignalPipeline` 的 `AltAlphaFilter`（高管增持/龙虎榜机构净买/融资情绪）和 `HolderConcentrationFilter`（筹码集中度）对策略产生的买入信号进行二次确认；数据缺失时默认放行，不阻塞信号。
- **实时资金流过滤**保留在 `SignalPipeline` 的 `MoneyFlowFilter`，策略层不重复。
- 这种分层设计让策略本身聚焦于**可回测的主力行为形态**，把**低覆盖、易延迟的另类数据**放在过滤层，避免策略在回测中因数据缺失产生零信号或前视偏差。

### 3.2 为什么不是 A1/A3

| 方案 | 结论 | 原因 |
|------|------|------|
| A1 纯价量 | 不选 | 没有真正用到"主力资金"数据，偏离用户需求 |
| A2 价量+另类因子 | **选中** | 真正用龙虎榜/筹码/高管增持，可缓存、可回测，与现有 pipeline 衔接 |
| A3 价量+实时资金流 | 不选 | 破坏回测一致性，且 MoneyFlowFilter 已覆盖 |

## 四、策略设计

### 4.1 长线策略：`NorthForceLongStrategy`

**核心逻辑**：模拟"北向+公募"类长线主力的建仓/持仓行为。策略层只使用 OHLCV 识别"趋势向上 + 缩量回踩 MA20 + 近期有资金关注"的形态；筹码/高管增减持等另类 Alpha 由 `SignalPipeline` 二次过滤补充。

**买入条件（硬 AND）**：

| # | 条件 | 说明 |
|---|------|------|
| 1 | 周线/日线趋势向上 | MA20 > MA60 且 MA20 斜率 > 0 |
| 2 | 股价回调至支撑位企稳 | 收盘价在 MA20 上方，且距离 ≤ `pullback_to_ma_pct` |
| 3 | 不追涨 | 当日涨幅 ≤ `max_daily_gain_pct` |
| 4 | 近期有资金关注 | 20 日涨幅 ≥ `recent_momentum_pct`，或近 `recent_spike_window` 日有单日大涨 |
| 5 | 非历史天量 | 当日量比 ≤ `max_vol_ratio` |

**卖出条件（任一，且仅在持仓窗口内生效）**：

| # | 条件 | 说明 |
|---|------|------|
| 1 | 跌破入场 low × (1 - `stop_loss_pct`) | 入场低点保护 |
| 2 | 连续 2 日收盘跌破 MA20 | 趋势破坏（避免单日噪音） |
| 3 | MA20 下穿 MA60 | 中期趋势反转 |
| 4 | 高位放量滞涨 | 量 > `stagnation_vol_ratio` 倍均量，涨幅极小且上影线长 |
| 5 | 时间止损 | 持仓 ≥ `time_stop_window` 日且收益 < `time_stop_threshold` |
| 6 | 动态止盈 | 单日涨幅 ≥ `take_profit_pct` × 0.6 且阳线 |

**置信度评分**：基于回踩距离、20 日动量、MA 斜率、量能健康度、是否阳线，上限 0.85。

**生产增强**：买入信号会再经过 `HolderConcentrationFilter`（筹码不散户化）和 `AltAlphaFilter`（高管增持/融资情绪等）确认；数据缺失时放行。

**类别**：`main_force`

### 4.2 短线策略：`DragonTigerShortStrategy`

**核心逻辑**：模拟"机构+游资"波段/短线行为。策略层使用 OHLCV 识别"近 5 日涨停 → 断板调整 → 1-3 日内阳线反包断板日最高价"的形态；龙虎榜机构净买入等另类 Alpha 由 `SignalPipeline` 二次过滤补充。

**买入条件（硬 AND）**：

| # | 条件 | 说明 |
|---|------|------|
| 1 | 近 `limit_up_lookback` 日有涨停板 | 涨幅 ≥ `limit_up_pct`（默认 9.5%，留容差） |
| 2 | 断板后反包 | 当日收盘价 > 断板日最高价，且为阳线 |
| 3 | MA20 向上 | 中期趋势支撑，收盘价 > MA20 |
| 4 | 非历史天量 | 当日量比 ≤ `max_vol_ratio` |

**卖出条件（任一，且仅在持仓窗口内生效）**：

| # | 条件 | 说明 |
|---|------|------|
| 1 | 跌破入场点 low × (1 - `stop_loss_pct`) | 反包失效 |
| 2 | 跌破 MA20 收盘 | 趋势破坏 |
| 3 | 高位放量滞涨 | 量 > `stagnation_vol_ratio` 倍均量，涨幅极小 |
| 4 | 时间止损 | 持仓 ≥ `time_stop_window` 日且收益 < `time_stop_threshold` |
| 5 | 动态止盈 | 单日涨幅 ≥ `take_profit_pct` × 0.6 且阳线 |

**置信度评分**：基于断板日缩量程度、反包量能、反包力度（是否突破涨停日高点）、MA 斜率、时效性、是否阳线，上限 0.90。

**生产增强**：买入信号会再经过 `AltAlphaFilter`（龙虎榜机构净买入 `lhb_inst_net` 等）确认；数据缺失时放行。

**类别**：`main_force`

## 五、数据依赖与获取方式

| 数据 | 来源 | 已有能力 |
|------|------|----------|
| 龙虎榜机构净买入 | `AltAlphaFactorEngine.lhb_inst_net` | ✅ DragonTigerCollector |
| 筹码集中度 | `AltAlphaFactorEngine.holder_concentrate` | ✅ HolderStructureCollector |
| 高管增减持 | `AltAlphaFactorEngine.insider_buy` | ✅ InsiderTradingCollector |
| 融资情绪 | `AltAlphaFactorEngine.margin_sentiment` | ✅ MarginTradeCollector |
| 个股资金流 | `SignalPipeline.MoneyFlowFilter` | ✅ DataManager.fetch_stock_flow |
| K 线 | `DataManager.fetch_stock_history` | ✅ 三级冗余 |

## 六、集成点

### 6.1 新增文件

- `src/gugu/strategies/north_force_long.py` — 长线策略
- `src/gugu/strategies/dragon_tiger_short.py` — 短线策略
- `tests/unit/test_north_force_long.py` — 长线策略单元测试
- `tests/unit/test_dragon_tiger_short.py` — 短线策略单元测试

### 6.2 修改文件

- `src/gugu/strategies/registry.py` — 注册两只新策略
- `src/gugu/strategies/__init__.py` — 导出（如需要）
- `config/strategy_defaults.yaml` — 添加默认参数
- `config/strategy_manifest.yaml` — 添加策略元数据（默认 enabled: false，待回测验证后开启）
- `docs/TECH.md` — 补充策略说明

### 6.3 不修改的部分

- `Strategy` 基类保持同步；
- `SignalPipeline` 过滤链不变；
- `MarketFilter`/`RegimeDetector` 继续负责熊市拦截。

## 七、回测与验证计划

1. **单元测试**：覆盖买入/卖出条件的边界场景，因子缺失时的降级行为。
2. **单股回测**：选 5-10 只代表性股票（大消费、医药、科技），跑 500 日历史数据。
3. **多股聚合回测**：用 `scripts/backtest_all_strategies.py` 跑 50-100 只股票，评估 4 项解锁条件：
   - 样本外 Sharpe > 0
   - 参数稳定性 ≥ 50%
   - 最大回撤 < 25%
   - 收益 ≥ 0
4. **分阶段回测**：牛/熊/震荡市分别验证，确认策略在对应 regime 下的表现。
5. ** paper 验证**：通过后在 `track_live` 或 `paper` 模式下运行 30+ 信号，再决定是否启用。

## 八、自动调参/调优

已实现接入 `scripts/optimize_params.py` 的 Walk-Forward 自动调参框架：

- 两只策略的参数网格、配置键、约束函数已写入 `scripts/optimize_params.py`；
- 关键可调参数：均线周期、量能阈值、止损止盈比例、`min_confidence` 等；
- 调度器每周日 03:00 自动运行 `_safe_optimize_params()`，对 6 只行业代表股进行多股聚合 Walk-Forward；
- 调优目标：样本外 Sharpe > 0、参数稳定性 ≥ 50%、最大回撤 < 25%、收益 ≥ 0；
- 通过 4 项解锁条件后自动写回 `config/strategy_defaults.yaml` 并更新 `strategy_manifest.yaml` 的 validation 字段；未通过时不污染生产参数。

当前状态：
- `north_force_long`：v0.6.2 诊断 6 蓝筹 × 200 天平均收益 -1.70%、夏普 -2.49、胜率 0%，已从 `enabled_by_regime` 移除，状态 `backtest_failed`。
- `dragon_tiger_short`：v0.6.2 诊断 10 高波动股 × 200 天平均收益 -0.94%、夏普 -0.44、信号稀少（10 股仅 7 个买入信号），300454 单股拖累 -8.87%，已从 `bull/rally` 启用列表移除，状态 `backtest_failed`。
- 两只策略均不再进入生产信号融合；周日 03:00 Walk-Forward 自动调参继续运行，但 4 项解锁条件通过前不会写回参数。

## 九、风险与注意事项

1. **龙虎榜数据覆盖率低**：很多股票不会上榜，短线策略可能长期无信号。
2. **另类因子更新频率**：股东户数是季度数据，高管增减持是事件数据，存在空窗期。
3. **策略默认禁用**：新策略回测验证通过前，manifest 中 enabled 设为 false，不影响生产。
4. **避免过度拟合**：参数调整必须在样本外验证，不可只看训练集表现。

## 十、任务规划（已完成）

| # | 任务 | 优先级 | 状态 | 产出 |
|---|------|--------|------|------|
| 1 | 实现 `NorthForceLongStrategy` | P0 | ✅ | `src/gugu/strategies/north_force_long.py` + `tests/unit/test_north_force_long.py` |
| 2 | 实现 `DragonTigerShortStrategy` | P0 | ✅ | `src/gugu/strategies/dragon_tiger_short.py` + `tests/unit/test_dragon_tiger_short.py` |
| 3 | 注册策略、配置 defaults/manifest | P0 | ✅ | `registry.py` / `strategy_defaults.yaml` / `strategy_manifest.yaml` 已同步 |
| 4 | 运行单股/多股回测 | P0 | ✅ | 58 股 × 300 天回测报告已写入 `config/strategy_manifest.yaml` validation_gate |
| 5 | 根据回测结果调整参数 | P1 | ✅ | v0.6.2 诊断后两只策略均禁用；`optimize_params.py` 网格已放宽，周日 Walk-Forward 持续调优 |
| 6 | 更新 TECH.md 与 manifest | P1 | ✅ | `docs/TECH.md` / `docs/PRODUCT.md` / 本设计文档已同步 |
| 7 | 重启调度器验证任务注册 | P1 | ✅ | 调度器已运行（PID 86620），25 个定时任务已注册 |
| 8 | 接入 Walk-Forward 自动调参 | P2 | ✅ | `scripts/optimize_params.py` 已添加参数网格与约束函数 |
