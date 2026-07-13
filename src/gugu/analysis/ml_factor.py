"""因子正交化 + LightGBM 多因子模型。

正交化：用 Gram-Schmidt 方法去除因子间冗余，保留独立 alpha 信息。
LightGBM 模型：用正交化后的因子预测未来 5 日涨跌方向。

使用方式：
    from gugu.analysis.ml_factor import MLFactorModel
    model = MLFactorModel()
    model.train(factor_df, forward_returns)
    score = model.predict(factor_row)  # 0-1，>0.5 看涨
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

from gugu.utils.log import get_logger

logger = get_logger()


class FactorOrthogonalizer:
    """因子正交化器。

    使用 PCA 对有效因子做正交化，消除因子间高相关性，
    保留独立 alpha 信息，减少 LightGBM 过拟合风险。
    """

    def __init__(self, n_components: int | None = None, variance_threshold: float = 0.95) -> None:
        """初始化。

        Args:
            n_components: 保留的主成分数量，None 则按方差解释率自动选择
            variance_threshold: 累计方差解释率阈值（n_components=None 时生效）
        """
        self.n_components = n_components
        self.variance_threshold = variance_threshold
        self.scaler: StandardScaler | None = None
        self.pca: PCA | None = None
        self.factor_names: list[str] = []
        self.n_kept: int = 0

    def fit(self, factor_df: pd.DataFrame) -> FactorOrthogonalizer:
        """拟合正交化器。

        Args:
            factor_df: 因子矩阵（行=样本, 列=因子名）

        Returns:
            self
        """
        self.factor_names = list(factor_df.columns)
        data = factor_df.dropna().values

        if len(data) < 50:
            logger.warning(f"正交化样本不足: {len(data)}，跳过正交化")
            self.scaler = None
            self.pca = None
            self.n_kept = len(self.factor_names)
            return self

        self.scaler = StandardScaler()
        scaled = self.scaler.fit_transform(data)

        if self.n_components is not None:
            n = min(self.n_components, len(self.factor_names), len(data))
            self.pca = PCA(n_components=n)
        else:
            self.pca = PCA(n_components=self.variance_threshold)

        self.pca.fit(scaled)
        self.n_kept = self.pca.n_components_
        logger.info(
            f"因子正交化完成：{len(self.factor_names)} → {self.n_kept} 个主成分，"
            f"累计方差解释率: {float(np.sum(self.pca.explained_variance_ratio_)):.2%}"
        )
        return self

    def transform(self, factor_df: pd.DataFrame) -> np.ndarray:
        """将因子矩阵正交化。

        Args:
            factor_df: 因子矩阵

        Returns:
            正交化后的因子矩阵（行=样本, 列=主成分）
        """
        if self.scaler is None or self.pca is None:
            return np.asarray(factor_df.dropna().values)

        data = factor_df.reindex(columns=self.factor_names).fillna(0).values
        scaled = self.scaler.transform(data)
        return np.asarray(self.pca.transform(scaled))

    def fit_transform(self, factor_df: pd.DataFrame) -> np.ndarray:
        """拟合并转换。"""
        self.fit(factor_df)
        return self.transform(factor_df)


class MLFactorModel:
    """LightGBM 多因子模型。

    用正交化后的因子预测未来 5 日涨跌方向（二分类）。
    Walk-Forward 训练：只用历史数据训练，避免前视偏差。

    约束（适配 8GB RAM / 2 核 CPU）：
    - max_leaves=31（默认）
    - n_estimators=100
    - num_threads=2
    """

    MODEL_PATH = Path(__file__).resolve().parents[3] / "data" / "ml_factor_model.pkl"

    def __init__(self, forward_days: int = 5) -> None:
        """初始化。

        Args:
            forward_days: 未来收益天数
        """
        self.forward_days = forward_days
        self.orthogonalizer = FactorOrthogonalizer()
        self.model: Any = None  # lightgbm.LGBMClassifier
        self.feature_names: list[str] = []
        self.is_trained: bool = False

    def _prepare_labels(self, close: pd.Series) -> pd.Series:
        """准备标签：未来 N 日收益 > 0 → 1，否则 0。"""
        forward_ret = close.shift(-self.forward_days) / close - 1.0
        return (forward_ret > 0).astype(int)

    def train(
        self,
        factor_df: pd.DataFrame,
        close: pd.Series,
        factor_columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """训练 LightGBM 模型。

        Args:
            factor_df: 因子矩阵（含因子列 + close 列）
            close: 收盘价序列
            factor_columns: 使用的因子列名，None 则用所有数值列

        Returns:
            训练结果摘要
        """
        import lightgbm as lgb

        if factor_columns is None:
            factor_columns = [
                c for c in factor_df.columns
                if c not in ("date", "close", "open", "high", "low", "volume", "amount")
                and pd.api.types.is_numeric_dtype(factor_df[c])
            ]

        self.feature_names = factor_columns

        # 准备特征和标签
        feature_df = factor_df[factor_columns].copy()
        labels = self._prepare_labels(close)

        # 对齐
        aligned = feature_df.copy()
        aligned["_label"] = labels.values[: len(aligned)] if len(labels) >= len(aligned) else 0
        aligned = aligned.dropna(subset=["_label"])

        if len(aligned) < 100:
            logger.warning(f"训练样本不足: {len(aligned)}，跳过训练")
            return {"status": "skipped", "reason": "insufficient_samples", "samples": len(aligned)}

        # 正交化
        X = self.orthogonalizer.fit_transform(aligned[factor_columns])
        y = aligned["_label"].values[: len(X)].astype(int)

        if len(X) != len(y):
            min_len = min(len(X), len(y))
            X = X[:min_len]
            y = y[:min_len]

        # Walk-Forward：前 80% 训练，后 20% 验证
        split = int(len(X) * 0.8)
        X_train, X_val = X[:split], X[split:]
        y_train, y_val = y[:split], y[split:]

        if len(X_train) < 50 or len(np.unique(y_train)) < 2:
            logger.warning(f"训练集不足或标签单一: train={len(X_train)}, classes={len(np.unique(y_train))}")
            return {"status": "skipped", "reason": "insufficient_train_data"}

        self.model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=100,
            max_leaves=31,
            learning_rate=0.05,
            feature_fraction=0.8,
            bagging_fraction=0.8,
            bagging_freq=5,
            num_threads=2,
            verbose=-1,
            seed=42,
        )

        self.model.fit(
            X_train,
            y_train,
            eval_set=[(X_val, y_val)] if len(X_val) > 0 else None,
            callbacks=[lgb.log_evaluation(0)],
        )

        self.is_trained = True

        # 评估
        train_acc = float(self.model.score(X_train, y_train))
        val_acc = float(self.model.score(X_val, y_val)) if len(X_val) > 0 else 0.0

        result = {
            "status": "trained",
            "samples": len(X),
            "train_samples": len(X_train),
            "val_samples": len(X_val),
            "features": len(factor_columns),
            "orthogonalized_features": self.orthogonalizer.n_kept,
            "train_accuracy": round(train_acc, 4),
            "val_accuracy": round(val_acc, 4),
            "forward_days": self.forward_days,
        }
        logger.info(f"LightGBM 训练完成: {result}")
        return result

    def predict(self, factor_row: pd.Series | dict[str, float]) -> float:
        """预测单只股票的涨跌概率。

        Args:
            factor_row: 单行因子值

        Returns:
            涨的概率（0-1），>0.5 看涨
        """
        if not self.is_trained or self.model is None:
            return 0.5

        if isinstance(factor_row, dict):
            factor_row = pd.Series(factor_row)

        df = pd.DataFrame([factor_row])
        X = self.orthogonalizer.transform(df)
        if len(X) == 0:
            return 0.5

        proba = self.model.predict_proba(X)
        return float(proba[0][1])  # P(涨)

    def predict_batch(self, factor_df: pd.DataFrame) -> np.ndarray:
        """批量预测。

        Args:
            factor_df: 因子矩阵

        Returns:
            涨的概率数组（0-1）
        """
        if not self.is_trained or self.model is None:
            return np.full(len(factor_df), 0.5)

        X = self.orthogonalizer.transform(factor_df)
        if len(X) == 0:
            return np.full(len(factor_df), 0.5)

        proba = self.model.predict_proba(X)
        return np.asarray(proba[:, 1])

    def save(self, path: Path | None = None) -> Path:
        """保存模型到磁盘。"""
        path = path or self.MODEL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump(
                {
                    "orthogonalizer": self.orthogonalizer,
                    "model": self.model,
                    "feature_names": self.feature_names,
                    "forward_days": self.forward_days,
                    "is_trained": self.is_trained,
                },
                f,
            )
        logger.info(f"ML 模型已保存: {path}")
        return path

    def load(self, path: Path | None = None) -> bool:
        """从磁盘加载模型。"""
        path = path or self.MODEL_PATH
        if not path.exists():
            return False
        with path.open("rb") as f:
            data = pickle.load(f)
        self.orthogonalizer = data["orthogonalizer"]
        self.model = data["model"]
        self.feature_names = data["feature_names"]
        self.forward_days = data["forward_days"]
        self.is_trained = data["is_trained"]
        logger.info(f"ML 模型已加载: {path}")
        return True

    def get_feature_importance(self) -> dict[str, float]:
        """获取特征重要性。"""
        if not self.is_trained or self.model is None:
            return {}
        importance = self.model.feature_importances_
        names = [f"PC{i+1}" for i in range(len(importance))]
        return dict(zip(names, importance.tolist(), strict=True))
