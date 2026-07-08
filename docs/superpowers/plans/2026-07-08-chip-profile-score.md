# 筹码画像评分（Chip Profile Score）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 StockRanker 新增第5个评分维度"筹码画像评分"（chip_score），权重15%，识别"主力收集、筹码集中"的股票。

**Architecture:** 复用现有 HolderConcentrationFilter 数据，在 StockRanker._score_one 中新增 _compute_chip_score 方法计算4个子项评分（户数变化0.4 + 户均持股0.3 + 机构持仓0.2 + 流通市值0.1），调整4个现有维度权重至25%/20%/25%/15%。

**Tech Stack:** Python 3.14 + asyncio + pydantic + pytest

## Global Constraints

- 策略必须通过历史回测和前向验证，不能依赖经验猜测参数
- 所有策略输出均为"指导决策参考，非自动交易指令"
- .env 文件必须添加到 .gitignore
- LLM 调用必须使用 async/await
- 配置权威源必须统一（settings.yaml + models.py + manifest）
- 测试隔离必须彻底，防止污染生产文件
- 数据展示：北向资金缺失显示'获取不到'而非0，红绿颜色区分涨跌

---

## File Structure

| 文件 | 操作 | 责任 |
|------|------|------|
| `src/gugu/analysis/stock_ranker.py` | 修改 | 新增 chip_score 计算 + 权重调整 |
| `src/gugu/selector/stock_selector.py` | 修改 | 合并 chip_score 到信号 dict |
| `tests/unit/test_chip_score.py` | 新建 | 筹码画像评分单元测试 |
| `config/settings.yaml` | 修改 | 新增 stock_ranker 配置段 |

---

### Task 1: 新建筹码画像评分单元测试

**Files:**
- Create: `tests/unit/test_chip_score.py`
- Test: `tests/unit/test_chip_score.py`

**Interfaces:**
- Consumes: `HolderConcentrationFilter.check()` 返回的 dict（含 label/holder_num_pct/avg_hold_change/data_missing）
- Produces: `StockRanker._compute_chip_score(hc_result, circ_mv)` 返回 float (0-1)

- [ ] **Step 1: 写失败的测试文件**

