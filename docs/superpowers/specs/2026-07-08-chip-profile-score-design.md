# 筹码画像评分（Chip Profile Score）设计文档

**日期**: 2026-07-08
**版本**: v0.6.0
**状态**: 设计中

## 一、背景与目标

### 1.1 问题陈述

当前 StockRanker 的4个评分维度（因子30% + 基本面25% + 资金流25% + 板块20%）缺少"筹码结构"维度。用户反馈：比亚迪散户多、主力拉升困难，希望系统识别"资本青睐、拉升方便"的股票结构。

### 1.2 设计目标

- 在 StockRanker 新增第5个评分维度"筹码画像评分"（chip_score），权重15%
- 识别"主力收集、筹码集中"的股票，提升其在选股结果中的排名
- 降权"散户化、筹码分散"的股票，即使技术面/资金流短期走强
- 不引入新模块，与现有4维度统一架构

### 1.3 非目标

- 不做独立的筹码选股器（不产生买卖信号）
- 不计算绝对散户占比（org_hold_pct 数据不可靠）
- 不替换 SignalPipeline 中现有的 HolderConcentrationFilter 硬过滤

## 二、架构设计

### 2.1 评分维度权重调整

| 维度 | 原权重 | 新权重 | 变化 |
|------|--------|--------|------|
| 因子评分 | 30% | 25% | -5% |
| 基本面评分 | 25% | 20% | -5% |
| 资金流评分 | 25% | 25% | 0% |
| 板块评分 | 20% | 15% | -5% |
| **筹码画像评分** | - | **15%** | **新增** |

**权重调整理由**：
- 筹码15%反映慢变量趋势（季度披露），与资金流快变量互补
- 资金流25%保持不变，快变量仍是主导
- 板块降5%（与筹码有一定重叠：热门板块的筹码结构通常更优）
- 因子和基本面各降5%（这两个维度本身已较成熟）

### 2.2 筹码画像评分逻辑

```
chip_score = 户数变化评分(0.4) + 户均持股变化评分(0.3) + 机构持仓评分(0.2) + 流通市值评分(0.1)
```

| 子项 | 满分 | 评分规则 |
|------|------|---------|
| 户数变化 | 0.4 | 连续下降>20% → 0.4；单季下降>5% → 0.3；变化±5% → 0.2；单季上升>5% → 0.1；连续上升>20% → 0.0 |
| 户均持股变化 | 0.3 | 上升>15% → 0.3；上升5-15% → 0.2；变化±5% → 0.15；下降>5% → 0.05；下降>20% → 0.0 |
| 机构持仓 | 0.2 | org_hold_pct > 40% → 0.2；20-40% → 0.15；10-20% → 0.1；<10% → 0.05；数据缺失 → 0.1（中性） |
| 流通市值 | 0.1 | 50-500亿 → 0.1（主力易控盘）；500-2000亿 → 0.05；>2000亿或<50亿 → 0.03 |

**避雷针特殊处理**：当 HolderConcentrationFilter 判定为"避雷针"（户数激增>30%）时，chip_score 直接置为 0.0。

**数据缺失处理**：当筹码数据不可用时（季度未披露），chip_score = 0.5（中性默认分，不惩罚也不奖励）。

### 2.3 数据流

```
StockRanker._score_one(symbol)
  ├─ [现有] 因子评分 → factor_score
  ├─ [现有] 基本面评分 → fund_score
  ├─ [现有] 资金流评分 → flow_score
  ├─ [现有] 板块评分 → sector_score
  └─ [新增] 筹码画像评分 → chip_score
       ├─ HolderConcentrationFilter.check(symbol) → label/holder_num_pct/avg_hold_change
       ├─ 计算 4 个子项评分
       └─ 避雷针 → 0.0 / 数据缺失 → 0.5

total = factor*0.25 + fund*0.20 + flow*0.25 + sector*0.15 + chip*0.15
```

## 三、组件变更清单

### 3.1 修改文件

| 文件 | 变更内容 |
|------|---------|
| `src/gugu/analysis/stock_ranker.py` | 新增 chip_score 计算逻辑、更新权重公式、更新返回字典 |
| `src/gugu/selector/stock_selector.py` | 合并 chip_score 到信号 dict（line 174-185） |
| `config/settings.yaml` | 新增 chip_score_weight 配置项（可选，默认0.15） |
| `src/gugu/config/models.py` | 新增 ChipScoreConfig 配置模型（可选） |

### 3.2 新增文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_chip_score.py` | 筹码画像评分单元测试 |

### 3.3 不变文件

- `src/gugu/filters/holder_concentration.py` — 复用现有 HolderConcentrationFilter，不修改
- `src/gugu/data/collectors/holder_structure.py` — 复用现有数据采集器，不修改
- `src/gugu/engine/signal_pipeline.py` — 现有筹码硬过滤保持不变

## 四、实现细节

### 4.1 StockRanker._score_one 修改

在现有4个评分维度之后，新增筹码画像评分：

```python
# 5. 筹码画像评分（15%权重）
from gugu.filters.holder_concentration import HolderConcentrationFilter

hc_filter = HolderConcentrationFilter()
hc_result = await hc_filter.check(symbol)
chip_score = self._compute_chip_score(hc_result, symbol)

# 更新加权公式
total = (
    factor_score * 0.25 + fund_score * 0.20 + flow_score * 0.25
    + sector_score * 0.15 + chip_score * 0.15
)
```

