# 迭代 7：数据质量与通知优化

> 2026-07-02

## 1. 背景

迭代 6 完成了仓颉书籍深度融合和 P0 认知引擎落地，项目达到 506 测试 / 87% 覆盖率。然而，在实际运行过程中发现了两个关键问题：

1. **飞书重复通知**：同一信号在一天内被多次推送（09:30/10:30/13:05/14:30 各一次），原因是去重机制是纯内存的，引擎重启后丢失
2. **价格数据异常**：贵州茅台（600519）飞书通知显示价格 1500.0，与实际市场价格不符，原因是实时行情获取失败时静默回退到历史收盘价，且无任何标记

同时，用户希望宏观日报能够包含大师兄视角的解读，而不仅仅是数据展示。

本次迭代的核心目标：**修复数据质量问题，优化通知机制，增强宏观日报的认知深度**。

## 2. 本次迭代目标

| 目标 | 说明 |
|------|------|
| 信号去重持久化 | 将 `_notified_signals` 字典持久化到文件，引擎重启后自动恢复 |
| 价格数据校验 | 新增 `price_is_realtime` 标记，实时行情获取失败时拒绝发送信号 |
| 大师兄解读 | 宏观日报添加 LLM 驱动的大师兄视角解读 |
| 测试验证 | 806 测试全部通过，70% 覆盖率 |

## 3. 改动范围

### 3.1 修改文件

| 文件 | 修改要点 |
|------|----------|
| `src/gugu/engine/main.py` | 新增 `_notified_signals_file` 属性，实现 `_load_notified_signals()` 和 `_save_notified_signals()` 方法，`_mark_notified()` 持久化到文件 |
| `src/gugu/engine/signal_pipeline.py` | 新增 `price_is_realtime` 标记，实时行情获取失败时拒绝发送信号（L148-153） |
| `src/gugu/macro/scheduler.py` | 在 `_push_macro_report()` 中调用 `WisdomAdvisor` 生成大师兄解读，添加到飞书卡片末尾 |
| `tests/unit/test_signal_pipeline.py` | 新增 `rt_all` fixture 提供模拟实时行情数据，更新所有测试方法传入 `rt_all` 参数 |
| `CLAUDE.md` | 更新核心价值（信号去重、宏观解读）、通知类型（宏观简报含大师兄解读）、元数据（v0.3.3） |
| `docs/TECH.md` | 更新可观测性数据文件（新增 `data/notified_signals.json`）、主业务流转（信号去重机制） |
| `docs/PRODUCT.md` | 更新通知类型（宏观简报含大师兄解读）、迭代记录（迭代 25） |
| `README.md` | 更新核心特性（信号去重机制、宏观简报含大师兄解读） |

### 3.2 新增文件

| 文件 | 说明 |
|------|------|
| `data/notified_signals.json` | 信号去重记录（symbol_direction_strategy → date），引擎重启后自动恢复 |

## 4. 架构演进

### 4.1 信号去重机制

```
迭代 6（纯内存去重）:
  TradingEngine._notified_signals: dict[str, str] = {}
  ↓ 问题：引擎重启后丢失，导致重复通知

迭代 7（持久化去重）:
  TradingEngine._notified_signals_file = PROJECT_ROOT / "data" / "notified_signals.json"
  TradingEngine._notified_signals = self._load_notified_signals()
  
  _mark_notified(signal):
    self._notified_signals[dedup_key] = date.today().isoformat()
    self._save_notified_signals()  # 持久化到文件
  
  _load_notified_signals():
    if self._notified_signals_file.exists():
      data = json.load(self._notified_signals_file)
      return {k: v for k, v in data.items() if v == today_str}  # 清理过期记录
    return {}
```

### 4.2 价格数据校验

```
迭代 6（静默回退）:
  signal["price"] = float(df.iloc[-1]["close"])
  if rt_all is not None and not rt_all.empty:
    try:
      rt_price = float(row.iloc[0]["price"])
      if rt_price > 0:
        signal["price"] = rt_price  # 覆盖历史收盘价
    except Exception:
      pass  # 静默失败，使用历史收盘价
  ↓ 问题：无法区分实时价格和历史价格，可能发送错误数据

迭代 7（显式标记）:
  signal["price"] = float(df.iloc[-1]["close"])
  signal["price_is_realtime"] = False  # 默认标记为非实时
  
  if rt_all is not None and not rt_all.empty:
    try:
      rt_price = float(row.iloc[0]["price"])
      if rt_price > 0:
        signal["price"] = rt_price
        signal["price_is_realtime"] = True  # 标记为实时价格
    except Exception:
      pass
  
  # 严格校验：实时行情获取失败时拒绝发送信号
  if not price_is_realtime:
    logger.warning(f"{symbol} 实时行情获取失败，价格={price}来自历史K线（非实时），丢弃该信号。")
    return None
```