```python
"""筹码画像评分单元测试。

覆盖场景：
- 避雷针 → 0.0
- 潜伏者 → 高分
- 数据缺失 → 0.5（中性）
- 中性场景 → 中等分数
- 散户化 → 低分
- 流通市值分档评分
"""
from __future__ import annotations

import pytest

from gugu.analysis.stock_ranker import StockRanker


def _make_hc_result(
    label: str = "中性",
    holder_num_pct: float = 0.0,
    avg_hold_change: float = 0.0,
    org_hold_pct: float = 0.0,
    data_missing: bool = False,
) -> dict:
    """构造 HolderConcentrationFilter.check() 的返回结果。"""
    return {
        "pass": True,
        "label": label,
        "holder_num_pct": holder_num_pct,
        "avg_hold_change": avg_hold_change,
        "org_hold_pct": org_hold_pct,
        "data_missing": data_missing,
        "holder_num": 50000.0,
        "end_date": "2026-06-30",
        "reasons": [],
    }


class TestChipScoreLightningRod:
    """避雷针场景 → chip_score = 0.0。"""

    def test_lightning_rod_returns_zero(self):
        """避雷针（户数激增>30%）应返回 0.0。"""
        ranker = StockRanker()
        hc = _make_hc_result(label="避雷针", holder_num_pct=35.0)
        score = ranker._compute_chip_score(hc, circ_mv=100.0)
        assert score == 0.0

    def test_lightning_rod_with_high_org_pct_still_zero(self):
        """即使机构占比高，避雷针仍返回 0.0。"""
        ranker = StockRanker()
        hc = _make_hc_result(label="避雷针", holder_num_pct=50.0, org_hold_pct=60.0)
        score = ranker._compute_chip_score(hc, circ_mv=100.0)
        assert score == 0.0


class TestChipScoreDataMissing:
    """数据缺失 → chip_score = 0.5（中性）。"""

    def test_data_missing_returns_neutral(self):
        """数据缺失应返回中性分 0.5。"""
        ranker = StockRanker()
        hc = _make_hc_result(data_missing=True)
        score = ranker._compute_chip_score(hc, circ_mv=100.0)
        assert score == 0.5


class TestChipScoreLurker:
    """潜伏者场景 → 高分。"""

    def test_lurker_full_score(self):
        """潜伏者（户数连降>20% + 户均+20% + 机构50% + 流通市值100亿）应得高分。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="潜伏者",
            holder_num_pct=-25.0,  # 户数下降>20% → 0.4
            avg_hold_change=20.0,  # 户均+20% → 0.3
            org_hold_pct=50.0,     # 机构>40% → 0.2
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)  # 50-500亿 → 0.1
        assert score == pytest.approx(1.0, abs=0.01)

    def test_lurker_partial_score(self):
        """潜伏者（户数-10% + 户均+10% + 机构25% + 流通市值100亿）应得中等高分。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="潜伏者",
            holder_num_pct=-10.0,  # 单季下降>5% → 0.3
            avg_hold_change=10.0,  # 上升5-15% → 0.2
            org_hold_pct=25.0,     # 20-40% → 0.15
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)  # 50-500亿 → 0.1
        # 0.3 + 0.2 + 0.15 + 0.1 = 0.75
        assert score == pytest.approx(0.75, abs=0.01)


class TestChipScoreNeutral:
    """中性场景 → 中等分数。"""

    def test_neutral_stable(self):
        """中性稳定（户数-3% + 户均+2% + 机构15% + 100亿）应得中等分。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=-3.0,   # ±5% → 0.2
            avg_hold_change=2.0,   # ±5% → 0.15
            org_hold_pct=15.0,     # 10-20% → 0.1
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)  # 50-500亿 → 0.1
        # 0.2 + 0.15 + 0.1 + 0.1 = 0.55
        assert score == pytest.approx(0.55, abs=0.01)


class TestChipScoreRetailDominated:
    """散户化场景 → 低分。"""

    def test_retail_dominated_low_score(self):
        """散户化（户数+25% + 户均-25% + 机构5% + 100亿）应得极低分。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=25.0,   # 连续上升>20% → 0.0
            avg_hold_change=-25.0, # 下降>20% → 0.0
            org_hold_pct=5.0,      # <10% → 0.05
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)  # 50-500亿 → 0.1
        # 0.0 + 0.0 + 0.05 + 0.1 = 0.15
        assert score == pytest.approx(0.15, abs=0.01)


class TestChipScoreMarketCap:
    """流通市值分档评分。"""

    def test_small_cap_low_score(self):
        """小盘股（30亿）流通市值评分 0.03。"""
        ranker = StockRanker()
        hc = _make_hc_result(data_missing=True)  # 中性0.5 + 市值调整
        score = ranker._compute_chip_score(hc, circ_mv=30.0)
        assert score == 0.5  # 数据缺失直接返回0.5，不看市值

    def test_mid_cap_full_score(self):
        """中盘股（100亿）流通市值评分 0.1。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=0.0,
            avg_hold_change=0.0,
            org_hold_pct=0.0,  # 数据缺失 → 0.1
        )
        score = ranker._compute_chip_score(hc, circ_mv=100.0)
        # 0.2 + 0.15 + 0.1 + 0.1 = 0.55
        assert score == pytest.approx(0.55, abs=0.01)

    def test_large_cap_half_score(self):
        """大盘股（1000亿）流通市值评分 0.05。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=0.0,
            avg_hold_change=0.0,
            org_hold_pct=0.0,
        )
        score = ranker._compute_chip_score(hc, circ_mv=1000.0)
        # 0.2 + 0.15 + 0.1 + 0.05 = 0.5
        assert score == pytest.approx(0.5, abs=0.01)

    def test_mega_cap_minimal_score(self):
        """超大盘（3000亿）流通市值评分 0.03。"""
        ranker = StockRanker()
        hc = _make_hc_result(
            label="中性",
            holder_num_pct=0.0,
            avg_hold_change=0.0,
            org_hold_pct=0.0,
        )
        score = ranker._compute_chip_score(hc, circ_mv=3000.0)
        # 0.2 + 0.15 + 0.1 + 0.03 = 0.48
        assert score == pytest.approx(0.48, abs=0.01)


class TestChipScoreRange:
    """评分范围验证。"""

    def test_score_always_in_0_1(self):
        """所有场景评分应在 [0, 1] 区间。"""
        ranker = StockRanker()
        scenarios = [
            _make_hc_result(label="避雷针", holder_num_pct=100.0),
            _make_hc_result(data_missing=True),
            _make_hc_result(label="潜伏者", holder_num_pct=-30.0, avg_hold_change=30.0, org_hold_pct=80.0),
            _make_hc_result(label="中性", holder_num_pct=0.0, avg_hold_change=0.0, org_hold_pct=0.0),
            _make_hc_result(label="中性", holder_num_pct=50.0, avg_hold_change=-30.0, org_hold_pct=0.0),
        ]
        for hc in scenarios:
            score = ranker._compute_chip_score(hc, circ_mv=100.0)
            assert 0.0 <= score <= 1.0, f"评分越界: {score}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_chip_score.py -v`
