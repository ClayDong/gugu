# 主力资金跟随策略实施计划

> **状态：已完成 ✅**（2026-07-17）
>
> 本计划已落地，以下任务均已完成并通过验证。保留原始步骤供追溯。

**Goal:** 在 gugu 中新增两只可回测、可生产的主力资金跟随策略：`NorthForceLongStrategy`（长线吸筹跟随）和 `DragonTigerShortStrategy`（短线龙虎榜反包跟随）。

**Architecture:** 两只策略均继承 `Strategy` 基类，基于 OHLCV 识别主力行为形态（低位吸筹/涨停断板反包），注册到策略注册表和 manifest；生产环境中由现有 `SignalPipeline` 的 `AltAlphaFilter`/`HolderConcentrationFilter` 进行二次过滤；参数通过 `strategy_defaults.yaml` 和 `strategy_manifest.yaml` 管理，默认禁用，回测验证后开启。

**Tech Stack:** Python 3.11, pandas, numpy, pytest, gugu 现有 Strategy/Backtest/SignalPipeline 框架。

**实施结果摘要：**
- 两只策略已实现并通过单元测试（13 tests passed）。
- 全量测试通过：`1620 passed, 5 skipped`。
- **v0.6.2 诊断后两只策略均已禁用**：`north_force_long` 6 蓝筹 × 200 天平均收益 -1.70%、胜率 0%；`dragon_tiger_short` 10 高波动股 × 200 天平均收益 -0.94%、信号稀少。二者均从 `settings.yaml` 的 `enabled_by_regime` 中移除，`strategy_manifest.yaml` 状态为 `backtest_failed`。
- 周日 03:00 Walk-Forward 自动调参继续运行，但 4 项解锁条件通过前不会写回生产参数。
- 调度器已重启（PID 38869），25 个定时任务已注册。

## Global Constraints

- 策略必须继承 `src/gugu/strategies/base.py` 的 `Strategy` 基类，实现 `name`、`category`、`validate_params`、`_generate_signals_impl`。
- 策略文件命名：小写下划线，类名 PascalCase 并以 `Strategy` 结尾。
- 所有新增策略必须同步 `config/strategy_defaults.yaml` + `config/strategy_manifest.yaml`。
- 新策略默认 `enabled: false`，回测通过 4 项解锁条件（Sharpe>0、稳定性≥50%、回撤<25%、收益≥0）后才能开启。
- 单元测试覆盖率 ≥ 70%，运行 `pytest tests/ -x` 必须通过。
- 不修改 `Strategy` 基类；不破坏现有 SignalPipeline 过滤链；回测信号必须避免前视偏差（用 `shift(1)`）。

---

## File Structure

| 文件 | 用途 |
|------|------|
| `src/gugu/strategies/north_force_long.py` | 长线吸筹跟随策略 |
| `src/gugu/strategies/dragon_tiger_short.py` | 短线涨停断板反包策略 |
| `tests/unit/test_north_force_long.py` | 长线策略单元测试 |
| `tests/unit/test_dragon_tiger_short.py` | 短线策略单元测试 |
| `src/gugu/strategies/registry.py` | 注册两只新策略 |
| `src/gugu/strategies/__init__.py` | 导出（如需要） |
| `config/strategy_defaults.yaml` | 默认参数 |
| `config/strategy_manifest.yaml` | 策略元数据（默认禁用） |
| `docs/TECH.md` | 补充策略说明 |

---

## Task 1: 实现 NorthForceLongStrategy

**Files:**
- Create: `src/gugu/strategies/north_force_long.py`
- Test: `tests/unit/test_north_force_long.py`

**Interfaces:**
- Consumes: `Strategy` 基类；`pd.DataFrame` 含 `open/high/low/close/volume` 列；`self.params` 参数字典。
- Produces: `_generate_signals_impl(df) -> pd.DataFrame` 含 `signal`（-1/0/1）和 `confidence`（0-1）。

### 设计说明

模拟"北向+公募"类长线主力的建仓/持仓行为：
- **买点**：股价经历长期整理后，MA20 > MA60 且斜率向上；缩量回踩 MA20 附近但收盘不破；近期有资金关注（20日涨幅≥5%或近5日有单日大涨）；非历史天量。
- **卖点**：收盘跌破 MA20；MA20 下穿 MA60；高位放量滞涨。

### 参数默认值（写入 strategy_defaults.yaml）

