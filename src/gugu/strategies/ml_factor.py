"""ML 多因子策略：用 LightGBM 预测分数生成买卖信号（v4.2）。

⚠️ 策略状态：enabled=False，未通过 OOS 准入门禁（2/6 项通过）

v4.2 Train/Test Leak 修复：
- 修复 train_global_model() 用全量数据训练的严重泄漏问题
- v1-v4.1 的 5/6 门禁通过是泄漏产物，OOS 真实表现为 -1.14% avg return
- 50 股 × 365 天 60/40 分割：26 股 0 交易，仅 6 股盈利，盈利比 12%

v4.1 优化：
- 修复前视偏差：波动率/MA 改为逐 bar 滚动计算，不再用期末值套全程
- 阈值调优：buy=0.66, sell=0.34, min_confidence=0.61
- 冷却期延长：cooldown_days=16
- 动态阈值：高波动(>0.3)收紧买入+0.03，低波动(<0.15)放宽-0.02

v4 优化：
- 全局模型训练：回测脚本先用多只股票数据联合训练模型
- 测试表明固定止盈止损和 trailing stop 适得其反，故不启用

策略逻辑：
- 买入信号：ML 预测分数 > buy_threshold 且不在冷却期内 且市场状态允许
- 卖出信号：ML 预测分数 < sell_threshold 或持仓超过 max_hold_days
- 置信度：ML 预测分数本身
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class MLFactorStrategy(Strategy):
    """LightGBM 多因子策略（v4.2，未通过 OOS 门禁）。"""

    name = "ml_factor"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成 ML 信号。"""
        result = df.copy()

        buy_threshold = float(self.params.get("buy_threshold", 0.66))
        sell_threshold = float(self.params.get("sell_threshold", 0.34))
        min_confidence = float(self.params.get("min_confidence", 0.61))
        cooldown_days = int(self.params.get("cooldown_days", 16))
        max_hold_days = int(self.params.get("max_hold_days", 0))

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
        close = df["close"].values
        n = len(close)

        # v4.1 修复前视偏差：逐 bar 滚动计算波动率和 MA
        # 原代码用 close[-21:] 计算一个全局 vol 套用到所有 bar → 前视偏差
        close_series = pd.Series(close)
        log_ret = np.log(close_series / close_series.shift(1))
        rolling_vol = log_ret.rolling(20, min_periods=10).std() * np.sqrt(252)
        rolling_ma20 = close_series.rolling(20, min_periods=10).mean()
        rolling_ma60 = close_series.rolling(60, min_periods=30).mean()

        vol_arr = rolling_vol.fillna(0.2).to_numpy()
        ma20_arr = rolling_ma20.to_numpy(copy=True)
        nan_mask = np.isnan(ma20_arr)
        ma20_arr[nan_mask] = close[nan_mask]
        ma60_arr = rolling_ma60.to_numpy(copy=True)
        nan_mask = np.isnan(ma60_arr)
        ma60_arr[nan_mask] = close[nan_mask]

        # 逐 bar 生成信号（动态阈值：高波动收紧买入，低波动放宽）
        raw_signal = np.zeros(n, dtype=int)
        bear_blocked = 0

        for i in range(n):
            vol = vol_arr[i]
            dyn_buy = buy_threshold + 0.03 if vol > 0.3 else buy_threshold - 0.02 if vol < 0.15 else buy_threshold
            dyn_sell = sell_threshold - 0.02 if vol > 0.3 else sell_threshold + 0.02 if vol < 0.15 else sell_threshold

            if scores_arr[i] > dyn_buy:
                raw_signal[i] = 1
            elif scores_arr[i] < dyn_sell:
                raw_signal[i] = -1

            # 逐 bar 熊市过滤：MA20 < MA60 且价格 < MA60
            if raw_signal[i] == 1 and i >= 60 and ma20_arr[i] < ma60_arr[i] and close[i] < ma60_arr[i]:
                raw_signal[i] = 0
                bear_blocked += 1

        # 信号冷却期控制：连续买入信号间隔 ≥ cooldown_days
        signal = raw_signal.copy()
        last_buy_idx = -cooldown_days
        for i in range(n):
            if signal[i] == 1:
                if i - last_buy_idx < cooldown_days:
                    signal[i] = 0
                else:
                    last_buy_idx = i

        # v4.1 时间止损：持仓超过 max_hold_days 强制卖出
        # ML 预测 5 日收益，超期说明预测已失效
        if max_hold_days > 0:
            i = 0
            while i < n:
                if signal[i] == 1:
                    exit_idx = min(i + max_hold_days, n - 1)
                    # 检查 (i, exit_idx] 是否已有卖出信号
                    has_sell = False
                    for j in range(i + 1, exit_idx + 1):
                        if signal[j] == -1:
                            has_sell = True
                            break
                    # 仅当 exit_idx 无买入信号时才强制卖出（避免覆盖买入）
                    if not has_sell and exit_idx > i and signal[exit_idx] != 1:
                        signal[exit_idx] = -1
                    i = exit_idx + 1
                else:
                    i += 1

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
            f"ML 策略 v4.2 生成信号: {buy_count} 买入, {sell_count} 卖出, "
            f"冷却抑制 {suppressed} 个, 熊市拦截 {bear_blocked} 个, "
            f"共 {n} 行"
        )

        return result
