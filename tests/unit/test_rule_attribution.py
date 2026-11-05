"""规则级归因工具测试（2026-11-05）。

这个工具的价值在于「把整体不行拆成哪一条值得研究」，所以测试重点是：
- 只统计选中的规则，不能把别的规则混进来；
- 规则来自策略自己的 rule_masks，没有实现就如实拒绝，不许猜；
- 口径与体检表共用（同一套 collect_events），否则两表没法对比。
"""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.rule_attribution import RuleSubset, attribute, render_markdown


class _FakeRules:
    """两条规则的假策略：A 每天都发，B 从第 5 天起才发。"""

    def rule_masks(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        idx = df.index
        return {
            "A": pd.Series(True, index=idx),
            "B": pd.Series([i >= 5 for i in range(len(idx))], index=idx),
        }


class _NoRules:
    def generate_signals(self, df, is_backtest=False):  # noqa: ARG002
        out = df.copy()
        out["signal"] = 0
        return out


def _frame_and_index(rows: int = 20) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    dates = pd.date_range("2024-03-01", periods=rows, freq="D").strftime("%Y-%m-%d").tolist()
    stock = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * rows,
            "high": [10.5] * rows,
            "low": [9.5] * rows,
            "close": [10.0] * rows,
            "volume": [1_000_000] * rows,
        }
    )
    index = stock.copy()
    index[["open", "high", "low", "close"]] = 100.0
    return {"X": stock}, index


def test_rule_subset_counts_only_selected_rules() -> None:
    frames, index = _frame_and_index()
    a_only = RuleSubset(_FakeRules(), ("A",))
    b_only = RuleSubset(_FakeRules(), ("B",))
    both = RuleSubset(_FakeRules(), None)

    df = frames["X"].copy()
    assert int(a_only.generate_signals(df)["signal"].sum()) == 20
    assert int(b_only.generate_signals(df)["signal"].sum()) == 15
    assert int(both.generate_signals(df)["signal"].sum()) == 20


def test_rule_subset_value_is_binary() -> None:
    frames, _ = _frame_and_index()
    out = RuleSubset(_FakeRules(), ("B",)).generate_signals(frames["X"].copy())
    assert set(out["signal"].unique()) <= {0, 1}


def test_attribute_returns_union_first_then_each_rule() -> None:
    frames, index = _frame_and_index()
    rows = attribute(
        _FakeRules(),
        frames,
        index,
        horizon=2,
        cost_rate=0.0,
        start="2024-01-01",
    )
    assert rows[0]["strategy"] == "全集 OR（现状）"
    assert [r["strategy"] for r in rows[1:]] == ["A", "B"]


def test_attribute_union_density_not_less_than_single_rule() -> None:
    """并集密度必须 ≥ 任一单条规则密度，否则说明聚合方向错了。"""
    frames, index = _frame_and_index()
    rows = attribute(_FakeRules(), frames, index, horizon=2, cost_rate=0.0, start="2024-01-01")
    union = rows[0]["density"]
    assert all(union >= r["density"] for r in rows[1:])


def test_attribute_handles_no_events() -> None:
    frames, index = _frame_and_index()
    rows = attribute(_FakeRules(), frames, index, horizon=2, cost_rate=0.0, start="2099-01-01")
    assert all(r["samples"] == 0 for r in rows)
    assert all(r["mean_excess"] is None for r in rows)


def test_render_markdown_lists_rules_and_notes() -> None:
    frames, index = _frame_and_index()
    rows = attribute(_FakeRules(), frames, index, horizon=2, cost_rate=0.004, start="2024-01-01")
    meta = {
        "strategy": "demo",
        "start": "2024-03-01",
        "cost": 0.004,
        "horizon": 2,
        "index": "sh000300",
        "symbols": 1,
    }
    text = render_markdown(rows, meta)
    assert "| 全集 OR（现状） |" in text
    assert "| A |" in text and "| B |" in text
    assert "策略：demo" in text
    assert "只能当线索" in text


def test_main_rejects_strategy_without_rule_masks(tmp_path, monkeypatch) -> None:
    """没实现分解就如实拒绝，不许凭猜测拆规则。"""
    import scripts.rule_attribution as mod

    pool = tmp_path / "pool.json"
    pool.write_text('{"events": [{"code": "sz000001"}]}', encoding="utf-8")
    monkeypatch.setattr(mod, "load_index", lambda *a, **k: _frame_and_index()[1])
    monkeypatch.setattr(mod, "load_frames", lambda *a, **k: (_frame_and_index()[0], []))
    monkeypatch.setattr("gugu.strategies.registry.get_strategy", lambda name: _NoRules())

    code = mod.main(["--pool-file", str(pool), "--cache-dir", str(tmp_path)])
    assert code == 1


def test_main_errors_when_pool_missing(tmp_path) -> None:
    import scripts.rule_attribution as mod

    code = mod.main(["--pool-file", str(tmp_path / "nope.json"), "--cache-dir", str(tmp_path)])
    assert code == 1