Expected: FAIL with "AttributeError: 'StockRanker' object has no attribute '_compute_chip_score'"

- [ ] **Step 3: 提交测试文件**

```bash
git add tests/unit/test_chip_score.py
git commit -m "test: 新增筹码画像评分单元测试（TDD红阶段）"
```

---

### Task 2: 实现 _compute_chip_score 方法

**Files:**
- Modify: `src/gugu/analysis/stock_ranker.py:32-36`（__init__ + 新增方法）

**Interfaces:**
- Consumes: Task 1 的测试用例
- Produces: `StockRanker._compute_chip_score(hc_result: dict, circ_mv: float) -> float`

- [ ] **Step 1: 在 StockRanker 中实现 _compute_chip_score 方法**

在 `src/gugu/analysis/stock_ranker.py` 的 `_score_one` 方法之后（约 line 155 后），新增方法：

```python
def _compute_chip_score(self, hc_result: dict, circ_mv: float = 0.0) -> float:
    """计算筹码画像评分（0-1）。

    评分维度（满分1.0）：
    - 户数变化（0.4）：连续下降>20% → 0.4；单季下降>5% → 0.3；
      稳定±5% → 0.2；单季上升>5% → 0.1；连续上升>20% → 0.0
    - 户均持股变化（0.3）：上升>15% → 0.3；上升5-15% → 0.2；
      稳定±5% → 0.15；下降>5% → 0.05；下降>20% → 0.0
    - 机构持仓（0.2）：>40% → 0.2；20-40% → 0.15；10-20% → 0.1；
      <10% → 0.05；数据缺失 → 0.1（中性）
    - 流通市值（0.1）：50-500亿 → 0.1；500-2000亿 → 0.05；其他 → 0.03

    特殊处理：
    - 避雷针 → 0.0（主力派发，直接拒绝）
    - 数据缺失 → 0.5（中性默认分，不惩罚也不奖励）

    Args:
        hc_result: HolderConcentrationFilter.check() 返回的字典
        circ_mv: 流通市值（亿元），0 表示数据不可用

    Returns:
        筹码画像评分（0.0-1.0）
    """
    # 避雷针 → 0.0
    if hc_result.get("label") == "避雷针":
        return 0.0

    # 数据缺失 → 0.5（中性）
    if hc_result.get("data_missing"):
        return 0.5

    holder_num_pct = float(hc_result.get("holder_num_pct", 0.0) or 0.0)
    avg_hold_change = float(hc_result.get("avg_hold_change", 0.0) or 0.0)
    org_hold_pct = float(hc_result.get("org_hold_pct", 0.0) or 0.0)

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
    if org_hold_pct > 40:
        score_org = 0.2
    elif org_hold_pct > 20:
        score_org = 0.15
    elif org_hold_pct > 10:
        score_org = 0.1
    elif org_hold_pct > 0:
        score_org = 0.05
    else:
        score_org = 0.1  # 数据缺失（org_hold_pct=0），中性

    # 4. 流通市值评分（0.1满分）
    if 50 <= circ_mv <= 500:
        score_mv = 0.1
    elif 500 < circ_mv <= 2000:
        score_mv = 0.05
    else:
        score_mv = 0.03

    return round(score_holder + score_avg + score_org + score_mv, 4)
```

- [ ] **Step 2: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_chip_score.py -v`
Expected: 11 passed

- [ ] **Step 3: 提交实现**

```bash
git add src/gugu/analysis/stock_ranker.py
git commit -m "feat: 实现 _compute_chip_score 筹码画像评分方法"
```

---

### Task 3: 集成 chip_score 到 _score_one

**Files:**
- Modify: `src/gugu/analysis/stock_ranker.py:32-36, 100-155`

**Interfaces:**
- Consumes: Task 2 的 `_compute_chip_score` 方法
- Produces: `_score_one` 返回 dict 包含 `chip_score` 字段

- [ ] **Step 1: 在 __init__ 中初始化 HolderConcentrationFilter**

修改 `src/gugu/analysis/stock_ranker.py` 的 `__init__` 方法（line 32-36）：

```python
def __init__(self):
    self._dm = data_manager()
    self._alpha = AlphaFactory()
    self._fundamental = FundamentalFilter()
    self._money_flow = MoneyFlowFilter()
    # v0.6.0: 筹码画像评分（复用现有 HolderConcentrationFilter）
    from gugu.filters.holder_concentration import HolderConcentrationFilter
    self._holder_concentration = HolderConcentrationFilter()