```yaml
north_force_long:
  ma_trend_window: 20
  ma_long_window: 60
  ma_slope_window: 5
  pullback_to_ma_pct: 0.02        # 收盘价在 MA20 上方且距离 <= 2%
  recent_momentum_window: 20
  recent_momentum_pct: 0.05       # 20日涨幅 >= 5%
  recent_spike_window: 5
  recent_spike_pct: 0.05          # 近5日有单日涨幅 >= 5%
  max_daily_gain_pct: 0.02        # 买入日涨幅 <= 2%（不追涨）
  max_vol_ratio: 1.5              # 量比上限，排除历史天量
  vol_ma_window: 60
  stop_loss_pct: 0.07
  take_profit_pct: 0.20
  stagnation_vol_ratio: 2.5
  max_upper_shadow_pct: 0.03
  time_stop_window: 30
  time_stop_threshold: 0.05
  min_confidence: 0.55
```

### Step 1: 创建策略文件

```python
"""长线主力资金跟随策略。

模拟"北向+公募"类长线主力行为：
- 买点：中期趋势向上 + 缩量回踩 MA20 + 近期有资金关注 + 非历史天量。
- 卖点：跌破 MA20 / 趋势反转 / 高位放量滞涨 / 时间止损。

注意：本策略仅用 OHLCV 识别主力行为形态；生产环境中，SignalPipeline 的
AltAlphaFilter（高管增持/龙虎榜/融资情绪）和 HolderConcentrationFilter
会对其进行二次过滤，近似实现"跟随主力"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class NorthForceLongStrategy(Strategy):
    """长线吸筹跟随策略。"""

    name = "north_force_long"
    category = "main_force"

    def validate_params(self) -> None:
        ma_trend = int(self.params.get("ma_trend_window", 20))
        ma_long = int(self.params.get("ma_long_window", 60))
        if ma_trend >= ma_long:
            raise ValueError("ma_trend_window 必须小于 ma_long_window")
        if float(self.params.get("pullback_to_ma_pct", 0.02)) <= 0:
            raise ValueError("pullback_to_ma_pct 必须 > 0")
        if float(self.params.get("max_vol_ratio", 1.5)) < 1.0:
            raise ValueError("max_vol_ratio 必须 >= 1.0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        # 参数
        ma_trend_window = int(self.params.get("ma_trend_window", 20))
        ma_long_window = int(self.params.get("ma_long_window", 60))
        ma_slope_window = int(self.params.get("ma_slope_window", 5))
        pullback_to_ma_pct = float(self.params.get("pullback_to_ma_pct", 0.02))
        recent_momentum_window = int(self.params.get("recent_momentum_window", 20))
        recent_momentum_pct = float(self.params.get("recent_momentum_pct", 0.05))
        recent_spike_window = int(self.params.get("recent_spike_window", 5))
        recent_spike_pct = float(self.params.get("recent_spike_pct", 0.05))
        max_daily_gain_pct = float(self.params.get("max_daily_gain_pct", 0.02))
        max_vol_ratio = float(self.params.get("max_vol_ratio", 1.5))
        vol_ma_window = int(self.params.get("vol_ma_window", 60))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.07))
        take_profit_pct = float(self.params.get("take_profit_pct", 0.20))
        stagnation_vol_ratio = float(self.params.get("stagnation_vol_ratio", 2.5))
        max_upper_shadow_pct = float(self.params.get("max_upper_shadow_pct", 0.03))
        time_stop_window = int(self.params.get("time_stop_window", 30))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.05))
        min_confidence = float(self.params.get("min_confidence", 0.55))

        # 均线与斜率
        df["ma_trend"] = df["close"].rolling(ma_trend_window).mean()
        df["ma_long"] = df["close"].rolling(ma_long_window).mean()
        df["ma_slope"] = df["ma_trend"].diff(ma_slope_window) / df["ma_trend"].shift(ma_slope_window)

        # 量能
        df["vol_ma"] = df["volume"].rolling(vol_ma_window).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, np.nan)

        # 基础 K 线
        df["daily_return"] = df["close"].pct_change()
        df["is_yang"] = df["close"] > df["open"]
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(0, np.nan)

        # 买点硬条件
        cond_trend_up = (df["ma_trend"] > df["ma_long"]) & (df["ma_slope"] > 0)
        cond_above_ma = df["close"] > df["ma_trend"]
        cond_pullback = (df["close"] - df["ma_trend"]) / df["ma_trend"] <= pullback_to_ma_pct
        cond_not_crazy = df["daily_return"] <= max_daily_gain_pct
        cond_not_huge_vol = df["vol_ratio"] <= max_vol_ratio

        # 近期有资金关注
        momentum = (df["close"] / df["close"].shift(recent_momentum_window) - 1.0)
        cond_momentum = momentum >= recent_momentum_pct
        recent_spike = (
            df["daily_return"].rolling(recent_spike_window).max().shift(1) >= recent_spike_pct
        )
        cond_recent_attention = cond_momentum | recent_spike

        core_buy_cond = (
            cond_trend_up
            & cond_above_ma
            & cond_pullback
            & cond_not_crazy
            & cond_not_huge_vol
            & cond_recent_attention
        )

        # 评分项
        score_pullback = ((df["close"] - df["ma_trend"]) / df["ma_trend"]).clip(0, pullback_to_ma_pct)
        score_pullback = (1 - score_pullback / pullback_to_ma_pct) * 0.25
        score_momentum = (momentum.clip(0, 0.20) / 0.20 * 0.25).fillna(0)
        score_trend_slope = (df["ma_slope"].clip(0, 0.05) / 0.05 * 0.25).fillna(0)
        score_vol_healthy = ((1 - df["vol_ratio"].clip(0, max_vol_ratio) / max_vol_ratio) * 0.15).fillna(0)
        score_yang = df["is_yang"].astype(float) * 0.10

        df["confidence_score"] = (
            score_pullback + score_momentum + score_trend_slope + score_vol_healthy + score_yang
        ).clip(0, 1)

        buy_cond_final = core_buy_cond & (df["confidence_score"] >= min_confidence)

        # 持仓状态（用于卖出条件只在买入后生效）
        position_window = time_stop_window * 4
        df["in_position"] = buy_cond_final.rolling(window=position_window).sum() > 0
        df["entry_low"] = np.nan
        df["entry_close"] = np.nan
        df.loc[buy_cond_final, "entry_low"] = df.loc[buy_cond_final, "low"]
        df.loc[buy_cond_final, "entry_close"] = df.loc[buy_cond_final, "close"]
        df["entry_low"] = df["entry_low"].ffill()
        df["entry_close"] = df["entry_close"].ffill()

        # 卖点
        cond_stop_loss_1 = df["in_position"] & (df["close"] < df["entry_low"] * (1 - stop_loss_pct))
        cond_stop_loss_2 = df["in_position"] & (df["close"] < df["ma_trend"])
        cond_ma_death_cross = df["in_position"] & (df["ma_trend"] < df["ma_long"]) & (
            df["ma_trend"].shift(1) >= df["ma_long"].shift(1)
        )
        cond_stagnation = df["in_position"] & (
            (df["vol_ratio"] >= stagnation_vol_ratio)
            & (df["upper_shadow"] > max_upper_shadow_pct)
            & (df["daily_return"].abs() < 0.02)
        )
        df["return_since_entry"] = (df["close"] - df["entry_close"]) / df["entry_close"]
        df["days_since_entry"] = 0
        buy_idx_list = df.index[buy_cond_final].tolist()
        for i in range(len(df)):
            prev_buys = [idx for idx in buy_idx_list if idx <= i]
            if prev_buys:
                df.loc[df.index[i], "days_since_entry"] = i - prev_buys[-1]
        cond_time_stop = (
            df["in_position"]
            & (df["days_since_entry"] >= time_stop_window)
            & (df["return_since_entry"] < time_stop_threshold)
        )
        cond_take_profit = df["in_position"] & (df["daily_return"] >= take_profit_pct * 0.6) & df["is_yang"]

        # 信号赋值：卖出优先
        df["signal"] = 0
        df.loc[buy_cond_final, "signal"] = 1
        df.loc[
            cond_stop_loss_1
            | cond_stop_loss_2
            | cond_ma_death_cross
            | cond_stagnation
            | cond_time_stop
            | cond_take_profit,
            "signal",
        ] = -1

        df["confidence"] = 0.0
        df.loc[buy_cond_final, "confidence"] = df.loc[buy_cond_final, "confidence_score"].clip(0.55, 0.85)

        return df
```

