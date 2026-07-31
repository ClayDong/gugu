"""策略相关性分析模块。

用于分析多策略信号之间的相关性，识别冗余策略和互补策略，
为策略池精简和权重优化提供数据支持。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class StrategyCorrelationResult:
    """策略相关性分析结果。"""

    correlation_matrix: pd.DataFrame
    redundant_pairs: list[tuple[str, str, float]]
    complementary_pairs: list[tuple[str, str, float]]
    independence_scores: dict[str, float]
    recommendations: list[str]


def build_signal_matrix(
    signals: dict[str, list[tuple[str, int, float]]],
    symbols: list[str],
) -> pd.DataFrame:
    """将策略信号转换为信号矩阵。

    Args:
        signals: {strategy_name: [(symbol, signal, confidence), ...]}
        symbols: 股票代码列表

    Returns:
        DataFrame: 列为策略，行为股票×信号方向的矩阵
    """
    matrix: dict[str, list[float]] = {}
    symbol_index: list[str] = []

    for symbol in symbols:
        for direction in ("buy", "sell"):
            symbol_index.append(f"{symbol}_{direction}")

    for strategy_name, strategy_signals in signals.items():
        row: dict[str, float] = {}
        for symbol in symbols:
            matched = [(sig, conf) for sym, sig, conf in strategy_signals if sym == symbol]
            buy_conf = sum(conf for sig, conf in matched if sig == 1)
            sell_conf = sum(conf for sig, conf in matched if sig == -1)
            row[f"{symbol}_buy"] = buy_conf
            row[f"{symbol}_sell"] = sell_conf
        matrix[strategy_name] = [row.get(idx, 0.0) for idx in symbol_index]

    df = pd.DataFrame(matrix, index=symbol_index)
    return df


def analyze_correlation(
    signal_matrix: pd.DataFrame,
    redundancy_threshold: float = 0.7,
    complementary_threshold: float = -0.3,
) -> StrategyCorrelationResult:
    """分析策略信号相关性。

    Args:
        signal_matrix: 策略信号矩阵（列为策略）
        redundancy_threshold: 冗余判定阈值（绝对相关性 > 此值）
        complementary_threshold: 互补判定阈值（相关性 < 此值）

    Returns:
        StrategyCorrelationResult
    """
    if signal_matrix.empty or len(signal_matrix.columns) < 2:
        return StrategyCorrelationResult(
            correlation_matrix=pd.DataFrame(),
            redundant_pairs=[],
            complementary_pairs=[],
            independence_scores={},
            recommendations=["策略数量不足，无法计算相关性"],
        )

    # 处理常数列（某策略无信号时方差为0）
    valid_cols = [col for col in signal_matrix.columns if signal_matrix[col].std() > 1e-10]
    if len(valid_cols) < 2:
        return StrategyCorrelationResult(
            correlation_matrix=pd.DataFrame(),
            redundant_pairs=[],
            complementary_pairs=[],
            independence_scores=dict.fromkeys(signal_matrix.columns, 1.0),
            recommendations=["有效策略数量不足，无法计算相关性"],
        )

    df = signal_matrix[valid_cols]
    corr = df.corr(method="pearson")

    redundant_pairs: list[tuple[str, str, float]] = []
    complementary_pairs: list[tuple[str, str, float]] = []

    strategies = list(corr.columns)
    for i, s1 in enumerate(strategies):
        for s2 in strategies[i + 1 :]:
            val = corr.loc[s1, s2]
            if abs(val) >= redundancy_threshold:
                redundant_pairs.append((s1, s2, val))
            elif val <= complementary_threshold:
                complementary_pairs.append((s1, s2, val))

    # 独立性评分：与该策略平均绝对相关性的倒数（越独立越高）
    independence_scores: dict[str, float] = {}
    for s in strategies:
        others = [x for x in strategies if x != s]
        if others:
            avg_corr = corr.loc[s, others].abs().mean()
            independence_scores[s] = round(1.0 - avg_corr, 4)
        else:
            independence_scores[s] = 1.0

    recommendations = _build_recommendations(
        redundant_pairs, complementary_pairs, independence_scores
    )

    return StrategyCorrelationResult(
        correlation_matrix=corr,
        redundant_pairs=sorted(redundant_pairs, key=lambda x: abs(x[2]), reverse=True),
        complementary_pairs=sorted(complementary_pairs, key=lambda x: x[2]),
        independence_scores=independence_scores,
        recommendations=recommendations,
    )


def _build_recommendations(
    redundant_pairs: list[tuple[str, str, float]],
    complementary_pairs: list[tuple[str, str, float]],
    independence_scores: dict[str, float],
) -> list[str]:
    """生成优化建议。"""
    recommendations: list[str] = []

    if redundant_pairs:
        recommendations.append(
            f"发现 {len(redundant_pairs)} 对高相关策略，建议降权或去重: "
            + ", ".join([f"{a}-{b}({c:.2f})" for a, b, c in redundant_pairs[:3]])
        )

    if complementary_pairs:
        recommendations.append(
            f"发现 {len(complementary_pairs)} 对互补策略，建议保留组合: "
            + ", ".join([f"{a}-{b}({c:.2f})" for a, b, c in complementary_pairs[:3]])
        )

    if independence_scores:
        most_independent = max(independence_scores.items(), key=lambda x: x[1])
        recommendations.append(
            f"独立性最高策略: {most_independent[0]} (score={most_independent[1]:.2f})"
        )

    if not recommendations:
        recommendations.append("策略相关性较低，当前组合分散度良好")

    return recommendations


def recommend_weights(
    correlation_result: StrategyCorrelationResult,
    base_weights: dict[str, float],
) -> dict[str, float]:
    """基于相关性分析结果推荐权重。

    规则：
    - 高冗余策略对：降低权重
    - 高独立性策略：保持或提升权重
    - 权重归一化到与 base_weights 相同的总和
    """
    if not base_weights:
        return {}

    total = sum(base_weights.values())
    adjusted = dict(base_weights)

    # 对冗余对中的策略轻微降权
    for a, b, corr in correlation_result.redundant_pairs:
        if corr > 0.8:
            factor = 0.9
        else:
            factor = 0.95
        if a in adjusted:
            adjusted[a] *= factor
        if b in adjusted:
            adjusted[b] *= factor

    # 对独立性高的策略提升权重
    for name, score in correlation_result.independence_scores.items():
        if name in adjusted and score > 0.7:
            adjusted[name] *= 1.05

    # 归一化
    new_total = sum(adjusted.values())
    if new_total > 0:
        adjusted = {k: v / new_total * total for k, v in adjusted.items()}

    return adjusted
