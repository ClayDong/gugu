# 资金流异动监控策略设计文档

**版本**: v0.5.0
**日期**: 2026-07-07
**作者**: 基于豆包 AI「龙头股散户化」分析 + 用户需求

## 一、需求背景

### 1.1 用户核心诉求

1. **实时盯盘监控资金流入流出**：根据电脑性能自适应频率，监控大资金异动
2. **判断资金对股价的影响**：什么样的资金流入会带动股价上涨，什么样的流出会造成下跌
3. **主力出逃特征识别**：持续放量下跌 + 大单净流出加速 + 机构筹码分散 + 支撑位有效跌破
4. **股票资金画像**：散户/机构持仓比例判断，散户过多的股票不入手
5. **飞书通知**：先监控大的行业，资金异动即时推送
6. **回测验证**：新策略必须经过回测验证

### 1.2 豆包 AI 案例验证

5 个历史回测案例验证「筹码集中易涨，分散易跌」规律：

| 案例 | 户数变化 | 股价表现 | 验证方向 |
|------|---------|---------|---------|
| 比亚迪 | 20万→71.87万（+260%） | 箱体震荡承压 | 分散→跌 |
| 盛达资源 | 单月+112.6% | -62% | 暴增→暴跌 |
| 赛力斯 | +50% | -60% | 分散→大跌 |
| 牧原股份 | 27.9→18.6万 | +49% | 集中→涨 |
| 牧原股份 | 18.6→23.4万 | -31% | 分散→跌 |

**关键阈值**：
- 避雷针：单季户数增 > 30% / 单月 > 15% / 户均持股减 > 20%
- 潜伏者：连续 2-3 季度户数降 > 20% + 低位企稳

## 二、架构设计：三层防御体系

### 2.1 总体架构

```
┌─────────────────────────────────────────────────────┐
│ 第 1 层：筹码结构过滤器（季度，慢变量）              │
│ 数据源：akshare stock_zh_a_gdhs（股东户数）          │
│ 集成点：SignalPipeline 步骤 2.4                    │
│ 作用：拒绝散户化股票入场，标记潜伏者股票            │
└─────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────┐
│ 第 2 层：资金流异动策略（日度，中变量）             │
│ 数据源：fetch_stock_flow（已有，5档资金流）         │
│ 形态：新策略 capital_flow_alert，注册到 _REGISTRY   │
│ 作用：4 卖出信号 + 3 买入信号                       │
└─────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────┐
│ 第 3 层：盘中实时盯盘（分钟级，快变量）             │
│ 数据源：fetch_stock_realtime + fetch_stock_flow     │
│ 形态：IntradayMonitor 独立任务，按性能分级          │
│ 作用：超大单异动即时告警，主力连续流出预警          │
└─────────────────────────────────────────────────────┘
```

### 2.2 性能分级策略

启动时检测 CPU 核数 + 可用内存，自动选择模式：

| 模式 | 触发条件 | 扫描频率 | 扫描范围 | API 调用/分钟 |
|------|---------|---------|---------|--------------|
| 高性能 | ≥8 核 + ≥16GB | 每 30 秒 | watchlist + 持仓（≤50 只） | ~100 |
| 中性能 | 4-8 核 或 8-16GB | 每 2 分钟 | watchlist + 持仓（≤30 只） | ~30 |
| 低性能 | <4 核 或 <8GB | 每 5 分钟 | 仅持仓（≤5 只） | ~5 |
| 关闭 | 手动 off | 不扫描 | - | 0 |

手动覆盖：`settings.yaml intraday_monitor.performance_mode: auto|high|medium|low|off`

## 三、第 1 层：筹码结构过滤器

### 3.1 数据采集器

**新建文件**：`src/gugu/data/collectors/holder_structure.py`

**类**：`HolderStructureCollector`

**数据源**：
- 主源：akshare `ak.stock_zh_a_gdhs(symbol="最新")` → 全市场股东户数快照
- 降级源：东方财富 HTTP 直连 `https://datacenter-web.eastmoney.com/api/data/v1/get`

