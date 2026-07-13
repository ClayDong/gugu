"""ML 多因子策略：用 LightGBM 预测分数生成买卖信号（v2）。

v2 优化：
- 信号冷却期：连续买入信号间隔 ≥ cooldown_days（默认 5 天）
- 阈值收紧：buy=0.62, sell=0.38, min_confidence=0.58
- 置信度衰减：冷却期内信号置信度降低

策略逻辑：
- 买入信号：ML 预测分数 > buy_threshold 且不在冷却期内
- 卖出信号：ML 预测分数 < sell_threshold
- 置信度：ML 预测分数本身
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class MLFactorStrategy(Strategy):
    """LightGBM 多因子策略（v2）。"""

    name = "ml_factor"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成 ML 信号。"""
        result = df.copy()

        buy_threshold = float(self.params.get("buy_threshold", 0.62))
        sell_threshold = float(self.params.get("sell_threshold", 0.38))
        min_confidence = float(self.params.get("min_confidence", 0.58))
        cooldown_days = int(self.params.get("cooldown_days", 5))

        from gugu.analysis.alpha_factory import AlphaFactory
        from gugu.analysis.ml_factor import MLFactorModel

        # 加载或训练模型
        model = MLFactorModel()
        if not model.load():
            logger.info("ML 模型未找到，尝试用当前数据训练...")
            factory = AlphaFactory()

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

        # 生成原始信号
        raw_signal = np.where(
            scores_arr > buy_threshold, 1,
            np.where(scores_arr < sell_threshold, -1, 0),
        )

        # 信号冷却期控制：连续买入信号间隔 ≥ cooldown_days
        signal = raw_signal.copy()
        last_buy_idx = -cooldown_days  # 初始化为负数，允许第一个信号
        for i in range(len(signal)):
            if signal[i] == 1:
                if i - last_buy_idx < cooldown_days:
                    signal[i] = 0  # 冷却期内，取消买入信号
                else:
                    last_buy_idx = i

        # 置信度
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
        suppressed = int(np.sum((raw_signal == 1) & (signal == 0)))
        logger.info(
            f"ML 策略 v2 生成信号: {buy_count} 买入, {sell_count} 卖出, "
            f"冷却抑制 {suppressed} 个, 共 {len(signal)} 行"
        )

        return result
