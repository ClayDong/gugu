"""启发式优化器单元测试：GA + PSO + StrategyParamOptimizer。"""

from __future__ import annotations

from unittest import mock

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.heuristic_optimizer import (
    BaseHeuristicOptimizer,
    GAOptimizer,
    OptimizationResult,
    ParamSpace,
    PSOOptimizer,
    StrategyParamOptimizer,
)
from gugu.strategies.base import Strategy


class TestParamSpace:
    """ParamSpace 基础功能测试。"""

    def test_sample_int_returns_int(self) -> None:
        """整数参数采样应返回整数。"""
        space = ParamSpace(name="window", min_val=5, max_val=20, is_int=True)
        rng = np.random.default_rng(42)
        for _ in range(50):
            val = space.sample(rng)
            assert isinstance(val, int)
            assert 5 <= val <= 20

    def test_sample_float_returns_float(self) -> None:
        """浮点参数采样应返回浮点数。"""
        space = ParamSpace(name="ratio", min_val=0.1, max_val=0.9, step=0.01, is_int=False)
        rng = np.random.default_rng(42)
        for _ in range(50):
            val = space.sample(rng)
            assert isinstance(val, float)
            assert 0.1 <= val <= 0.9

    def test_clip_int_within_range(self) -> None:
        """整数参数裁剪应在合法范围内。"""
        space = ParamSpace(name="window", min_val=5, max_val=20, is_int=True)
        assert space.clip(10) == 10
        assert space.clip(3) == 5
        assert space.clip(25) == 20

    def test_clip_float_with_step(self) -> None:
        """浮点参数裁剪应按 step 量化。"""
        space = ParamSpace(name="ratio", min_val=0.1, max_val=0.9, step=0.05, is_int=False)
        assert abs(space.clip(0.123) - 0.10) < 0.001
        assert abs(space.clip(0.57) - 0.55) < 0.001
        assert abs(space.clip(0.95) - 0.90) < 0.001


class TestOptimizationResult:
    """OptimizationResult 数据结构测试。"""

    def test_result_fields(self) -> None:
        """结果应包含所有必要字段。"""
        result = OptimizationResult(
            best_params={"x": 10, "y": 0.5},
            best_score=1.5,
            best_metrics={"sharpe": 1.2, "win_rate": 0.6},
            generations=20,
            population_size=30,
        )
        assert result.best_params == {"x": 10, "y": 0.5}
        assert result.best_score == 1.5
        assert result.best_metrics["sharpe"] == 1.2
        assert result.history == []