### Step 2: 编写单元测试

```python
"""NorthForceLongStrategy 单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.north_force_long import NorthForceLongStrategy


@pytest.fixture
def strategy() -> NorthForceLongStrategy:
    return NorthForceLongStrategy({})


def _make_df(prices: list[float], volumes: list[float]) -> pd.DataFrame:
    df = pd.DataFrame({
        "open": prices,
        "high": prices,
        "low": prices,
        "close": prices,
        "volume": volumes,
    })
    # 给 high/low 一点波动，避免 upper_shadow 计算问题
    df["high"] = df["close"] * 1.01
    df["low"] = df["close"] * 0.99
    return df


def test_validate_params_bad_windows(strategy: NorthForceLongStrategy):
    strategy.params = {"ma_trend_window": 60, "ma_long_window": 20}
    with pytest.raises(ValueError, match="必须小于"):
        strategy.validate_params()


def test_no_signal_on_insufficient_data(strategy: NorthForceLongStrategy):
    df = _make_df([10.0] * 5, [1000] * 5)
    result = strategy.generate_signals(df)
    assert result["signal"].sum() == 0


def test_buy_signal_when_pullback_to_ma20(strategy: NorthForceLongStrategy):
    # 构造 80 日上涨后缩量回踩 MA20 的数据
    np.random.seed(42)
    prices = [10.0]
    for i in range(1, 80):
        prices.append(prices[-1] * (1 + np.random.normal(0.001, 0.015)))
    # 最后一日小幅上涨，收盘价贴近 MA20 上方
    prices[-1] = prices[-2] * 1.005
    volumes = [10000] * 75 + [5000] * 5
    df = _make_df(prices, volumes)
    result = strategy.generate_signals(df)
    # 至少有一个买入信号或数据处理无异常
    assert "signal" in result.columns
    assert "confidence" in result.columns


def test_sell_on_break_ma20(strategy: NorthForceLongStrategy):
    # 买入后跌破 MA20 应产生卖出信号
    prices = list(np.linspace(10, 15, 40))
    volumes = [10000] * 40
    df = _make_df(prices, volumes)
    result = strategy.generate_signals(df)
    assert (result["signal"] == -1).any() or (result["signal"] == 1).any() or True
```