### 4.3 大师兄解读

```
迭代 6（纯数据展示）:
  macro/report.py:
    builder.build_early_card(snapshot)  # 仅展示金/油/汇/债/G 数据
    ↓ 问题：缺乏认知深度，用户无法理解数据背后的意义

迭代 7（LLM 驱动解读）:
  macro/scheduler.py:
    wisdom = WisdomAdvisor()
    summary = builder.build_text_summary(snapshot)
    
    # 调用 LLM 生成大师兄视角的宏观解读
    interpretation = await wisdom._advise_llm_async(
      signal={
        "symbol": "MACRO",
        "direction": "hold",
        "price": 0,
        "confidence": 0,
        "reason": f"宏观数据解读：{summary}",
      },
    )
    
    if interpretation and interpretation.get("reason"):
      # 将解读添加到卡片末尾
      interpretation_md = f"\n\n**📖 大师兄解读**\n{interpretation['reason']}"
      card["elements"].append({"tag": "markdown", "content": interpretation_md})
```

## 5. 关键设计决策

### 5.1 信号去重持久化策略

**决策**：将 `_notified_signals` 字典持久化到 JSON 文件，每次标记后自动保存。

**原因**：
- 引擎可能在两次扫描之间崩溃或被 watchdog 重启
- 纯内存去重机制在重启后丢失，导致重复通知
- 持久化到文件后，引擎重启时自动恢复去重状态

**代价**：
- 每次标记后需要写入文件，增加 IO 开销
- 需要清理过期记录（只保留今日的），避免文件无限增长

**冲突处理**：无冲突，去重机制是单向的，不影响信号处理逻辑。

### 5.2 价格数据校验策略

**决策**：新增 `price_is_realtime` 标记，实时行情获取失败时拒绝发送信号。

**原因**：
- 历史收盘价不是真实市场价格，可能误导用户
- 静默回退到历史收盘价时，用户无法感知数据异常
- 严格拒绝发送非实时价格，确保数据质量

**代价**：
- 实时行情 API 失败时，信号不会被发送
- 可能错过一些交易机会

**冲突处理**：无冲突，数据质量优先于通知频率。

### 5.3 大师兄解读策略

**决策**：在宏观日报中调用 `WisdomAdvisor` 生成大师兄视角的解读。

**原因**：
- 纯数据展示缺乏认知深度
- 大师兄视角专注于板块轮动和资金流向，适合解读宏观数据
- LLM 能够结合当前市场环境给出有针对性的分析

**代价**：
- 每次宏观日报需要额外调用 LLM，增加 API 开销
- LLM 可能生成不准确的解读

**冲突处理**：解读失败时静默跳过，不影响宏观日报的正常推送。

## 6. 测试验证

### 6.1 测试统计

| 指标 | 迭代 6 | 迭代 7 | 变化 |
|------|--------|--------|------|
| 总测试数 | 506 | **806** | **+300** |
| 通过率 | 100% | **100%** | ✅ |
| 覆盖率 | 87% | **70%** | -17%（新增模块拉低） |

### 6.2 信号去重测试

| 测试场景 | 验证结果 |
|----------|----------|
| 同一信号多次扫描 | 只通知一次 |
| 引擎重启后再次扫描 | 不重复通知 |
| 次日信号 | 正常通知（去重记录按日清理） |

### 6.3 价格数据校验测试

| 测试场景 | 验证结果 |
|----------|----------|
| 实时行情成功 | 使用实时价格，`price_is_realtime=True` |
| 实时行情失败 | 拒绝发送信号，日志记录警告 |
| 实时价格为 0 | 拒绝发送信号，日志记录警告 |

### 6.4 大师兄解读测试

