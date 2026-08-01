"""Strong-typed Pydantic config models for the gugu trading system.

All config sections from ``config/settings.yaml`` are represented as
``BaseModel`` subclasses.  The top-level :class:`AppConfig` wraps every
section and provides a ``from_settings()`` factory that reads the YAML file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# ──────────────────────────────────────────────────────────────────────
#  Individual section models
# ──────────────────────────────────────────────────────────────────────


class RiskConfig(BaseModel):
    """三级风控参数."""

    max_position_ratio: float = Field(default=0.30, ge=0.0, le=1.0, description="单股仓位上限")
    daily_loss_warn: float = Field(default=0.03, ge=0.0, le=1.0, description="日亏预警阈值")
    daily_loss_halt: float = Field(default=0.05, ge=0.0, le=1.0, description="日亏熔断阈值")
    max_total_positions: int = Field(default=5, ge=1, description="最大持仓数")
    max_same_industry: int = Field(default=2, ge=1, description="同行业最大持仓数")
    t_plus_1: bool = Field(default=True, description="T+1 限制（系统默认开启）")
    max_holding_days_warn: int = Field(
        default=30, ge=1, description="持仓 N 天未盈利 → 警告减半仓（防阴跌套牢）"
    )
    max_holding_days_halt: int = Field(
        default=60, ge=1, description="持仓 N 天未盈利 → 强制清仓（防阴跌套牢）"
    )
    max_drawdown_halt: float = Field(
        default=0.15, ge=0.0, le=1.0, description="组合最大回撤熔断阈值"
    )


class DataConfig(BaseModel):
    """数据源配置."""

    primary_source: str = Field(default="sina", description="主数据源")
    fallback_sources: list[str] = Field(
        default_factory=lambda: ["akshare"], description="降级源列表"
    )
    fetch_interval_seconds: int = Field(default=120, ge=10, description="实时采集间隔（秒）")
    fail_threshold: int = Field(default=3, ge=1, description="连续失败 N 次触发降级")
    fail_cooldown_seconds: int = Field(default=300, ge=0, description="降级冷却时间（秒）")
    cache_ttl_seconds: int = Field(default=120, ge=1, description="实时数据缓存 TTL（秒）")
    history_db: str = Field(default="sqlite:///data/gugu.db", description="历史数据库 URL")
    parquet_dir: str = Field(default="data/parquet", description="Parquet 文件目录")

    # ── syntactic-sugar aliases for callers transitioning from dict keys ──
    @property
    def source(self) -> str:
        return self.primary_source

    @property
    def fallback(self) -> str:
        return self.fallback_sources[0] if self.fallback_sources else "sina"

    @property
    def fetch_interval(self) -> int:
        return self.fetch_interval_seconds


class StrategyConfig(BaseModel):
    """策略配置."""

    enabled: list[str] = Field(
        default_factory=list,
        description="启用的策略列表（向后兼容；如配置 enabled_by_regime 则被其覆盖）",
    )
    # v0.5.6 优化1：按市场状态启用策略（替代全局 enabled）
    # 核心思想：策略没有"好/坏"之分，只有"适用/不适用"之分
    # 当 enabled_by_regime 非空时，覆盖 enabled 列表
    enabled_by_regime: dict[str, list[str]] = Field(
        default_factory=dict,
        description="按市场状态启用的策略映射，键为 bull/sideways/bear/rally/crash",
    )
    # v0.7.3 优化：策略初始权重，基于 Walk-Forward 验证结果分配
    # 权重会在 StrategyPool 中根据近期表现动态调整
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "box_breakout": 0.30,
            "double_yin": 0.25,
            "rsi_reversal": 0.15,
        },
        description="策略初始权重（总和应为 1.0）",
    )
    signal_fusion: str = Field(
        default="majority", description="信号融合规则: unanimous / majority / any"
    )
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0, description="最低置信度阈值")
    auto_select: bool = Field(default=False, description="是否启用自动选股")
    select_top_n: int = Field(default=50, ge=1, description="全市场资金流排序取前 N")
    select_max_candidates: int = Field(default=10, ge=1, description="最大选股数量")
    # v0.7.5 P0-1修复：策略分组管理
    # verified_strategies: 通过 Walk-Forward 验证的策略（无条件启用）
    verified_strategies: list[str] = Field(
        default_factory=list,
        description="验证通过的策略列表（通过 Walk-Forward 4项门控）",
    )
    # experimental_strategies: 逻辑闭环但未验证的策略（仅 paper 模式参与信号融合）
    experimental_strategies: list[str] = Field(
        default_factory=list,
        description="实验性策略列表（仅 paper 模式参与信号融合）",
    )
    # experimental_weights: 实验性策略权重（仅 paper 模式使用）
    experimental_weights: dict[str, float] = Field(
        default_factory=dict,
        description="实验性策略权重（仅 paper 模式使用）",
    )

    # ── aliases ──
    @property
    def fusion_mode(self) -> str:
        return self.signal_fusion

    def get_enabled_for_regime(self, regime: str | None = None) -> list[str]:
        """获取指定市场状态下启用的策略列表。

        v0.7.5 P0-1修复：优先使用 verified_strategies + experimental_strategies。
        - 若 verified_strategies 非空：以 verified 为基础，叠加 enabled_by_regime 过滤
        - 否则：回退到旧逻辑（enabled_by_regime → enabled）

        Args:
            regime: 市场状态（bull/sideways/bear/rally/crash），None 表示未知

        Returns:
            启用的策略名称列表
        """
        # v0.7.5 P0-1修复：优先使用 verified_strategies
        if self.verified_strategies:
            # 如果 enabled_by_regime 有配置，按 regime 过滤
            if self.enabled_by_regime:
                if regime and regime in self.enabled_by_regime:
                    # verified 策略与 regime 配置取交集
                    verified = [
                        s for s in self.enabled_by_regime[regime] if s in self.verified_strategies
                    ]
                    # experimental 策略也加入（在 track_live/paper 模式下产生信号用于绩效追踪）
                    experimental = []
                    if hasattr(self, "experimental_strategies") and self.experimental_strategies:
                        experimental = [
                            s
                            for s in self.enabled_by_regime[regime]
                            if s in self.experimental_strategies
                        ]
                    return verified + experimental
                # regime 未知：返回所有 verified + experimental
                result = list(self.verified_strategies)
                if hasattr(self, "experimental_strategies") and self.experimental_strategies:
                    result.extend(self.experimental_strategies)
                return result
            # 无 regime 配置：返回所有 verified
            return list(self.verified_strategies)

        # 向后兼容：旧逻辑
        if not self.enabled_by_regime:
            return list(self.enabled)

        if regime and regime in self.enabled_by_regime:
            return list(self.enabled_by_regime[regime])

        # regime 未知或未配置：返回所有 regime 的并集（保守降级）
        all_strategies: set[str] = set()
        for strategies in self.enabled_by_regime.values():
            all_strategies.update(strategies)
        return sorted(all_strategies)

    def get_all_enabled_unique(self) -> list[str]:
        """获取所有 regime 中出现过的策略（去重），用于回测全量验证。"""
        if not self.enabled_by_regime:
            return list(self.enabled)
        all_strategies: set[str] = set()
        for strategies in self.enabled_by_regime.values():
            all_strategies.update(strategies)
        return sorted(all_strategies)


class LeopardUniverseConfig(BaseModel):
    """豹哥三要素选股池过滤器配置.

    来源：豹哥战法（微信公众号文章）
    三要素是选股池准入，不是择时信号。默认关闭，因为：
    - 三要素要求换手率≥15%（高度活跃），与 pullback_low/double_yin 缩量低吸逻辑冲突
    - 适合与 box_breakout（突破追势）配合，趋势市手动开启
    """

    enabled: bool = Field(default=False, description="是否启用三要素选股池预筛")
    turnover_min: float = Field(default=15.0, ge=0, description="换手率下限(%)")
    amount_min: float = Field(default=1_000_000_000, ge=0, description="成交额下限(元，10亿)")
    circ_mv_min: float = Field(default=7_000_000_000, ge=0, description="流通市值下限(元，70亿)")


class PaperConfig(BaseModel):
    """模拟盘资金参数."""

    initial_capital: float = Field(default=1_000_000, ge=1, description="模拟本金")
    commission_rate: float = Field(default=0.00025, ge=0.0, lt=1.0, description="佣金费率")
    stamp_tax: float = Field(default=0.0005, ge=0.0, lt=1.0, description="印花税（卖出）")
    slippage: float = Field(default=0.002, ge=0.0, lt=1.0, description="滑点")


class LiveConfig(BaseModel):
    """实盘配置."""

    broker: str = Field(default="qmt", description="实盘券商接口")
    confirm_required: bool = Field(default=True, description="下单前人工确认")


class ExecutionConfig(BaseModel):
    """执行层配置."""

    mode: str = Field(
        default="paper",
        description="运行模式: signal_only / paper / track_live / live",
    )
    paper: PaperConfig = Field(default_factory=PaperConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    scan_concurrency: int = Field(default=10, description="全市场扫描并发数（P3 优化）")

    # ── aliases ──
    @property
    def confirm_required(self) -> bool:
        return self.live.confirm_required


class SignalAggregationConfig(BaseModel):
    """信号聚合配置 — 降低飞书通知疲劳.

    Attributes:
        min_interval_minutes: 同一只股票两次信号的最小间隔（分钟）。
        max_daily_signals: 单只股票每日信号上限。
        batch_direction_signals: 是否将同一方向的信号批量合并为一张卡片。
    """

    min_interval_minutes: int = Field(default=30, ge=5, description="同股最小通知间隔（分钟）")
    max_daily_signals: int = Field(default=10, ge=1, le=100, description="单股每日信号上限")
    batch_direction_signals: bool = Field(default=True, description="同方向信号批量聚合")


class FeishuConfig(BaseModel):
    """飞书通知配置."""

    enabled: bool = Field(default=True, description="全局开关")
    notify_signal: bool = Field(default=True, alias="signal")
    notify_daily_report: bool = Field(default=True, alias="daily_report")
    notify_risk_alert: bool = Field(default=True, alias="risk_alert")
    notify_backtest: bool = Field(default=True, alias="backtest")
    notify_system_error: bool = Field(default=True, alias="system_error")
    daily_report_times: list[str] = Field(
        default_factory=lambda: ["09:10", "11:35", "15:10"],
        description="日报推送时段",
        alias="report_times",
    )
    signal_aggregation: SignalAggregationConfig = Field(
        default_factory=SignalAggregationConfig,
        description="信号聚合配置",
    )

    model_config = {"populate_by_name": True}


class StockRankerConfig(BaseModel):
    """StockRanker 选股评分器配置.

    settings.yaml 中 stock_ranker 段对应的强类型模型。
    权重总和应等于 1.0（由配置一致性测试校验）。

    v0.7.1 新增：对应 settings.yaml stock_ranker 段，修复配置脱节问题。
    """

    factor_weight: float = Field(default=0.25, ge=0.0, le=1.0, description="因子评分权重")
    fundamental_weight: float = Field(default=0.20, ge=0.0, le=1.0, description="基本面评分权重")
    money_flow_weight: float = Field(default=0.25, ge=0.0, le=1.0, description="资金流评分权重")
    sector_weight: float = Field(default=0.15, ge=0.0, le=1.0, description="板块评分权重")
    chip_score_weight: float = Field(default=0.15, ge=0.0, le=1.0, description="筹码画像评分权重")
    chip_score_neutral: float = Field(
        default=0.5, ge=0.0, le=1.0, description="数据缺失时的中性评分"
    )


class FlowReportConfig(BaseModel):
    """资金流报告配置.

    settings.yaml 中 flow_report 段对应的强类型模型。

    v0.7.1 新增：对应 settings.yaml flow_report 段，修复配置脱节问题。
    """

    enabled: bool = Field(default=True, description="资金流报告推送开关")
    morning_time: str = Field(default="08:00", description="盘前资金流报告推送时间")
    close_time: str = Field(default="15:10", description="收盘资金流报告推送时间")
    sector_top_n: int = Field(default=5, ge=1, le=20, description="行业TOP N数量")
    stock_strong_top_n: int = Field(default=5, ge=1, le=30, description="强流入个股TOP N数量")


class SchedulerTasksConfig(BaseModel):
    """调度任务开关配置."""

    macro_morning: bool = Field(default=True, description="08:00 早间全球简报")
    perspective_daily: bool = Field(default=True, description="08:30 十大视角日报")
    signal_morning: bool = Field(default=True, description="09:10 盘前信号汇总")
    macro_preamarket: bool = Field(default=True, description="09:15 盘前宏观简报")
    scan_open: bool = Field(default=True, description="09:30 开盘扫描")
    scan_mid_morning: bool = Field(default=True, description="10:30 盘中扫描")
    signal_noon: bool = Field(default=True, description="11:35 午盘信号汇总")
    macro_noon: bool = Field(default=True, description="11:40 午间宏观简报")
    scan_afternoon: bool = Field(default=True, description="13:05 午盘扫描")
    rsi_scan: bool = Field(default=True, description="13:30 RSI 专项扫描（仅 rsi_reversal 策略）")
    scan_close: bool = Field(default=True, description="14:30 尾盘扫描")
    screener: bool = Field(default=True, description="14:30 尾盘选股")
    flow_afternoon: bool = Field(default=True, description="14:30 盘后资金流决策报告")
    signal_close: bool = Field(default=True, description="15:10 收盘信号汇总")
    macro_close: bool = Field(default=True, description="15:15 收盘宏观简报")
    signal_performance: bool = Field(default=True, description="15:20 信号绩效验证")
    concept_daily: bool = Field(default=False, description="20:00 每日概念（默认关闭）")
    altalpha_fetch: bool = Field(default=True, description="18:00 另类因子盘后采集")
    alpha_summary: bool = Field(default=True, description="18:05 因子综合评分报告")
    portfolio_report: bool = Field(default=True, description="15:35 持仓认知分析报告")
    sentiment_daily: bool = Field(default=True, description="20:30 舆情日报")
    weekly_optimize: bool = Field(default=True, description="周日 03:00 周参数优化")
    heuristic_optimize: bool = Field(
        default=False,
        description="每月1/15日 04:00 启发式优化（GA/PSO交替，默认关闭由settings.yaml控制）",
    )
    weekly_review: bool = Field(default=True, description="周日 20:00 周复盘")
    intraday_monitor: bool = Field(default=True, description="每分钟 盘中实时盯盘")
    perspective_afternoon: bool = Field(
        default=False, description="15:30 收盘后十大视角二次分析（默认关闭）"
    )


class SchedulerConfig(BaseModel):
    """调度器配置."""

    timezone: str = Field(default="Asia/Shanghai", description="时区")
    trading_days_only: bool = Field(default=True, description="仅在交易日执行")
    scan_times: list[str] = Field(
        default_factory=lambda: ["09:30", "10:30", "13:05", "14:30"],
        description="盘中扫描时刻",
    )
    tasks: SchedulerTasksConfig = Field(
        default_factory=SchedulerTasksConfig,
        description="调度任务开关",
    )


class WisdomConfig(BaseModel):
    """交易智慧配置."""

    skill_dir: str = Field(default="src/gugu/wisdom/skills", description="项目内 skill 目录")
    fallback_dirs: list[str] = Field(default_factory=list, description="额外的 skill 目录")
    skill_names: list[str] = Field(
        default_factory=lambda: [
            "stock-entry-decision",
            "stock-stop-loss-decision",
            "stock-position-sizing",
            "stock-profit-taking-decision",
            "stock-trailing-stop",
            "stock-psychology-check",
        ],
        description="启用的 skill 列表",
    )
    # P0 修复：enable_multi_perspective 配置开关此前在 pydantic 中被静默丢弃
    # 导致 settings.yaml 中 enable_multi_perspective: false 完全不生效，
    # 每次决策仍无条件注入 28 本书的多视角上下文
    books_dir: str = Field(default="src/gugu/wisdom/skills/books", description="书籍视角目录")
    enable_multi_perspective: bool = Field(
        default=False,
        description="是否启用多视角裁决（默认关闭，新手不需要利弗莫尔/格雷厄姆的权重裁决）",
    )
    max_perspectives: int = Field(default=3, description="每次决策最多参考的视角数")
    # LLM 超时与重试配置
    llm_timeout: float = Field(default=20.0, description="LLM 请求超时时间（秒）")
    llm_max_retries: int = Field(default=2, description="LLM 请求最大重试次数")
    llm_retry_delay: float = Field(default=1.0, description="重试间隔（秒）")


class FundamentalConfig(BaseModel):
    """基本面过滤配置."""

    pe_min: float = Field(default=0, description="PE 下限")
    pe_max: float = Field(default=60, description="PE 上限")
    pb_min: float = Field(default=0, description="PB 下限")
    pb_max: float = Field(default=8, description="PB 上限")
    roe_min: float = Field(default=10, description="ROE 下限（%，P1-1优化：提升到10%巴菲特标准）")
    revenue_growth_min: float = Field(default=-20, description="营收增长率下限（%）")
    debt_ratio_max: float = Field(default=70, description="P1-1新增：负债率上限（%）")


class LogConfig(BaseModel):
    """日志配置."""

    level: str = Field(default="INFO", description="日志级别")
    dir: str = Field(default="logs", description="日志目录")
    rotation: str = Field(default="10 MB", description="日志轮转大小")
    retention: str = Field(default="30 days", description="日志保留时间")


class WebConfig(BaseModel):
    """Web 仪表盘安全配置."""

    admin_api_key: str = Field(
        default="",
        description="Web 管理接口 API Key。为空时不拒绝请求（仅本地开发），生产环境务必配置。",
    )
    require_auth_for_sensitive: bool = Field(
        default=False,
        description="是否对敏感接口（下单、解除熔断等）强制 API Key 认证。",
    )


class PerspectiveConfig(BaseModel):
    """十大视角分析配置."""

    enabled: bool = Field(default=True, description="是否启用十大视角分析")
    perspectives: list[str] = Field(
        default_factory=lambda: [
            "buffett",
            "munger",
            "soros",
            "lynch",
            "livermore",
            "qiu_guo_lu",
            "chen_jiang_ting",
            "technical",
            "investing_habits",
            "dashixiong",
        ],
        description="启用的视角列表",
    )
    pass_threshold: float = Field(default=6.0, ge=0.0, le=10.0, description="单视角通过阈值")
    composite_pass_threshold: float = Field(
        default=7.0, ge=0.0, le=10.0, description="综合评分通过阈值"
    )
    min_perspective_score: float = Field(
        default=4.0, ge=0.0, le=10.0, description="单视角最低容忍分（低于此否决）"
    )
    auto_analyze_watchlist: bool = Field(default=True, description="是否自动分析自选股")
    scan_with_perspectives: bool = Field(default=True, description="选股时是否运行视角分析")
    report_time: str = Field(default="08:30", description="每日推送时间（交易日）")
    top_n_report: int = Field(default=5, ge=1, description="报告展示前N只")


class HolderConcentrationConfig(BaseModel):
    """筹码集中度过滤器配置.

    股东户数季度披露，作为慢变量过滤器，拒绝散户化股票入场。
    来源：豆包AI「龙头股散户化」分析 + 6 案例回测验证。
    """

    enabled: bool = Field(default=True, description="是否启用筹码集中度过滤")
    quarterly_change_pct_threshold: float = Field(
        default=30.0, ge=0.0, description="季度户数增幅阈值(%)，超过即拒绝"
    )
    monthly_change_pct_threshold: float = Field(
        default=15.0, ge=0.0, description="月度户数增幅阈值(%)，超过即拒绝"
    )
    avg_hold_change_threshold: float = Field(
        default=-20.0, description="户均持股变化率阈值(%)，低于即拒绝"
    )
    lurker_consecutive_quarters: int = Field(default=2, ge=1, description="潜伏者连续季度数")
    lurker_total_decline_threshold: float = Field(
        default=20.0, ge=0.0, description="潜伏者累计降幅阈值(%)"
    )


class AltAlphaFilterConfig(BaseModel):
    """另类 Alpha 因子过滤器配置.

    将事件驱动/行为金融因子接入信号流水线，为买入信号提供额外维度评分。
    数据缺失时默认放行，避免阻塞已有策略信号。
    """

    enabled: bool = Field(default=True, description="是否启用另类 Alpha 因子过滤")
    block_threshold: float = Field(default=-0.50, description="综合评分低于此值时标记过滤")
    boost_threshold: float = Field(default=0.30, description="综合评分高于此值时提升 confidence")
    confidence_boost: float = Field(
        default=0.10, ge=0.0, le=0.5, description="高评分时的 confidence 增量"
    )
    altalpha_weight: float = Field(
        default=0.30, ge=0.0, le=1.0, description="另类因子在综合评分中的权重"
    )
    fetch_financial: bool = Field(default=False, description="是否获取财务数据（较慢）")


class IntradayMonitorConfig(BaseModel):
    """盘中实时盯盘配置.

    按性能分级频率扫描持仓+watchlist 的资金流异动，
    发现超大单异动、连续资金流、行业异动、持仓预警时推送告警。
    """

    enabled: bool = Field(default=True, description="是否启用盘中实时盯盘")
    performance_mode: str = Field(default="auto", description="性能模式: auto|high|medium|low|off")
    realtime_recommend: bool = Field(
        default=True, description="v0.9.0 盘中实时推荐（策略信号持续扫描）"
    )
    signal_cooldown_sec: int = Field(
        default=1800, ge=60, description="v0.9.0 同一(股票,方向)信号最小推送间隔（秒）"
    )
    signal_scan_interval_sec: int = Field(
        default=300, ge=60, description="v0.9.0 策略全量扫描间隔（秒），单机性能节流"
    )
    custom_watchlist: list[str] = Field(
        default_factory=list, description="自定义监控范围（留空则按性能模式自动决定）"
    )
    alert_thresholds: dict[str, Any] = Field(
        default_factory=lambda: {
            # 单位：亿元 / %，与 DataManager 返回单位对齐
            # P0-1 修复：原按元单位配置，但数据源已 /1e8 转为亿元
            "super_large_inflow": 1.0,
            "super_large_outflow": 1.0,
            "main_pct_threshold": 5.0,
            "consecutive_days": 3,
            "consecutive_total": 3.0,
            "industry_flow_threshold": 10.0,
            "position_outflow_warning": 0.5,
        },
        description="告警阈值（单位：亿元 / %）",
    )


# ──────────────────────────────────────────────────────────────────────
#  Top-level application config
# ──────────────────────────────────────────────────────────────────────


class AppConfig(BaseModel):
    """gugu 交易系统全局配置.

    All section configs live under their own attribute.  Use :meth:`from_settings`
    to load from ``config/settings.yaml``.
    """

    watchlist: list[str] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    wisdom: WisdomConfig = Field(default_factory=WisdomConfig)
    fundamental: FundamentalConfig = Field(default_factory=FundamentalConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    perspective: PerspectiveConfig = Field(default_factory=PerspectiveConfig)
    holder_concentration_filter: HolderConcentrationConfig = Field(
        default_factory=HolderConcentrationConfig,
        description="筹码集中度过滤器配置",
    )
    altalpha_filter: AltAlphaFilterConfig = Field(
        default_factory=AltAlphaFilterConfig,
        description="另类 Alpha 因子过滤器配置",
    )
    intraday_monitor: IntradayMonitorConfig = Field(
        default_factory=IntradayMonitorConfig,
        description="盘中实时盯盘配置",
    )
    stock_ranker: StockRankerConfig = Field(
        default_factory=StockRankerConfig,
        description="StockRanker 选股评分器配置（v0.7.1 新增）",
    )
    flow_report: FlowReportConfig = Field(
        default_factory=FlowReportConfig,
        description="资金流报告配置（v0.7.1 新增）",
    )

    # ── classmethod factory ──────────────────────────────────────────

    @classmethod
    def from_settings(cls, yaml_path: str | None = None) -> AppConfig:
        """Load config from ``settings.yaml`` and return a validated instance.

        Parameters
        ----------
        yaml_path:
            Absolute path to the YAML file.  Defaults to
            ``<project_root>/config/settings.yaml``.
        """
        if yaml_path is None:
            project_root = Path(__file__).resolve().parents[3]
            yaml_path = str(project_root / "config" / "settings.yaml")

        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with path.open("r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        return cls.model_validate(raw)

    # ── compat helper ────────────────────────────────────────────────

    def flatten(self) -> dict[str, Any]:
        """Return the old dict-style config so callers using bracket-style
        access (e.g. ``settings()["risk"]["max_position_ratio"]``) keep
        working during migration.

        Example
        -------
        >>> cfg = AppConfig.from_settings()
        >>> cfg.flatten()["risk"]["max_position_ratio"]
        0.3
        """
        return flatten_config(self)


def flatten_config(app_config: AppConfig) -> dict[str, Any]:
    """Convert a validated ``AppConfig`` into the legacy nested-dict format.

    This is the *compat function* described in the gugu config migration
    plan -- it lets existing code that does ``settings().get("risk", {}).
    get("max_position_ratio")`` continue to work unchanged.
    """
    return {
        "watchlist": list(app_config.watchlist),
        "data": {
            "primary_source": app_config.data.primary_source,
            "fallback_sources": list(app_config.data.fallback_sources),
            "fetch_interval_seconds": app_config.data.fetch_interval_seconds,
            "fail_threshold": app_config.data.fail_threshold,
            "fail_cooldown_seconds": app_config.data.fail_cooldown_seconds,
            "cache_ttl_seconds": app_config.data.cache_ttl_seconds,
            "history_db": app_config.data.history_db,
            "parquet_dir": app_config.data.parquet_dir,
        },
        "strategy": {
            "enabled": list(app_config.strategy.enabled),
            "signal_fusion": app_config.strategy.signal_fusion,
            "min_confidence": app_config.strategy.min_confidence,
            "auto_select": app_config.strategy.auto_select,
            "select_top_n": app_config.strategy.select_top_n,
            "select_max_candidates": app_config.strategy.select_max_candidates,
        },
        "risk": {
            "max_position_ratio": app_config.risk.max_position_ratio,
            "daily_loss_warn": app_config.risk.daily_loss_warn,
            "daily_loss_halt": app_config.risk.daily_loss_halt,
            "max_total_positions": app_config.risk.max_total_positions,
            "max_same_industry": app_config.risk.max_same_industry,
            "t_plus_1": app_config.risk.t_plus_1,
            "max_holding_days_warn": app_config.risk.max_holding_days_warn,
            "max_holding_days_halt": app_config.risk.max_holding_days_halt,
            "max_drawdown_halt": app_config.risk.max_drawdown_halt,
        },
        "execution": {
            "mode": app_config.execution.mode,
            "paper": {
                "initial_capital": app_config.execution.paper.initial_capital,
                "commission_rate": app_config.execution.paper.commission_rate,
                "stamp_tax": app_config.execution.paper.stamp_tax,
                "slippage": app_config.execution.paper.slippage,
            },
            "live": {
                "broker": app_config.execution.live.broker,
                "confirm_required": app_config.execution.live.confirm_required,
            },
        },
        "feishu": {
            "enabled": app_config.feishu.enabled,
            "notify_signal": app_config.feishu.notify_signal,
            "notify_daily_report": app_config.feishu.notify_daily_report,
            "notify_risk_alert": app_config.feishu.notify_risk_alert,
            "notify_backtest": app_config.feishu.notify_backtest,
            "notify_system_error": app_config.feishu.notify_system_error,
            "daily_report_times": list(app_config.feishu.daily_report_times),
        },
        "scheduler": {
            "timezone": app_config.scheduler.timezone,
            "trading_days_only": app_config.scheduler.trading_days_only,
            "scan_times": list(app_config.scheduler.scan_times),
        },
        "wisdom": {
            "skill_dir": app_config.wisdom.skill_dir,
            "fallback_dirs": list(app_config.wisdom.fallback_dirs),
            "skill_names": list(app_config.wisdom.skill_names),
        },
        "fundamental": {
            "pe_min": app_config.fundamental.pe_min,
            "pe_max": app_config.fundamental.pe_max,
            "pb_min": app_config.fundamental.pb_min,
            "pb_max": app_config.fundamental.pb_max,
            "roe_min": app_config.fundamental.roe_min,
            "revenue_growth_min": app_config.fundamental.revenue_growth_min,
        },
        "log": {
            "level": app_config.log.level,
            "dir": app_config.log.dir,
            "rotation": app_config.log.rotation,
            "retention": app_config.log.retention,
        },
        "web": {
            "admin_api_key": app_config.web.admin_api_key,
            "require_auth_for_sensitive": app_config.web.require_auth_for_sensitive,
        },
        "perspective": {
            "enabled": app_config.perspective.enabled,
            "perspectives": list(app_config.perspective.perspectives),
            "pass_threshold": app_config.perspective.pass_threshold,
            "composite_pass_threshold": app_config.perspective.composite_pass_threshold,
            "min_perspective_score": app_config.perspective.min_perspective_score,
            "auto_analyze_watchlist": app_config.perspective.auto_analyze_watchlist,
            "scan_with_perspectives": app_config.perspective.scan_with_perspectives,
            "report_time": app_config.perspective.report_time,
            "top_n_report": app_config.perspective.top_n_report,
        },
        "holder_concentration_filter": {
            "enabled": app_config.holder_concentration_filter.enabled,
            "quarterly_change_pct_threshold": app_config.holder_concentration_filter.quarterly_change_pct_threshold,
            "monthly_change_pct_threshold": app_config.holder_concentration_filter.monthly_change_pct_threshold,
            "avg_hold_change_threshold": app_config.holder_concentration_filter.avg_hold_change_threshold,
            "lurker_consecutive_quarters": app_config.holder_concentration_filter.lurker_consecutive_quarters,
            "lurker_total_decline_threshold": app_config.holder_concentration_filter.lurker_total_decline_threshold,
        },
        "altalpha_filter": {
            "enabled": app_config.altalpha_filter.enabled,
            "block_threshold": app_config.altalpha_filter.block_threshold,
            "boost_threshold": app_config.altalpha_filter.boost_threshold,
            "confidence_boost": app_config.altalpha_filter.confidence_boost,
            "altalpha_weight": app_config.altalpha_filter.altalpha_weight,
            "fetch_financial": app_config.altalpha_filter.fetch_financial,
        },
        "intraday_monitor": {
            "enabled": app_config.intraday_monitor.enabled,
            "performance_mode": app_config.intraday_monitor.performance_mode,
            "realtime_recommend": app_config.intraday_monitor.realtime_recommend,
            "signal_cooldown_sec": app_config.intraday_monitor.signal_cooldown_sec,
            "signal_scan_interval_sec": app_config.intraday_monitor.signal_scan_interval_sec,
            "custom_watchlist": list(app_config.intraday_monitor.custom_watchlist),
            "alert_thresholds": dict(app_config.intraday_monitor.alert_thresholds),
        },
        "stock_ranker": {
            "factor_weight": app_config.stock_ranker.factor_weight,
            "fundamental_weight": app_config.stock_ranker.fundamental_weight,
            "money_flow_weight": app_config.stock_ranker.money_flow_weight,
            "sector_weight": app_config.stock_ranker.sector_weight,
            "chip_score_weight": app_config.stock_ranker.chip_score_weight,
            "chip_score_neutral": app_config.stock_ranker.chip_score_neutral,
        },
        "flow_report": {
            "enabled": app_config.flow_report.enabled,
            "morning_time": app_config.flow_report.morning_time,
            "close_time": app_config.flow_report.close_time,
            "sector_top_n": app_config.flow_report.sector_top_n,
            "stock_strong_top_n": app_config.flow_report.stock_strong_top_n,
        },
    }