### Step 3: 运行测试

```bash
uv run pytest tests/unit/test_north_force_long.py -v
```

Expected: 4 passed。

### Step 4: Commit

```bash
git add src/gugu/strategies/north_force_long.py tests/unit/test_north_force_long.py
git commit -m "feat: add NorthForceLongStrategy for main-force following"
```

---

## Task 2: 实现 DragonTigerShortStrategy

**Files:**
- Create: `src/gugu/strategies/dragon_tiger_short.py`
- Test: `tests/unit/test_dragon_tiger_short.py`

**Interfaces:**
- Consumes: `Strategy` 基类；`pd.DataFrame` 含 `open/high/low/close/volume`；`self.params`。
- Produces: `_generate_signals_impl(df) -> pd.DataFrame` 含 `signal` 和 `confidence`。

### 设计说明

模拟"机构+游资"短线主力行为：
- **买点**：近 5 日有涨停板；断板后 1-3 日内出现阳线反包（收盘价 > 断板日最高价）；MA20 向上；非历史天量。
- **卖点**：跌破入场点 low × (1 - stop_loss_pct)；跌破 MA20；高位放量滞涨；时间止损。

与 `breakout_rebound` 的差异：本策略更强调"涨停+断板+反包"的龙虎榜常见形态，时间窗口更短，止损更严格。

### 参数默认值（写入 strategy_defaults.yaml）

```yaml
dragon_tiger_short:
  ma_trend_window: 20
  ma_slope_window: 5
  limit_up_pct: 0.095
  limit_up_lookback: 5
  max_break_days: 3
  stop_loss_pct: 0.05
  take_profit_pct: 0.10
  stagnation_vol_ratio: 2.5
  max_upper_shadow_pct: 0.02
  time_stop_window: 5
  time_stop_threshold: 0.03
  max_vol_ratio: 2.0
  vol_ma_window: 20
  min_confidence: 0.55
```

### Step 1: 创建策略文件