### 4.2 _compute_chip_score 方法

```python
def _compute_chip_score(self, hc_result: dict, symbol: str) -> float:
    """计算筹码画像评分（0-1）。

    评分维度：
    - 户数变化（0.4）：连续下降>20% → 0.4；单季下降>5% → 0.3；...
    - 户均持股变化（0.3）：上升>15% → 0.3；上升5-15% → 0.2；...
    - 机构持仓（0.2）：org_hold_pct > 40% → 0.2；...
    - 流通市值（0.1）：50-500亿 → 0.1；...
    """
    # 避雷针 → 0.0
    if hc_result.get("label") == "避雷针":
        return 0.0

    # 数据缺失 → 0.5（中性）
    if hc_result.get("data_missing"):
        return 0.5

    holder_num_pct = hc_result.get("holder_num_pct", 0.0)
    avg_hold_change = hc_result.get("avg_hold_change", 0.0)

    # 1. 户数变化评分（0.4满分）
    if holder_num_pct < -20:
        score_holder = 0.4  # 连续下降>20%
    elif holder_num_pct < -5:
        score_holder = 0.3  # 单季下降>5%
    elif abs(holder_num_pct) <= 5:
        score_holder = 0.2  # 稳定
    elif holder_num_pct < 20:
        score_holder = 0.1  # 单季上升>5%
    else:
        score_holder = 0.0  # 连续上升>20%

    # 2. 户均持股变化评分（0.3满分）
    if avg_hold_change > 15:
        score_avg = 0.3
    elif avg_hold_change > 5:
        score_avg = 0.2
    elif abs(avg_hold_change) <= 5:
        score_avg = 0.15
    elif avg_hold_change > -20:
        score_avg = 0.05
    else:
        score_avg = 0.0

    # 3. 机构持仓评分（0.2满分）— 数据缺失时中性0.1
    org_pct = hc_result.get("org_hold_pct", 0.0) or 0.0
    if org_pct > 40:
        score_org = 0.2
    elif org_pct > 20:
        score_org = 0.15
    elif org_pct > 10:
        score_org = 0.1
    elif org_pct > 0:
        score_org = 0.05
    else:
        score_org = 0.1  # 数据缺失，中性

    # 4. 流通市值评分（0.1满分）
    circ_mv = await self._dm.fetch_market_cap(symbol)  # 需确认接口
    if 50 <= circ_mv <= 500:
        score_mv = 0.1
    elif 500 < circ_mv <= 2000:
        score_mv = 0.05
    else:
        score_mv = 0.03

    return score_holder + score_avg + score_org + score_mv
```

### 4.3 配置项

```yaml
# settings.yaml 新增
stock_ranker:
  chip_score_weight: 0.15  # 筹码画像评分权重
  chip_score_neutral: 0.5  # 数据缺失时的中性评分
```

### 4.4 测试策略

| 测试场景 | 预期 chip_score |
|---------|----------------|
| 避雷针（户数+35%） | 0.0 |
| 潜伏者（户数连续-20%，户均+20%） | 0.4+0.3+0.2+0.1 = 1.0 |
| 数据缺失 | 0.5 |
| 中性（户数-3%，户均+2%） | 0.3+0.2+0.1+0.1 = 0.7 |
| 散户化（户数+25%，户均-25%） | 0.0+0.0+0.05+0.05 = 0.1 |
| 小盘股（流通市值30亿） | score_mv = 0.03 |
| 大盘股（流通市值3000亿） | score_mv = 0.03 |

## 五、风险与缓解

### 5.1 数据滞后风险

**风险**：筹码数据季度披露，报告期到披露日有3-4个月滞后。
**缓解**：
- chip_score 权重仅15%，不会主导选股决策
- 配合资金流评分（25%）对冲：资金流是日频快变量，能反映主力近期行为
- 飞书卡片标注"数据截至X月X日"，用户知晓滞后

### 5.2 权重调整影响

**风险**：调整4个现有维度权重，可能导致选股结果变化。
**缓解**：
- 调整幅度小（每个维度-5%），对排序影响有限
- 实现后跑全市场选股对比（调整前后Top10重合度应>70%）
- 如重合度<70%，回调筹码权重到10%

### 5.3 流通市值数据获取

**风险**：`fetch_market_cap` 接口可能不存在或不可靠。
**缓解**：
- 降级处理：接口失败时 score_mv = 0.05（中性）
- 或用 `fetch_stock_meta` 中的 circ_mv 字段（如已存在）

## 六、验收标准

1. **单元测试通过**：test_chip_score.py 覆盖所有评分场景
2. **配置一致性测试通过**：settings.yaml + models.py + manifest 一致
3. **全量测试通过**：不影响现有测试（1289+ passed）
4. **选股结果对比**：调整前后 Top10 重合度 >70%
5. **飞书卡片展示**：选股结果包含 chip_score 字段

## 七、后续演进方向

- v0.6.1：飞书选股卡片新增"筹码画像"折叠层，展示4个子项评分
- v0.6.2：回测验证 chip_score 对选股收益的影响
- v0.6.3：探索用龙虎榜数据（日频）补充季度筹码数据的滞后
