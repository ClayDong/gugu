"""市场过滤器模块。"""

from gugu.filters.altalpha import AltAlphaFilter
from gugu.filters.breakout_rebound_prefilter import BreakoutReboundPrefilter
from gugu.filters.first_board_prefilter import FirstBoardPrefilter
from gugu.filters.ma20_prefilter import MA20Prefilter
from gugu.filters.market_regime import MarketRegimeDetector
from gugu.filters.zhen_dahu_prefilter import ZhenDahuPrefilter

__all__ = [
    "AltAlphaFilter",
    "BreakoutReboundPrefilter",
    "FirstBoardPrefilter",
    "MA20Prefilter",
    "MarketRegimeDetector",
    "ZhenDahuPrefilter",
]