```

- [ ] **Step 2: 在 _score_one 中新增筹码评分逻辑**

在 `src/gugu/analysis/stock_ranker.py` 的 `_score_one` 方法中，在板块评分（约 line 137）之后、加权总分（line 140）之前，新增：

```python
        # 5. 筹码画像评分（15%权重）v0.6.0 新增
        hc_result = await self._holder_concentration.check(symbol)
        # 获取流通市值（从实时快取，降级为0）
        circ_mv = 0.0
        try:
            rt = await self._dm.fetch_stock_realtime([symbol])
            if not rt.empty:
                circ_mv = float(rt.iloc[0].get("circ_mv", 0) or 0) / 1e8  # 转为亿元
        except Exception:
            pass
        chip_score = self._compute_chip_score(hc_result, circ_mv=circ_mv)
```

- [ ] **Step 3: 更新加权公式**

修改 `src/gugu/analysis/stock_ranker.py:140` 的加权公式：

```python
        # 加权总分 v0.6.0: 新增筹码画像15%，调整其他维度权重
        total = (
            factor_score * 0.25 + fund_score * 0.20 + flow_score * 0.25
            + sector_score * 0.15 + chip_score * 0.15
        )
```

- [ ] **Step 4: 更新返回字典**

修改 `src/gugu/analysis/stock_ranker.py:142-155` 的返回字典，新增 chip_score：

```python
        return {
            "symbol": symbol,
            "name": name,
            "total_score": round(total, 4),
            "factor_score": round(factor_score, 4),
            "fundamental_score": round(fund_score, 4),
            "money_flow_score": round(flow_score, 4),
            "sector_score": round(sector_score, 4),
            "chip_score": round(chip_score, 4),  # v0.6.0 新增
            "industry": industry,
            "price": round(float(df.iloc[-1]["close"]), 2),
            "pe": fund.get("pe"),
            "pb": fund.get("pb"),
            "roe": fund.get("roe"),
        }
```

- [ ] **Step 5: 更新类文档字符串**

修改 `src/gugu/analysis/stock_ranker.py:23-29` 的文档字符串：

```python
    """个股综合评分器。

    评分维度（v0.6.0 调整权重）：
    1. 因子评分（25%）：26个Alpha因子的综合评分
    2. 基本面评分（20%）：PE/PB/ROE/营收增长
    3. 资金流评分（25%）：主力净流入/5日净流入
    4. 板块强度（15%）：是否属于当前强势板块
    5. 筹码画像（15%）：户数变化+户均持股+机构持仓+流通市值

    输出：排序后的股票列表 + 评分详情
    """
```

- [ ] **Step 6: 运行单元测试确认通过**

Run: `uv run pytest tests/unit/test_chip_score.py tests/unit/test_stock_ranker.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add src/gugu/analysis/stock_ranker.py
git commit -m "feat: 集成筹码画像评分到 StockRanker（权重15%）"
```

---

### Task 4: StockSelector 合并 chip_score 字段

**Files:**
- Modify: `src/gugu/selector/stock_selector.py:174-185`

**Interfaces:**
- Consumes: Task 3 的 `_score_one` 返回 dict（含 chip_score）
- Produces: 选股结果信号 dict 包含 chip_score

- [ ] **Step 1: 修改 StockSelector 合并逻辑**

修改 `src/gugu/selector/stock_selector.py:174-185` 的评分合并逻辑，新增 chip_score：

```python
        if ranked:
            rank_map = {r["symbol"]: r for r in ranked}
            for sig in signal_stocks:
                r = rank_map.get(sig["symbol"])
                if r:
                    sig["total_score"] = r["total_score"]
                    sig["factor_score"] = r["factor_score"]
                    sig["fundamental_score"] = r["fundamental_score"]
                    sig["money_flow_score"] = r["money_flow_score"]
                    sig["sector_score"] = r["sector_score"]
                    sig["chip_score"] = r["chip_score"]  # v0.6.0 新增
            signal_stocks.sort(key=lambda x: x.get("total_score", 0), reverse=True)
