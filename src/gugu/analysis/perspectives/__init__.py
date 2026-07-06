"""十大视角分析系统——多投资大师认知框架量化评分。

模块：
- data_types: 核心数据模型（StockDataContext, PerspectiveScore, PerspectiveAnalysisResult）
- scorer: 10个投资大师视角评分函数
- engine: 核心分析引擎（单股/批量分析）
- integrators: 与现有模块的集成桥接
"""

from gugu.analysis.perspectives.data_types import (
    ALL_PERSPECTIVES,
    COMPOSITE_PASS_THRESHOLD,
    DEFAULT_WEIGHTS,
    MIN_PERSPECTIVE_SCORE,
    PASS_THRESHOLD,
    PERSPECTIVE_BUFFETT,
    PERSPECTIVE_CHEN_JIANG_TING,
    PERSPECTIVE_DASHIXIONG,
    PERSPECTIVE_INVESTING_HABITS,
    PERSPECTIVE_LIVERMORE,
    PERSPECTIVE_LYNCH,
    PERSPECTIVE_MUNGER,
    PERSPECTIVE_QIU_GUO_LU,
    PERSPECTIVE_SOROS,
    PERSPECTIVE_TECHNICAL,
    PerspectiveAnalysisResult,
    PerspectiveScore,
    StockDataContext,
)
from gugu.analysis.perspectives.engine import PerspectiveEngine
from gugu.analysis.perspectives.integrators import (
    attach_perspective_to_signal,
    build_context_from_kline,
    build_context_from_signal,
    filter_by_perspective,
)
from gugu.analysis.perspectives.scorer import SCORERS

__all__ = [
    # Data types
    "ALL_PERSPECTIVES",
    "COMPOSITE_PASS_THRESHOLD",
    "DEFAULT_WEIGHTS",
    "MIN_PERSPECTIVE_SCORE",
    "PASS_THRESHOLD",
    "PERSPECTIVE_BUFFETT",
    "PERSPECTIVE_CHEN_JIANG_TING",
    "PERSPECTIVE_DASHIXIONG",
    "PERSPECTIVE_INVESTING_HABITS",
    "PERSPECTIVE_LIVERMORE",
    "PERSPECTIVE_LYNCH",
    "PERSPECTIVE_MUNGER",
    "PERSPECTIVE_QIU_GUO_LU",
    "PERSPECTIVE_SOROS",
    "PERSPECTIVE_TECHNICAL",
    "PerspectiveAnalysisResult",
    "PerspectiveScore",
    "StockDataContext",
    # Engine
    "PerspectiveEngine",
    # Integrators
    "attach_perspective_to_signal",
    "build_context_from_kline",
    "build_context_from_signal",
    "filter_by_perspective",
    # Scorer
    "SCORERS",
]