class TestGAOptimizer:
    """遗传算法优化器测试。"""

    def test_ga_optimize_simple_quadratic(self) -> None:
        """GA 应能找到二次函数的最优点。"""
        spaces = [ParamSpace(name="x", min_val=-5, max_val=5, step=0.1, is_int=False)]

        def fitness(params: dict[str, float]) -> float:
            x = params["x"]
            return -(x - 2.5) ** 2

        ga = GAOptimizer(
            param_spaces=spaces,
            fitness_func=fitness,
            population_size=20,
            generations=30,
            seed=42,
        )
        result = ga.optimize(verbose=False)

        assert result.best_score > -1.0
        assert abs(result.best_params["x"] - 2.5) < 0.5
        assert result.generations == 30
        assert result.population_size == 20
        assert len(result.history) == 30

    def test_ga_history_monotonic_improvement(self) -> None:
        """GA 最优得分应单调不减。"""
        spaces = [ParamSpace(name="x", min_val=0, max_val=10, step=0.01, is_int=False)]

        def fitness(params: dict[str, float]) -> float:
            x = params["x"]
            return -((x - 7.5) ** 2)

        ga = GAOptimizer(
            param_spaces=spaces,
            fitness_func=fitness,
            population_size=25,
            generations=15,
            seed=123,
        )
        result = ga.optimize()

        best_so_far = -float("inf")
        for h in result.history:
            assert h["best_score"] >= best_so_far -1e-6
            best_so_far = h["best_score"]

    def test_ga_respects_constraints(self) -> None:
        """GA 应尊重参数搜索空间约束。"""
        spaces = [
            ParamSpace(name="short", min_val=3, max_val=10, is_int=True),
            ParamSpace(name="long", min_val=15, max_val=30, is_int=True),
        ]

        def fitness(params: dict[str, float]) -> float:
            return float(params["short"] + params["long"])

        ga = GAOptimizer(
            param_spaces=spaces,
            fitness_func=fitness,
            population_size=20,
            generations=10,
            seed=42,
        )
        result = ga.optimize()

        assert 3 <= result.best_params["short"] <= 10
        assert 15 <= result.best_params["long"] <= 30
        assert isinstance(result.best_params["short"], int)
        assert isinstance(result.best_params["long"], int)

    def test_ga_handles_invalid_fitness(self) -> None:
        """GA 应优雅处理适应度函数异常。"""
        spaces = [ParamSpace(name="x", min_val=0, max_val=10, is_int=False)]
        call_count = 0

        def flaky_fitness(params: dict[str, float]) -> float:
            nonlocal call_count
            call_count += 1
            if params["x"] < 3.0:
                raise ValueError("invalid")
            return float(params["x"])

        ga = GAOptimizer(
            param_spaces=spaces,
            fitness_func=flaky_fitness,
            population_size=15,
            generations=5,
            seed=42,
        )
        result = ga.optimize()

        assert result is not None
        assert call_count > 0


class TestPSOOptimizer:
    """粒子群优化器测试。"""

    def test_pso_optimize_simple_quadratic(self) -> None:
        """PSO 应能找到二次函数的最优点。"""
        spaces = [ParamSpace(name="x", min_val=-5, max_val=5, step=0.1, is_int=False)]

        def fitness(params: dict[str, float]) -> float:
            x = params["x"]
            return -(x - 1.5) ** 2

        pso = PSOOptimizer(
            param_spaces=spaces,
            fitness_func=fitness,
            population_size=20,
            generations=30,
            seed=42,
        )
        result = pso.optimize(verbose=False)

        assert result.best_score > -1.0
        assert abs(result.best_params["x"] - 1.5) < 0.5
        assert result.generations == 30
        assert result.population_size == 20
        assert len(result.history) == 30

    def test_pso_history_monotonic_improvement(self) -> None:
        """PSO 最优得分应单调不减。"""
        spaces = [ParamSpace(name="x", min_val=0, max_val=10, step=0.01, is_int=False)]

        def fitness(params: dict[str, float]) -> float:
            x = params["x"]
            return -((x - 6.0) ** 2)

        pso = PSOOptimizer(
            param_spaces=spaces,
            fitness_func=fitness,
            population_size=25,
            generations=15,
            seed=42,
        )
        result = pso.optimize()

        best_so_far = -float("inf")
        for h in result.history:
            assert h["best_score"] >= best_so_far -1e-6
            best_so_far = h["best_score"]

    def test_pso_respects_search_space(self) -> None:
        """PSO 应尊重参数搜索空间边界。"""
        spaces = [
            ParamSpace(name="a", min_val=0.1, max_val=0.5, step=0.01, is_int=False),
            ParamSpace(name="b", min_val=5, max_val=20, is_int=True),
        ]

        def fitness(params: dict[str, float]) -> float:
            return float(params["a"] * params["b"])

        pso = PSOOptimizer(
            param_spaces=spaces,
            fitness_func=fitness,
            population_size=20,
            generations=10,
            seed=42,
        )
        result = pso.optimize()

        assert 0.1 <= result.best_params["a"] <= 0.5
        assert 5 <= result.best_params["b"] <= 20

    def test_pso_with_tuple_fitness(self) -> None:
        """PSO 应支持返回 (score, metrics) 元组的适应度函数。"""
        spaces = [ParamSpace(name="x", min_val=0, max_val=10, is_int=False)]

        def fitness_with_metrics(params: dict[str, float]) -> tuple[float, dict]:
            x = params["x"]
            score = -((x - 5.0) ** 2)
            return score, {"custom_metric": x * 2}

        pso = PSOOptimizer(
            param_spaces=spaces,
            fitness_func=fitness_with_metrics,
            population_size=15,
            generations=10,
            seed=42,
        )
        result = pso.optimize()

        assert result.best_score > -1.0
        assert abs(result.best_params["x"] - 5.0) < 1.0


