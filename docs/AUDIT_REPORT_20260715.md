# 审计报告 2026-07-15

## 审计范围

1. **数据真实性审计**：所有股票数据（行情、资金流、股东户数）来源是否为真实数据源，是否存在 mock/模拟数据
2. **飞书通知完整性审计**：所有飞书通知中，有股票代码的地方是否同时包含股票名称

---

## 1. 数据真实性审计

### 1.1 数据采集层

| 采集器 | 数据源 | 是否真实 | 是否存在模拟数据 |
|--------|--------|---------|----------------|
| SinaCollector (`collectors/fallback.py`) | 新浪/网易财经 HTTP API | 是 | 否 |
| EastMoneyCollector (`collectors/eastmoney_collector.py`) | 东方财富 push2 HTTP API | 是 | 否 |
| AkshareCollector (`collectors/akshare_collector.py`) | akshare（封装东方财富 API） | 是 | 否 |
| HolderStructureCollector (`collectors/holder_structure.py`) | akshare + 东方财富 datacenter API | 是 | 否 |

**结论**：所有采集器均连接真实外部数据 API，未发现任何 mock、模拟数据或伪造数据逻辑。

### 1.2 数据管理层

| 模块 | 职责 | 是否包含模拟数据 |
|------|------|----------------|
| `data/manager.py` | 多源调度、自动降级、三级缓存 | 否（纯调度逻辑） |
| `data/quality.py` | 数据质量校验（链式校验模式） | 否（纯校验逻辑） |
| `data/stock_names.py` | 股票名称 fallback 映射表 | 否（离线名称字典，用于 API 返回空名称时的降级） |
| `data/cache.py` | 内存 + Parquet 持久化缓存 | 否（纯缓存逻辑，过期自动重请求） |

### 1.3 回测与绩效分析

| 模块 | 数据来源 | 是否真实 |
|------|---------|---------|
| `backtest/engine.py` | 外部传入的 OHLCV DataFrame（真实 K 线） | 是 |
| `backtest/metrics.py` | 回测引擎输出的权益曲线和交易列表 | 是 |
| `analysis/performance.py` | 外部传入交易记录 + 沪深300真实基准数据 | 是 |
| `execution/paper.py` | 实时行情价格 + 配置初始资金 | 是（random 仅用于部分成交/涨跌停概率模拟，不影响价格数据本身） |

### 1.4 PaperBroker 中的 random 使用

`execution/paper.py` 中使用 `random` 的 3 处场景，均为模拟市场微观结构效应，不生成价格或收益数据：

1. **部分成交模拟**：模拟大单流动性不足场景
2. **涨跌停附近成交概率**：模拟真实市场中涨停买不到/跌停卖不出的情况
3. **按市值分级的滑点和部分成交概率**：静态配置参数

**结论**：所有随机行为仅限于成交概率建模，成交价始终基于真实市场价格。

---

## 2. 飞书通知完整性审计

### 2.1 审计标准

飞书通知卡片中涉及股票代码的地方，必须同时显示股票名称，格式为 `名称(代码)`。

### 2.2 逐文件审查结果

| 文件 | 审查结论 | 问题 |
|------|---------|------|
| `notifier/formatter.py` | **2 处问题** | 见下方 |
| `notifier/flow_report.py` | 通过 | - |
| `notifier/intraday_alert.py` | 通过 | - |
| `notifier/daily_report_manager.py` | 通过 | - |
| `notifier/perspective_report.py` | 通过 | - |

### 2.3 发现的问题及修复

#### 问题 1：`format_daily_report` 移动止损段

**位置**：`formatter.py` 第 700-705 行

**修改前**：
```python
stop_lines.append(f"- {sym}: 止损 {stop:.2f} / 最高 {highest:.2f} ({sig})")
```

**修改后**：
```python
name = lookup_stock_name(sym) or sym
stop_lines.append(f"- {name}({sym}): 止损 {stop:.2f} / 最高 {highest:.2f} ({sig})")
```

#### 问题 2：`format_backtest_report` 标题和标的

**位置**：`formatter.py` 第 815-822 行

**修改前**：
```python
symbol = report.get("symbol", "")
title = f"回测报告 · {strategy}"
if symbol:
    title += f" · {symbol}"
sections = [
    f"**策略**：{strategy}\n**标的**：{symbol if symbol else '多股聚合'}\n"
    ...
]
```

**修改后**：
```python
symbol = report.get("symbol", "")
name = lookup_stock_name(symbol) or symbol
title = f"回测报告 · {strategy}"
if symbol:
    title += f" · {name}({symbol})"
if symbol:
    header = f"**策略**：{strategy}\n**标的**：{name}({symbol})"
else:
    header = f"**策略**：{strategy}\n**标的**：多股聚合"
sections = [
    f"{header}\n"
    ...
]
```

---

## 3. 审计结论

| 维度 | 结果 |
|------|------|
| 数据真实性 | **通过** - 所有股票数据均来自真实数据源，无模拟/伪造数据 |
| 飞书通知完整性 | **已修复** - 发现 2 处仅有股票代码无股票名称的问题，已全部修复 |