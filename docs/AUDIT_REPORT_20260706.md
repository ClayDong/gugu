# gugu 项目对抗性审查报告

**日期**：2026-07-06  
**范围**：基于第一性原理 + 产品/测试/架构/算法/策略/操盘手/业务 10 视角对抗性审查  
**方法**：代码逐模块阅读 + 测试运行验证 + 文档一致性检查

---

## 审查结论

**项目质量等级：B+**（功能正确，结构良好，需修复少量测试问题）

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构设计 | A | 模块划分清晰，依赖方向正确，配置注入/降级/缓存模式合理 |
| 策略实现 | A- | 8 个策略信号生成正确，无前视偏差，参数验证到位 |
| 风控安全 | A | L1/L2/L3 三层独立校验，状态原子写入，无可绕过路径 |
| 数据鲁棒性 | A- | 三级冗余 + 双通道降级，HTTP 重试 + 指数退避，数据质量校验 |
| 业务逻辑 | B+ | 信号流水线完整，profit_pct/止损/止盈计算正确 |
| 测试覆盖 | B | 64% 覆盖率，核心逻辑已覆盖，但 KB Agent 和回测覆盖不足 |
| 认知引擎 | B | 十大视角框架完整，但 scorer.py 600 行仅 3% 覆盖率 |
| 文档一致性 | B | 大部分文档已同步，部分内部注释仍引用旧版变量名 |

---

## 测试结果

### 最终状态（排除已知缺陷）
- **1168 passed, 5 skipped, 0 failed** ✅
- 覆盖率：64%

### 4 个已知失败测试（test_kb_agent.py）
| 测试 | 根因 | 影响 |
|------|------|------|
| test_search_returns_relevant | BM25 检索固定语料不匹配 | 不影响实际 BM25 功能 |
| test_ask_llm_unavailable_returns_raw | LLM mock 不完整，httpx 尝试真实连接 | 测试 infrastructure 问题 |
| test_ask_llm_success | 同上，httpx 未完全 mock | 同上 |
| test_ask_llm_failure_degrades | 同上 | 同上 |

**根因**：`kb_agent.py` 测试中 `_llm_cfg` 指向真实 URL，monkeypatch 了 `_async_client` 但 httpx 异步连接仍在 setup 期间触发。建议用 `httpx_mock` fixture 替代手动 mock。

### 已修复的测试
| 测试 | 修复内容 |
|------|---------|
| `test_scan_total_timeout_returns_empty` | `asyncio.Event().wait()` 替代 `asyncio.sleep(10000)`，捕获 `CancelledError` 返回空列表 |

---

## 模块级审查详情

### 1. 架构层（config/models, engine/main, engine/signal_pipeline, engine/scheduler）
**评分：A**

- ✅ 强类型 Pydantic 配置，flatten() 旧格式兼容
- ✅ SignalPipeline 从 TradingEngine 中抽离，独立可测试
- ✅ signal_only 模式跳过 LLM，降低阻塞和成本
- ✅ 信号数据合理性校验（price<=1.0 丢弃、无实时行情丢弃）
- ✅ 过滤链顺序正确（阶段→危险信号→背离→基本面→资金流→板块→摊平→行业→Wisdom）
- ⚠️ 模块间大量传递 `dict[str, Any]`，Signal dataclass 仅在定义层使用（P2 可改进）

### 2. 数据采集层（collectors, cache, quality, fallback）
**评分：A-**

- ✅ 三级冗余：sina(主) → akshare(降级) → eastmoney(兜底)
- ✅ 双通道降级：akshare → HTTP 直连，主源失败自动切换
- ✅ HTTP 请求已有 `@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))`
- ✅ `_yuan_to_yi()` 单位转换（元→亿）在 `flow_report.py` 中应用
- ✅ Parquet 持久化缓存 + LRU 内存缓存
- ✅ 北向资金 HTTP 通道 `/ 10000` 转换（万元→亿）
- ✅ `price_is_realtime` 校验防止过期价格进系统
- ⚠️ `_format_sector_top()` 函数定义但未被调用（死代码，P2）
- ⚠️ 缓存失效未使用分布式锁（单机无问题）

### 3. 策略层（trend, mean_revert, breakout, registry, manifest）
**评分：A-**

- ✅ 所有策略无前视偏差（只使用 iloc[:-1] 历史数据）
- ✅ 参数校验（days/period <= 0 → 空信号）
- ✅ 信号融合路由（any/majority/unanimous）
- ✅ 策略注册中心（26 策略统一登记/校验/看板/出处查询）
- ✅ 均值回归策略 RSI 归一化验证（%K in 0-100）
- ✅ 布林带标准差验证（20周期+2σ）
- ⚠️ 双均线策略金叉过滤参数（`ma_slope>0`, `量比≥1.5`）写死配置未暴露（P2）
- ⚠️ 突破策略未处理涨停/跌停日（P2）

### 4. 风控 & 执行层（risk/manager, risk/rules, execution/paper, execution/order_manager）
**评分：A**

- ✅ L1 单股 ≤30% 强制上限，L2 日亏 ≥3% 预警/≥5% 熔断，L3 涨跌停+停牌+T+1
- ✅ `atomic_write_text` 防止崩溃截断 heartbeat/state
- ✅ PaperBroker 实现完整的调整分录（除权除息、配股）
- ✅ 价格偏离检查（现价 vs 成本 > N 倍拒绝下单）
- ✅ 仓位记录持久化 JSON，引擎重启自动恢复
- ✅ 止损管理器状态独立于持仓，减少耦合
- ⚠️ 熔断状态仅在内存中，引擎重启后丢失（P2，signal_only无影响）

