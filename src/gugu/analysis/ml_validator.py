"""ML 模型验证与解释工具箱。

为 ML 多因子策略提供不依赖 sklearn/lightgbm 的验证、解释和监控能力。
当 sklearn 可用时，自动启用更丰富的指标；否则使用 numpy/pandas 实现降级版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

try:
    from sklearn.metrics import (
        roc_auc_score,
    )

    _SKLEARN_AVAILABLE = True
except Exception:  # pragma: no cover - sklearn 未安装时降级
    _SKLEARN_AVAILABLE = False


@dataclass
class MLValidationReport:
    """ML 模型验证报告。"""

    accuracy: float
    precision: float
    recall: float
    f1: float
    auc_roc: float
    ic: float  # 预测分数与未来收益的秩相关系数
    sample_count: int
    positive_ratio: float
    feature_importance: dict[str, float] = field(default_factory=dict)
    permutation_importance: dict[str, float] = field(default_factory=dict)
    drift_score: float = 0.0
    recommendations: list[str] = field(default_factory=list)


def _safe_binary_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    """计算基础二分类指标，兼容 sklearn 不可用的情况。"""
    if len(y_true) == 0 or len(y_pred) == 0:
        return {"accuracy": 0.0, "precision": 0.0, "recall": 0.0, "f1": 0.0}

    y_true = np.asarray(y_true).astype(int)
    y_pred = np.asarray(y_pred).astype(int)

    accuracy = float(np.mean(y_true == y_pred))

    tp = float(np.sum((y_true == 1) & (y_pred == 1)))
    fp = float(np.sum((y_true == 0) & (y_pred == 1)))
    fn = float(np.sum((y_true == 1) & (y_pred == 0)))

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _compute_auc_roc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """计算 AUC-ROC，无 sklearn 时使用梯形法近似。"""
    if _SKLEARN_AVAILABLE:
        try:
            return float(roc_auc_score(y_true, y_score))  # type: ignore[operator]
        except Exception:
            pass

    y_true = np.asarray(y_true)
    y_score = np.asarray(y_score)
    if len(np.unique(y_true)) < 2:
        return 0.5

    order = np.argsort(-y_score)
    y_sorted = y_true[order]
    n_pos = np.sum(y_true == 1)
    n_neg = len(y_true) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5

    cum_neg = np.cumsum(y_sorted == 0)
    tpr = np.cumsum(y_sorted == 1) / n_pos
    fpr = cum_neg / n_neg
    _trapz = getattr(np, "trapezoid", None) or np.trapz
    return float(_trapz(tpr, fpr))


def _compute_ic(scores: np.ndarray, forward_returns: np.ndarray) -> float:
    """计算预测分数与未来收益的 Spearman IC。"""
    if len(scores) < 10:
        return 0.0
    df = pd.DataFrame({"score": scores, "ret": forward_returns}).dropna()
    if len(df) < 10:
        return 0.0
    return float(df["score"].corr(df["ret"], method="spearman"))


def validate_model_predictions(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    y_score: np.ndarray | None = None,
    forward_returns: np.ndarray | None = None,
) -> dict[str, float]:
    """验证模型预测结果，返回核心指标。

    Args:
        y_true: 真实标签（0/1）
        y_pred: 预测标签（0/1）
        y_score: 预测概率/分数（用于 AUC）
        forward_returns: 未来收益（用于 IC）

    Returns:
        dict: 包含 accuracy/precision/recall/f1/auc_roc/ic 的字典
    """
    metrics = _safe_binary_metrics(y_true, y_pred)
    metrics["auc_roc"] = _compute_auc_roc(y_true, y_score) if y_score is not None else 0.5
    metrics["ic"] = (
        _compute_ic(y_score if y_score is not None else y_pred.astype(float), forward_returns)
        if forward_returns is not None
        else 0.0
    )
    return metrics


def compute_permutation_importance(
    features: pd.DataFrame,
    y: np.ndarray,
    score_func,
    n_repeats: int = 5,
    seed: int | None = 42,
) -> dict[str, float]:
    """计算排列重要性（模型无关）。

    Args:
        X: 特征矩阵
        y: 真实标签
        score_func: 评分函数，接收 (X, y) 返回 float 分数（越高越好）
        n_repeats: 重复次数
        seed: 随机种子

    Returns:
        dict: {特征名: 重要性}
    """
    if seed is not None:
        rng = np.random.default_rng(seed)
    else:
        rng = np.random.default_rng()

    baseline = score_func(features, y)
    importance: dict[str, float] = {}

    for col in features.columns:
        drops: list[float] = []
        original = features[col].copy()
        for _ in range(n_repeats):
            permuted = original.sample(frac=1.0, random_state=int(rng.integers(0, 2**31))).values
            features[col] = permuted
            try:
                perm_score = score_func(features, y)
            except Exception:
                perm_score = baseline
            drops.append(max(0.0, baseline - perm_score))
            features[col] = original
        importance[col] = float(np.mean(drops))

    return importance


def detect_model_drift(
    reference_scores: np.ndarray,
    current_scores: np.ndarray,
    threshold: float = 0.1,
) -> dict[str, float]:
    """检测模型预测分布漂移（KS 统计量近似）。

    Args:
        reference_scores: 参考期预测分数
        current_scores: 当前期预测分数
        threshold: 漂移告警阈值

    Returns:
        dict: {drift_score, threshold, is_drifted}
    """
    ref = np.asarray(reference_scores)
    cur = np.asarray(current_scores)
    if len(ref) == 0 or len(cur) == 0:
        return {"drift_score": 0.0, "threshold": threshold, "is_drifted": False}

    # 使用 KS 统计量：两个样本经验分布的最大差异
    all_vals = np.sort(np.concatenate([ref, cur]))
    if len(all_vals) == 0:
        return {"drift_score": 0.0, "threshold": threshold, "is_drifted": False}

    def _ecdf(values: np.ndarray, x: np.ndarray) -> np.ndarray:
        return np.searchsorted(np.sort(values), x, side="right") / len(values)

    ref_cdf = _ecdf(ref, all_vals)
    cur_cdf = _ecdf(cur, all_vals)
    ks = float(np.max(np.abs(ref_cdf - cur_cdf)))

    return {
        "drift_score": ks,
        "threshold": threshold,
        "is_drifted": ks > threshold,
    }


def build_validation_report(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    y_score: np.ndarray | None = None,
    forward_returns: np.ndarray | None = None,
    feature_importance: dict[str, float] | None = None,
    permutation_importance: dict[str, float] | None = None,
    drift_info: dict[str, float] | None = None,
) -> MLValidationReport:
    """构建 ML 模型验证报告。"""
    metrics = validate_model_predictions(y_true, y_pred, y_score, forward_returns)
    y_true_arr = np.asarray(y_true)

    recommendations: list[str] = []
    if metrics["accuracy"] < 0.5:
        recommendations.append("准确率低于随机水平，建议检查标签或特征")
    if metrics["auc_roc"] < 0.55:
        recommendations.append("AUC-ROC 过低，模型区分能力弱")
    if metrics["ic"] < 0.02:
        recommendations.append("预测分数与未来收益 IC 过低，信号有效性不足")
    if drift_info and drift_info.get("is_drifted"):
        recommendations.append(
            f"检测到模型漂移 (score={drift_info['drift_score']:.3f})，建议重新训练"
        )
    if not recommendations:
        recommendations.append("模型验证通过，建议持续监控")

    return MLValidationReport(
        accuracy=metrics["accuracy"],
        precision=metrics["precision"],
        recall=metrics["recall"],
        f1=metrics["f1"],
        auc_roc=metrics["auc_roc"],
        ic=metrics["ic"],
        sample_count=len(y_true_arr),
        positive_ratio=float(np.mean(y_true_arr == 1)),
        feature_importance=feature_importance or {},
        permutation_importance=permutation_importance or {},
        drift_score=drift_info.get("drift_score", 0.0) if drift_info else 0.0,
        recommendations=recommendations,
    )


def explain_prediction(
    feature_values: dict[str, float],
    baseline: dict[str, float] | None = None,
) -> dict[str, float]:
    """生成单条预测的简化解释：特征偏离基线的幅度。

    Args:
        feature_values: 当前特征值
        baseline: 特征基线（均值），为空时使用 0

    Returns:
        dict: {特征名: 偏离度}
    """
    baseline = baseline or {}
    deviations = {}
    for name, value in feature_values.items():
        base = baseline.get(name, 0.0)
        std = max(abs(base), 1e-6)
        deviations[name] = round((value - base) / std, 4)
    return deviations


def validate_ml_strategy_oos(
    predictions: pd.DataFrame,
    close: pd.Series,
    forward_days: int = 5,
) -> MLValidationReport:
    """对 ML 策略做样本外验证。

    Args:
        predictions: DataFrame 含 'score', 'signal' 列
        close: 收盘价序列
        forward_days: 前瞻收益天数

    Returns:
        MLValidationReport
    """
    df = pd.DataFrame(
        {
            "score": predictions["score"].values,
            "signal": predictions["signal"].values,
            "close": close.values[: len(predictions)],
        }
    )
    df["forward_ret"] = df["close"].shift(-forward_days) / df["close"] - 1.0
    df = df.dropna(subset=["forward_ret"])

    if len(df) < 30:
        return MLValidationReport(
            accuracy=0.0,
            precision=0.0,
            recall=0.0,
            f1=0.0,
            auc_roc=0.5,
            ic=0.0,
            sample_count=len(df),
            positive_ratio=0.0,
            recommendations=["样本量不足，无法完成 OOS 验证"],
        )

    # 用收益分位数构造伪标签：top 40% 为 1，bottom 40% 为 0
    q60 = df["forward_ret"].quantile(0.6)
    q40 = df["forward_ret"].quantile(0.4)
    y_true = pd.Series(0, index=df.index)
    y_true[df["forward_ret"] > q60] = 1
    y_true[df["forward_ret"] < q40] = 0
    y_true = y_true[(df["forward_ret"] > q60) | (df["forward_ret"] < q40)]

    aligned = df.loc[y_true.index].copy()
    y_pred = (aligned["score"] > 0.5).astype(int)

    return build_validation_report(
        y_true=y_true.values,
        y_pred=y_pred.values,
        y_score=aligned["score"].values,
        forward_returns=aligned["forward_ret"].values,
    )
