"""因子正交化 + LightGBM 多因子模型（v2 优化版）。

v2 优化：
1. 因子方向处理：训练前计算 IC，负 IC 因子取反
2. 特征工程：加入因子一阶差分 + 5 日滚动均值
3. 标签优化：用收益分位数分类（top 40% 涨 / bottom 40% 跌 / 中间丢弃）
4. Walk-Forward 滚动窗口：替代静态 80/20 分割
5. 信号频率控制：在策略层加入冷却期

使用方式：
    from gugu.analysis.ml_factor import MLFactorModel
    model = MLFactorModel()
    model.train(factor_df, close)
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
        self.n_components = n_components
        self.variance_threshold = variance_threshold
        self.scaler: StandardScaler | None = None
        self.pca: PCA | None = None
        self.factor_names: list[str] = []
        self.n_kept: int = 0

    def fit(self, factor_df: pd.DataFrame) -> FactorOrthogonalizer:
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
        if self.scaler is None or self.pca is None:
            return np.asarray(factor_df.dropna().values)

        data = factor_df.reindex(columns=self.factor_names).fillna(0).values
        scaled = self.scaler.transform(data)
        return np.asarray(self.pca.transform(scaled))

    def fit_transform(self, factor_df: pd.DataFrame) -> np.ndarray:
        self.fit(factor_df)
        return self.transform(factor_df)


def _compute_factor_ic(
    factor_df: pd.DataFrame,
    close: pd.Series,
    factor_columns: list[str],
    forward_days: int,
) -> dict[str, float]:
    """计算因子 IC，返回 {因子名: IC 均值}。"""
    forward_ret = close.shift(-forward_days) / close - 1.0
    ic_means: dict[str, float] = {}

    for col in factor_columns:
        if col not in factor_df.columns:
            continue
        aligned = pd.DataFrame({"f": factor_df[col], "r": forward_ret}).dropna()
        if len(aligned) < 20:
            ic_means[col] = 0.0
            continue
        # 滚动 20 天窗口计算 IC 均值
        window = 20
        ics: list[float] = []
        for i in range(0, len(aligned) - window, 5):
            chunk = aligned.iloc[i : i + window]
            if len(chunk) < 10:
                continue
            if chunk["f"].std() == 0 or chunk["r"].std() == 0:
                continue
            ic = chunk["f"].corr(chunk["r"], method="spearman")
            if not np.isnan(ic):
                ics.append(ic)
        ic_means[col] = float(np.mean(ics)) if ics else 0.0

    return ic_means


def _add_feature_engineering(factor_df: pd.DataFrame, factor_columns: list[str]) -> pd.DataFrame:
    """特征工程：加入因子一阶差分 + 5 日滚动均值 + Top5 因子交互特征。"""
    enhanced = factor_df.copy()
    for col in factor_columns:
        if col not in enhanced.columns:
            continue
        # 一阶差分（因子变化率）
        enhanced[f"{col}_diff"] = enhanced[col].diff().fillna(0)
        # 5 日滚动均值（因子平滑）
        enhanced[f"{col}_ma5"] = enhanced[col].rolling(5, min_periods=1).mean().fillna(0)

    # 因子交互特征：Top 5 IC 因子的两两乘积
    # 只对 IC 最高的前 5 个因子做交叉，避免特征爆炸
    top5_candidates = ["obv_divergence", "ma_deviation_60", "ma_deviation_20", "di_cross", "momentum_20d"]
    available_top5 = [c for c in top5_candidates if c in enhanced.columns]
    for i in range(len(available_top5)):
        for j in range(i + 1, len(available_top5)):
            a, b = available_top5[i], available_top5[j]
            enhanced[f"{a}_x_{b}"] = enhanced[a] * enhanced[b]

    return enhanced


class MLFactorModel:
    """LightGBM 多因子模型（v2）。

    优化项：
    1. 因子方向处理：训练前计算 IC，负 IC 因子取反
    2. 特征工程：因子一阶差分 + 5 日滚动均值
    3. 标签优化：收益分位数分类（top 40% = 1, bottom 40% = 0, 中间丢弃）
    4. Walk-Forward 滚动窗口训练
    """

    MODEL_PATH = Path(__file__).resolve().parents[3] / "data" / "ml_factor_model.pkl"

    def __init__(self, forward_days: int = 5) -> None:
        self.forward_days = forward_days
        self.orthogonalizer = FactorOrthogonalizer()
        self.model: Any = None
        self.feature_names: list[str] = []
        self.reversed_factors: list[str] = []
        self.is_trained: bool = False

    def _prepare_labels_quantile(self, close: pd.Series) -> pd.Series:
        """标签优化：用收益分位数分类。

        top 40% 收益 → 1（涨）
        bottom 40% 收益 → 0（跌）
        中间 20% → -1（丢弃，不参与训练）
        """
        forward_ret = close.shift(-self.forward_days) / close - 1.0
        q60 = forward_ret.quantile(0.6)
        q40 = forward_ret.quantile(0.4)
        labels = pd.Series(0, index=forward_ret.index)
        labels[forward_ret > q60] = 1
        labels[forward_ret < q40] = 0
        labels[(forward_ret >= q40) & (forward_ret <= q60)] = -1  # 丢弃
        return labels

    def train(
        self,
        factor_df: pd.DataFrame,
        close: pd.Series,
        factor_columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """训练 LightGBM 模型。"""
        import lightgbm as lgb

        if factor_columns is None:
            factor_columns = [
                c for c in factor_df.columns
                if c not in ("date", "close", "open", "high", "low", "volume", "amount")
                and pd.api.types.is_numeric_dtype(factor_df[c])
            ]

        # 优化1：因子方向处理
        ic_means = _compute_factor_ic(factor_df, close, factor_columns, self.forward_days)
        self.reversed_factors = [
            name for name, ic in ic_means.items()
            if ic < -0.02
        ]
        if self.reversed_factors:
            logger.info(f"反转 {len(self.reversed_factors)} 个负 IC 因子: {self.reversed_factors}")

        feature_df = factor_df[factor_columns].copy()
        for col in self.reversed_factors:
            if col in feature_df.columns:
                feature_df[col] = -feature_df[col]

        # 优化2：特征工程
        enhanced_df = _add_feature_engineering(feature_df, factor_columns)
        enhanced_cols = list(enhanced_df.columns)
        self.feature_names = enhanced_cols

        # 优化3：标签优化（分位数分类）
        labels = self._prepare_labels_quantile(close)

        # 对齐并丢弃中间标签
        aligned = enhanced_df.copy()
        aligned["_label"] = labels.values[: len(aligned)] if len(labels) >= len(aligned) else 0
        aligned = aligned[aligned["_label"] >= 0].dropna(subset=["_label"])

        if len(aligned) < 100:
            logger.warning(f"训练样本不足: {len(aligned)}，跳过训练")
            return {"status": "skipped", "reason": "insufficient_samples", "samples": len(aligned)}

        # 正交化
        X = self.orthogonalizer.fit_transform(aligned[enhanced_cols])
        y = aligned["_label"].values[: len(X)].astype(int)

        if len(X) != len(y):
            min_len = min(len(X), len(y))
            X = X[:min_len]
            y = y[:min_len]

        # 优化4：Walk-Forward 滚动窗口
        # 使用 3 个滚动窗口，取最后一个模型
        n = len(X)
        window_size = max(100, n // 3)
        best_val_acc = 0.0
        best_model = None

        for start in range(0, max(1, n - window_size - 20), max(20, window_size // 4)):
            end = start + window_size
            if end + 20 > n:
                break
            X_w = X[start:end]
            y_w = y[start:end]
            if len(np.unique(y_w)) < 2:
                continue

            X_val_w = X[end : min(end + 20, n)]
            y_val_w = y[end : min(end + 20, n)]

            m = lgb.LGBMClassifier(
                objective="binary",
                n_estimators=80,
                max_leaves=15,
                learning_rate=0.05,
                feature_fraction=0.7,
                bagging_fraction=0.7,
                bagging_freq=5,
                num_threads=2,
                verbose=-1,
                seed=42,
            )
            m.fit(
                X_w,
                y_w,
                eval_set=[(X_val_w, y_val_w)] if len(X_val_w) > 0 else None,
                callbacks=[lgb.log_evaluation(0)],
            )
            val_acc = float(m.score(X_val_w, y_val_w)) if len(X_val_w) > 0 else 0.0
            if val_acc >= best_val_acc:
                best_val_acc = val_acc
                best_model = m

        if best_model is None:
            # 降级到静态分割
            split = int(len(X) * 0.8)
            X_train, X_val = X[:split], X[split:]
            y_train, y_val = y[:split], y[split:]
            if len(X_train) < 50 or len(np.unique(y_train)) < 2:
                return {"status": "skipped", "reason": "insufficient_train_data"}
            best_model = lgb.LGBMClassifier(
                objective="binary",
                n_estimators=80,
                max_leaves=15,
                learning_rate=0.05,
                feature_fraction=0.7,
                bagging_fraction=0.7,
                bagging_freq=5,
                num_threads=2,
                verbose=-1,
                seed=42,
            )
            best_model.fit(
                X_train, y_train,
                eval_set=[(X_val, y_val)] if len(X_val) > 0 else None,
                callbacks=[lgb.log_evaluation(0)],
            )
            best_val_acc = float(best_model.score(X_val, y_val)) if len(X_val) > 0 else 0.0

        self.model = best_model
        self.is_trained = True

        # 全量评估
        train_acc = float(self.model.score(X, y))

        result = {
            "status": "trained",
            "samples": len(X),
            "features": len(enhanced_cols),
            "orthogonalized_features": self.orthogonalizer.n_kept,
            "train_accuracy": round(train_acc, 4),
            "val_accuracy": round(best_val_acc, 4),
            "forward_days": self.forward_days,
            "reversed_factors": len(self.reversed_factors),
            "feature_engineering": True,
            "label_type": "quantile_40_60",
            "walk_forward": True,
        }
        logger.info(f"LightGBM v2 训练完成: {result}")
        return result

    def predict(self, factor_row: pd.Series | dict[str, float]) -> float:
        """预测单只股票的涨跌概率。"""
        if not self.is_trained or self.model is None:
            return 0.5

        if isinstance(factor_row, dict):
            factor_row = pd.Series(factor_row)

        # 应用因子反转
        row = factor_row.copy()
        for col in self.reversed_factors:
            if col in row.index:
                row[col] = -row[col]

        # 特征工程：对原始因子列做增强
        base_cols = [c for c in self.feature_names if not c.endswith(("_diff", "_ma5"))]
        df = pd.DataFrame([row])
        # 只对存在的列做特征工程
        available_cols = [c for c in base_cols if c in df.columns]
        enhanced = _add_feature_engineering(df, available_cols)
        # 用 reindex 补齐所有训练特征列，缺失的填 0
        enhanced = enhanced.reindex(columns=self.feature_names, fill_value=0)
        X = self.orthogonalizer.transform(enhanced)
        if len(X) == 0:
            return 0.5

        proba = self.model.predict_proba(X)
        return float(proba[0][1])

    def predict_batch(self, factor_df: pd.DataFrame) -> np.ndarray:
        """批量预测。"""
        if not self.is_trained or self.model is None:
            return np.full(len(factor_df), 0.5)

        # 应用因子反转
        df = factor_df.copy()
        for col in self.reversed_factors:
            if col in df.columns:
                df[col] = -df[col]

        # 特征工程
        base_cols = [c for c in self.feature_names if not c.endswith(("_diff", "_ma5"))]
        available_cols = [c for c in base_cols if c in df.columns]
        enhanced = _add_feature_engineering(df, available_cols)
        enhanced = enhanced.reindex(columns=self.feature_names, fill_value=0)
        X = self.orthogonalizer.transform(enhanced)
        if len(X) == 0:
            return np.full(len(factor_df), 0.5)

        proba = self.model.predict_proba(X)
        return np.asarray(proba[:, 1])

    def save(self, path: Path | None = None) -> Path:
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
                    "reversed_factors": self.reversed_factors,
                },
                f,
            )
        logger.info(f"ML 模型已保存: {path}")
        return path

    def load(self, path: Path | None = None) -> bool:
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
        self.reversed_factors = data.get("reversed_factors", [])
        logger.info(f"ML 模型已加载: {path}")
        return True

    def get_feature_importance(self) -> dict[str, float]:
        if not self.is_trained or self.model is None:
            return {}
        importance = self.model.feature_importances_
        names = [f"PC{i+1}" for i in range(len(importance))]
        return dict(zip(names, importance.tolist(), strict=True))