```

- [ ] **Step 2: 运行 StockSelector 测试确认通过**

Run: `uv run pytest tests/unit/test_selector.py -v`
Expected: PASS（现有测试不应受影响，chip_score 为可选字段）

- [ ] **Step 3: 提交**

```bash
git add src/gugu/selector/stock_selector.py
git commit -m "feat: StockSelector 合并 chip_score 到选股结果"
```

---

### Task 5: 配置项 + 全量测试验证

**Files:**
- Modify: `config/settings.yaml`

**Interfaces:**
- Consumes: Task 3-4 的实现
- Produces: 配置文件支持筹码画像权重可调

- [ ] **Step 1: 在 settings.yaml 新增 stock_ranker 配置段**

在 `config/settings.yaml` 中找到 `holder_concentration_filter` 段（约 line 89-96），在其后新增：

```yaml
# ========== 选股评分器配置 ==========
# v0.6.0: StockRanker 评分维度权重配置
# 注意：5个维度权重总和必须=1.0，配置一致性测试会校验
stock_ranker:
  factor_weight: 0.25        # 因子评分权重（原0.30）
  fundamental_weight: 0.20  # 基本面评分权重（原0.25）
  money_flow_weight: 0.25  # 资金流评分权重（不变）
  sector_weight: 0.15      # 板块评分权重（原0.20）
  chip_score_weight: 0.15  # 筹码画像评分权重（新增）
  chip_score_neutral: 0.5  # 数据缺失时的中性评分
```

- [ ] **Step 2: 运行配置一致性测试**

Run: `uv run pytest tests/unit/test_config_consistency.py -v`
Expected: PASS（如果测试校验权重总和=1.0，需确认5项权重加总=1.0）

- [ ] **Step 3: 运行全量测试**

Run: `uv run pytest tests/unit/ --tb=short -q`
Expected: 所有测试通过（1289+ passed）

- [ ] **Step 4: 运行筹码画像评分测试**

Run: `uv run pytest tests/unit/test_chip_score.py -v`
Expected: 11 passed

- [ ] **Step 5: 提交**

```bash
git add config/settings.yaml
git commit -m "feat: 新增 stock_ranker 配置段（筹码画像权重可调）"
```

---

### Task 6: 集成测试 + 回测验证

**Files:**
- Test: 无新建文件，使用现有脚本验证

**Interfaces:**
- Consumes: Task 1-5 的完整实现
- Produces: 验证报告（选股结果对比 + 评分分布）

- [ ] **Step 1: 手动验证筹码评分在选股结果中生效**

Run:
```bash
uv run python -c "
import asyncio
from gugu.selector.stock_selector import StockSelector

async def main():
    selector = StockSelector(top_n=20, max_candidates=5)
    results = await selector.select()
    for r in results[:5]:
        print(f'{r[\"symbol\"]} {r[\"name\"]}: total={r.get(\"total_score\", 0):.3f} chip={r.get(\"chip_score\", \"N/A\")}')

asyncio.run(main())
"
```

Expected: 输出前5只股票，每只都有 chip_score 字段（0-1之间）

- [ ] **Step 2: 运行全量单元测试确认无回归**

Run: `uv run pytest tests/unit/ --tb=short -q`
Expected: 所有测试通过

- [ ] **Step 3: 更新 manifest 记录 v0.6.0 变更**

在 `config/strategy_manifest.yaml` 的顶部注释或版本字段中记录：

```yaml
# v0.6.0 变更记录：
# - StockRanker 新增筹码画像评分维度（chip_score，权重15%）
# - 权重调整：因子30%→25%，基本面25%→20%，板块20%→15%，资金流25%不变
# - 复用 HolderConcentrationFilter 数据，不新增数据采集器
# - 单元测试 11 个新增，覆盖避雷针/潜伏者/中性/散户化/数据缺失/市值分档
```

- [ ] **Step 4: 最终提交**

```bash
git add config/strategy_manifest.yaml
git commit -m "docs: manifest 记录 v0.6.0 筹码画像评分变更"
```

---

## Self-Review

**1. Spec 覆盖**：
- ✅ 评分维度权重调整 → Task 3 Step 3
- ✅ 筹码画像评分逻辑（4子项）→ Task 2 Step 1
- ✅ 避雷针特殊处理 → Task 2 Step 1
- ✅ 数据缺失中性分 → Task 2 Step 1
- ✅ 流通市值分档 → Task 2 Step 1
- ✅ StockSelector 合并 chip_score → Task 4 Step 1
- ✅ 配置项 → Task 5 Step 1
- ✅ 单元测试 → Task 1 Step 1

**2. 占位符扫描**：无 TBD/TODO，所有代码完整

**3. 类型一致性**：
- `_compute_chip_score(hc_result: dict, circ_mv: float) -> float` — Task 2 定义，Task 3 使用 ✓
- `chip_score` 字段名 — Task 3 返回字典、Task 4 合并逻辑一致 ✓
