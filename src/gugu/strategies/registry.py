"""策略注册表。按名称获取策略实例。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from gugu.strategies.asset_combo import AssetComboStrategy
from gugu.strategies.base import Strategy
from gugu.strategies.blogger_rules import ChenXueZhangStrategy, LinJuDaYeStrategy
from gugu.strategies.breakout import BoxBreakoutStrategy, DualThrustStrategy
from gugu.strategies.breakout_rebound import BreakoutReboundStrategy
from gugu.strategies.capital_flow_alert import CapitalFlowAlertStrategy
from gugu.strategies.chen_jiangting import ChenJiangtingStrategy
from gugu.strategies.cloud_trend import CloudTrendStrategy
from gugu.strategies.double_yin import DoubleYinMoneyBagStrategy
from gugu.strategies.dragon_tiger_short import DragonTigerShortStrategy
from gugu.strategies.fanbao_strategy import FanbaoStrategy
from gugu.strategies.first_board import FirstBoardStrategy
from gugu.strategies.huamulan_crash_layout import HuamulanCrashLayoutStrategy
from gugu.strategies.huamulan_fake_dip import HuamulanFakeDipStrategy
from gugu.strategies.huamulan_tail_pick import HuamulanTailPickStrategy
from gugu.strategies.industry_capital_momentum import IndustryCapitalMomentumStrategy
from gugu.strategies.leopard_three_factors import LeopardThreeFactorsStrategy
from gugu.strategies.ma5_momentum import MA5MomentumStrategy
from gugu.strategies.ma20_trend import MA20TrendStrategy
from gugu.strategies.mean_revert import BollingerStrategy, KDJStrategy, RSIStrategy
from gugu.strategies.ml_factor import MLFactorStrategy
from gugu.strategies.ml_gradient_boost import MLGradientBoostStrategy
from gugu.strategies.money_flow_follow import MoneyFlowFollowStrategy
from gugu.strategies.north_force_long import NorthForceLongStrategy
from gugu.strategies.price_volume import PriceVolumeSignalStrategy
from gugu.strategies.pullback_low import PullbackLowStrategy
from gugu.strategies.rebound_elite import ReboundEliteStrategy
from gugu.strategies.rebound_system import ReboundSystemStrategy
from gugu.strategies.regime_cycle import (
    BollingerOversoldReboundStrategy,
    DeathValleyReboundStrategy,
    RegimeCycleFourStrategy,
)
from gugu.strategies.trend import DualMAStrategy, MACDStrategy, TurtleStrategy
from gugu.strategies.weekly_combo import WeeklyComboStrategy
from gugu.strategies.zhen_dahu import ZhenDahuStrategy

# 策略注册表：name -> class
_REGISTRY: dict[str, type[Strategy]] = {
    "turtle": TurtleStrategy,
    "dual_ma": DualMAStrategy,
    "macd": MACDStrategy,
    "bollinger": BollingerStrategy,
    "rsi_reversal": RSIStrategy,
    "kdj": KDJStrategy,
    "box_breakout": BoxBreakoutStrategy,
    "dual_thrust": DualThrustStrategy,
    "money_flow_follow": MoneyFlowFollowStrategy,
    "chen_jiangting": ChenJiangtingStrategy,
    "leopard_three_factors": LeopardThreeFactorsStrategy,
    "ma5_momentum": MA5MomentumStrategy,  # 5日均线动量策略（放量大阳线+回踩MA5）
    "pullback_low": PullbackLowStrategy,  # 震荡市专用低吸回踩策略
    "double_yin": DoubleYinMoneyBagStrategy,  # 双阴钱袋子策略（主力洗盘拐点）
    "breakout_rebound": BreakoutReboundStrategy,  # 断板反包策略（短线主力行为）
    "rebound_elite": ReboundEliteStrategy,  # 断板反包·精英追涨（高活跃妖股+反包追涨，验证有效形态）
    "rebound_system": ReboundSystemStrategy,  # 断板反包·陈学长实战体系完整版（手册量化：四阶段/三买点/六卖出/成交额体系）
    "capital_flow_alert": CapitalFlowAlertStrategy,  # 资金流异动监控
    "ml_factor": MLFactorStrategy,  # LightGBM 多因子策略
    "north_force_long": NorthForceLongStrategy,  # 长线主力吸筹跟随
    "dragon_tiger_short": DragonTigerShortStrategy,  # 短线龙虎榜反包跟随
    "cloud_trend": CloudTrendStrategy,  # 云策略：三维趋势+两类入场+分层仓位
    # hot_money 已移除（2026-08-01 样本外证伪：集中度超标不可信，见 strategy_manifest.yaml）
    "industry_capital_momentum": IndustryCapitalMomentumStrategy,  # 行业资金强度+动量确认
    "first_board": FirstBoardStrategy,  # 首板战法（北京炒家）：首板套利+极致风控+机械执行
    "ma20_trend": MA20TrendStrategy,  # 20日均线趋势跟随战法（趋势为王+回踩低吸+放量突破）
    "zhen_dahu": ZhenDahuStrategy,  # 珍大户价值投资策略（价值回归+行业优先+护城河+安全边际）
    "weekly_combo": WeeklyComboStrategy,  # 周线选股策略：四句心法实战化（倍量+金叉+回踩+三连阳）
    "huamulan_tail_pick": HuamulanTailPickStrategy,  # 花木兰尾盘选股六步法（小资金复利首选）
    "huamulan_crash_layout": HuamulanCrashLayoutStrategy,  # 花木兰空仓等暴跌布局法（大资金稳健复利）
    "huamulan_fake_dip": HuamulanFakeDipStrategy,  # 花木兰早盘假摔低吸战法（捕捉主力洗盘）
    "chenxuezhang": ChenXueZhangStrategy,
    "linjudaye": LinJuDaYeStrategy,
    "ml_gradient_boost": MLGradientBoostStrategy,
    "price_volume_signal": PriceVolumeSignalStrategy,  # 换手率+量比尾盘黄金信号（选股入场漏斗）
    "regime_cycle_four": RegimeCycleFourStrategy,  # 四周期模型：银山谷买入/死亡谷卖出（公众号战法）
    "death_valley_rebound": DeathValleyReboundStrategy,  # 幻灭期修复反弹：3日不创新低+KDJ金叉+5日线
    "bollinger_oversold_rebound": BollingerOversoldReboundStrategy,  # 布林下轨超跌反弹（抢反弹）
    "fanbao": FanbaoStrategy,  # 小圈圈儿《反包策略》：炸板反包+高位跌停反包
    "asset_combo": AssetComboStrategy,  # 认知资产组合验证策略，conditional_pass（2026-08-15: 512只主板500d，WF 5d +0.82%/p=0.36，但持仓Sharpe全负；非独立盈利策略，gap+regime弱势可作为风控工具）
}


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    """获取策略实例。

    Args:
        name: 策略名称
        params: 覆盖默认参数

    Returns:
        Strategy 实例

    Raises:
        KeyError: 策略未注册
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"策略 '{name}' 未注册，可用: {list(_REGISTRY.keys())}")
    return cls(params)