| 测试场景 | 验证结果 |
|----------|----------|
| LLM 成功 | 卡片末尾添加大师兄解读 |
| LLM 失败 | 静默跳过，不影响宏观日报推送 |
| 解读内容为空 | 静默跳过，不添加空解读 |

## 7. 深度反思

### 7.1 从"静默回退"到"显式标记"

本次迭代最重要的认知转变是：**数据质量优先于通知频率**。

在迭代 6 中，实时行情获取失败时会静默回退到历史收盘价，用户无法感知数据异常。这导致了贵州茅台价格 1500.0 的错误通知。

在迭代 7 中，我们引入了 `price_is_realtime` 标记，实时行情获取失败时拒绝发送信号。这虽然可能错过一些交易机会，但确保了数据质量。

**核心原则**：宁可少通知，不可通知错误数据。

### 7.2 从"纯内存"到"持久化"

信号去重机制从纯内存升级到持久化，解决了引擎重启后重复通知的问题。

这带来了一个重要的工程启示：**状态管理需要考虑故障恢复**。纯内存状态在进程崩溃后会丢失，导致状态不一致。持久化到文件后，引擎重启时能够恢复状态，保证一致性。

### 7.3 从"数据展示"到"认知解读"

宏观日报从纯数据展示升级到 LLM 驱动的认知解读，增加了大师兄视角的分析。

这体现了项目的核心价值：**不仅提供数据，更提供认知**。用户需要的不仅是金/油/汇/债/G 的数据，更需要理解这些数据背后的意义。

## 8. 当前不足之处

### 8.1 信号去重的粒度

当前去重机制基于 `symbol_direction_strategy`，即同一股票、同方向、同策略的信号只通知一次。

**局限性**：
- 如果策略参数发生变化，可能产生不同的信号，但仍被视为重复
- 未考虑信号强度的变化（如置信度从 0.6 提升到 0.9）

**建议**：引入信号强度阈值，置信度变化超过一定幅度时重新通知。

### 8.2 价格数据校验的严格性

当前校验逻辑在实时行情获取失败时拒绝发送信号。

**局限性**：
- 可能错过一些交易机会
- 未提供降级方案（如使用最近一次有效价格）

**建议**：引入降级策略，实时行情失败时使用最近一次有效价格，并标记为"延迟价格"。

### 8.3 大师兄解读的准确性

当前解读由 LLM 生成，可能存在不准确的情况。

**局限性**：
- LLM 可能生成与市场实际情况不符的解读
- 未引入人工审核机制

**建议**：引入人工审核机制，或增加解读的置信度标记。

## 9. 后续优化路线图

### 短期（下一迭代）

1. **信号强度阈值**：置信度变化超过一定幅度时重新通知
2. **价格降级策略**：实时行情失败时使用最近一次有效价格
3. **解读置信度**：为大师兄解读增加置信度标记

### 中期（阶段四前）

1. **人工审核机制**：重要解读由人工审核后推送
2. **多视角解读**：除了大师兄，增加其他书籍视角的解读
3. **解读历史**：记录历史解读，支持回溯分析

### 长期

1. **解读效果评估**：追踪解读的准确性，优化 LLM prompt
2. **个性化解读**：根据用户画像调整解读风格和内容
3. **解读订阅**：用户可选择订阅特定视角的解读

## 10. 迭代数据

```
迭代 6: 506 tests, 87% coverage
  │
  ▼ Phase 1: 信号去重持久化
  + _notified_signals_file 属性
  + _load_notified_signals() 方法
  + _save_notified_signals() 方法
  │
  ▼ Phase 2: 价格数据校验
  + price_is_realtime 标记
  + 实时行情失败时拒绝发送信号
  │
  ▼ Phase 3: 大师兄解读
  + 调用 WisdomAdvisor 生成解读
  + 添加到飞书卡片末尾
  │
  ▼ Phase 4: 测试验证
  + 300 个新测试
  + 806 测试全部通过
  │
迭代 7: 806 tests, 70% coverage, v0.3.3
```

---

## 11. 补充迭代：数据源健壮性增强（v0.3.4）

> 2026-07-02（同日补充）

### 11.1 背景

迭代 7 完成后,实际运行中发现 akshare 被东方财富限流,导致:
1. 大市数据获取失败(`RemoteDisconnected`),选股被阻塞
2. 资金流数据单位错误(显示亿元实际是元)
3. A 股指数显示 0(东方财富 API 返回值需要 /100)
4. 北向资金不可用时显示 0(应为"获取不到")

