"""十大视角分析系统 — 数据模型。

从第一性原理出发，将投资大师的认知框架量化为可计算的评分维度。
每个视角独立评分（0-10），最终加权汇总为综合评分。

设计原则：
1. 每个视角是独立的评分函数，输入统一的数据上下文，输出 0-10 分
2. 最低分决定木桶效应——单个视角极低分意味着该维度存在致命缺陷
3. 权重反映该视角在选股决策中的相对重要性
4. 所有评分必须可解释——每个分数附带推理链
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 视角名称常量 ──
PERSPECTIVE_BUFFETT = "buffett"
PERSPECTIVE_MUNGER = "munger"
PERSPECTIVE_SOROS = "soros"
PERSPECTIVE_LYNCH = "lynch"
PERSPECTIVE_LIVERMORE = "livermore"
PERSPECTIVE_QIU_GUO_LU = "qiu_guo_lu"
PERSPECTIVE_CHEN_JIANG_TING = "chen_jiang_ting"
PERSPECTIVE_TECHNICAL = "technical"
PERSPECTIVE_INVESTING_HABITS = "investing_habits"
PERSPECTIVE_DASHIXIONG = "dashixiong"

ALL_PERSPECTIVES = [
    PERSPECTIVE_BUFFETT,
    PERSPECTIVE_MUNGER,
    PERSPECTIVE_SOROS,
    PERSPECTIVE_LYNCH,
    PERSPECTIVE_LIVERMORE,
    PERSPECTIVE_QIU_GUO_LU,
    PERSPECTIVE_CHEN_JIANG_TING,
    PERSPECTIVE_TECHNICAL,
    PERSPECTIVE_INVESTING_HABITS,
    PERSPECTIVE_DASHIXIONG,
]

# ── 默认权重（总和=1.0） ──
DEFAULT_WEIGHTS: dict[str, float] = {
    PERSPECTIVE_BUFFETT: 0.15,          # 价值投资基石：护城河+安全边际
    PERSPECTIVE_MUNGER: 0.10,           # 多元思维+反向思考
    PERSPECTIVE_SOROS: 0.10,            # 反身性+趋势判断
    PERSPECTIVE_LYNCH: 0.10,            # 成长股分类+PEG
    PERSPECTIVE_LIVERMORE: 0.10,        # 关键点+趋势跟随
    PERSPECTIVE_QIU_GUO_LU: 0.10,       # 行业格局+估值
    PERSPECTIVE_CHEN_JIANG_TING: 0.10,  # 趋势+仓位+止损
    PERSPECTIVE_TECHNICAL: 0.10,         # 技术指标综合
    PERSPECTIVE_INVESTING_HABITS: 0.08,  # 行为金融+纪律
    PERSPECTIVE_DASHIXIONG: 0.07,       # 宏观五维框架
}

# ── 评分通过阈值 ──
PASS_THRESHOLD = 6.0        # 单视角最低通过分
MIN_PERSPECTIVE_SCORE = 4.0  # 单视角最低容忍分（低于此直接否决）
COMPOSITE_PASS_THRESHOLD = 7.0  # 综合评分通过阈值


@dataclass
class StockDataContext:
    """股票分析上下文——所有视角评分的统一输入。

    设计为懒加载模式：各视角按需访问字段，不强制要求全部字段存在。
    缺失字段由评分函数自行处理（降级或跳过）。
    """
    symbol: str
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0          # 当日涨跌幅 (%)
    volume_ratio: float = 0.0        # 量比
    turnover_pct: float = 0.0        # 换手率 (%)
    mcap_billion: float = 0.0        # 流通市值 (亿)

    # 估值指标
    pe_static: float = 0.0           # 静态 PE
    pe_dynamic: float = 0.0          # 动态 PE (Forward)
    pb: float = 0.0                  # PB
    roe: float = 0.0                 # ROE (%)
    revenue_growth: float = 0.0      # 营收增长率 (%)
    profit_growth: float = 0.0       # 净利润增长率 (%)

    # 均线系统
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    ma120: float = 0.0
    ma250: float = 0.0

    # 技术指标
    rsi14: float = 0.0               # RSI(14)
    macd_dif: float = 0.0
    macd_dea: float = 0.0
    macd_hist: float = 0.0
    bollinger_upper: float = 0.0
    bollinger_lower: float = 0.0
    atr14: float = 0.0               # ATR(14)

    # 价格位置
    high_52w: float = 0.0            # 52周最高
    low_52w: float = 0.0             # 52周最低
    distance_from_52w_high: float = 0.0  # 距52w高点 (%)
    distance_from_52w_low: float = 0.0    # 距52w低点 (%)

    # 资金流
    main_net_inflow: float = 0.0      # 主力净流入 (亿)
    main_net_inflow_5d: float = 0.0   # 5日主力净流入 (亿)
    retail_net_inflow: float = 0.0    # 散户净流入 (亿)

    # 市场环境
    market_stage: str = ""           # 市场阶段: bull/bear/sideway/crash
    sector_rotation_strength: float = 0.0  # 板块轮动强度
    hot_sector: bool = False         # 是否属于热门板块

    # 公司质量
    debt_ratio: float = 0.0          # 资产负债率 (%)
    operating_cash_flow: float = 0.0  # 经营现金流 (亿)
    free_cash_flow: float = 0.0       # 自由现金流 (亿)
    dividend_yield: float = 0.0      # 股息率 (%)
    institutional_holding: float = 0.0  # 机构持股比例 (%)

    # 管理层
    insider_buying: bool = False     # 内部人增持
    insider_selling: bool = False    # 内部人减持

    # 行业
    industry: str = ""               # 行业名称
    industry_pe_avg: float = 0.0     # 行业平均PE
    is_monopoly: bool = False        # 是否垄断/龙头
    moat_type: str = ""             # 护城河类型: brand/cost/network/switching/unknown

    # 宏观（大师兄五维）
    gold_price: float = 0.0
    oil_price: float = 0.0
    usd_cny: float = 0.0
    us10y_yield: float = 0.0
    market_regime: str = ""          # 市场状态: risk_on/risk_off/neutral

    # 原始K线数据（可选，供高级分析）
    kline_data: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        d = {}
        for k, v in self.__dict__.items():
            if k != "kline_data":
                d[k] = v
        return d


@dataclass
class PerspectiveScore:
    """单一视角的评分结果。"""
    perspective: str                 # 视角名称
    score: float                     # 0-10 分
    weight: float                    # 该视角权重
    confidence: float = 1.0         # 评分置信度 (0-1)，数据不足时降低
    reasoning: str = ""             # 评分推理链（可解释性）
    key_factors: list[str] = field(default_factory=list)  # 关键因子
    risk_flags: list[str] = field(default_factory=list)   # 风险标记
    verdict: str = ""               # 一句话判定

    @property
    def weighted_score(self) -> float:
        """加权得分 = 分数 × 权重 × 置信度。"""
        return self.score * self.weight * self.confidence

    @property
    def passed(self) -> bool:
        """是否通过（≥ 通过阈值）。"""
        return self.score >= PASS_THRESHOLD

    @property
    def is_fatal(self) -> bool:
        """是否致命缺陷（低于最低容忍分）。"""
        return self.score < MIN_PERSPECTIVE_SCORE


@dataclass
class PerspectiveAnalysisResult:
    """十大视角综合分析结果。"""
    symbol: str
    name: str
    price: float
    timestamp: str = ""

    scores: list[PerspectiveScore] = field(default_factory=list)
    composite_score: float = 0.0     # 加权综合评分
    min_score: float = 10.0          # 最低单视角分（木桶效应）
    min_perspective: str = ""        # 最低分视角名称
    passed_count: int = 0            # 通过视角数
    fatal_count: int = 0             # 致命缺陷数
    overall_verdict: str = ""        # 综合判定
    overall_reasoning: str = ""      # 综合推理
    top_strengths: list[str] = field(default_factory=list)  # 核心优势
    top_risks: list[str] = field(default_factory=list)      # 核心风险
    suggestion: str = ""             # 操作建议
    entry_price: float | None = None   # 建议入场价
    stop_loss_price: float | None = None  # 建议止损价
    target_price: float | None = None     # 建议目标价

    @property
    def passed(self) -> bool:
        """是否通过综合评估。"""
        return (
            self.composite_score >= COMPOSITE_PASS_THRESHOLD
            and self.fatal_count == 0
            and self.passed_count >= 7  # 至少7个视角通过
        )

    @property
    def summary(self) -> str:
        """一行摘要。"""
        status = "✅ 通过" if self.passed else "❌ 未通过"
        return (
            f"{self.symbol} {self.name} | "
            f"综合{self.composite_score:.1f}分 {status} | "
            f"通过{self.passed_count}/10 | "
            f"最低{self.min_score:.1f}({self.min_perspective})"
        )

    def get_score(self, perspective: str) -> PerspectiveScore | None:
        """获取指定视角的评分。"""
        for s in self.scores:
            if s.perspective == perspective:
                return s
        return None

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": self.price,
            "timestamp": self.timestamp,
            "composite_score": round(self.composite_score, 2),
            "min_score": round(self.min_score, 2),
            "min_perspective": self.min_perspective,
            "passed_count": self.passed_count,
            "fatal_count": self.fatal_count,
            "overall_verdict": self.overall_verdict,
            "overall_reasoning": self.overall_reasoning,
            "top_strengths": self.top_strengths,
            "top_risks": self.top_risks,
            "suggestion": self.suggestion,
            "entry_price": self.entry_price,
            "stop_loss_price": self.stop_loss_price,
            "target_price": self.target_price,
            "passed": self.passed,
            "scores": [
                {
                    "perspective": s.perspective,
                    "score": s.score,
                    "weight": s.weight,
                    "confidence": s.confidence,
                    "weighted_score": round(s.weighted_score, 3),
                    "passed": s.passed,
                    "is_fatal": s.is_fatal,
                    "reasoning": s.reasoning,
                    "key_factors": s.key_factors,
                    "risk_flags": s.risk_flags,
                    "verdict": s.verdict,
                }
                for s in self.scores
            ],
        }
