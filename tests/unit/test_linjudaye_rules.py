"""linjudaye 规则分解的契约测试（2026-11-05）。

`rule_masks()` 是规则归因工具的唯一真源，所以它的**不变量**必须被锁死：
1. 规则并集 == `generate_signals` 的买入位（逐位相同，不是"差不多"）；
2. 规则名与声明顺序稳定，否则工具表头会漂；
3. 跑的是配置里的参数，不是代码默认值。

第 3 条有实际教训：我最初写归因脚本时复制了一份规则、不读 self.params，
结果用的是代码默认（low_vol_ratio=0.5、golden_level=0.618），
而 config/strategy_defaults.yaml 里是 0.7/0.7。同一份数据，
R01 密度算出 0.4%（错）而不是 2.8%（对）——复制规则会连参数一起丢。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.blogger_rules.linjudaye import LinJuDaYeStrategy


def _walk(rows: int = 160, seed: int = 7) -> pd.DataFrame:
    """可复现的行情：随机游走 + 成交量的周期性放大，保证会触发若干规则。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0, 0.018, rows)
    close = 10.0 * np.cumprod(1 + ret)
    open_ = close * (1 + rng.normal(0, 0.004, rows))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, rows))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, rows))
    volume = rng.uniform(5e5, 3e6, rows) * (1 + 0.8 * (np.arange(rows) % 11 == 0))
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=rows, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


def test_rule_names_are_stable_and_ordered() -> None:
    strat = LinJuDaYeStrategy()
    names = list(strat.rule_masks(_walk()))
    assert names == [name for name, _ in LinJuDaYeStrategy.RULE_CONFIDENCE]
    assert len(names) == 5


def test_rule_union_equals_real_buy_signals() -> None:
    """核心不变量：分解出的规则并集必须与线上信号逐位相同。"""
    strat = LinJuDaYeStrategy(params={})
    for seed in (1, 7, 21):
        df = _walk(seed=seed)
        signals = strat.generate_signals(df.copy(), is_backtest=True)
        buy = (pd.to_numeric(signals["signal"], errors="coerce").fillna(0) == 1).to_numpy()
        union = np.zeros(len(df), dtype=bool)
        for mask in strat.rule_masks(df.copy()).values():
            union |= mask.fillna(False).to_numpy()
        assert np.array_equal(buy, union), f"seed={seed} 规则并集与真实信号不一致"


def test_rules_actually_fire_on_sample_data() -> None:
    """并集相等也可能是"两边都空"——要确认样本确实触发了规则。"""
    strat = LinJuDaYeStrategy()
    masks = strat.rule_masks(_walk())
    assert any(bool(m.fillna(False).any()) for m in masks.values())


def test_params_change_masks_not_code_defaults() -> None:
    """改参数必须改变掩码：证明分解读的是注入参数而非硬编码默认值。"""
    df = _walk()
    loose = LinJuDaYeStrategy(params={"low_vol_ratio": 0.9}).rule_masks(df.copy())["R01_地量抄底"]
    tight = LinJuDaYeStrategy(params={"low_vol_ratio": 0.2}).rule_masks(df.copy())["R01_地量抄底"]
    assert int(loose.fillna(False).sum()) >= int(tight.fillna(False).sum())
    assert int(loose.fillna(False).sum()) != int(tight.fillna(False).sum())


def test_confidence_follows_declared_rule_order() -> None:
    """同一根 bar 命中多条规则时，靠后的规则覆盖置信度——顺序必须可预期。"""
    strat = LinJuDaYeStrategy(params={})
    df = _walk(seed=3)
    signals = strat.generate_signals(df.copy(), is_backtest=True)
    masks = strat.rule_masks(df.copy())
    values = {name: conf for name, conf in LinJuDaYeStrategy.RULE_CONFIDENCE}
    for idx in df.index:
        hits = [name for name, mask in masks.items() if bool(mask.fillna(False).iloc[idx])]
        if not hits:
            continue
        expected = values[hits[-1]]
        assert signals.loc[idx, "confidence"] == pytest.approx(expected)


def test_missing_columns_raise() -> None:
    strat = LinJuDaYeStrategy()
    with pytest.raises(ValueError, match="缺失列"):
        strat.rule_masks(pd.DataFrame({"close": [1.0, 2.0]}))
