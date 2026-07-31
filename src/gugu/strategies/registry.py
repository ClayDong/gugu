"""策略注册表。按名称获取策略实例。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from gugu.strategies.base import Strategy
from gugu.strategies.blogger_rules import ChenXueZhangStrategy, LinJuDaYeStrategy
from gugu.strategies.breakout import BoxBreakoutStrategy, DualThrustStrategy
from gugu.strategies.breakout_rebound import BreakoutReboundStrategy
from gugu.strategies.capital_flow_alert import CapitalFlowAlertStrategy
from gugu.strategies.chen_jiangting import ChenJiangtingStrategy
from gugu.strategies.cloud_trend import CloudTrendStrategy
from gugu.strategies.double_yin import DoubleYinMoneyBagStrategy
from gugu.strategies.dragon_tiger_short import DragonTigerShortStrategy
from gugu.strategies.first_board import FirstBoardStrategy
from gugu.strategies.hot_money import HotMoneyStrategy
from gugu.strategies.huamulan_crash_layout import HuamulanCrashLayoutStrategy
from gugu.strategies.huamulan_fake_dip import HuamulanFakeDipStrategy
from gugu.strategies.huamulan_ghost_breakout import HuamulanGhostBreakoutStrategy
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
from gugu.strategies.pullback_low import PullbackLowStrategy
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
    "capital_flow_alert": CapitalFlowAlertStrategy,  # 资金流异动监控
    "ml_factor": MLFactorStrategy,  # LightGBM 多因子策略
    "north_force_long": NorthForceLongStrategy,  # 长线主力吸筹跟随
    "dragon_tiger_short": DragonTigerShortStrategy,  # 短线龙虎榜反包跟随
    "cloud_trend": CloudTrendStrategy,  # 云策略：三维趋势+两类入场+分层仓位
    "hot_money": HotMoneyStrategy,  # 游资策略：三数交易法（涨停筛选+缩量入场+放量离场）
    "industry_capital_momentum": IndustryCapitalMomentumStrategy,  # 行业资金强度+动量确认
    "first_board": FirstBoardStrategy,  # 首板战法（北京炒家）：首板套利+极致风控+机械执行
    "ma20_trend": MA20TrendStrategy,  # 20日均线趋势跟随战法（趋势为王+回踩低吸+放量突破）
    "zhen_dahu": ZhenDahuStrategy,  # 珍大户价值投资策略（价值回归+行业优先+护城河+安全边际）
    "weekly_combo": WeeklyComboStrategy,  # 周线选股策略：四句心法实战化（倍量+金叉+回踩+三连阳）
    "huamulan_tail_pick": HuamulanTailPickStrategy,  # 花木兰尾盘选股六步法（小资金复利首选）
    "huamulan_crash_layout": HuamulanCrashLayoutStrategy,  # 花木兰空仓等暴跌布局法（大资金稳健复利）
    "huamulan_fake_dip": HuamulanFakeDipStrategy,  # 花木兰早盘假摔低吸战法（捕捉主力洗盘）
    "huamulan_ghost_breakout": HuamulanGhostBreakoutStrategy,
    "chenxuezhang": ChenXueZhangStrategy,
    "linjudaye": LinJuDaYeStrategy,
    "ml_gradient_boost": MLGradientBoostStrategy,
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
    注：按 regime 过滤由 get_enabled_strategies_for_regime() 实现
    """
    from gugu.config import get_config

    cfg = get_config().strategy
    # v0.5.6: 优先使用 enabled_by_regime 的并集
    enabled = cfg.get_all_enabled_unique() if cfg.enabled_by_regime else cfg.enabled
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

    cfg = get_config().strategy
    enabled = cfg.get_enabled_for_regime(regime)
    return [get_strategy(name) for name in enabled if name in _REGISTRY]


def reload_strategies() -> None:
    """清除策略缓存，使下次 get_enabled_strategies 调用重新读取配置（O-01 修复）。

    用于配置热更新场景：修改 settings.yaml 后调用此方法，
    无需重启进程即可生效。也可通过飞书指令触发。
    """
    get_enabled_strategies.cache_clear()
    from gugu.utils.log import get_logger

    get_logger().info("策略缓存已清除，下次调用将重新读取配置")