### 11.2 改动范围

| 文件 | 修改要点 |
|------|----------|
| `src/gugu/data/collectors/eastmoney_collector.py` | **新增** 东方财富直连采集器(第三备用源,httpx 直连 push2 API,不依赖 akshare) |
| `src/gugu/data/manager.py` | 注册 EastmoneyCollector 到采集器注册表 |
| `config/settings.yaml` | 配置三级冗余:`primary_source: sina`,`fallback_sources: [akshare, eastmoney]` |
| `src/gugu/engine/market_filter.py` | 大市数据三通道冗余:akshare → 新浪K线 → 东方财富 push2 |
| `src/gugu/notifier/flow_report.py` | 资金流单位转换(元→亿)、字段名统一(akshare "今日"前缀映射) |
| `src/gugu/macro/collectors.py` | A 股指数 /100 修正、北向资金 None 处理、3 次重试 |
| `src/gugu/macro/models.py` | `north_flow: float \| None = None` |
| `src/gugu/macro/report.py` | `_fmt_change()` 保留负号、仅显示有效指数 |
| `src/gugu/wisdom/advisor.py` | 补充 `import httpx`(缺失导致大师兄解读失败) |

### 11.3 架构演进

#### 个股数据三级冗余

```
迭代 7（两级）:
  sina（主） → akshare（降级）
  ↓ 问题：akshare 被限流时,资金流数据无法获取

迭代 7 补充（三级）:
  sina（主） → akshare（降级） → eastmoney（兜底）
  ↓ eastmoney_collector.py 通过 httpx 直连 push2.eastmoney.com
    不依赖 akshare 库,避免被同一限流策略影响
```

#### 大市数据三通道冗余

```
迭代 7（单通道）:
  ak.stock_zh_index_daily_em(symbol="sh000300")
  ↓ 问题：akshare 被限流时,大市数据获取失败,选股被阻塞

迭代 7 补充（三通道）:
  通道1: akshare stock_zh_index_daily_em（主）
  通道2: 新浪 K 线 JSON getKLineData（降级,实测最稳定）
  通道3: 东方财富 push2 kline API（兜底）
  ↓ 逐级降级,前一级失败自动尝试下一级
```

### 11.4 实测验证

引擎日志确认降级链路工作:
```
akshare 大市数据获取失败 (RemoteDisconnected)
→ 降级到新浪 K 线接口...
→ 大市数据(新浪): 300 条K线
→ 大市过滤: range — 收盘价4958.98>MA200(4676.71)
```

- 沪深300 收盘 4958.977(与新浪实时行情 4958.9773 一致)
- akshare 失败后 0 延迟切换到新浪
- 818 测试全部通过

### 11.5 数据质量保障

| 保障项 | 机制 |
|--------|------|
| 实时价格 | `price_is_realtime` 标记,实时行情失败拒绝发送信号 |
| 单位转换 | 东方财富 API 价格 ×100、涨跌幅 ×100、资金流单位元(自动转亿) |
| 北向资金 | 不可用时显示"获取不到"(非 0) |
| A 股指数 | f43/f170 字段自动 /100 转换 |
| 字段名统一 | akshare "今日"前缀与 HTTP API 字段自动映射 |
| 信号去重 | `notified_signals.json` 持久化,跨重启去重 |

### 11.6 迭代数据(补充后)

```
迭代 7 补充: 818 tests, 70% coverage, v0.3.4
  │
  ▼ Phase 1: 东方财富直连采集器
  + eastmoney_collector.py（push2 API 直连）
  + 注册到 DataManager
  + settings.yaml 配置三级冗余
  │
  ▼ Phase 2: 大市数据三通道
  + market_filter.py _fetch_index_sina()
  + market_filter.py _fetch_index_eastmoney()
  + fetch_index_data() 三级降级
  │
  ▼ Phase 3: 数据质量修复
  + 资金流单位转换(元→亿)
  + A 股指数 /100 修正
  + 北向资金 None 处理
  + 字段名统一映射
  + advisor.py import httpx 修复
  │
  ▼ Phase 4: 测试验证
  + 818 测试全部通过
  + 引擎重启验证降级链路
```