**返回字段**：
```python
DataFrame[
    symbol,           # 股票代码
    name,             # 股票名称
    holder_num,       # 股东户数
    holder_num_change,# 户数变化（较上期）
    holder_num_pct,   # 户数变化率%
    avg_hold_amount,  # 户均持股数
    avg_hold_change,  # 户均持股变化率%
    end_date,         # 报告期
    org_hold_pct,     # 机构持股比例%（可选）
]
```

**缓存策略**：
- 股东户数季度披露，缓存 24 小时（避免频繁调用）
- 存储到 `data/holder_structure/{date}.parquet`

### 3.2 筹码集中度过滤器

**新建文件**：`src/gugu/filters/holder_concentration.py`

**类**：`HolderConcentrationFilter`

**核心方法**：
```python
async def check(self, symbol: str) -> dict:
    """返回 {pass, label, holder_change_pct, avg_hold_change, reasons}"""
```

**避雷针信号**（任一触发 → pass=False，拒绝买入）：
1. 单季度户数增幅 > 30%
2. 户均持股环比减少 > 20%
3. 户数已连续 2 季度增长且累计 > 20%

**潜伏者信号**（同时满足 → pass=True，置信度 +0.15）：
1. 连续 2 季度户数下降，累计降幅 > 20%
2. 户均持股环比增加 > 15%

**数据缺失处理**：
- 季度数据未披露：默认放行（pass=True），reasons 标记"数据不可用"
- 与 MoneyFlowFilter 区别：筹码是慢变量，不应因数据缺失而拒绝整个信号

**集成点**：SignalPipeline 步骤 2.4（资金流之后、板块轮动之前）

## 四、第 2 层：资金流异动策略

### 4.1 策略类

**新建文件**：`src/gugu/strategies/capital_flow_alert.py`

**类**：`CapitalFlowAlertStrategy(Strategy)`

**name**：`capital_flow_alert`

**重要约束**：
- 策略本身只用 OHLCV 数据生成基础信号（与 SignalRouter 兼容）
- 真正的资金流判断在 SignalPipeline 步骤 2 完成，策略只读取 `signal["money_flow"]` 增强
- 这样设计避免破坏策略基类的"无外部 IO"原则

### 4.2 信号生成逻辑

#### 4.2.1 卖出信号（4 核心特征，任一触发）

**第一性原理**：资金出逃早于股价大跌，4 特征同时出现 = 主力正在撤离

```python
# 特征 1：持续放量下跌（3 日内 2 日放量且收跌）
cond_volume_drop = (
    (df["volume"] > df["vol_ma5"] * 1.2).rolling(3).sum() >= 2
) & (df["close"].pct_change().rolling(3).sum() < -0.03)

# 特征 2：大单净流出加速（5 日主力净流入持续为负且递减）
# 注：策略层只用 OHLCV，真正的大单判断在 MoneyFlowFilter
# 策略用「量价背离」近似：放量但收盘走弱
cond_divergence = (
    (df["volume"] > df["vol_ma5"] * 1.3)
    & (df["close"] < df["close"].shift(1))
    & (df["close"].shift(1) > df["close"].shift(2))  # 前一日还是涨
)

# 特征 3：跌破 MA20 支撑（机构筹码分散后必破支撑）
cond_break_ma20 = df["close"] < df["ma20"]

# 特征 4：高位巨量滞涨（主力出货）
cond_stagnation = (
    (df["volume"] > df["vol_ma5"] * 2.0)
    & (df["upper_shadow"] > 0.02)
    & (df["daily_return"].abs() < 0.02)
)
```

**4 特征中任一触发卖出信号**（confidence 0.5），2 个以上同时触发 confidence 0.8。

#### 4.2.2 买入信号（3 核心特征，同时满足）

```python
# 特征 1：缩量回踩不破 MA20（主力洗盘结束）
cond_shrink_pullback = (
    (df["volume"] < df["vol_ma5"] * 0.8)
    & (df["close"] >= df["ma20"])
    & (df["daily_return"] > -0.02)
)

# 特征 2：MA20 向上拐头
cond_ma20_up = df["ma20"] > df["ma20"].shift(5)

# 特征 3：前期有强势表现（5 日内曾有涨幅 > 5%）
cond_recent_strong = df["close"].pct_change(5).max() >= 0.05  # 简化版
```

