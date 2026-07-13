"""ML 多因子策略：用 LightGBM 预测分数生成买卖信号。

策略逻辑：
- 买入信号：ML 预测分数 > buy_threshold（默认 0.6）
- 卖出信号：ML 预测分数 < sell_threshold（默认 0.4）
- 置信度：ML 预测分数本身

约束：
- 模型未训练时不产生信号（返回全 0）
- 回测模式下自动训练（用历史数据）
- 实盘模式下从磁盘加载已训练模型
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class MLFactorStrategy(Strategy):
    """LightGBM 多因子策略。

    使用 MLFactorModel 预测涨跌概率，生成买卖信号。
    需要先训练模型（或从磁盘加载）。
    """

    name = "ml_factor"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成 ML 信号。"""
        result = df.copy()

        buy_threshold = float(self.params.get("buy_threshold", 0.6))
        sell_threshold = float(self.params.get("sell_threshold", 0.4))
        min_confidence = float(self.params.get("min_confidence", 0.55))

        # 延迟导入避免循环依赖
        from gugu.analysis.alpha_factory import AlphaFactory
        from gugu.analysis.ml_factor import MLFactorModel

        # 加载或训练模型
        model = MLFactorModel()
        if not model.load():
            logger.info("ML 模型未找到，尝试用当前数据训练...")
            factory = AlphaFactory()

            # 构建因子矩阵
            factor_rows: list[dict[str, float]] = []
            for i in range(len(df)):
                chunk = df.iloc[: i + 1]
                if len(chunk) < 30:
                    factor_rows.append({})
                    continue
                factors = factory.compute_all(chunk)
                row = {name: f.value for name, f in factors.items()}
                factor_rows.append(row)

            factor_df = pd.DataFrame(factor_rows)
            factor_cols = [c for c in factor_df.columns if c != "date"]

            if len(factor_df) < 100 or not factor_cols:
                logger.warning(f"训练样本不足: {len(factor_df)}，ML 策略不产生信号")
                result["signal"] = 0
                result["confidence"] = 0.0
                return result

            train_result = model.train(factor_df, df["close"], factor_cols)
            if train_result.get("status") != "trained":
                logger.warning(f"ML 模型训练失败: {train_result}")
                result["signal"] = 0
                result["confidence"] = 0.0
                return result

            model.save()

        # 计算因子并预测
        factory = AlphaFactory()
        scores: list[float] = []

        for i in range(len(df)):
            chunk = df.iloc[: i + 1]
            if len(chunk) < 30:
                scores.append(0.5)
                continue
            factors = factory.compute_all(chunk)
            row = {name: f.value for name, f in factors.items()}
            score = model.predict(row)
            scores.append(score)

        scores_arr = np.array(scores)

        # 生成信号
        signal = np.where(scores_arr > buy_threshold, 1, np.where(scores_arr < sell_threshold, -1, 0))
        confidence = np.where(
            scores_arr > min_confidence,
            np.clip(scores_arr, 0, 1),
            0.0,
        )

        result["signal"] = signal
        result["confidence"] = confidence
        result["ml_score"] = scores_arr

        buy_count = int(np.sum(signal == 1))
        sell_count = int(np.sum(signal == -1))
        logger.info(f"ML 策略生成信号: {buy_count} 买入, {sell_count} 卖出, 共 {len(signal)} 行")

        return result