class TestStrategyParamOptimizer:
    """策略参数优化器集成测试。"""

    def test_build_param_spaces_from_grid(self) -> None:
        """应从参数网格正确构建 ParamSpace。"""

        class DummyStrategy(Strategy):
            name = "dummy"
            category = "test"

            def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
                return df

        param_grid = {
            "window": [5, 10, 15, 20],
            "ratio": [0.1, 0.2, 0.3],
        }
        optimizer = StrategyParamOptimizer(
            strategy_cls=DummyStrategy,
            stock_dfs={},
            param_grid=param_grid,
            population_size=10,
            generations=5,
        )

        spaces = optimizer.param_spaces
        assert len(spaces) == 2
        space_names = {s.name for s in spaces}
        assert space_names == {"window", "ratio"}

        window_space = next(s for s in spaces if s.name == "window")
        assert window_space.is_int is True
        assert window_space.min_val == 5
        assert window_space.max_val == 20

    def test_clip_and_constrain_with_valid_params(self) -> None:
        """合法参数应通过约束检查。"""

        class DummyStrategy(Strategy):
            name = "dummy"
            category = "test"

            def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
                return df

        optimizer = StrategyParamOptimizer(
            strategy_cls=DummyStrategy,
            stock_dfs={},
            param_grid={"short": [3, 5, 10], "long": [15, 20, 30]},
            population_size=10,
            generations=5,
        )

        result = optimizer._clip_and_constrain({"short": 7, "long": 25})
        assert result is not None
        assert result["short"] == 7
        assert result["long"] == 25

    def test_clip_and_constrain_with_constraint_fn(self) -> None:
        """自定义约束函数应过滤非法参数。"""

        class DummyStrategy(Strategy):
            name = "dummy"
            category = "test"

            def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
                return df

        def window_order_constraint(params: dict[str, float]) -> bool:
            return params["fast"] < params["slow"]

        optimizer = StrategyParamOptimizer(
            strategy_cls=DummyStrategy,
            stock_dfs={},
            param_grid={"fast": [5, 10, 15], "slow": [10, 20, 30]},
            param_constraints=window_order_constraint,
            population_size=10,
            generations=5,
        )

        valid = optimizer._clip_and_constrain({"fast": 5, "slow": 20})
        assert valid is not None
        assert valid["fast"] < valid["slow"]

        invalid = optimizer._clip_and_constrain({"fast": 25, "slow": 10})
        assert invalid is None

    def test_optimize_raises_on_unknown_algorithm(self) -> None:
        """未知算法应抛出 ValueError。"""

        class DummyStrategy(Strategy):
            name = "dummy"
            category = "test"

            def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
                return df

        optimizer = StrategyParamOptimizer(
            strategy_cls=DummyStrategy,
            stock_dfs={},
            param_grid={"x": [1, 2, 3]},
            algorithm="unknown",
        )

        with pytest.raises(ValueError, match="不支持的优化算法"):
            optimizer.optimize()

    def test_fitness_returns_negative_inf_for_invalid(self) -> None:
        """无效参数应返回 -inf 适应度。"""

        class DummyStrategy(Strategy):
            name = "dummy"
            category = "test"

            def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
                return df

        optimizer = StrategyParamOptimizer(
            strategy_cls=DummyStrategy,
            stock_dfs={},
            param_grid={"x": [1, 2, 3]},
            param_constraints=lambda p: False,
        )

        score = optimizer._fitness({"x": 2})
        assert score == -float("inf")

    def test_fitness_with_stock_data_mocked(self) -> None:
        """使用 mock 回测引擎测试适应度函数。"""

        class DummyStrategy(Strategy):
            name = "dummy"
            category = "test"

            def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
                return df

        mock_df = pd.DataFrame(
            {
                "date": pd.bdate_range("2024-01-01", periods=80),
                "open": [100.0] * 80,
                "high": [102.0] * 80,
                "low": [98.0] * 80,
                "close": [100.0 + i * 0.1 for i in range(80)],
                "volume": [1_000_000] * 80,
                "amount": [100_000_000.0] * 80,
            }
        )

        optimizer = StrategyParamOptimizer(
            strategy_cls=DummyStrategy,
            stock_dfs={"TEST001": mock_df},
            param_grid={"window": [5, 10]},
            population_size=5,
            generations=2,
        )

        with mock.patch("gugu.analysis.heuristic_optimizer.BacktestEngine") as MockEngine:
            mock_instance = mock.Mock()
            mock_result = mock.Mock()
            mock_result.metrics = {
                "sharpe": 1.0,
                "total_return": 0.1,
                "max_drawdown": 0.05,
                "win_rate": 0.6,
            }
            mock_result.trades = [mock.Mock()] * 5
            mock_instance.run.return_value = mock_result
            MockEngine.return_value = mock_instance

            score = optimizer._fitness({"window": 10})

            assert score > -float("inf")
            assert isinstance(score, float)


