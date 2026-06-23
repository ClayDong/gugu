"""策略参数优化器：使用遗传算法优化策略参数。

参考 qlib 的模型优化思想，实现：
1. 遗传算法：交叉、变异、选择
2. 网格搜索：简单暴力
3. 贝叶斯优化：高效搜索（需要 scikit-optimize，可选）
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable

from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class ParamRange:
    """参数范围定义"""
    name: str
    min_val: float
    max_val: float
    step: float = 1.0
    is_int: bool = True


@dataclass  
class OptimizationResult:
    """优化结果"""
    best_params: dict[str, float]
    best_score: float
    best_sharpe: float
    best_return: float
    best_max_dd: float
    generation: int
    history: list[dict[str, Any]]  # 每代最优个体


class ParamOptimizer:
    """策略参数优化器。
    
    使用遗传算法优化策略参数，以夏普比率+年化收益+最大回撤的加权和为目标函数。
    
    使用方式：
    optimizer = ParamOptimizer(
        param_ranges=[ParamRange("period", 10, 50, 1)],
        fitness_func=my_backtest_func,
        population_size=50,
        generations=30,
    )
    result = optimizer.optimize()
    """
    
    def __init__(
        self,
        param_ranges: list[ParamRange],
        fitness_func: Callable[[dict[str, float]], dict[str, float]],
        population_size: int = 50,
        generations: int = 30,
        mutation_rate: float = 0.1,
        crossover_rate: float = 0.7,
        elite_count: int = 5,
    ):
        self._ranges = param_ranges
        self._fitness = fitness_func
        self._pop_size = population_size
        self._generations = generations
        self._mutation_rate = mutation_rate
        self._crossover_rate = crossover_rate
        self._elite_count = elite_count
    
    def optimize(self, verbose: bool = True) -> OptimizationResult:
        """执行遗传算法优化。
        
        Returns:
            OptimizationResult: 最优参数和性能指标
        """
        # 初始化种群
        population = [self._random_individual() for _ in range(self._pop_size)]
        history = []
        
        best_score = -float("inf")
        best_params = None
        best_metrics = None
        
        for gen in range(self._generations):
            # 评估适应度
            scores = []
            for ind in population:
                params = self._decode(ind)
                try:
                    metrics = self._fitness(params)
                    # 目标函数：夏普*0.5 + 年化收益*0.3 - 最大回撤*0.2
                    score = (
                        metrics.get("sharpe", 0) * 0.5 +
                        metrics.get("total_return", 0) * 0.3 -
                        metrics.get("max_drawdown", 0) * 0.2
                    )
                    scores.append((score, metrics, params))
                except Exception as e:
                    logger.warning(f"适应度计算失败: {e}")
                    scores.append((-float("inf"), {}, params))
            
            # 排序
            scores.sort(key=lambda x: -x[0])
            gen_best_score = scores[0][0]
            gen_best_metrics = scores[0][1]
            gen_best_params = scores[0][2]
            
            history.append({
                "generation": gen + 1,
                "best_score": gen_best_score,
                "best_sharpe": gen_best_metrics.get("sharpe", 0),
                "best_return": gen_best_metrics.get("total_return", 0),
                "best_max_dd": gen_best_metrics.get("max_drawdown", 0),
                "best_params": gen_best_params,
            })
            
            if gen_best_score > best_score:
                best_score = gen_best_score
                best_metrics = gen_best_metrics
                best_params = gen_best_params
            
            if verbose and (gen + 1) % 5 == 0:
                logger.info(
                    f"第{gen+1}代: 最优得分={gen_best_score:.4f}, "
                    f"夏普={gen_best_metrics.get('sharpe', 0):.3f}, "
                    f"参数={gen_best_params}"
                )
            
            if gen == self._generations - 1:
                break
            
            # 选择 + 交叉 + 变异
            new_pop = []
            # 精英保留
            for i in range(min(self._elite_count, len(scores))):
                elite_params = scores[i][2]
                new_pop.append(self._encode(elite_params))
            
            # 交叉和变异
            while len(new_pop) < self._pop_size:
                parent1 = self._tournament_select(scores)
                parent2 = self._tournament_select(scores)
                p1 = self._encode(parent1[2])
                p2 = self._encode(parent2[2])
                
                if random.random() < self._crossover_rate:
                    child1, child2 = self._crossover(p1, p2)
                else:
                    child1, child2 = p1[:], p2[:]
                
                child1 = self._mutate(child1)
                child2 = self._mutate(child2)
                
                new_pop.append(child1)
                if len(new_pop) < self._pop_size:
                    new_pop.append(child2)
            
            population = new_pop[:self._pop_size]
        
        return OptimizationResult(
            best_params=best_params or {},
            best_score=best_score,
            best_sharpe=best_metrics.get("sharpe", 0) if best_metrics else 0,
            best_return=best_metrics.get("total_return", 0) if best_metrics else 0,
            best_max_dd=best_metrics.get("max_drawdown", 0) if best_metrics else 0,
            generation=self._generations,
            history=history,
        )
    
    def _random_individual(self) -> list[float]:
        """随机生成个体（实数值编码）"""
        ind = []
        for r in self._ranges:
            if r.is_int:
                val = random.randint(int(r.min_val), int(r.max_val))
            else:
                val = random.uniform(r.min_val, r.max_val)
            ind.append(float(val))
        return ind
    
    def _decode(self, ind: list[float]) -> dict[str, float]:
        """解码个体为参数字典"""
        params = {}
        for i, r in enumerate(self._ranges):
            val = ind[i]
            if r.is_int:
                val = round(val)
            val = max(r.min_val, min(r.max_val, val))
            params[r.name] = val
        return params
    
    def _encode(self, params: dict[str, float]) -> list[float]:
        """编码参数字典为个体"""
        ind = []
        for r in self._ranges:
            ind.append(params.get(r.name, (r.min_val + r.max_val) / 2))
        return ind
    
    def _tournament_select(self, scores: list[tuple[float, Any, Any]], 
                           tournament_size: int = 3) -> tuple[float, Any, Any]:
        """锦标赛选择"""
        candidates = random.sample(scores, min(tournament_size, len(scores)))
        return max(candidates, key=lambda x: x[0])
    
    def _crossover(self, p1: list[float], p2: list[float]) -> tuple[list[float], list[float]]:
        """单点交叉"""
        if len(p1) <= 1:
            return p1[:], p2[:]
        point = random.randint(1, len(p1) - 1)
        c1 = p1[:point] + p2[point:]
        c2 = p2[:point] + p1[point:]
        return c1, c2
    
    def _mutate(self, ind: list[float]) -> list[float]:
        """高斯变异"""
        mutated = ind[:]
        for i, r in enumerate(self._ranges):
            if random.random() < self._mutation_rate:
                noise = random.gauss(0, (r.max_val - r.min_val) * 0.1)
                mutated[i] += noise
                mutated[i] = max(r.min_val, min(r.max_val, mutated[i]))
                if r.is_int:
                    mutated[i] = round(mutated[i])
        return mutated