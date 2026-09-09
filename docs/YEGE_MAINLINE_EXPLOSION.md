# 野哥主线爆发战法

## 定位

`yege_mainline_explosion` 是研究版主线识别策略，用于识别“消息驱动的早盘首板潮 + 板块指数带量突破 + 容量中军 + 老龙异动”共振后的核心/空间龙候选。

它不是单股形态策略，也不是把任何涨幅较大的股票称为龙头。买入必须依赖结构化事件证据；消息、板块、首板或老龙数据缺失时，策略 fail-closed，仅允许观察。

## 五关规则

1. **消息级别**：消息级别、来源、影响分数和时间必须可追溯。默认要求 `high` 级别。
2. **早盘首板潮**：开盘后 3–60 分钟内，同一板块至少 3 家、最多 5 家首板集中爆发。
3. **板块确认**：板块指数创观察窗口新高，且量比达到阈值。不能用单只股票上涨替代板块突破。
4. **容量中军**：板块内至少有一只百亿市值以上中军，五百亿以上作为更强确认；中军负反馈时事件失败。
5. **老龙异动和核心身份**：上一周期老龙异动证据可用，候选股必须是 `space_dragon`、`core` 或 `leader_candidate`。

## 执行与验证

- 事件发生日为 T，实际入场假设固定为 `next_session_open`，不能用 T 日收盘直接假设成交。
- T+1 验证保存次日开盘/最高/最低/收盘、开盘溢价、收盘溢价、首板活口留存率和中军负反馈。
- 事件研究、连续持仓回测和 paper 前向验证分开统计；不能把固定持有收益当作闭仓交易收益。
- 当前 `strategy_manifest.yaml` 状态为 `pending`，因为标准历史数据契约尚未统一提供板块、早盘、真实市值、老龙生命周期和活口字段。
- 当前不加入 `verified_strategies`，不切换 `live`。即使未来 paper 有成交，也需要完整 GatePolicy 和人工复核。

## 代码入口

- 规则检测：`src/gugu/analysis/yege_battle.py`
- 个股候选策略：`src/gugu/strategies/yege_mainline_explosion.py`
- 离线事件研究汇总：`scripts/yege_event_study.py`
- 事件测试：`tests/unit/test_yege_battle.py`
- 策略测试：`tests/unit/test_yege_mainline_explosion.py`

## 当前证据边界

已有的 `DragonHeadDetector`、`MarketEnvironment` 和情绪模块仍可能输出启发式或聚合证据。它们不能替代真实的全市场板块涨停历史、板块指数历史成交量、真实市值、老龙生命周期和首板活口队列。缺失时必须保留 `insufficient_evidence`/`unavailable` 状态，不将缺失解释为通过。
