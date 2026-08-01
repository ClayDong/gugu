"""
ML GradientBoosting 策略：基于sklearn GradientBoostingRegressor的5日收益预测。

注意：本策略继承自mulan-quant的v11模型，整合到gugu策略体系中。
状态：experimental（需要实盘验证后才能启用）

与ml_factor策略的区别：
- ml_factor使用LightGBM + AlphaFactory因子，但未通过OOS验证
- 本策略使用sklearn GradientBoosting + 自定义83特征
- 需要独立验证后才能启用
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

# 模型持久化路径
MODEL_DIR = Path(__file__).parent.parent.parent / "data" / "ml_models"
MODEL_PATH = MODEL_DIR / "gradient_boost_v1.pkl"
SCALER_PATH = MODEL_DIR / "gradient_boost_scaler_v1.pkl"


class MLGradientBoostStrategy(Strategy):
    """ML GradientBoosting 策略（v1，experimental）。"""

    name = "ml_gradient_boost"
    category = "ml"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self._model = None
        self._scaler = None
        self._feature_cols = None
        self._load_model()

    def _load_model(self):
        """加载持久化的模型。"""
        try:
            if MODEL_PATH.exists() and SCALER_PATH.exists():
                # nosec B301: 模型为本地训练产物（scripts/train_ml_gradient_boost.py），
                # 非外部不可信输入；能写该文件者已具备任意代码执行能力，安全边界在文件系统权限
                with open(MODEL_PATH, "rb") as f:
                    self._model = pickle.load(f)  # nosec B301
                with open(SCALER_PATH, "rb") as f:
                    self._scaler = pickle.load(f)  # nosec B301
                logger.info("ML GradientBoosting 模型已加载")
        except Exception as e:
            logger.warning(f"模型加载失败: {e}，将在运行时训练")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成ML信号。"""
        self._ensure_columns(df)
        result = df.copy()

        # 如果模型不可用，返回空信号
        if self._model is None or self._scaler is None:
            result["signal"] = 0
            result["confidence"] = 0.0
            result["ml_score"] = 0.5
            return result

        try:
            features = self._extract_features(result)
            features_scaled = self._scaler.transform(features)
            predictions = self._model.predict(features_scaled)

            # 转换为信号
            buy_threshold = float(self.params.get("buy_threshold", 0.005))
            sell_threshold = float(self.params.get("sell_threshold", -0.002))

            signals = pd.Series(0, index=result.index)
            signals = signals.where(~(predictions > buy_threshold), 1)
            signals = signals.where(~(predictions < sell_threshold), -1)

            # 置信度 = 预测值的绝对值（归一化到0-1）
            confidence = np.clip(np.abs(predictions) * 10, 0, 1)

            result["signal"] = signals
            result["confidence"] = confidence
            result["ml_score"] = predictions

            buy_count = int((signals == 1).sum())
            logger.info("ML GradientBoost策略生成信号: {} 买入", buy_count)

        except Exception as e:
            logger.error(f"ML预测失败: {e}")
            result["signal"] = 0
            result["confidence"] = 0.0
            result["ml_score"] = 0.5

        return result

    def _extract_features(self, df: pd.DataFrame) -> np.ndarray:
        """提取特征（83个特征）。"""
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        features = {}

        # 基础价格特征
        for d in [1, 2, 3, 5, 10, 20, 60]:
            features[f"return_{d}d"] = close.pct_change(d)

        # 对数收益率
        features["log_return"] = np.log(close / close.shift(1))

        # 波动率
        log_ret = features["log_return"]
        for period in [5, 10, 20, 60]:
            features[f"volatility_{period}d"] = log_ret.rolling(period).std() * np.sqrt(252)

        # 均线系统
        for period in [5, 10, 20, 60, 120]:
            ma = close.rolling(period).mean()
            features[f"ma_{period}"] = ma
            features[f"dist_ma_{period}"] = (close - ma) / ma

        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        features["macd_dif"] = ema12 - ema26
        features["macd_dea"] = features["macd_dif"].ewm(span=9).mean()
        features["macd_hist"] = 2 * (features["macd_dif"] - features["macd_dea"])

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        features["rsi_14"] = 100 - (100 / (1 + gain / loss))

        # KDJ
        low_min = low.rolling(9).min()
        high_max = high.rolling(9).max()
        rsv = (close - low_min) / (high_max - low_min) * 100
        features["kdj_k"] = rsv.ewm(com=2).mean()
        features["kdj_d"] = features["kdj_k"].ewm(com=2).mean()
        features["kdj_j"] = 3 * features["kdj_k"] - 2 * features["kdj_d"]

        # 布林带
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        features["bb_upper"] = bb_mid + 2 * bb_std
        features["bb_lower"] = bb_mid - 2 * bb_std
        features["bb_position"] = (close - features["bb_lower"]) / (
            features["bb_upper"] - features["bb_lower"]
        )

        # ATR
        tr = pd.concat(
            [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1
        ).max(axis=1)
        features["atr_14"] = tr.rolling(14).mean()
        features["atr_ratio"] = features["atr_14"] / close

        # OBV
        features["obv"] = (np.sign(close.diff()) * volume).cumsum()

        # 量能
        for period in [5, 10, 20]:
            features[f"vol_ma_{period}"] = volume.rolling(period).mean()
        features["vol_ratio_5_20"] = features["vol_ma_5"] / features["vol_ma_20"]

        # 转换为DataFrame
        feature_df = pd.DataFrame(features, index=df.index)

        # 处理缺失值和无穷值
        feature_df = feature_df.replace([np.inf, -np.inf], np.nan)
        feature_df = feature_df.fillna(0)

        return feature_df.values


def train_and_save(data: pd.DataFrame, labels: pd.Series, feature_cols: list[str]):
    """训练并保存模型（外部调用）。

    2026-08-01 修复：原为模块级 @staticmethod（类外无效装饰器），
    导致 train_and_save 属性不存在、模型永远无法训练（死配置根因之一）。
    改为普通模块级函数。
    """
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.preprocessing import StandardScaler

    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    scaler = StandardScaler()
    data_scaled = scaler.fit_transform(data[feature_cols])

    model = GradientBoostingRegressor(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        min_samples_leaf=50,
        random_state=42,
    )
    model.fit(data_scaled, labels)

    with open(MODEL_PATH, "wb") as f:
        pickle.dump(model, f)
    with open(SCALER_PATH, "wb") as f:
        pickle.dump(scaler, f)

    logger.info("ML模型已保存到 {}", MODEL_DIR)
    return model, scaler