def get_strategy_cls(name: str) -> type[Strategy] | None:
    """获取策略类（非实例），用于 Walk-Forward 优化等需要 Class 而非 instance 的场景。

    Args:
        name: 策略名称

    Returns:
        策略类，未注册返回 None
    """
    return _REGISTRY.get(name)


def list_strategies() -> list[str]:
    """列出所有已注册策略。"""
    return list(_REGISTRY.keys())


@lru_cache(maxsize=1)
def get_enabled_strategies() -> list[Strategy]:
    """获取配置中启用的策略实例列表（带缓存，全局共享同一批实例）。

    v0.5.6 优化1：优先使用 enabled_by_regime 的并集（向后兼容 enabled）
    v0.9.7 P0 修复：必须按 execution_mode 过滤 experimental 策略——
    原实现用 get_all_enabled_unique()（所有 regime 并集），会把 experimental
    策略（chenxuezhang/linjudaye 等）泄漏进 track_live/signal_only 模式，
    与 get_enabled_strategies_for_regime() 的行为不一致，导致自动选股路径
    跑"不应启用"的策略。现统一委托 get_enabled_for_regime(None, mode)。
    注：按 regime 过滤由 get_enabled_strategies_for_regime() 实现
    """
    from gugu.config import get_config

    cfg = get_config()
    strategy_cfg = cfg.strategy
    # v0.9.7: 统一按 execution_mode 过滤（track_live/signal_only/live 排除 experimental）
    if strategy_cfg.verified_strategies:
        enabled = strategy_cfg.get_enabled_for_regime(None, cfg.execution.mode)
    elif strategy_cfg.enabled_by_regime:
        enabled = strategy_cfg.get_all_enabled_unique()
    else:
        enabled = strategy_cfg.enabled
    return [get_strategy(name) for name in enabled if name in _REGISTRY]