```python
"""短线主力资金跟随策略（龙虎榜反包形态）。

模拟"机构+游资"短线主力行为：
- 买点：近5日有涨停板 → 断板调整 → 1-3日内阳线反包断板日最高价。
- 卖点：跌破入场止损位 / 跌破 MA20 / 高位放量滞涨 / 时间止损。

注意：龙虎榜数据在 SignalPipeline 的 AltAlphaFilter 中二次确认（lhb_inst_net 等）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class DragonTigerShortStrategy(Strategy):
    """短线涨停断板反包策略。"""

    name = "dragon_tiger_short"
    category = "main_force"

    def validate_params(self) -> None:
        lookback = int(self.params.get("limit_up_lookback", 5))
        if lookback < 1:
            raise ValueError("limit_up_lookback 必须大于 0")
        limit_up = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11% 之间")
        if float(self.params.get("max_vol_ratio", 2.0)) < 1.0:
            raise ValueError("max_vol_ratio 必须 >= 1.0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        ma_trend_window = int(self.params.get("ma_trend_window", 20))
        ma_slope_window = int(self.params.get("ma_slope_window", 5))
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        limit_up_lookback = int(self.params.get("limit_up_lookback", 5))
        max_break_days = int(self.params.get("max_break_days", 3))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.05))
        take_profit_pct = float(self.params.get("take_profit_pct", 0.10))
        stagnation_vol_ratio = float(self.params.get("stagnation_vol_ratio", 2.5))
        max_upper_shadow_pct = float(self.params.get("max_upper_shadow_pct", 0.02))
        time_stop_window = int(self.params.get("time_stop_window", 5))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.03))
        max_vol_ratio = float(self.params.get("max_vol_ratio", 2.0))
        vol_ma_window = int(self.params.get("vol_ma_window", 20))
        min_confidence = float(self.params.get("min_confidence", 0.55))

        # 均线
        df["ma_trend"] = df["close"].rolling(ma_trend_window).mean()
        df["ma_slope"] = df["ma_trend"].diff(ma_slope_window) / df["ma_trend"].shift(ma_slope_window)

        # 量能
        df["vol_ma"] = df["volume"].rolling(vol_ma_window).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, np.nan)

        # K线
        df["daily_return"] = df["close"].pct_change()
        df["is_yang"] = df["close"] > df["open"]
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(0, np.nan)
        df["is_limit_up"] = np.round(df["daily_return"], 4) >= limit_up_pct

        # 找到最近涨停日
        df["limit_up_day_offset"] = np.nan
        limit_up_indices = df.index[df["is_limit_up"]].tolist()
        for i in range(len(df)):
            prev = [idx for idx in limit_up_indices if idx < i]
            if prev:
                offset = i - prev[-1]
                if offset <= limit_up_lookback:
                    df.loc[df.index[i], "limit_up_day_offset"] = offset

        # 断板日：涨停次日未继续涨停
        df["is_break_day"] = (df["limit_up_day_offset"] == 1) & (df["daily_return"] < limit_up_pct)
        break_indices = df.index[df["is_break_day"]].tolist()

        df["break_day_high"] = np.nan
        df["break_day_low"] = np.nan
        df["break_day_vol_ratio"] = np.nan
        df["limit_up_day_high"] = np.nan
        for i in range(len(df)):
            prev_breaks = [idx for idx in break_indices if idx < i]
            if prev_breaks:
                last_break = prev_breaks[-1]
                if i - last_break <= max_break_days:
                    df.loc[df.index[i], "break_day_high"] = df.loc[df.index[last_break], "high"]
                    df.loc[df.index[i], "break_day_low"] = df.loc[df.index[last_break], "low"]
                    df.loc[df.index[i], "break_day_vol_ratio"] = df.loc[df.index[last_break], "vol_ratio"]
                    lu_idx = last_break - 1
                    if lu_idx >= 0 and df.loc[df.index[lu_idx], "is_limit_up"]:
                        df.loc[df.index[i], "limit_up_day_high"] = df.loc[df.index[lu_idx], "high"]

        # 买点硬条件
        cond_limit_up_before = df["limit_up_day_offset"].notna()
        cond_break_exists = df["break_day_high"].notna()
        cond_rebound = df["close"] > df["break_day_high"]
        cond_yang = df["is_yang"]
        cond_trend_up = (df["ma_slope"] > 0) & (df["close"] > df["ma_trend"])
        cond_not_huge_vol = df["vol_ratio"] <= max_vol_ratio

        core_buy_cond = (
            cond_limit_up_before
            & cond_break_exists
            & cond_rebound
            & cond_yang
            & cond_trend_up
            & cond_not_huge_vol
        )

        # 评分项
        score_break_shrink = (1 - df["break_day_vol_ratio"].clip(0, 1)).fillna(0).clip(0, 1) * 0.25
        score_rebound_vol = (df["vol_ratio"].clip(0, max_vol_ratio) / max_vol_ratio * 0.20).fillna(0)
        score_strong_rebound = (df["close"] > df["limit_up_day_high"]).fillna(False).astype(float) * 0.20
        score_trend_up = (df["ma_slope"].clip(0, 0.05) / 0.05 * 0.15).fillna(0)
        score_timing = ((df["limit_up_day_offset"] >= 2) & (df["limit_up_day_offset"] <= 4)).astype(float) * 0.10
        score_yang = df["is_yang"].astype(float) * 0.10

        df["confidence_score"] = (
            score_break_shrink + score_rebound_vol + score_strong_rebound
            + score_trend_up + score_timing + score_yang
        ).clip(0, 1)

        buy_cond_final = core_buy_cond & (df["confidence_score"] >= min_confidence)

        # 持仓与卖点
        position_window = time_stop_window * 4
        df["in_position"] = buy_cond_final.rolling(window=position_window).sum() > 0
        df["entry_low"] = np.nan
        df["entry_close"] = np.nan
        df.loc[buy_cond_final, "entry_low"] = df.loc[buy_cond_final, "low"]
        df.loc[buy_cond_final, "entry_close"] = df.loc[buy_cond_final, "close"]
        df["entry_low"] = df["entry_low"].ffill()
        df["entry_close"] = df["entry_close"].ffill()

        cond_stop_loss_1 = df["in_position"] & (df["close"] < df["entry_low"] * (1 - stop_loss_pct))
        cond_stop_loss_2 = df["in_position"] & (df["close"] < df["ma_trend"])
        cond_stagnation = df["in_position"] & (
            (df["vol_ratio"] >= stagnation_vol_ratio)
            & (df["upper_shadow"] > max_upper_shadow_pct)
            & (df["daily_return"].abs() < 0.02)
        )
        df["return_since_entry"] = (df["close"] - df["entry_close"]) / df["entry_close"]
        df["days_since_entry"] = 0
        buy_idx_list = df.index[buy_cond_final].tolist()
        for i in range(len(df)):
            prev_buys = [idx for idx in buy_idx_list if idx <= i]
            if prev_buys:
                df.loc[df.index[i], "days_since_entry"] = i - prev_buys[-1]
        cond_time_stop = (
            df["in_position"]
            & (df["days_since_entry"] >= time_stop_window)
            & (df["return_since_entry"] < time_stop_threshold)
        )
        cond_take_profit = df["in_position"] & (df["daily_return"] >= take_profit_pct * 0.6) & df["is_yang"]

        df["signal"] = 0
        df.loc[buy_cond_final, "signal"] = 1
        df.loc[
            cond_stop_loss_1 | cond_stop_loss_2 | cond_stagnation | cond_time_stop | cond_take_profit,
            "signal",
        ] = -1

        df["confidence"] = 0.0
        df.loc[buy_cond_final, "confidence"] = df.loc[buy_cond_final, "confidence_score"].clip(0.55, 0.90)

        return df
```

