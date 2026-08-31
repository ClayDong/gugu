"""当前运行时策略的固定数据基准测试。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from gugu.strategies.registry import get_strategy, list_strategies

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
_BENCHMARK_CSV = _FIXTURE_DIR / "benchmark_600519.csv"
_RUNTIME_STRATEGIES = (
    "rsi_reversal",
    "box_breakout",
    "double_yin",
    "linjudaye",
    "price_volume_signal",
    "regime_cycle_four",
)


@pytest.fixture(scope="module")
def benchmark_df() -> pd.DataFrame:
    """加载固定基准行情。"""
    df = pd.read_csv(_BENCHMARK_CSV)
    df["date"] = pd.to_datetime(df["date"])
    return df


@pytest.mark.parametrize("strategy_name", _RUNTIME_STRATEGIES)
def test_runtime_strategy_output_contract(
    benchmark_df: pd.DataFrame,
    strategy_name: str,
) -> None:
    """当前 registry 中的每个策略都必须满足统一输出契约。"""
    assert strategy_name in list_strategies()
    signals = get_strategy(strategy_name).generate_signals(benchmark_df)

    assert len(signals) == len(benchmark_df)
    assert {"signal", "confidence"}.issubset(signals.columns)
    assert signals["signal"].isin([-1, 0, 1]).all()
    confidence = signals["confidence"].dropna()
    assert (confidence >= 0).all()
    assert (confidence <= 1).all()


@pytest.mark.parametrize("strategy_name", ("box_breakout", "rsi_reversal"))
def test_verified_strategy_generates_signals(
    benchmark_df: pd.DataFrame,
    strategy_name: str,
) -> None:
    """生产 verified 策略在固定行情上至少产生一条有效信号。"""
    signals = get_strategy(strategy_name).generate_signals(benchmark_df)
    assert (signals["signal"] != 0).any(), f"{strategy_name} 未产生任何信号"


def test_box_breakout_handles_data_gracefully(benchmark_df: pd.DataFrame) -> None:
    """箱体突破策略即使没有满足突破条件也必须稳定返回结果。"""
    signals = get_strategy("box_breakout").generate_signals(benchmark_df)
    assert signals is not None
    assert len(signals) == len(benchmark_df)