class TestIntegrationScenarios:
    """集成场景测试。"""

    def test_ga_vs_pso_same_problem(self) -> None:
        """GA 和 PSO 在相同问题上应找到相似的最优解。"""

        def booth_fitness(params: dict[str, float]) -> float:
            x = params["x"]
            y = params["y"]
            return -((x + 2 * y - 7) ** 2 + (2 * x + y - 5) ** 2)

        spaces = [
            ParamSpace(name="x", min_val=-5, max_val=5, step=0.01, is_int=False),
            ParamSpace(name="y", min_val=-5, max_val=5, step=0.01, is_int=False),
        ]

        ga = GAOptimizer(
            param_spaces=spaces,
            fitness_func=booth_fitness,
            population_size=30,
            generations=40,
            seed=42,
        )
        ga_result = ga.optimize()

        pso = PSOOptimizer(
            param_spaces=spaces,
            fitness_func=booth_fitness,
            population_size=30,
            generations=40,
            seed=42,
        )
        pso_result = pso.optimize()

        assert ga_result.best_score > -5.0
        assert pso_result.best_score > -5.0

        assert abs(ga_result.best_params["x"] - 1.0) < 1.5
        assert abs(pso_result.best_params["x"] - 1.0) < 1.5

    def test_base_optimizer_evaluate_returns_tuple_fitness(self) -> None:
        """BaseHeuristicOptimizer._evaluate 应正确处理元组适应度。"""
        spaces = [ParamSpace(name="x", min_val=0, max_val=10, is_int=False)]

        def tuple_fitness(params: dict[str, float]) -> tuple[float, dict]:
            return float(params["x"]), {"info": "test"}

        ga = GAOptimizer(
            param_spaces=spaces,
            fitness_func=tuple_fitness,
            population_size=5,
            generations=2,
            seed=42,
        )
        score, metrics = ga._evaluate({"x": 5.0})
        assert score == 5.0
        assert metrics == {"info": "test"}

    def test_base_optimizer_evaluate_handles_exception(self) -> None:
        """BaseHeuristicOptimizer._evaluate 应捕获异常并返回 -inf。"""
        spaces = [ParamSpace(name="x", min_val=0, max_val=10, is_int=False)]

        def bad_fitness(params: dict[str, float]) -> float:
            raise RuntimeError("boom")

        ga = GAOptimizer(
            param_spaces=spaces,
            fitness_func=bad_fitness,
            population_size=5,
            generations=2,
            seed=42,
        )
        score, metrics = ga._evaluate({"x": 5.0})
        assert score == -float("inf")
        assert metrics is None