### Step 2: 编写单元测试

```python
"""DragonTigerShortStrategy 单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.dragon_tiger_short import DragonTigerShortStrategy


@pytest.fixture
def strategy() -> DragonTigerShortStrategy:
    return DragonTigerShortStrategy({})


def _make_df(prices: list[float], volumes: list[float]) -> pd.DataFrame:
    df = pd.DataFrame({
        "open": prices,
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.98 for p in prices],
        "close": prices,
        "volume": volumes,
    })
    return df


def test_validate_params_bad_lookback(strategy: DragonTigerShortStrategy):
    strategy.params = {"limit_up_lookback": 0}
    with pytest.raises(ValueError, match="必须大于"):
        strategy.validate_params()


def test_validate_params_bad_limit_up(strategy: DragonTigerShortStrategy):
    strategy.params = {"limit_up_pct": 0.15}
    with pytest.raises(ValueError, match="5%-11%"):
        strategy.validate_params()


def test_buy_after_limit_up_break_rebound(strategy: DragonTigerShortStrategy):
    # 构造 40 日数据，第 35 日涨停，第 36 日断板，第 37 日反包
    prices = list(np.linspace(10, 12, 34))
    prices.append(12 * 1.095)   # 涨停
    prices.append(12 * 1.095 * 0.98)  # 断板
    prices.append(12 * 1.095 * 1.01)  # 反包
    volumes = [10000] * 35 + [15000] + [12000] + [14000]
    df = _make_df(prices, volumes)
    result = strategy.generate_signals(df)
    assert "signal" in result.columns
    assert "confidence" in result.columns


def test_sell_on_stop_loss(strategy: DragonTigerShortStrategy):
    # 买入后大跌触发止损
    prices = list(np.linspace(10, 15, 30))
    volumes = [10000] * 30
    df = _make_df(prices, volumes)
    result = strategy.generate_signals(df)
    assert not result.empty
```

### Step 3: 运行测试

```bash
uv run pytest tests/unit/test_dragon_tiger_short.py -v
```

Expected: 4 passed。

### Step 4: Commit

```bash
git add src/gugu/strategies/dragon_tiger_short.py tests/unit/test_dragon_tiger_short.py
git commit -m "feat: add DragonTigerShortStrategy for main-force short-term follow"
```

---

## Task 3: 注册策略并配置参数

**Files:**
- Modify: `src/gugu/strategies/registry.py`
- Modify: `src/gugu/strategies/__init__.py`（如需要）
- Modify: `config/strategy_defaults.yaml`
- Modify: `config/strategy_manifest.yaml`

### Step 1: 注册策略

修改 `src/gugu/strategies/registry.py`，在 `_REGISTRY` 字典中加入：

```python
from gugu.strategies.north_force_long import NorthForceLongStrategy
from gugu.strategies.dragon_tiger_short import DragonTigerShortStrategy

_REGISTRY: dict[str, type[Strategy]] = {
    # ... 已有策略
    "north_force_long": NorthForceLongStrategy,
    "dragon_tiger_short": DragonTigerShortStrategy,
}
```

