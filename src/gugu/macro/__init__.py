"""宏观五维数据模块：金、油、汇、债、G。

基于 MingCe data_fetcher 的经验，提取适用于 gugu 的核心宏观数据采集。
每个维度独立采集，带超时和多源降级。
"""

from gugu.macro.collectors import MacroCollector
from gugu.macro.models import GoldData, OilData, FxData, BondData, DerivativeData

__all__ = [
    "MacroCollector",
    "GoldData",
    "OilData",
    "FxData",
    "BondData",
    "DerivativeData",
]