def get_enabled_strategies_for_regime(regime: str | None) -> list[Strategy]:
    """获取指定市场状态启用的策略实例列表（v0.5.6 优化1 新增）。

    Args:
        regime: 市场状态（bull/sideways/bear/rally/crash），None 表示未知

    Returns:
        对应 regime 启用的策略实例列表

    策略按市场状态启用，而非全局禁用：
    - 牛市：突破追势策略（box_breakout）
    - 震荡：拐点策略（double_yin）
    - 熊市：空仓观察（返回空列表）
    """
    from gugu.config import get_config

    app_cfg = get_config()
    cfg = app_cfg.strategy
    enabled = cfg.get_enabled_for_regime(regime, app_cfg.execution.mode)
    return [get_strategy(name) for name in enabled if name in _REGISTRY]


def reload_strategies() -> None:
    """清除策略缓存，使下次 get_enabled_strategies 调用重新读取配置（O-01 修复）。

    用于配置热更新场景：修改 settings.yaml 后调用此方法，
    无需重启进程即可生效。也可通过飞书指令触发。
    """
    get_enabled_strategies.cache_clear()
    from gugu.utils.log import get_logger

    get_logger().info("策略缓存已清除，下次调用将重新读取配置")


def verify_registry_consistency() -> tuple[list[str], list[str]]:
    """启动自检（v0.9.11 防线2）：注册表 vs 配置一致性检查。

    两级结果（返回 (fatal, warning) 二元组）：
    - fatal：配置启用但代码未注册 → get_strategy 会 KeyError，必须拒绝启动
    - warning：代码注册但配置未引用 → 死代码残留（不会发信号，但应清理；
      hot_money 2026-08-01 证伪后 5 处注册点残留的教训——残留越少，幽灵风险越低）

    Usage:
        fatal, warning = verify_registry_consistency()
        if fatal:
            raise SystemExit(1)
        for w in warning:
            logger.warning(w)
    """
    from gugu.config import get_config

    fatal: list[str] = []
    warning: list[str] = []
    cfg = get_config()
    strategy_cfg = cfg.strategy

    # 1. 配置启用但未注册（fail-fast：get_strategy 会 KeyError，提前暴露）
    configured = strategy_cfg.get_all_enabled_unique()
    for name in configured:
        if name not in _REGISTRY:
            fatal.append(f"配置启用但代码未注册: {name}")

    # 2. 代码注册但配置完全不引用（死代码残留，warning 不阻塞）
    all_configured = set(configured) | set(strategy_cfg.experimental_strategies)
    for name in _REGISTRY:
        if name not in all_configured:
            warning.append(f"代码注册但配置未启用/未标记: {name}（死代码残留，建议清理）")

    return fatal, warning