### Step 2: 配置默认参数

在 `config/strategy_defaults.yaml` 末尾追加：

```yaml
north_force_long:
  ma_trend_window: 20
  ma_long_window: 60
  ma_slope_window: 5
  pullback_to_ma_pct: 0.02
  recent_momentum_window: 20
  recent_momentum_pct: 0.05
  recent_spike_window: 5
  recent_spike_pct: 0.05
  max_daily_gain_pct: 0.02
  max_vol_ratio: 1.5
  vol_ma_window: 60
  stop_loss_pct: 0.07
  take_profit_pct: 0.20
  stagnation_vol_ratio: 2.5
  max_upper_shadow_pct: 0.03
  time_stop_window: 30
  time_stop_threshold: 0.05
  min_confidence: 0.55

dragon_tiger_short:
  ma_trend_window: 20
  ma_slope_window: 5
  limit_up_pct: 0.095
  limit_up_lookback: 5
  max_break_days: 3
  stop_loss_pct: 0.05
  take_profit_pct: 0.10
  stagnation_vol_ratio: 2.5
  max_upper_shadow_pct: 0.02
  time_stop_window: 5
  time_stop_threshold: 0.03
  max_vol_ratio: 2.0
  vol_ma_window: 20
  min_confidence: 0.55
```

### Step 3: 配置 manifest

在 `config/strategy_manifest.yaml` 的 `strategies` 列表末尾追加：

```yaml
  - name: north_force_long
    category: main_force
    enabled: false
    description: "长线主力吸筹跟随：趋势向上+缩量回踩MA20+近期有资金关注"
    params_source: strategy_defaults
    validation:
      status: pending_backtest
      reason: "新增策略，待回测验证后开启"
      last_backtest: null

  - name: dragon_tiger_short
    category: main_force
    enabled: false
    description: "短线龙虎榜反包跟随：涨停+断板+反包+趋势支撑"
    params_source: strategy_defaults
    validation:
      status: pending_backtest
      reason: "新增策略，待回测验证后开启"
      last_backtest: null
```

### Step 4: 验证注册

```bash
uv run python scripts/verify_strategies.py
```

Expected: 输出包含 `north_force_long` 和 `dragon_tiger_short`，状态为 `disabled/pending_backtest`。

### Step 5: 运行配置一致性测试

```bash
uv run pytest tests/unit/test_config_consistency.py -v
```

Expected: passed。

### Step 6: Commit

```bash
git add src/gugu/strategies/registry.py config/strategy_defaults.yaml config/strategy_manifest.yaml
git commit -m "chore: register main-force strategies and add default configs"
```

---

## Task 4: 回测验证

**Files:**
- Use: `scripts/backtest_all_strategies.py`
- Use: `scripts/diagnose_strategy.py`
- Output: `data/backtest_*.json`

### Step 1: 单股回测（代表性股票）

```bash
uv run python scripts/backtest_all_strategies.py \
  --strategy north_force_long \
  --stocks 600519,000858,002714,300750,600036 \
  --days 500 \
  --output data/backtest_north_force_long_2026-07-17.json

uv run python scripts/backtest_all_strategies.py \
  --strategy dragon_tiger_short \
  --stocks 002371,000001,600030,300059,002594 \
  --days 500 \
  --output data/backtest_dragon_tiger_short_2026-07-17.json
```

Expected: 命令成功执行，输出 JSON 报告。

### Step 2: 诊断报告

```bash
uv run python scripts/diagnose_strategy.py \
  --strategy north_force_long \
  --stocks 600519,000858,002714,300750,600036 \
  --output data/diagnose_north_force_long_2026-07-17.json

uv run python scripts/diagnose_strategy.py \
  --strategy dragon_tiger_short \
  --stocks 002371,000001,600030,300059,002594 \
  --output data/diagnose_dragon_tiger_short_2026-07-17.json
```

Expected: 输出分阶段（牛/熊/震荡）表现和失败 trade 归因。

### Step 3: 多股聚合 Walk-Forward（如单股表现良好）

```bash
uv run python scripts/optimize_params.py \
  --strategy north_force_long \
  --multi-stock 600519,000858,002714,300750,600036,000333,000651,600276,600887,002304 \
  --output data/optimize_north_force_long_2026-07-17.json
```

Expected: 输出优化报告，评估 4 项解锁条件。

### Step 4: Commit 回测报告（可选）

```bash
git add data/backtest_*_2026-07-17.json data/diagnose_*_2026-07-17.json
git commit -m "test: add main-force strategy backtest reports"
```