**置信度评分**：
- 基础 0.5（满足 3 条件）
- +0.15 缩量程度（vol_ratio < 0.6）
- +0.10 MA20 斜率强度
- +0.10 距离 MA20 距离（精准回踩）
- 上限 0.85

### 4.3 策略注册

```python
# src/gugu/strategies/registry.py
from gugu.strategies.capital_flow_alert import CapitalFlowAlertStrategy

_REGISTRY["capital_flow_alert"] = CapitalFlowAlertStrategy
```

### 4.4 配置

```yaml
# config/strategy_defaults.yaml
capital_flow_alert:
  ma20_window: 20
  vol_ma_window: 5
  shrink_ratio: 0.8
  breakout_volume_ratio: 1.3
  stagnation_volume_ratio: 2.0
  pullback_max_pct: -0.02
  recent_strong_window: 5
  recent_strong_threshold: 0.05
  confirm_days: 0
```

## 五、第 3 层：盘中实时盯盘

### 5.1 监控器

**新建文件**：`src/gugu/engine/intraday_monitor.py`

**类**：`IntradayMonitor`

**核心方法**：
```python
async def run_cycle(self) -> list[AlertItem]:
    """执行一次扫描周期，返回异动告警列表"""

async def start(self) -> None:
    """启动定时扫描（APScheduler 或 asyncio.create_task）"""
```

### 5.2 监控规则

**告警类型 1：超大单异动**（单股）
- 单股当日超大单净流入 > 1 亿 → 🟢 买入预警
- 单股当日超大单净流出 > 1 亿 → 🔴 卖出预警
- 单股主力净占比 > 5% → 🟢 强流入
- 单股主力净占比 < -5% → 🔴 强流出

**告警类型 2：连续资金流**（多日）
- 连续 3 日主力净流入且累计 > 3 亿 → 🟢 持续吸筹
- 连续 3 日主力净流出且累计 > 3 亿 → 🔴 持续出逃

**告警类型 3：行业资金流异动**
- 行业当日主力净流入 > 10 亿 → 🟢 行业强流入
- 行业当日主力净流出 > 10 亿 → 🔴 行业强流出
- 行业资金流与涨跌幅背离 → ⚠️ 顶背离/底背离预警

**告警类型 4：持仓监控**（针对当前持仓）
- 持仓股主力净流出 > 5000 万 → 🔴 减仓预警
- 持仓股跌破 MA20 → 🔴 止损预警

### 5.3 性能自适应

```python
import psutil
import os

def detect_performance_mode() -> str:
    """自动检测性能模式"""
    cpu_count = os.cpu_count() or 4
    mem = psutil.virtual_memory()
    mem_gb = mem.total / (1024**3)
    
    if cpu_count >= 8 and mem_gb >= 16:
        return "high"
    elif cpu_count >= 4 and mem_gb >= 8:
        return "medium"
    else:
        return "low"

PERFORMANCE_CONFIG = {
    "high":   {"interval_sec": 30,  "max_symbols": 50, "scan_watchlist": True},
    "medium": {"interval_sec": 120, "max_symbols": 30, "scan_watchlist": True},
    "low":    {"interval_sec": 300, "max_symbols": 5,  "scan_watchlist": False},
    "off":    {"interval_sec": 0,   "max_symbols": 0,  "scan_watchlist": False},
}
```

### 5.4 飞书告警卡片

**新建文件**：`src/gugu/notifier/intraday_alert.py`

**卡片结构**：
```
🟢🔴 盘中异动告警 14:35

【超大单异动】
🟢 600519 贵州茅台 超大单净流入 1.5亿
🔴 300750 宁德时代 超大单净流出 1.2亿

【行业资金流】
🟢 半导体 行业流入 15.3亿
🔴 房地产 行业流出 8.7亿

【持仓预警】
🔴 002714 牧原股份 主力净流出 6000万，建议关注

【决策建议】
关注半导体板块资金持续流入，警惕房地产板块出逃
```

