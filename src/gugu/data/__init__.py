"""数据层统一导出。"""

from gugu.data.collectors.akshare_collector import AkshareCollector
from gugu.data.collectors.base import BaseCollector
from gugu.data.collectors.fallback import SinaCollector
from gugu.data.manager import DataManager, data_manager
from gugu.data.quality import (
    DataQualityError,
    validate_sector_flow,
    validate_stock_flow,
    validate_stock_history,
)

__all__ = [
    "AkshareCollector",
    "BaseCollector",
    "DataManager",
    "DataQualityError",
    "SinaCollector",
    "data_manager",
    "validate_sector_flow",
    "validate_stock_flow",
    "validate_stock_history",
]