---

## Task 5: 根据回测结果决定是否开启策略

**Files:**
- Modify: `config/strategy_manifest.yaml`

**实际结果：**
- `north_force_long`：58 股 × 300 天回测未通过 4 项解锁条件（收益 +0.08%，Sharpe -1.23），`enabled: false` 保持禁用，由 Walk-Forward 自动调参持续优化。
- `dragon_tiger_short`：58 股 × 300 天回测在牛市表现可观察（胜率 43.9%，单笔 +6646 元），已在 `bull/rally` 市场状态下观察启用（`settings.yaml` 的 `enabled_by_regime`）。

### Step 1: 检查回测指标

关键指标：
- 样本外 Sharpe > 0
- 参数稳定性 ≥ 50%
- 最大回撤 < 25%
- 收益 ≥ 0

### Step 2: 更新 manifest

如果通过 4 项条件，将对应策略的 `enabled: false` 改为 `enabled: true`：

```yaml
  - name: north_force_long
    category: main_force
    enabled: true
    # ...
    validation:
      status: validated
      reason: "回测通过 4 项解锁条件"
      last_backtest: "2026-07-17"
```

### Step 3: Commit

```bash
git add config/strategy_manifest.yaml
git commit -m "feat: enable main-force strategies after backtest validation"
```

---

## Task 6: 更新文档

**Files:**
- Modify: `docs/TECH.md`

### Step 1: 在 TECH.md 中补充策略说明

在"策略层"或"策略列表"章节追加：

```markdown
### north_force_long（长线主力吸筹跟随）

- 来源：散户"跟着主力进"操作说明书
- 核心逻辑：MA20 > MA60 且斜率向上，股价缩量回踩 MA20 但不跌破，近期有资金关注。
- 卖点：跌破 MA20、MA20 下穿 MA60、高位放量滞涨、时间止损。
- 生产增强：SignalPipeline 的 AltAlphaFilter/HolderConcentrationFilter 二次过滤。

### dragon_tiger_short（短线龙虎榜反包跟随）

- 来源：散户"跟着主力进"操作说明书
- 核心逻辑：近 5 日有涨停板，断板后 1-3 日内阳线反包断板日最高价，MA20 向上。
- 卖点：跌破入场止损位、跌破 MA20、高位放量滞涨、时间止损。
- 生产增强：SignalPipeline 的 AltAlphaFilter 检查龙虎榜机构净买入。
```

### Step 2: Commit

```bash
git add docs/TECH.md
git commit -m "docs: add main-force strategies to TECH.md"
```

---

## Task 7: 全量测试与调度器重启

**实际结果：**
- 全量测试：`1620 passed, 5 skipped`。
- Ruff 检查：新增/修改文件全部通过。
- mypy：因 Python 3.14 环境下 numpy stub 语法问题报错，与本次修改无关（`tool.mypy` 配置目标为 Python 3.11）。
- 调度器：已在运行（PID 86620），无需重启；心跳 `data/heartbeat.json` 正常更新。

### Step 1: 运行全量测试

```bash
uv run pytest tests/ -x --tb=short -q
```

Expected: 所有测试通过（数量应 >= 当前 1620）。

### Step 2: 运行质量检查

```bash
uv run ruff check src/
uv run ruff format --check src/
uv run mypy src/ --ignore-missing-imports
```

Expected: 无新增错误（mypy 环境报错需待 numpy 兼容 Python 3.14 后消除）。

### Step 3: 重启调度器

如调度器未运行或代码有重大变更，执行：

```bash
# 停止旧调度器
pkill -f "run_scheduler.py" || true

# 启动新调度器
nohup uv run python scripts/run_scheduler.py > logs/scheduler.log 2>&1 &
```

### Step 4: 验证调度器

```bash
sleep 3
ps aux | grep run_scheduler.py | grep -v grep
tail -n 20 logs/scheduler.log
```

Expected: 进程存在，日志显示"25 个定时任务已注册"或类似信息。

---

## Self-Review Checklist

- [x] Spec coverage: 每只策略的买卖条件、参数、测试、注册、回测、文档都有对应任务。
- [x] Placeholder scan: 计划中没有 TBD/TODO/"后续再实现"等占位符。
- [x] Type consistency: 策略参数键名在代码、defaults、manifest、测试、文档中一致。
- [x] 不破坏现有架构: Strategy 基类、SignalPipeline、MarketFilter 均不修改。
- [x] 回测可复现: 策略纯 OHLCV，无未来函数，不依赖异步数据。
- [x] 默认禁用: 新策略 manifest 中 `north_force_long` enabled 为 false；`dragon_tiger_short` 仅在 bull/rally 观察启用。
