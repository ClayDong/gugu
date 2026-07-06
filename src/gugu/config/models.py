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


class DataConfig(BaseModel):
    """数据源配置."""

    primary_source: str = Field(default="sina", description="主数据源")
    fallback_sources: list[str] = Field(default_factory=lambda: ["akshare"], description="降级源列表")
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
        default_factory=lambda: ["box_breakout", "turtle", "dual_ma"],
        description="启用的策略列表",
    )
    signal_fusion: str = Field(
        default="majority", description="信号融合规则: unanimous / majority / any"
    )
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0, description="最低置信度阈值")
    auto_select: bool = Field(default=False, description="是否启用自动选股")
    select_top_n: int = Field(default=50, ge=1, description="全市场资金流排序取前 N")
    select_max_candidates: int = Field(default=10, ge=1, description="最大选股数量")
    # v0.4.6: 豹哥三要素选股池过滤器（默认关闭，仅与突破策略配合时开启）
    leopard_universe_filter: "LeopardUniverseConfig" = Field(
        default_factory=lambda: LeopardUniverseConfig(),
        description="豹哥三要素选股池过滤器配置",
    )

    # ── aliases ──
    @property
    def fusion_mode(self) -> str:
        return self.signal_fusion


class LeopardUniverseConfig(BaseModel):
    """豹哥三要素选股池过滤器配置.

    来源：豹哥战法（微信公众号文章）
    三要素是选股池准入，不是择时信号。默认关闭，因为：
    - 三要素要求换手率≥15%（高度活跃），与 pullback_low/double_yin 缩量低吸逻辑冲突
    - 适合与 box_breakout（突破追势）配合，趋势市手动开启
    """

    enabled: bool = Field(default=False, description="是否启用三要素选股池预筛")
    turnover_min: float = Field(default=15.0, ge=0, description="换手率下限(%)")
    amount_min: float = Field(
        default=1_000_000_000, ge=0, description="成交额下限(元，10亿)"
    )
    circ_mv_min: float = Field(
        default=7_000_000_000, ge=0, description="流通市值下限(元，70亿)"
    )


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

    mode: str = Field(default="paper", description="运行模式: backtest / paper / live")
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


class SchedulerConfig(BaseModel):
    """调度器配置."""

    timezone: str = Field(default="Asia/Shanghai", description="时区")
    trading_days_only: bool = Field(default=True, description="仅在交易日执行")
    scan_times: list[str] = Field(
        default_factory=lambda: ["09:30", "10:30", "13:05", "14:30"],
        description="盘中扫描时刻",
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


class FundamentalConfig(BaseModel):
    """基本面过滤配置."""

    pe_min: float = Field(default=0, description="PE 下限")
    pe_max: float = Field(default=100, description="PE 上限")
    pb_min: float = Field(default=0, description="PB 下限")
    pb_max: float = Field(default=15, description="PB 上限")
    roe_min: float = Field(default=0, description="ROE 下限（%）")
    revenue_growth_min: float = Field(default=-20, description="营收增长率下限（%）")


class FundItemConfig(BaseModel):
    """单只基金配置."""

    code: str = Field(default="", description="基金代码")
    name: str = Field(default="", description="基金名称")
    cost_price: float | None = Field(default=None, description="持仓成本价")
    base_investment: float = Field(default=1000, description="定投基准金额")


class FundConfig(BaseModel):
    """基金监控配置."""

    enabled: bool = Field(default=True, description="是否启用基金监控")
    funds: list[FundItemConfig] = Field(default_factory=list, description="基金列表")


class LogConfig(BaseModel):
    """日志配置."""

    level: str = Field(default="INFO", description="日志级别")
    dir: str = Field(default="logs", description="日志目录")
    rotation: str = Field(default="10 MB", description="日志轮转大小")
    retention: str = Field(default="30 days", description="日志保留时间")


class PerspectiveConfig(BaseModel):
    """十大视角分析配置."""

    enabled: bool = Field(default=True, description="是否启用十大视角分析")
    perspectives: list[str] = Field(
        default_factory=lambda: [
            "buffett", "munger", "soros", "lynch", "livermore",
            "qiu_guo_lu", "chen_jiang_ting", "technical",
            "investing_habits", "dashixiong",
        ],
        description="启用的视角列表",
    )
    pass_threshold: float = Field(default=6.0, ge=0.0, le=10.0, description="单视角通过阈值")
    composite_pass_threshold: float = Field(default=7.0, ge=0.0, le=10.0, description="综合评分通过阈值")
    min_perspective_score: float = Field(default=4.0, ge=0.0, le=10.0, description="单视角最低容忍分（低于此否决）")
    auto_analyze_watchlist: bool = Field(default=True, description="是否自动分析自选股")
    scan_with_perspectives: bool = Field(default=True, description="选股时是否运行视角分析")
    report_time: str = Field(default="08:30", description="每日推送时间（交易日）")
    top_n_report: int = Field(default=5, ge=1, description="报告展示前N只")


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
    fund: FundConfig = Field(default_factory=FundConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    perspective: PerspectiveConfig = Field(default_factory=PerspectiveConfig)

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
        "fund": {
            "enabled": app_config.fund.enabled,
            "funds": [
                {
                    "code": f.code,
                    "name": f.name,
                    "cost_price": f.cost_price,
                    "base_investment": f.base_investment,
                }
                for f in app_config.fund.funds
            ],
        },
        "log": {
            "level": app_config.log.level,
            "dir": app_config.log.dir,
            "rotation": app_config.log.rotation,
            "retention": app_config.log.retention,
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
    }
