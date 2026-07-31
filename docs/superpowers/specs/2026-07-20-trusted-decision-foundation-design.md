# 可信决策底座设计

日期：2026-07-20
状态：待用户审阅

## 1. 目标

将 gugu 从“信号、评分、通知并行输出”收敛为一条可验证的决策链，第一阶段不启用自动下单，优先解决：

1. 订单成交被错误当作盈利反馈；
2. 数据缺失、过期或评分异常时仍可能生成买入建议；
3. 十大视角的展示结果与实际门禁行为不一致；
4. 不同模块对胜率、收益和通过状态的定义不一致；
5. 通知无法清楚区分可执行建议、观察信号和阻断原因。

## 2. 非目标

本阶段不实现：

- 自动实盘下单；
- 完整交易账本和复杂持仓生命周期；
- 用户画像到仓位的完整个性化；
- 十大视角的统计独立性证明；
- 全市场、多周期、生产等价回测重构。

这些能力将在可信底座稳定后分阶段实现。

## 3. 核心设计

### 3.1 统一决策合同

在现有信号模型基础上增加统一决策字段，避免各模块维护不同的通过和结果语义。

最小字段：

- `decision_id`：一次决策的稳定标识；
- `decision_type`：`entry`、`exit`、`observe`；
- `action`：`buy`、`sell`、`hold`、`observe`、`blocked`；
- `status`：`candidate`、`blocked`、`approved`、`notified`、`executed`、`expired`、`closed`、`evaluated`；
- `data_quality`：`fresh`、`stale`、`partial`、`invalid`、`unavailable`；
- `data_timestamp`、`data_sources`；
- `block_reasons`；
- `perspective_result`；
- `entry_condition`、`invalidation_condition`、`expires_at`；
- `execution_status`；
- `outcome_status`、`outcome_metrics`。

现有字段继续兼容读取，但新流程只使用合同中的统一状态判断。

### 3.2 数据质量保守阻断

交易决策与研究展示分离：

- `fresh`：允许进入买入门禁；
- `stale`：允许研究和通知，但禁止买入；
- `partial`：核心字段缺失时禁止买入；
- `invalid`：禁止进入决策链；
- `unavailable`：禁止进入决策链。

不再用默认值 0 表示未知数据。若底层暂时需要兼容数值计算，必须同时保留字段缺失状态，并由决策门禁阻断。

### 3.3 十大视角强制门禁

所有买入候选统一经过同一门禁：

```text
composite_score >= 7.0
minimum_score >= 4.0
passed_count >= 7
no_fatal_perspective
no_perspective_error
no_core_data_missing
```

任一条件不满足：

```text
action = observe
status = blocked
```

评分器异常必须保留 `error` 状态，不能替换成普通 5 分继续聚合。没有视角结果的信号不能默认通过。

`overall_verdict` 与 `passed` 必须调用同一个判定结果，禁止出现“整体推荐买入但 passed=False”。

### 3.4 结果指标分层

统一使用明确命名的指标：

- `execution_success_rate`：订单成交成功率；
- `forward_5d_signal_hit_rate`：信号发出后五个交易日方向命中率；
- `closed_trade_win_rate`：有完整平仓结果的交易盈利率；
- `realized_pnl`：实际已实现盈亏；
- `estimated_signal_pnl`：基于假设仓位的估算收益。

订单成功不再作为盈利标签。没有平仓结果的信号只能进入前向收益追踪，不能进入 `closed_trade_win_rate` 或 Wisdom 历史盈利反馈。

### 3.5 通知语义

通知顶部优先展示：

1. 当前动作；
2. 当前状态；
3. 是否允许交易；
4. 阻断原因或通过依据；
5. 数据质量和时间；
6. 有效期、触发条件和失效条件。

被阻断的候选信号仍可通知，但必须使用“观察信号”，不得显示为“推荐买入”。详细视角解释放在后续区域。

## 4. 影响范围

重点修改：

- 信号/决策模型；
- 视角聚合与集成器；
- 信号流水线；
- Wisdom 历史结果判定；
- 归因与绩效字段；
- 通知格式化；
- 相关单元测试与端到端测试。

文档同步修改：

- `docs/PRODUCT.md`：明确当前默认是跟踪/通知，不是自动买卖；
- `docs/TECH.md`：明确视角门禁、数据状态和指标定义；
- `docs/ARCHITECTURE_DECISIONS.md`：记录统一决策合同和保守阻断原则。

## 5. 测试验收标准

必须覆盖：

1. 订单成交但后续亏损，不会被计为盈利反馈；
2. 没有平仓结果，不会进入交易胜率；
3. 核心数据缺失、过期或无效时，买入被阻断；
4. 视角评分异常时，买入被阻断；
5. 视角数量不足、最低分不足或综合分不足时，买入被阻断；
6. 无视角结果的信号不会默认通过；
7. `overall_verdict` 与 `passed` 永远一致；
8. 所有运行模式使用同一门禁结果；
9. 被阻断通知明确显示观察状态和阻断原因；
10. 旧历史数据仍能读取，但不会触发旧的错误盈利标签。

## 6. 风险与回滚

主要风险是门禁收紧后信号数量下降，以及旧数据字段不完整导致部分历史记录只能进入观察状态。通过保留现有字段兼容读取、只在新的买入决策路径启用严格状态，并以测试固定行为，确保可以按模块回滚。

本阶段完成后，系统才具备继续实现用户画像、持仓决策和执行反馈闭环的可信基础。
