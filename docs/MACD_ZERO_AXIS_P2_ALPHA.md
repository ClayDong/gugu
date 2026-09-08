# MACD 零轴上方策略，P2-Alpha 观察说明

## 定位

`macd_zero_axis` 是基于用户提供的六句 MACD 口诀实现的实验策略，不是已验证策略，不进入 `verified_strategies`，只在 paper 模式观察。

## 信号规则

买入需要同时满足：

1. `DIF > 0` 且 `DEA > 0`；
2. 最近确认窗口内出现 DIF 上穿 DEA；
3. 股价回踩 MA20 或近 5 日低点未破；
4. 数据完整、市场环境门禁通过。

退出规则：

- 趋势破坏或顶背离证据，在持仓期间产生退出信号；
- 移动止损默认 8%；
- 不把顶背离直接伪装成“减仓 50%”成交，当前统一按 paper 全平信号验证。

## 验证边界

- 事件研究必须使用下一交易日可执行入场，并扣除成本；
- 连续持仓回测以闭合 round-trip 为样本；
- 事件研究超额收益不能直接替代连续持仓收益；
- `GatePolicy` 未通过前，不能进入 verified，也不能用于 live。

## P2-Alpha 观察目标

- 观察至少 30 个可执行 paper 信号；
- 记录 `paper_execution_ledger.jsonl` 的成交、闭仓、realized PnL；
- 按 bull/rally/sideways/bear 分层；
- 观察顶背离退出是否减少回撤，而非只看胜率；
- 连续观察至少两周后再做 Gate 复验。
