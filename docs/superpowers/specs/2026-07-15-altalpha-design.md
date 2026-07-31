# 另类数据Alpha因子系统设计

> 日期：2026-07-15
> 状态：待审批
> 方案：方案A — 全部另类数据因子

## 一、目标

用免费数据源（akshare + 东方财富push2直连）构建8个另类因子，通过IC验证筛选有效因子，接入AlphaFactory，为策略层提供真正的Alpha信号。

## 二、因子清单

### P0 优先（学术验证过的Alpha来源）

| # | 因子名 | 数据源 | akshare API | 频率 | Alpha逻辑 |
|---|--------|--------|-------------|------|-----------|
| 1 | `insider_buy` | 高管增减持 | `stock_ggcg_em` | 每日 | 内部人净增持→看多信号 |
| 2 | `earnings_surprise` | 业绩预告 | `stock_yjyg_em` | 业绩季 | 预告增幅超预期→PEAD效应 |
| 3 | `lhb_inst_net` | 龙虎榜 | `stock_lhb_detail_em` | 每日 | 机构席位净买入→聪明钱 |
| 4 | `roic_quality` | 财务数据 | `stock_financial_analysis_indicator` | 季度 | ROE/ROIC高且稳定→质量因子 |

### P1 优先（高Alpha潜力）

| # | 因子名 | 数据源 | akshare API | 频率 | Alpha逻辑 |
|---|--------|--------|-------------|------|-----------|
| 5 | `block_discount` | 大宗交易 | `stock_dzjy_mrmx` | 每日 | 折价率大→机构减持信号（反向） |
| 6 | `margin_sentiment` | 融资融券 | `stock_margin_detail_sz/sh_se` | 每日 | 融资余额5日变化率→做多情绪 |
| 7 | `unlock_pressure` | 限售解禁 | `stock_share_unlock_em` | 每日 | 未来30日解禁市值占比→规避因子 |
| 8 | `holder_concentrate` | 股东户数 | 已有`holder_structure.py` | 季度 | 户数环比下降→筹码集中→主力潜伏 |

## 三、架构设计

### 3.1 新增文件结构

```
src/gugu/data/collectors/
├── altalpha/                   # 新增：另类数据采集器
│   ├── __init__.py
│   ├── base.py                 # AltAlphaCollector 基类（限流+缓存+降级）
│   ├── insider_trading.py      # 高管增减持
│   ├── earnings_forecast.py    # 业绩预告
│   ├── dragon_tiger.py         # 龙虎榜
│   ├── block_trade.py          # 大宗交易
│   ├── margin_trade.py         # 融资融券
│   ├── share_unlock.py         # 限售解禁
│   └── financial_data.py       # 财务数据（ROE/ROIC）

src/gugu/analysis/
├── altalpha_factors.py         # 新增：另类因子计算引擎
├── alpha_factory.py            # 修改：扩展接收另类因子
└── ic_analyzer.py              # 新增/修改：IC验证（如有则扩展）
```

### 3.2 数据流

```
akshare API → AltAlphaCollector（限流500ms+缓存）
    ↓
Parquet 持久化（data/altalpha/{date}_{type}.parquet）
    ↓
AltAlphaFactorEngine（计算因子值+横截面排名）
    ↓
AlphaFactory（与技术因子融合）
    ↓
MLFactorStrategy / 信号融合
```

### 3.3 AltAlphaCollector 基类设计

```python
class AltAlphaCollector(BaseCollector):
    """另类数据采集器基类"""

    # 所有子类共享的限流控制
    _last_call_ts: float = 0.0
    _min_interval: float = 0.5  # 500ms 限流
    _cache_ttl_hours: int = 12  # 默认缓存12小时

    async def _fetch_with_rate_limit(self, api_fn, *args, **kwargs):
        """带限流+缓存+降级的数据获取"""
        # 1. 先查 Parquet 缓存
        # 2. 限流：确保距上次调用 >= 500ms
        # 3. 调用 akshare API（asyncio.to_thread）
        # 4. 数据质量校验
        # 5. 写入 Parquet 缓存
        # 6. 失败时返回过期缓存（ignore_ttl=True）
```

### 3.4 因子计算逻辑

每个因子输出一个 **横截面 z-score**（-3 ~ +3），表示当前股票在全市场中的相对位置。

#### 因子1：insider_buy（高管增持）
```
输入：近30日高管增减持明细
计算：net_buy_amount = sum(变动股数 * 成交均价)
      insider_factor = net_buy_amount / 流通市值
输出：横截面 z-score
```

#### 因子2：earnings_surprise（业绩预告超预期）
```
输入：最新业绩预告
计算：forecast_growth = (预告净利润 - 上年净利润) / |上年净利润|
      surprise_factor = forecast_growth if 预告类型=预增 else -forecast_growth
输出：横截面 z-score（仅业绩季有值，非业绩季=0）
```

