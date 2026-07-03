"""分析层：市场择时、仓位控制、因子工厂、参数优化、板块轮动、个股评分、策略池、执行优化、持仓管理、绩效归因、信号追踪、技术指标、舆情分析。"""

from gugu.analysis.alpha_factory import AlphaFactor, AlphaFactory
from gugu.analysis.danger_signal import DangerSignalDetector, DangerSignalResult
from gugu.analysis.execution_optimizer import ExecutionOptimizer, ExecutionPlan, ExecutionReport
from gugu.analysis.money_management import KellyCalculator, KellyResult, PyramidingLevel
from gugu.analysis.no_average_down import AverageDownCheckResult, NoAverageDownChecker
from gugu.analysis.param_optimizer import OptimizationResult, ParamOptimizer, ParamRange
from gugu.analysis.performance import PerformanceAnalyzer, PerformanceReport
from gugu.analysis.position_controller import PositionBudget, PositionController
from gugu.analysis.position_manager import PositionAdvice, PositionManager
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector, RegimeEvidence
from gugu.analysis.sector_rotation import SW_INDUSTRY_MAP, SectorRotation
from gugu.analysis.signal_tracker import SignalTracker
from gugu.analysis.stage_detector import MarketStage, StageDetector, StageResult
from gugu.analysis.stock_ranker import StockRanker
from gugu.analysis.strategy_pool import StrategyPool, StrategyWeight
from gugu.analysis.sentiment import SentimentSystem, NewsFetcher, SentimentAnalyzer, SentimentReport
from gugu.analysis.trailing_stop import TrailingStopEngine, TrailingStopSignal, TrailingStopState

__all__ = [
    "SW_INDUSTRY_MAP",
    "AlphaFactor",
    "AlphaFactory",
    "AverageDownCheckResult",
    "DangerSignalDetector",
    "DangerSignalResult",
    "ExecutionOptimizer",
    "ExecutionPlan",
    "ExecutionReport",
    "KellyCalculator",
    "KellyResult",
    "MarketStage",
    "MultiPeriodRegimeDetector",
    "NoAverageDownChecker",
    "OptimizationResult",
    "ParamOptimizer",
    "ParamRange",
    "PerformanceAnalyzer",
    "PerformanceReport",
    "PositionAdvice",
    "PositionBudget",
    "PositionController",
    "PositionManager",
    "PyramidingLevel",
    "RegimeEvidence",
    "SectorRotation",
    "SentimentAnalyzer",
    "SentimentReport",
    "SentimentSystem",
    "NewsFetcher",
    "SignalTracker",
    "StageDetector",
    "StageResult",
    "StockRanker",
    "StrategyPool",
    "StrategyWeight",
    "TrailingStopEngine",
    "TrailingStopSignal",
    "TrailingStopState",
]