## 六、飞书通知集成

### 6.1 信号卡片新增筹码层

在 `format_signal()` 输出中新增折叠层：
```
📊 筹码结构
- 股东户数：23.4万（季度变化 +5%）
- 户均持股：4250 股（环比 -3%）
- 标记：⚠️ 户数缓慢增加，注意散户化倾向
```

### 6.2 盘中异动独立卡片

新建飞书卡片类型 `notify_intraday_alert`，使用独立调度任务。

### 6.3 行业资金流卡片（已有）

复用 `flow_report.py` 中的 `analyze_sectors()` 和 `build_morning_card()`。

## 七、配置变更

### 7.1 settings.yaml 新增

```yaml
# ========== 盘中实时盯盘 ==========
intraday_monitor:
  enabled: true
  performance_mode: auto    # auto|high|medium|low|off
  # 自定义监控范围（可选，留空则按性能模式自动决定）
  custom_watchlist: []
  # 告警阈值
  alert_thresholds:
    super_large_inflow: 100000000    # 单股超大单净流入 1 亿
    super_large_outflow: 100000000    # 单股超大单净流出 1 亿
    main_pct_threshold: 0.05         # 主力净占比 5%
    consecutive_days: 3              # 连续日数
    industry_flow_threshold: 1000000000  # 行业资金流 10 亿

# ========== 筹码结构过滤 ==========
holder_concentration_filter:
  enabled: true
  quarterly_change_pct_threshold: 30.0   # 季度户数增幅 30%
  monthly_change_pct_threshold: 15.0     # 月度户数增幅 15%
  avg_hold_change_threshold: -20.0        # 户均持股减少 20%
  lurker_consecutive_quarters: 2         # 潜伏者连续季度数
  lurker_total_decline_threshold: 20.0   # 潜伏者累计降幅 20%
```

### 7.2 strategy_defaults.yaml 新增

```yaml
capital_flow_alert:
  ma20_window: 20
  vol_ma_window: 5
  shrink_ratio: 0.8
  breakout_volume_ratio: 1.3
  stagnation_volume_ratio: 2.0
  pullback_max_pct: -0.02
  recent_strong_window: 5
  recent_strong_threshold: 0.05
  confirm_days: 0
```

### 7.3 strategy_manifest.yaml 新增

```yaml
signal_strategies:
  capital_flow_alert:
    name: 资金流异动监控
    module: gugu.strategies.capital_flow_alert
    class: CapitalFlowAlertStrategy
    source: 豆包AI「龙头股散户化」分析 + 实战派资金流监控
    enabled: true
    summary: 4卖出特征识别主力出逃 + 3买入特征捕捉洗盘结束
    params_entry: strategy_defaults.yaml
    validation: v0.5.0 单元测试通过，待回测验证
    notes: |
      核心观点：资金出逃早于股价大跌，4 特征同时出现 = 主力撤离。
      卖出：放量下跌 + 量价背离 + 跌破MA20 + 巨量滞涨
      买入：缩量回踩不破MA20 + MA20向上 + 前期强势
```

## 八、回测验证计划

### 8.1 6 个案例基准验证

用豆包 AI 提到的 6 个案例做基准验证：
- 比亚迪、隆基绿能、盛达资源、赛力斯、铜陵有色、牧原股份

**验证目标**：
- 策略是否能在跌幅前发出卖出信号
- 策略是否能在涨幅前发出买入信号

### 8.2 全市场大样本回测

```bash
uv run python scripts/backtest_all_strategies.py --days 365 --strategies capital_flow_alert
```

**验收标准**（4 项解锁条件，与 v0.4.4 一致）：
1. Sharpe > 0
2. 参数稳定性 ≥ 50%
3. 回撤 < 25%
4. 收益 ≥ 0

### 8.3 资金流过滤器历史验证

对 MoneyFlowFilter 做历史回测：
- 取历史 100 只股票
- 对每只股票在每次信号时调用 `money_flow_filter.check()`
- 统计通过率与未来 5 日涨跌相关性

