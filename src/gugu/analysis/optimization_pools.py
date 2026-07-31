"""策略优化股票池配置。

将按策略类型匹配的股票池集中管理，供 optimize_params.py 和
scheduler_jobs.py 共享，避免两处重复定义导致不一致。

第一性原理：不同策略的 alpha 来源不同，必须用匹配其假设的股票来优化参数。
例如：用茅台优化涨停策略会过拟合到低换手大盘股，用游资股优化价值投资策略会失真。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from gugu.config import PROJECT_ROOT
from gugu.utils.log import get_logger

logger = get_logger()

# 默认代表性股票池（50只，覆盖多个行业，解决单股过拟合问题）
# 选股标准：各行业龙头 + 不同市值 + 不同估值水平
DEFAULT_REPRESENTATIVE_STOCKS = [
    # 白酒/食品饮料
    "600519",
    "000858",
    "002304",
    "600887",
    "603288",
    # 医药生物
    "600276",
    "000538",
    "300760",
    "603259",
    "300015",
    # 银行
    "601398",
    "600036",
    "601288",
    "601328",
    "600016",
    # 保险/券商
    "601318",
    "601628",
    "600030",
    "000776",
    "601211",
    # 房地产/建筑
    "600048",
    "001979",
    "601668",
    "600585",
    # 家电
    "000333",
    "600690",
    "000651",
    "002508",
    # 汽车
    "600104",
    "002594",
    "000625",
    "601127",
    # 电子/半导体
    "002415",
    "603986",
    "688981",
    "002371",
    # 新能源
    "300750",
    "002459",
    "601012",
    "600438",
    # 互联网/科技
    "600570",
    "300033",
    "002230",
    "688111",
    # 消费/零售
    "600887",
    "002024",
    "601888",
    "000100",
]

# 按策略类型匹配的默认股票池
STRATEGY_STOCK_POOLS: dict[str, str] = {
    # 突破类：活跃中小盘（波动大、箱体明显、涨停活跃）
    "box_breakout": "603501,002371,002472,300033,002230",
    "dual_thrust": "603501,002371,002472,300033,002230",
    "chen_jiangting": "603501,002371,002472,300033,002230",
    # 趋势类：各行业趋势龙头（走势连贯、量价配合好）
    "turtle": "600519,300750,002594,300760,002371",
    "dual_ma": "600519,300750,002594,300760,002371",
    "macd": "600519,300750,002594,300760,002371",
    "ma20_trend": "600519,300750,002594,300760,002371",
    "ma5_momentum": "600519,300750,002594,002371",
    "cloud_trend": "600519,300750,002594,300760,002371",
    "money_flow_follow": "600519,300750,002594,300760,002371",
    "north_force_long": "600519,300750,002594,300760,002371",
    # 均值回归：高波动趋势股（有足够振幅做反弹）
    "bollinger": "300750,601012,600438,603259,002594",
    "rsi_reversal": "300750,601012,600438,603259,002594",
    "kdj": "300750,601012,600438,603259,002594",
    "pullback_low": "300750,601012,600438,603259,002594",
    "double_yin": "300750,601012,600438,603259,000858",
    # 游资/短线：高换手活跃股（涨停频繁、成交量大）
    "hot_money": "002472,300033,002230,603501",
    "first_board": "002472,300033,002230,603501",
    "dragon_tiger_short": "002472,300033,002230,603501",
    "breakout_rebound": "002472,300033,002230,603501",
    # 价值投资：大市值白马（PE/PB/ROE 稳定）
    "zhen_dahu": "600519,000858,600276,300760,601318",
    # ML/多因子：跨行业代表股（防止单行业偏差）
    "ml_gradient_boost": "600519,300750,002594,300760,002371",
    "ml_factor": "600519,300750,002594,300760,002371",
    # 博主规则：热点+白马组合
    "chenxuezhang": "300033,002230,300760,000858",
    "linjudaye": "600519,300760,002594,300750",
    # 资金流异动：高波动趋势股
    "capital_flow_alert": "300750,601012,600438,603259,002594",
    # 花木兰系列
    "huamulan_tail_pick": "002472,300033,002230,603501",
    "huamulan_crash_layout": "600519,000858,600276,300760,601318",
    "huamulan_fake_dip": "300750,601012,600438,603259,002594",
    "huamulan_ghost_breakout": "002472,300033,002230,603501",
    # 周线：多行业趋势股
    "weekly_combo": "600519,300750,002594,300760,002371",
    # 默认兜底
    "_default": "600519,300750,002594,300760,002371",
}


def _load_defaults_yaml() -> dict[str, Any]:
    """加载 strategy_defaults.yaml（无缓存，便于运行时热更新）。"""
    path = Path(PROJECT_ROOT) / "config" / "strategy_defaults.yaml"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("读取 strategy_defaults.yaml 失败: {}", e)
        return {}


def get_strategy_stock_pool(strategy_name: str, defaults: dict[str, Any] | None = None) -> str:
    """获取策略的默认优化股票池。

    优先级：
    1. strategy_defaults.yaml 中该策略的 default_stock_pool 字段（用户可覆盖）
    2. STRATEGY_STOCK_POOLS 按策略类型默认匹配
    3. DEFAULT_REPRESENTATIVE_STOCKS（全市场代表，逗号拼接）
    """
    if defaults is None:
        defaults = _load_defaults_yaml()
    user_pool = defaults.get(strategy_name, {}).get("default_stock_pool")
    if user_pool and isinstance(user_pool, str) and user_pool.strip():
        return user_pool.strip()
    return STRATEGY_STOCK_POOLS.get(strategy_name, STRATEGY_STOCK_POOLS["_default"])
