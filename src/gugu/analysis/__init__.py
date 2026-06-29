"""分析层：市场择时、仓位控制、因子工厂、参数优化、板块轮动、个股评分、策略池、执行优化、持仓管理、绩效归因、信号追踪、技术指标。"""
from gugu.analysis.regime_detector import MultiPeriodRegimeDetector, RegimeEvidence
from gugu.analysis.position_controller import PositionBudget, PositionController
from gugu.analysis.alpha_factory import AlphaFactory, AlphaFactor
from gugu.analysis.param_optimizer import ParamOptimizer, ParamRange, OptimizationResult
from gugu.analysis.sector_rotation import SectorRotation, SW_INDUSTRY_MAP
from gugu.analysis.stock_ranker import StockRanker
from gugu.analysis.strategy_pool import StrategyPool, StrategyWeight
from gugu.analysis.execution_optimizer import ExecutionOptimizer, ExecutionPlan, ExecutionReport
from gugu.analysis.position_manager import PositionManager, PositionAdvice
from gugu.analysis.performance import PerformanceAnalyzer, PerformanceReport
from gugu.analysis.signal_tracker import SignalTracker
from gugu.analysis.stage_detector import StageDetector, MarketStage, StageResult
from gugu.analysis.trailing_stop import TrailingStopEngine, TrailingStopState, TrailingStopSignal
from gugu.analysis.danger_signal import DangerSignalDetector, DangerSignalResult
from gugu.analysis.no_average_down import NoAverageDownChecker, AverageDownCheckResult

__all__ = [
    "MultiPeriodRegimeDetector",
    "RegimeEvidence",
    "PositionBudget",
    "PositionController",
    "AlphaFactory",
    "AlphaFactor",
    "ParamOptimizer",
    "ParamRange",
    "OptimizationResult",
    "SectorRotation",
    "SW_INDUSTRY_MAP",
    "StockRanker",
    "StrategyPool",
    "StrategyWeight",
    "ExecutionOptimizer",
    "ExecutionPlan",
    "ExecutionReport",
    "PositionManager",
    "PositionAdvice",
    "PerformanceAnalyzer",
    "PerformanceReport",
    "SignalTracker",
    "StageDetector",
    "MarketStage",
    "StageResult",
    "TrailingStopEngine",
    "TrailingStopState",
    "TrailingStopSignal",
    "DangerSignalDetector",
    "DangerSignalResult",
    "NoAverageDownChecker",
    "AverageDownCheckResult",
]