"""因子 IC 分析模块：评估单只股票内因子对自身未来收益的预测力。

注意：本模块计算的是**时序 IC**（Time-Series IC），即单只股票内因子值
与未来收益的 Spearman 秩相关系数。这与量化基金行业通用的**截面 IC**
（Cross-Sectional IC，同一时点所有股票的因子排名与收益排名的秩相关）
有本质区别：

- 时序 IC：评估因子对单只股票未来走势的预测能力（样本 = 时间窗口）
- 截面 IC：评估因子在全市场选股中的区分能力（样本 = 全市场股票）

时序 IC 的统计显著性低于截面 IC（样本量小，存在序列自相关），
筛选标准应更严格。建议使用时 |IC| > 0.08 为有效，|IC| < 0.05 为无效。

使用方式：
    from gugu.analysis.factor_ic import FactorICAnalyzer
    analyzer = FactorICAnalyzer()
    result = analyzer.analyze(df, factor_values, forward_days=5)
    effective = analyzer.get_effective_factors(result, ic_threshold=0.03, ir_threshold=0.3)"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class FactorICResult:
    """单个因子的 IC 分析结果。"""

    factor_name: str
    ic_mean: float  # IC 均值
    ic_std: float  # IC 标准差
    ir: float  # Information Ratio = ic_mean / ic_std
    ic_positive_ratio: float  # IC > 0 的比例
    ic_series: list[float] = field(default_factory=list)  # 每日 IC 序列
    is_effective: bool = False  # 是否为有效因子
    rank: int = 0  # 有效性排名


class FactorICAnalyzer:
    """因子 IC 分析器。

    计算每个因子值与未来收益率的 Spearman 秩相关系数，
    评估因子的预测力和稳定性。
    """

    def __init__(self, forward_days: int = 5) -> None:
        """初始化。

        Args:
            forward_days: 未来收益的计算天数（默认 5 日）
        """
        self.forward_days = forward_days

    def compute_forward_returns(self, close: pd.Series) -> pd.Series:
        """计算未来 N 日收益率。

        Args:
            close: 收盘价序列

        Returns:
            未来 N 日收益率序列（最后 N 个为 NaN）
        """
        return close.shift(-self.forward_days) / close - 1.0

    def compute_ic(
        self,
        factor_values: pd.Series,
        forward_returns: pd.Series,
    ) -> float:
        """计算单期 IC（Spearman 秩相关）。

        Args:
            factor_values: 因子值序列
            forward_returns: 未来收益序列

        Returns:
            IC 值（-1 到 1），NaN 如果样本不足
        """
        aligned = pd.DataFrame({"factor": factor_values, "return": forward_returns}).dropna()
        if len(aligned) < 10:
            return 0.0
        ic, _p = stats.spearmanr(aligned["factor"], aligned["return"])
        return float(ic) if not np.isnan(ic) else 0.0

    def analyze(
        self,
        df: pd.DataFrame,
        factor_columns: list[str],
        close_column: str = "close",
    ) -> dict[str, FactorICResult]:
        """分析多个因子的 IC。

        Args:
            df: 包含因子值和收盘价的 DataFrame
            factor_columns: 因子列名列表
            close_column: 收盘价列名

        Returns:
            因子名到 IC 结果的映射
        """
        forward_returns = self.compute_forward_returns(df[close_column])

        results: dict[str, FactorICResult] = {}

        for col in factor_columns:
            if col not in df.columns:
                logger.warning(f"因子列 {col} 不存在，跳过")
                continue

            factor_series = df[col]
            ic_series: list[float] = []

            # 滚动计算每日截面 IC（窗口 20 天）
            window = 20
            for i in range(window, len(df) - self.forward_days, 5):
                end = min(i + window, len(df))
                fac_chunk = factor_series.iloc[i:end]
                ret_chunk = forward_returns.iloc[i:end]
                ic = self.compute_ic(fac_chunk, ret_chunk)
                ic_series.append(ic)

            if not ic_series:
                results[col] = FactorICResult(
                    factor_name=col,
                    ic_mean=0.0,
                    ic_std=0.0,
                    ir=0.0,
                    ic_positive_ratio=0.0,
                    ic_series=[],
                )
                continue

            ic_arr = np.array(ic_series)
            ic_mean = float(np.mean(ic_arr))
            ic_std = float(np.std(ic_arr))
            ir = ic_mean / ic_std if ic_std > 0 else 0.0
            ic_pos_ratio = float(np.mean(ic_arr > 0))

            results[col] = FactorICResult(
                factor_name=col,
                ic_mean=round(ic_mean, 4),
                ic_std=round(ic_std, 4),
                ir=round(ir, 4),
                ic_positive_ratio=round(ic_pos_ratio, 4),
                ic_series=ic_series,
            )

        # 排名
        ranked = sorted(results.values(), key=lambda r: abs(r.ic_mean), reverse=True)
        for idx, r in enumerate(ranked):
            r.rank = idx + 1

        return results

    def get_effective_factors(
        self,
        results: dict[str, FactorICResult],
        ic_threshold: float = 0.03,
        ir_threshold: float = 0.3,
    ) -> list[str]:
        """筛选有效因子。

        Args:
            results: analyze() 的输出
            ic_threshold: IC 均值绝对值阈值
            ir_threshold: IR 绝对值阈值

        Returns:
            有效因子名列表
        """
        effective = []
        for name, r in results.items():
            if abs(r.ic_mean) >= ic_threshold and abs(r.ir) >= ir_threshold:
                r.is_effective = True
                effective.append(name)
            else:
                r.is_effective = False

        logger.info(
            f"因子 IC 分析完成：{len(effective)}/{len(results)} 个因子有效 "
            f"(IC>{ic_threshold}, |IR|>{ir_threshold})"
        )
        return effective

    def summary(self, results: dict[str, FactorICResult]) -> dict[str, Any]:
        """生成分析摘要。"""
        effective = [r for r in results.values() if r.is_effective]
        ineffective = [r for r in results.values() if not r.is_effective]

        return {
            "total_factors": len(results),
            "effective_count": len(effective),
            "ineffective_count": len(ineffective),
            "effective_factors": [r.factor_name for r in effective],
            "ineffective_factors": [r.factor_name for r in ineffective],
            "top_5_by_ic": [
                {
                    "name": r.factor_name,
                    "ic_mean": r.ic_mean,
                    "ir": r.ir,
                    "rank": r.rank,
                }
                for r in sorted(results.values(), key=lambda x: abs(x.ic_mean), reverse=True)[:5]
            ],
            "forward_days": self.forward_days,
        }


def compute_factor_matrix(
    df: pd.DataFrame,
    factory: Any | None = None,
) -> pd.DataFrame:
    """从 AlphaFactory 计算因子矩阵。

    对每行数据计算所有因子值，输出因子矩阵（行=日期, 列=因子名）。

    Args:
        df: OHLCV DataFrame
        factory: AlphaFactory 实例，不传则新建

    Returns:
        因子矩阵 DataFrame
    """
    if factory is None:
        from gugu.analysis.alpha_factory import AlphaFactory

        factory = AlphaFactory()

    factor_rows: list[dict[str, float]] = []

    for i in range(len(df)):
        chunk = df.iloc[: i + 1]
        if len(chunk) < 30:
            factor_rows.append({})
            continue
        factors = factory.compute_all(chunk)
        row = {name: f.value for name, f in factors.items()}
        row["date"] = df.iloc[i]["date"] if "date" in df.columns else i
        factor_rows.append(row)

    return pd.DataFrame(factor_rows)
