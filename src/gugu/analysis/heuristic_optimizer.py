"""启发式策略参数优化器：遗传算法(GA) + 粒子群优化(PSO)。

为 gugu 的短线全自动、自适应调参需求设计，特点：
- 不依赖外部重依赖（仅用标准库 + numpy/pandas）
- 参数空间从 strategy_defaults.yaml + 预定义网格自动构建
- 目标函数支持夏普、收益风险比、Calmar、胜率等多目标加权
- 约束函数可嵌入参数语义约束（如 short_window < long_window）
- 输出参数需通过 4 项解锁条件（与 scripts/optimize_params.py 对齐）
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from gugu.backtest.engine import BacktestEngine
from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class ParamSpace:
    """单个参数的搜索空间。"""

    name: str
    min_val: float
    max_val: float
    step: float = 1.0
    is_int: bool = True

    def sample(self, rng: np.random.Generator | None = None) -> float:
        """在搜索空间内随机采样一个值。"""
        if rng is None:
            rng = np.random.default_rng()
        if self.is_int:
            return int(rng.integers(int(self.min_val), int(self.max_val) + 1))
        return float(rng.uniform(self.min_val, self.max_val))

    def clip(self, value: float) -> float:
        """将值裁剪到合法范围并按 step 量化。"""
        value = max(self.min_val, min(self.max_val, value))
        if self.is_int:
            return int(round(value))
        # 浮点参数按 step 量化
        if self.step > 0:
            value = round(value / self.step) * self.step
        return value


@dataclass
class OptimizationResult:
    """启发式优化结果。"""

    best_params: dict[str, float]
    best_score: float
    best_metrics: dict[str, float]
    history: list[dict[str, Any]] = field(default_factory=list)
    generations: int = 0
    population_size: int = 0


class BaseHeuristicOptimizer(ABC):
    """启发式优化器基类。"""

    def __init__(
        self,
        param_spaces: list[ParamSpace],
        fitness_func: Callable[[dict[str, float]], float],
        population_size: int = 30,
        generations: int = 20,
        seed: int | None = None,
        # P2-4: Early Stopping 参数
        early_stop_patience: int = 5,  # 连续 N 代无改进时提前停止
        early_stop_min_improvement: float = 1e-6,  # 最小改进阈值
    ):
        self.param_spaces = {p.name: p for p in param_spaces}
        self.param_names = list(self.param_spaces.keys())
        self.fitness_func = fitness_func
        self.population_size = population_size
        self.generations = generations
        self.rng = np.random.default_rng(seed)
        self.early_stop_patience = early_stop_patience
        self.early_stop_min_improvement = early_stop_min_improvement

    @abstractmethod
    def optimize(self, verbose: bool = False) -> OptimizationResult:
        """执行优化并返回最优参数。"""

    def _random_params(self) -> dict[str, float]:
        """随机生成一组参数。"""
        return {name: space.sample(self.rng) for name, space in self.param_spaces.items()}

    def _clip_params(self, params: dict[str, float]) -> dict[str, float]:
        """将参数裁剪到合法范围。"""
        return {
            name: self.param_spaces[name].clip(value)
            for name, value in params.items()
            if name in self.param_spaces
        }

    def _evaluate(self, params: dict[str, float]) -> tuple[float, dict[str, float] | None]:
        """评估一组参数的适应度，返回 (score, metrics)。"""
        try:
            score = self.fitness_func(params)
            if isinstance(score, tuple):
                return score[0], score[1]
            return score, {}
        except Exception as e:
            logger.debug(f"适应度计算失败: {e}")
            return -float("inf"), None

    def _is_meaningful_improvement(self, new_score: float, old_score: float) -> bool:
        """判断得分是否构成真实改进（早停用）。

        v0.9.55 防护：非有限得分必须显式处理，否则当适应度全为 -inf 时
        `-inf - (-inf) = NaN`，NaN 与任何数比较均为 False → 早停计数器永不
        递增、优化跑满全部世代撞超时预算（实证：double_yin 2026-09-15 /
        10-01 / 10-15 三次启发式优化全部超时，日志全为 byte 级相同回测行）。
        """
        if math.isnan(new_score) or math.isnan(old_score):
            return False
        if not math.isfinite(old_score) and math.isfinite(new_score):
            # 从 -inf（初始值）首次进入有限域 = 实质改进
            return True
        if not math.isfinite(new_score):
            # 新值非有限（全 -inf 景观）→ 无改进
            return False
        return new_score - old_score >= self.early_stop_min_improvement


class GAOptimizer(BaseHeuristicOptimizer):
    """遗传算法参数优化器。"""

    def __init__(
        self,
        param_spaces: list[ParamSpace],
        fitness_func: Callable[[dict[str, float]], float],
        population_size: int = 30,
        generations: int = 20,
        mutation_rate: float = 0.1,
        crossover_rate: float = 0.7,
        elite_ratio: float = 0.1,
        seed: int | None = None,
        early_stop_patience: int = 5,
        early_stop_min_improvement: float = 1e-6,
    ):
        super().__init__(
            param_spaces,
            fitness_func,
            population_size,
            generations,
            seed,
            early_stop_patience,
            early_stop_min_improvement,
        )
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.elite_count = max(1, int(population_size * elite_ratio))

    def optimize(self, verbose: bool = False) -> OptimizationResult:
        """执行遗传算法优化。"""
        population = [self._random_params() for _ in range(self.population_size)]
        history: list[dict[str, Any]] = []

        best_score = -float("inf")
        best_params: dict[str, float] = {}
        best_metrics: dict[str, float] = {}
        # P2-4: Early Stopping 计数器
        _no_improve_count = 0
        _prev_score: float | None = None

        for gen in range(self.generations):
            # 评估种群
            scored: list[tuple[float, dict[str, float] | None, dict[str, float]]] = []
            for ind in population:
                score, metrics = self._evaluate(ind)
                scored.append((score, metrics, ind))

            # 按得分排序
            scored.sort(key=lambda x: -x[0])

            gen_best_score, gen_best_metrics, gen_best_params = scored[0]
            if gen_best_metrics is None:
                gen_best_metrics = {}

            if gen_best_score > best_score:
                best_score = gen_best_score
                best_params = gen_best_params.copy()
                best_metrics = gen_best_metrics

            history.append(
                {
                    "generation": gen + 1,
                    "best_score": gen_best_score,
                    "best_params": gen_best_params,
                    "best_metrics": gen_best_metrics,
                }
            )

            if verbose and (gen + 1) % 5 == 0:
                logger.info(
                    f"GA 第{gen + 1}代: 最优得分={gen_best_score:.4f}, 参数={gen_best_params}"
                )

            # P2-4: Early Stopping（v0.9.55：非有限得分按"无改进"处理，
            # 修复全 -inf 景观下 -inf - -inf = NaN 导致早停失效、跑满世代）
            if not self._is_meaningful_improvement(gen_best_score, best_score):
                _no_improve_count += 1
            else:
                _no_improve_count = 0
            if _no_improve_count >= self.early_stop_patience:
                _actual_generations = gen + 1
                if self.generations - _actual_generations > 2:
                    logger.info(
                        f"GA Early Stopping at gen {gen + 1}, "
                        f"best_score={best_score:.4f} (no improvement for {_no_improve_count} gens)"
                    )
                break

            # 精英保留
            new_population = [
                scored[i][2].copy() for i in range(min(self.elite_count, len(scored)))
            ]

            # 选择、交叉、变异
            while len(new_population) < self.population_size:
                parent1 = self._tournament_select(scored)
                parent2 = self._tournament_select(scored)

                if self.rng.random() < self.crossover_rate:
                    child1, child2 = self._crossover(parent1, parent2)
                else:
                    child1, child2 = parent1.copy(), parent2.copy()

                child1 = self._mutate(child1)
                child2 = self._mutate(child2)

                new_population.append(child1)
                if len(new_population) < self.population_size:
                    new_population.append(child2)

            population = new_population[: self.population_size]

        return OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            best_metrics=best_metrics,
            history=history,
            generations=self.generations,
            population_size=self.population_size,
        )

    def _tournament_select(
        self,
        scored: list[tuple[float, Any, dict[str, float]]],
        tournament_size: int = 3,
    ) -> dict[str, float]:
        """锦标赛选择。"""
        candidates = self.rng.choice(
            len(scored), size=min(tournament_size, len(scored)), replace=False
        )
        candidates = [scored[i] for i in candidates]
        return max(candidates, key=lambda x: x[0])[2]

    def _crossover(
        self, parent1: dict[str, float], parent2: dict[str, float]
    ) -> tuple[dict[str, float], dict[str, float]]:
        """单点交叉（按参数名顺序）。"""
        names = list(parent1.keys())
        if len(names) <= 1:
            return parent1.copy(), parent2.copy()
        point = self.rng.integers(1, len(names))
        child1, child2 = {}, {}
        for i, name in enumerate(names):
            if i < point:
                child1[name] = parent1[name]
                child2[name] = parent2[name]
            else:
                child1[name] = parent2[name]
                child2[name] = parent1[name]
        return child1, child2

    def _mutate(self, params: dict[str, float]) -> dict[str, float]:
        """高斯变异。"""
        mutated = params.copy()
        for name, space in self.param_spaces.items():
            if self.rng.random() < self.mutation_rate:
                range_size = space.max_val - space.min_val
                noise = self.rng.normal(0, range_size * 0.1)
                mutated[name] = space.clip(mutated[name] + noise)
        return mutated


class PSOOptimizer(BaseHeuristicOptimizer):
    """粒子群优化参数优化器。"""

    def __init__(
        self,
        param_spaces: list[ParamSpace],
        fitness_func: Callable[[dict[str, float]], float],
        population_size: int = 30,
        generations: int = 20,
        w: float = 0.7,
        c1: float = 1.5,
        c2: float = 1.5,
        seed: int | None = None,
        early_stop_patience: int = 5,
        early_stop_min_improvement: float = 1e-6,
    ):
        super().__init__(
            param_spaces,
            fitness_func,
            population_size,
            generations,
            seed,
            early_stop_patience,
            early_stop_min_improvement,
        )
        self.w = w
        self.c1 = c1
        self.c2 = c2

    def optimize(self, verbose: bool = False) -> OptimizationResult:
        """执行粒子群优化。"""
        # 初始化粒子位置和速度
        particles: list[dict[str, float]] = [
            self._random_params() for _ in range(self.population_size)
        ]
        velocities: dict[int, dict[str, float]] = {
            i: dict.fromkeys(self.param_names, 0.0) for i in range(self.population_size)
        }

        personal_best: list[tuple[float, dict[str, float]]] = []
        for p in particles:
            score, _ = self._evaluate(p)
            personal_best.append((score, p.copy()))

        # v0.9.55：初始种群适应度全为 -inf（策略在当前股票池上 trades≥3 样本
        # 不足）→ 继续搜索既无意义又会跑满世代撞超时预算，直接拒绝并输出
        # 可诊断警告（实证：double_yin 股票池 4/5 只 500 天 0 交易）。
        if not any(math.isfinite(pb[0]) for pb in personal_best):
            logger.warning(
                "PSO 初始种群适应度全为 -inf（策略在当前股票池 trades≥3 样本"
                "不足），参数优化无意义，提前跳过"
            )
            return OptimizationResult(
                best_params=particles[0].copy(),
                best_score=-float("inf"),
                best_metrics={},
                history=[],
                generations=0,
                population_size=self.population_size,
            )

        global_best_score = max(pb[0] for pb in personal_best)
        global_best_params = personal_best[0][1].copy()

        history: list[dict[str, Any]] = []

        # P2-4: Early Stopping 计数器
        _no_improve_count = 0
        _prev_score: float | None = None

        for gen in range(self.generations):
            for i, particle in enumerate(particles):
                score, _metrics = self._evaluate(particle)

                # 更新个体最优
                if score > personal_best[i][0]:
                    personal_best[i] = (score, particle.copy())

                # 更新全局最优
                if score > global_best_score:
                    global_best_score = score
                    global_best_params = particle.copy()

                # 更新速度
                for name in self.param_names:
                    space = self.param_spaces[name]
                    range_size = space.max_val - space.min_val
                    r1, r2 = self.rng.random(), self.rng.random()
                    velocities[i][name] = (
                        self.w * velocities[i][name]
                        + self.c1 * r1 * (personal_best[i][1][name] - particle[name])
                        + self.c2 * r2 * (global_best_params[name] - particle[name])
                    )
                    # 限制速度不超过参数范围的 20%
                    max_velocity = range_size * 0.2
                    velocities[i][name] = max(-max_velocity, min(max_velocity, velocities[i][name]))

                # 更新位置
                for name in self.param_names:
                    particle[name] = self.param_spaces[name].clip(
                        particle[name] + velocities[i][name]
                    )

            history.append(
                {
                    "generation": gen + 1,
                    "best_score": global_best_score,
                    "best_params": global_best_params.copy(),
                }
            )

            if verbose and (gen + 1) % 5 == 0:
                logger.info(
                    f"PSO 第{gen + 1}代: 最优得分={global_best_score:.4f}, "
                    f"参数={global_best_params}"
                )

            # P2-4: Early Stopping（v0.9.55：同 GA，非有限得分按"无改进"处理）
            if _prev_score is not None and not self._is_meaningful_improvement(
                global_best_score, _prev_score
            ):
                _no_improve_count += 1
            else:
                _no_improve_count = 0
            _prev_score = global_best_score
            if _no_improve_count >= self.early_stop_patience:
                _actual_generations = gen + 1
                if self.generations - _actual_generations > 2:
                    logger.info(
                        f"PSO Early Stopping at gen {gen + 1}, "
                        f"best_score={global_best_score:.4f} (no improvement for {_no_improve_count} gens)"
                    )
                break

        # 最终评估最优参数
        _, best_metrics = self._evaluate(global_best_params)
        if best_metrics is None:
            best_metrics = {}

        return OptimizationResult(
            best_params=global_best_params,
            best_score=global_best_score,
            best_metrics=best_metrics,
            history=history,
            generations=self.generations,
            population_size=self.population_size,
        )


class StrategyParamOptimizer:
    """面向策略参数的启发式优化器封装。

    将策略参数网格转换为 ParamSpace，用回测引擎作为 fitness 函数，
    支持 GA/PSO 两种算法。
    """

    def __init__(
        self,
        strategy_cls: type[Strategy],
        stock_dfs: dict[str, pd.DataFrame],
        param_grid: dict[str, list],
        param_constraints: Callable[[dict], bool] | None = None,
        algorithm: str = "ga",
        population_size: int = 30,
        generations: int = 20,
        seed: int | None = None,
    ):
        self.strategy_cls = strategy_cls
        self.stock_dfs = stock_dfs
        self.param_grid = param_grid
        self.param_constraints = param_constraints
        self.algorithm = algorithm.lower()
        self.population_size = population_size
        self.generations = generations
        self.seed = seed

        self.param_spaces = self._build_param_spaces()

    def _build_param_spaces(self) -> list[ParamSpace]:
        """从参数网格构建 ParamSpace 列表。"""
        spaces: list[ParamSpace] = []
        for name, values in self.param_grid.items():
            if not values:
                continue
            # 判断是否为整数参数
            is_int = all(isinstance(v, int) for v in values)
            min_val = min(values)
            max_val = max(values)
            # step 取相邻值的最小差值
            sorted_values = sorted(set(values))
            if len(sorted_values) > 1:
                step = min(
                    sorted_values[i + 1] - sorted_values[i] for i in range(len(sorted_values) - 1)
                )
            else:
                step = 1.0 if is_int else 0.01
            spaces.append(
                ParamSpace(name=name, min_val=min_val, max_val=max_val, step=step, is_int=is_int)
            )
        return spaces

    def _fitness(self, params: dict[str, float]) -> float:
        """适应度函数：多股聚合回测的加权评分。"""
        params = self._clip_and_constrain(params)
        if params is None:
            return -float("inf")

        engine = BacktestEngine()
        total_score = 0.0
        valid_count = 0

        for symbol, df in self.stock_dfs.items():
            if len(df) < 60:
                continue
            try:
                strategy = self.strategy_cls(params)
                result = engine.run(strategy, df, symbol=symbol)
                metrics = result.metrics

                sharpe = metrics.get("sharpe", 0)
                total_return = metrics.get("total_return", 0)
                max_dd = metrics.get("max_drawdown", 0)
                win_rate = metrics.get("win_rate", 0)
                trades = len(result.trades)

                # 交易次数过少，认为不可信
                if trades < 3:
                    continue

                # Calmar 比率（回撤为零时取年化收益）
                calmar = total_return / max_dd if max_dd > 0 else total_return * 5

                # 综合评分：夏普 40% + Calmar 30% + 胜率 20% + 收益 10%
                score = sharpe * 0.4 + calmar * 0.3 + win_rate * 0.2 + total_return * 0.1
                total_score += score
                valid_count += 1
            except Exception as e:
                logger.debug("回测失败 {}: {}", symbol, e)
                continue

        if valid_count == 0:
            return -float("inf")

        return total_score / valid_count

    def _clip_and_constrain(self, params: dict[str, float]) -> dict[str, float] | None:
        """裁剪参数并检查约束。"""
        clipped = {
            space.name: space.clip(params.get(space.name, space.min_val))
            for space in self.param_spaces
        }
        if self.param_constraints is not None and not self.param_constraints(clipped):
            return None
        return clipped

    def optimize(self, verbose: bool = False) -> OptimizationResult:
        """执行优化。"""
        if self.algorithm == "ga":
            optimizer: BaseHeuristicOptimizer = GAOptimizer(
                param_spaces=self.param_spaces,
                fitness_func=self._fitness,
                population_size=self.population_size,
                generations=self.generations,
                seed=self.seed,
            )
        elif self.algorithm == "pso":
            optimizer = PSOOptimizer(
                param_spaces=self.param_spaces,
                fitness_func=self._fitness,
                population_size=self.population_size,
                generations=self.generations,
                seed=self.seed,
            )
        else:
            raise ValueError(f"不支持的优化算法: {self.algorithm}，仅支持 ga/pso")

        return optimizer.optimize(verbose=verbose)