## 九、风险与对抗性审查

### 9.1 已识别风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 股东户数季度披露滞后 | 信号延迟 3 个月 | 作为慢变量过滤器，不作为日内信号 |
| 实时盯盘增加 API 调用 | 触发东财限流 | 性能分级 + 缓存 + 限流保护 |
| 策略过拟合 | 历史有效未来失效 | 全市场大样本回测 + Walk-Forward |
| 筹码数据缺失 | 过滤器失效 | 默认放行，仅标记不阻塞 |
| LLM 调用延迟 | 卡住 SignalPipeline | signal_only 模式跳过 LLM |

### 9.2 第一性原理验证

1. **为什么股东户数是领先指标？** 因为机构建仓需要时间，户数减少 = 机构在吸筹，领先于股价启动
2. **为什么 4 特征同时出现才是主力出逃？** 单一特征可能是噪音，多特征共振才是趋势性变化
3. **为什么按性能分级？** 实时盯盘是 I/O 密集型，低配机器强行高频扫描会导致系统卡死
4. **为什么策略层不直接调用资金流 API？** 保持策略基类的"无外部 IO"原则，避免回测时无法复现

## 十、实施清单

### 10.1 新建文件

| 文件 | 层 | 职责 |
|------|----|------|
| `src/gugu/data/collectors/holder_structure.py` | 1 | 股东户数采集器 |
| `src/gugu/filters/holder_concentration.py` | 1 | 筹码集中度过滤器 |
| `src/gugu/strategies/capital_flow_alert.py` | 2 | 资金流异动策略 |
| `src/gugu/engine/intraday_monitor.py` | 3 | 盘中实时监控器 |
| `src/gugu/notifier/intraday_alert.py` | 3 | 盘中告警卡片 |
| `tests/unit/test_holder_concentration.py` | - | 第 1 层测试 |
| `tests/unit/test_capital_flow_alert.py` | - | 第 2 层测试 |
| `tests/unit/test_intraday_monitor.py` | - | 第 3 层测试 |

### 10.2 修改文件

| 文件 | 修改内容 |
|------|---------|
| `src/gugu/strategies/registry.py` | 注册 capital_flow_alert |
| `src/gugu/engine/signal_pipeline.py` | 集成筹码过滤器（步骤 2.4） |
| `src/gugu/engine/scheduler.py` | 注册盘中监控任务 |
| `src/gugu/notifier/feishu.py` | 新增 notify_intraday_alert 方法 |
| `src/gugu/notifier/formatter.py` | 信号卡片新增筹码结构层 |
| `config/settings.yaml` | 新增 intraday_monitor + holder_concentration_filter 配置 |
| `config/strategy_defaults.yaml` | 新增 capital_flow_alert 参数 |
| `config/strategy_manifest.yaml` | 新增 capital_flow_alert 条目 |

### 10.3 测试清单

**第 1 层测试**（test_holder_concentration.py）：
- 默认阈值测试
- 避雷针信号触发（季度增 > 30%）
- 潜伏者信号触发（连续 2 季降 > 20%）
- 数据缺失降级
- 户均持股减少 20% 拒绝
- describe() 返回正确

**第 2 层测试**（test_capital_flow_alert.py）：
- 参数校验
- 4 卖出信号触发
- 3 买入信号触发
- 置信度评分
- 边界场景
- 与 SignalRouter 兼容性

**第 3 层测试**（test_intraday_monitor.py）：
- 性能模式自动检测
- 超大单异动告警
- 连续资金流告警
- 行业资金流告警
- 持仓监控
- 告警去重（同一信号 5 分钟内不重复推送）
- 非交易时段跳过

## 十一、上线流程

1. 实施第 1 层（筹码过滤器）+ 单测
2. 实施第 2 层（资金流异动策略）+ 单测
3. 实施第 3 层（盘中盯盘）+ 单测
4. 飞书通知集成
5. 配置文件更新
6. 全量测试 `pytest`
7. 全市场回测验证
8. 代码 review
9. 重启 scheduler + web
10. 上线验证（观察一个交易日）