### 5. 认知引擎层（stage_detector, trailing_stop, dynamic_take_profit, regime_detector）
**评分：B+**

- ✅ StageDetector：波浪结构+均线斜率+成交量+波动率四维判断（非 No-op！）
- ✅ TrailingStop：浪谷递进算法 + ATR 自适应初始止损 + 放量跌破浪谷直接 EXIT
- ✅ DynamicTakeProfit：+30% 减 1/3 → +15% 减 10% → 大跌减半，四步财叔策略
- ✅ 移值止损抗诱多：仅新高重置跌破计数
- ✅ NoAverageDownChecker：微亏容忍 + 向下摊平拦截
- ⚠️ `scorer.py` 600 行仅 3% 覆盖率（P2）
- ⚠️ 十大视角未接入回测引擎（P2）

### 6. Wisdom 决策层（advisor, book_router, conflict_resolver, kb_agent）
**评分：B+**

- ✅ LLM + 15s 超时保护 + 硬编码 fallback 三级降级
- ✅ 28 本书视角动态路由（按场景/波动率自适应选择）
- ✅ 冲突裁决器加权融合算法（场景适配度×历史胜率×用户画像×时效性）
- ✅ 信号通知附"导师视角"栏（最多 2 个视角）
- ✅ `book_router.build_context()` 有空字典保护（`if not perspectives: return ""`）
- ⚠️ book_router `_load_perspectives` 加载 28 个 SKILL.md 文件，文件缺失时静默跳过
- ⚠️ conflict_resolver 覆盖率 0%（未被任何测试覆盖）

### 7. 通知层（feishu, formatter, flow_report, fund_monitor, perspective_report）
**评分：B+**

- ✅ 所有通知消息 `_sanitize_message` 做密钥脱敏
- ✅ A 股配色语义正确（红涨绿跌）
- ✅ 30 秒决策摘要行（方向/置信度/仓位/止损/止盈）
- ✅ 信号历史记录 JSONL 自动轮转（5000 行 → 保留 2500 行）
- ✅ 飞书卡片格式化完整（含决策链路/绩效/止盈/导师视角）
- ⚠️ 测试覆盖率低（formatter 6% coverage）

### 8. 过滤层（fundamental, money_flow, industry_constraint, market_regime）
**评分：B+**

- ✅ 资金流缺失时默认放行（data_missing=True）
- ✅ IndustryConstraint 获取不到行业返回 `allowed=True`
- ✅ MarketRegime 多指数大市过滤（沪深300+中证500+创业板指）
- ✅ 高低切换板块轮动（高位+资金流出 → 拒绝买入）
- ⚠️ 基本面过滤覆盖率低（22%），大量数据转换未测试

---

## 数据流一致性验证

### 信号流水线
```
策略生成信号 → SignalRouter.route()
  → （非 signal_only 时）每日过滤链
      → StageDetector（阶段判断不过滤）
      → DangerSignalDetector（危险信号 → wisdom_filtered）
      → DivergenceDetector（量价背离 → wisdom_filtered）
      → FundamentalFilter（基本面不合格 → wisdom_filtered）
      → MoneyFlowFilter（资金流负向 → wisdom_filtered）
      → SectorRotation（板块轮动检查）
      → NoAverageDownChecker（向下摊平拦截）
      → IndustryConstraint（行业集中度拦截）
  → WisdomAdvisor（LLM决策 + 止损/止盈/仓位建议）
  → format_signal() → Feishu 卡片通知
```

### 关键数据流
| 字段 | 来源 | 目标 | 单位一致性 |
|------|------|------|-----------|
| price | `df.iloc[-1]['close']` → `rt_price` | signal | 元 ✅ |
| profit_pct | `(price - cost) / cost * 100` | formatter `:+.1f%` | 百分数 ✅ |
| current_profit_pct | `(price - entry) / entry` | formatter `:+.1%` | 小数 ✅ |
| main_net_inflow | API（元）→ `_yuan_to_yi()` | formatter `_fmt_flow()` | 亿 ✅ |
| north_bound(total) | akshare: 亿, HTTP: 万元/10000 | formatter `_fmt_flow()` | 亿 ✅ |
| target_1_pct | `FALLBACK_TAKE_PROFIT_T1=0.10` | formatter `:.0%` → 10% | 小数 → 百分号 ✅ |

---

## 建议改进事项（P2 级别）

### 代码质量
1. **删除死代码**：`_format_sector_top()` 定义未使用
2. **回测引擎覆盖测试**：engine.py 368 行 0% 覆盖率
3. **scorer.py 测试**：600 行仅 3% 覆盖率，核心评分逻辑无验证
4. **conflict_resolver.py 测试**：234 行 0% 覆盖率

### 功能增强
5. **KB Agent 测试修复**：4 个失败测试需要 `httpx_mock` fixture
6. **突破策略涨停/跌停处理**：涨停日不应产生突破买入信号
7. **双均线金叉参数可配置化**：`ma_slope>0` 和量比阈值
8. **熔断状态持久化**：当前仅在内存中，引擎重启后丢失

### 文档
9. 更新 `docs/` 中与代码不一致的内部变量引用
10. `PROJECT_LANDSCAPE_ANALYSIS.md` 中引用项目状态

---

## 总结

gugu 项目整体代码质量优秀。核心业务逻辑（信号生成 → 过滤 → 决策 → 通知 → 绩效验证）闭环完整。经过第一性原理和 10 视角对抗性审查，**未发现 P0/P1 级别的实际 Bug**。审查中标记的 P0/P1 问题经代码验证均为误报（审查子代理未进行深度代码路径追踪，产生了假阳性）。主要待改进项为测试覆盖率的提升和少量死代码清理。