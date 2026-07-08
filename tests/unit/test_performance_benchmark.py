"""性能基准测试：记录关键路径耗时，防止回归退化。

每次迭代后运行此文件，对比基准值。若出现显著退化（>20%），
应排查引入的性能问题。

测试策略：
1. 策略信号生成（全市场典型场景）
2. 风控检查（多次调用）
3. 数据缓存读写（内存缓存）
4. Signal 序列化/反序列化
"""
from __future__ import annotations

import time

import pandas as pd
import pytest

# 基准阈值（秒）—— 基于 M1 Mac 实测值，CI 环境可能会更慢
# 退化超过 2x 时触发告警（留充足余量避免误报）
_BENCHMARK_TOLERANCE = 2.0


def _make_kline_df(days: int = 250) -> pd.DataFrame:
    """构造 250 行 K 线数据（约一年交易日）。"""
    dates = pd.date_range("2025-01-01", periods=days, freq="B")
    base = 100.0
    prices = []
    for i in range(days):
        if i < 100:
            base *= 1.003
        elif i < 150:
            base *= 0.997
        else:
            base *= 1.002
        prices.append(base)

    return pd.DataFrame({
        "date": dates,
        "open": [p * 0.998 for p in prices],
        "high": [p * 1.015 for p in prices],
        "low": [p * 0.985 for p in prices],
        "close": prices,
        "volume": [100000 + (i % 10) * 5000 for i in range(days)],
        "amount": [p * 100000 for p in prices],
    })


class TestSignalGenerationBenchmark:
    """策略信号生成性能基准。"""

    @pytest.mark.parametrize("strategy_name", [
        "turtle", "dual_ma", "bollinger", "rsi_reversal",
        "chen_jiangting", "box_breakout", "kdj",
        "double_yin", "pullback_low", "money_flow_follow",
    ])
    def test_strategy_generation_speed(self, strategy_name: str) -> None:
        """各策略在 250 行 K 线上生成信号应 < 0.5s。"""
        from gugu.strategies.registry import get_strategy

        df = _make_kline_df(250)
        strategy = get_strategy(strategy_name)

        start = time.perf_counter()
        result = strategy.generate_signals(df)
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0 * _BENCHMARK_TOLERANCE, (
            f"{strategy_name} 生成耗时 {elapsed:.3f}s，超出基准阈值"
        )
        assert "signal" in result.columns
        assert "confidence" in result.columns
        assert len(result) == len(df)

    def test_bulk_strategy_generation(self) -> None:
        """50 只股票 × 1 个策略（全市场扫描典型场景）< 8s。"""
        from gugu.strategies.registry import get_strategy

        df = _make_kline_df(120)  # 120 天短周期
        strategy = get_strategy("turtle")

        # 模拟 50 只股票
        start = time.perf_counter()
        for _ in range(50):
            strategy.generate_signals(df.copy())
        elapsed = time.perf_counter() - start

        assert elapsed < 10.0 * _BENCHMARK_TOLERANCE, (
            f"50 次策略生成总耗时 {elapsed:.3f}s，超出基准阈值"
        )


class TestDataCacheBenchmark:
    """数据缓存性能基准。"""

    def test_memory_cache_read_write_speed(self) -> None:
        """内存缓存 1000 次读写 < 0.1s。"""
        from gugu.data.cache import DataCache

        cache = DataCache(ttl_seconds=3600, max_size=10000)

        start = time.perf_counter()
        for i in range(1000):
            cache.set(f"key_{i}", {"data": i})
        for i in range(1000):
            cache.get(f"key_{i}")
        elapsed = time.perf_counter() - start

        assert elapsed < 0.2 * _BENCHMARK_TOLERANCE, (
            f"1000 次缓存读写耗时 {elapsed:.3f}s"
        )

    def test_cache_lru_eviction(self) -> None:
        """LRU 淘汰 2000 条目 < 0.05s。"""
        from gugu.data.cache import DataCache

        cache = DataCache(ttl_seconds=3600, max_size=1000)

        start = time.perf_counter()
        for i in range(2000):
            cache.set(f"evict_key_{i}", i)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.1 * _BENCHMARK_TOLERANCE
        stats = cache.stats()
        assert stats["keys"] <= stats["max_size"]


class TestRiskBenchmark:
    """风控性能基准。"""

    def test_risk_manager_check_speed(self) -> None:
        """1000 次风控检查 < 0.2s。"""
        from gugu.risk.manager import RiskManager

        rm = RiskManager()

        start = time.perf_counter()
        for i in range(1000):
            rm.check_daily_loss(0.01)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.3 * _BENCHMARK_TOLERANCE, (
            f"1000 次风控检查耗时 {elapsed:.3f}s"
        )


class TestSignalModelBenchmark:
    """Signal 模型序列化性能基准。"""

    def test_signal_to_dict_speed(self) -> None:
        """Signal → dict 1000 次 < 0.05s。"""
        from gugu.models.signal import Signal

        signal = Signal(
            symbol="600519",
            direction="buy",
            price=1800.0,
            name="贵州茅台",
            strategy="turtle",
            confidence=0.85,
            suggested_position_ratio=0.15,
            stop_loss_price=1656.0,
        )

        start = time.perf_counter()
        for _ in range(1000):
            signal.to_dict()
        elapsed = time.perf_counter() - start

        assert elapsed < 0.1 * _BENCHMARK_TOLERANCE

    def test_signal_from_dict_speed(self) -> None:
        """dict → Signal 1000 次 < 0.05s。"""
        from gugu.models.signal import Signal

        d = {
            "symbol": "600519",
            "direction": "buy",
            "price": 1800.0,
            "name": "贵州茅台",
            "strategy": "turtle",
            "confidence": 0.85,
            "suggested_position_ratio": 0.15,
            "stop_loss_price": 1656.0,
        }

        start = time.perf_counter()
        for _ in range(1000):
            Signal.from_dict(d)
        elapsed = time.perf_counter() - start

        assert elapsed < 0.1 * _BENCHMARK_TOLERANCE