#### 因子3：lhb_inst_net（龙虎榜机构净买）
```
输入：近5日龙虎榜明细
计算：inst_net_5d = sum(机构席位净买额)
      lhb_factor = inst_net_5d / 流通市值
输出：横截面 z-score
```

#### 因子4：roic_quality（质量因子）
```
输入：最近4季度财务指标
计算：roe_avg = mean(近4季ROE)
      growth_avg = mean(近4季营收增长率)
      quality_factor = 0.6 * roe_avg + 0.4 * growth_avg
输出：横截面 z-score
```

#### 因子5：block_discount（大宗折价）
```
输入：近30日大宗交易明细
计算：avg_discount = mean((收盘价-成交价) / 收盘价)
      block_factor = -avg_discount  # 折价越大越看空（反向）
输出：横截面 z-score
```

#### 因子6：margin_sentiment（融资情绪）
```
输入：近5日融资余额
计算：margin_change = (今日融资余额 - 5日前融资余额) / 5日前融资余额
输出：横截面 z-score
```

#### 因子7：unlock_pressure（解禁压力）
```
输入：未来30日解禁计划
计算：unlock_ratio = 解禁市值 / 当前流通市值
      unlock_factor = -unlock_ratio  # 解禁压力大越看空（反向）
输出：横截面 z-score
```

#### 因子8：holder_concentrate（筹码集中度）
```
输入：最近2期股东户数
计算：holder_change = (最新户数 - 上期户数) / 上期户数
      concentrate_factor = -holder_change  # 户数减少=筹码集中=看多
输出：横截面 z-score
```

## 四、IC验证流程

### 4.1 验证方法

1. **数据准备**：对沪深300成分股，拉取2023-01-01至2025-12-31的因子值
2. **IC计算**：每交易日，因子值 vs 未来5日/10日收益率的相关系数
3. **有效性标准**：
   - |IC均值| > 0.03
   - IC_IR（IC均值/IC标准差）> 0.5
   - IC同向率 > 55%
4. **结果输出**：IC报告 + 因子衰减曲线

### 4.2 验证脚本

```bash
# IC验证（离线运行，不影响生产）
uv run python scripts/verify_altalpha_factors.py --start 2023-01-01 --end 2025-12-31
```

## 五、集成点

### 5.1 AlphaFactory 扩展

```python
# alpha_factory.py 新增方法
def compute_altalpha_factors(self, symbol: str, alt_data: dict) -> dict[str, float]:
    """计算另类因子，返回 z-score 字典"""
    factors = {}
    for name, computer in self._altalpha_computers.items():
        try:
            factors[name] = computer.compute(symbol, alt_data)
        except Exception:
            factors[name] = 0.0  # 因子缺失时为0（中性）
    return factors
```

### 5.2 MLFactorStrategy 扩展

将另类因子与技术因子一起送入 LightGBM，重新训练模型。
旧模型仅26个技术因子（OOS负收益），加入8个另类因子后预期改善。

### 5.3 调度集成

```yaml
# settings.yaml 新增
scheduler:
  tasks:
    altalpha_fetch: true   # 18:00 盘后批量采集另类数据
    altalpha_ic: false     # 周日03:00 IC验证（默认关闭）
```

## 六、限流与容错

| 措施 | 说明 |
|------|------|
| 500ms 限流 | 所有 akshare 调用间隔 ≥ 500ms |
| Parquet 缓存 | 12小时缓存，避免重复调用 |
| 过期缓存兜底 | API失败时返回上次缓存（即使过期） |
| 批量采集 | 18:00 盘后一次性批量获取，非盘中实时 |
| 降级策略 | akshare失败 → 东方财富push2直连 → 本地缓存 |

## 七、实施顺序

1. **基建**：AltAlphaCollector 基类 + Parquet缓存
2. **P0因子**：insider_buy + earnings_surprise + lhb_inst_net + roic_quality
3. **IC验证**：对P0因子跑IC分析，确认有效性
4. **P1因子**：block_discount + margin_sentiment + unlock_pressure + holder_concentrate
5. **集成**：接入AlphaFactory + MLFactorStrategy 重新训练
6. **调度**：添加盘后批量采集任务

## 八、风险

| 风险 | 应对 |
|------|------|
| akshare接口不稳定 | 限流+缓存+东方财富push2降级 |
| 因子IC不显著 | 先P0验证，无效则调整或放弃 |
| 数据缺失（部分股票无龙虎榜/大宗） | 缺失因子设为0（中性），不剔除股票 |
| 季度数据更新慢 | 财务因子用业绩预告/快报补位 |
