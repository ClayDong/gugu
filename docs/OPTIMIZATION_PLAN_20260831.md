# gugu 优化计划（2026-08-31 评审产出）

> 来源：2026-08-31 第一性 + 对抗性多专家评审（量化/数据/工程/安全/新手产品/测试六视角）。
> 定位锚点：**辅助炒股新手进行决策**——新手缺的不是更多信号，而是"何时不动 / 亏了怎么办 / 为什么 / 防止毁灭性错误"。
> 状态标记：✅ 已完成 · 🔄 进行中 · ⬜ 待办

---

## P0（本周，小改动高杠杆）

| # | 事项 | 状态 | 说明 |
|---|------|------|------|
| P0-1 | 提交存量清理（拆分 commit，不 push） | ✅ | 代码/配置/测试：`e6dfe9a`；文档与本计划：`4048ed3`。130 文件/-15726 行；本轮最终回归 2070 passed / 152 skipped |
| P0-2 | Web 安全默认值加固 | ✅ | `run_web.py --host` 默认 `127.0.0.1`；`WebConfig.require_auth_for_sensitive` 默认 True；无管理 key 时敏感接口 503 禁用；API Key 使用 `secrets.compare_digest`；新增回归测试 |
| P0-3 | 统一 bear 语义 | ✅ | settings.yaml 与 PRODUCT.md 明确区分信号策略（bear/crash 不买入）和选股器（bear 候选×0.4，仅观察） |

## P1（1-2 月，聚焦新手价值）

| # | 事项 | 状态 | 说明 |
|---|------|------|------|
| P1-1 | 通知分级制（Action/Digest/Archive 三档） | ✅ | `NotificationTier` + 语义去重键 + `notification_archive.jsonl`；普通日报/选股/观察/持仓报告进入持久化 Digest，09:12/15:40 flush；Action 信号/风控/止损即时发送，失败保留重试队列 |
| P1-2 | 卡片新手化 | ✅ | 买入卡显示按计划数量/入场价/止损价估算的最大亏损与账户占比；缺失数据明确“暂无法估算”；绩效兼容 `sample_count/total_signals` 并显示 `n=`；验证等级未知时明确仅供观察 |
| P1-3 | 增强过滤器只降不升 | ✅ | 移除 3 处买入侧加成（signal_only 另类因子、筹码潜伏者、生产路径另类因子）；量价背离卖出加成保留，因为它只强化退出/避险方向 |
| P1-4 | 研究账本最小版 | ✅ | 独立 `src/gugu/research/ledger.py` 使用原子/加锁/严格 JSON；已接入单股、在线批量、离线批量、离线 Walk-Forward 和三类优化入口，记录数据范围、参数空间、Gate verdict 与写回状态 |
| P1-5 | 保命模块分支覆盖 | ✅ | 新增 `test_safety_invariants.py`，覆盖 RiskManager 安全上限、熔断恢复/隔离；同时补强 track_live、数据质量、registry、实时执行路径回归 |

## P2（有空再做）

| # | 事项 | 状态 | 说明 |
|---|------|------|------|
| P2-1 | 拆 12 个 500+ 行大文件 | ⬜ | signal_pipeline.py(1382) 优先，按过滤链步骤拆 |
| P2-2 | 事件引擎实装或删除 | ⬜ | 当前是死代码 |
| P2-3 | 08:00-08:30 三个晨间任务合并为一张晨报 | ⬜ | 随 P1-1 一起设计 |

## 不要做清单（红线）

- ❌ 不为信号产量调低 min_confidence（v0.7.9 从 0.6→0.4 的反复要警惕）
- ❌ 不在 verified_strategies=[] 时"先切 paper 看看效果"
- ❌ 不给 LLM/wisdom 任何拦截或放行权（保持 advisory）
- ❌ 不为减少禁用数量下调 gate-v1
- ❌ 不在数据源不稳定时扩策略池或切 live
- ❌ 不用 --force 写回生产参数

## 已确认做得好、保持不动的

- track_live 只读 + order() 永远拒绝（engine_mixin.py:432）
- 买入 fail-closed（quality_state.py:17 只有 fresh_valid 可买）
- 信号 shift(1) 防前视（backtest/engine.py:265）+ 多股 WF 真实日期对齐（engine.py:1026+）
- 事件研究 vs 持仓回测口径分离制度
- 熔断持久化 + 硬编码安全上限 min() 封顶
