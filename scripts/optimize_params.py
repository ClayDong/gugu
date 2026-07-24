"""Walk-Forward 参数优化脚本：对所有启用策略运行 Walk-Forward 优化，结果写回 strategy_defaults.yaml。

用法：
    # 显示所有启用策略的当前参数
    python scripts/optimize_params.py --show

    # 对所有启用策略运行 Walk-Forward 优化（基于 600519 基准数据）
    python scripts/optimize_params.py --symbol 600519 --days 500 --splits 3

    # 对指定策略运行优化
    python scripts/optimize_params.py --strategy turtle --symbol 600519

    # 优化并写回 strategy_defaults.yaml（必须通过 4 项解锁条件）
    python scripts/optimize_params.py --symbol 600519 --days 500 --write

    # 强制写回（跳过解锁条件，仅紧急情况使用）
    python scripts/optimize_params.py --symbol 600519 --days 500 --write --force

P1 修复：参数写回解锁条件（防止过拟合参数污染生产）
基于第一性原理：参数验证的本质是"证明参数不会让用户亏钱"，
而非"找到最优参数"。Walk-Forward 训练 Sharpe 5.01 vs 测试 Sharpe -6.28
明显过拟合，必须建立门控机制。

4 项解锁条件（全部满足才能写回）：
1. 样本外 Sharpe > 0（不过拟合基础条件）
2. 参数稳定性：同一参数在 ≥50% 窗口被选中
3. 最大回撤可控：avg_max_dd < 25%
4. 收益非负：avg_return ≥ 0（允许持平但不允许系统性亏损）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest.engine import walk_forward_optimize, walk_forward_optimize_multi_stock  # noqa: E402
from gugu.config import PROJECT_ROOT  # noqa: E402
from gugu.data import data_manager  # noqa: E402
from gugu.strategies.registry import get_strategy, list_strategies  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()

# P1-2 新增：默认代表性股票池（50只，覆盖多个行业，解决单股过拟合问题）
# 选股标准：各行业龙头 + 不同市值 + 不同估值水平
DEFAULT_REPRESENTATIVE_STOCKS = [
    # 白酒/食品饮料
    "600519", "000858", "002304", "600887", "603288",
    # 医药生物
    "600276", "000538", "300760", "603259", "300015",
    # 银行
    "601398", "600036", "601288", "601328", "600016",
    # 保险/券商
    "601318", "601628", "600030", "000776", "601211",
    # 房地产/建筑
    "600048", "001979", "601668", "600585",
    # 家电
    "000333", "600690", "000651", "002508",
    # 汽车
    "600104", "002594", "000625", "601127",
    # 电子/半导体
    "002415", "603986", "688981", "002371",
    # 新能源
    "300750", "002459", "601012", "600438",
    # 互联网/科技
    "600570", "300033", "002230", "688111",
    # 消费/零售
    "600887", "002024", "601888", "000100",
]

# 参数网格定义：策略名 → 参数网格
# 基于 config/strategy_defaults.yaml 中的每个策略
STRATEGY_PARAM_GRIDS: dict[str, dict[str, list]] = {
    "turtle": {
        "breakout_window": [10, 15, 20, 25, 30],
        "exit_window": [5, 10, 15, 20],
        "atr_window": [10, 14, 20, 26],
        "risk_per_trade": [0.005, 0.01, 0.015, 0.02],
        "max_units": [2, 4, 6],
    },
    "bollinger": {
        "window": [10, 15, 20, 25, 30],
        "num_std": [1.5, 2.0, 2.5, 3.0],
        "stop_loss_atr": [1.0, 1.5, 2.0, 2.5, 3.0],
    },
    "dual_ma": {
        "short_window": [3, 5, 8, 10, 13],
        "long_window": [10, 15, 20, 26, 30, 50],
    },
    "box_breakout": {
        "box_window": [10, 15, 20, 25, 30],
        "volume_ratio": [1.2, 1.5, 1.8, 2.0],
    },
    "rsi_reversal": {
        "window": [9, 14, 20],
        "oversold": [20, 25, 30, 35],
        "overbought": [65, 70, 75, 80],
    },
    "macd": {
        "short_window": [8, 12, 16],
        "long_window": [20, 26, 30],
        "signal_window": [5, 9, 12],
    },
    "kdj": {
        "window": [5, 9, 14],
        "oversold": [15, 20, 25, 30],
        "overbought": [70, 75, 80, 85],
    },
    "dual_thrust": {
        "lookback": [3, 5, 8, 10],
        "k_up": [0.3, 0.5, 0.7, 0.9],
        "k_down": [0.3, 0.5, 0.7, 0.9],
    },
    # P0 补全：两个新策略的参数网格（上轮未注册，参数永远无法被历史数据验证）
    "money_flow_follow": {
        "new_high_window": [10, 15, 20, 30],
        "new_high_lookback": [60, 90, 120, 180],
        "pullback_max_pct": [0.03, 0.05, 0.08, 0.10],
        "volume_expand_ratio": [1.2, 1.5, 1.8, 2.0],
        "volume_shrink_days": [1, 2, 3],
        "recent_gain_threshold": [0.02, 0.03, 0.05],
        "abnormal_volume_ratio": [2.0, 2.5, 3.0, 4.0],
    },
    "chen_jiangting": {
        "resistance_window": [10, 15, 20, 30],
        "volume_ratio": [1.2, 1.5, 1.8, 2.0],
        "vol_ma_window": [10, 20, 30],
        "ma_short": [10, 15, 20, 25],
        "ma_long": [40, 50, 60, 90],
        "max_daily_gain_pct": [0.05, 0.07, 0.09],
    },
    "double_yin": {
        "ma_slope_window": [3, 5, 8],
        "first_yin_vol_ratio_max": [0.8, 0.9, 1.0],
        "second_yin_vol_ratio_max": [0.5, 0.6, 0.7],
        "doji_body_max": [0.008, 0.01, 0.015],
        "small_yang_max_gain": [0.02, 0.03, 0.04],
        "breakout_vol_ratio_min": [1.5, 2.0, 2.5],
        "take_profit_pct": [0.06, 0.08, 0.10],
        "stagnation_vol_ratio": [2.0, 2.5, 3.0],
    },
    # v0.6.2 新增：主力资金跟随策略参数网格（已缩小至核心维度，避免组合爆炸）
    "north_force_long": {
        "ma_trend_window": [20, 30],
        "ma_long_window": [60, 90],
        "pullback_to_ma_pct": [0.02, 0.03, 0.05],
        "max_daily_gain_pct": [0.04, 0.07],
        "max_vol_ratio": [2.0, 2.5, 3.0],
        "stop_loss_pct": [0.05, 0.07],
        "take_profit_pct": [0.10, 0.20],
        "min_confidence": [0.40, 0.50, 0.60],
    },
    "dragon_tiger_short": {
        # v0.6.2 调优：网格覆盖默认值并放宽，解决 trades=0 问题
        "limit_up_pct": [0.07, 0.085, 0.10],  # 默认 0.07，覆盖 7%/8.5%/10%
        "limit_up_lookback": [5, 10, 15],  # 默认 10，覆盖 5/10/15 日
        "max_break_days": [3, 5, 7],  # 默认 5，覆盖 3/5/7 日反包窗口
        "stop_loss_pct": [0.05, 0.07],
        "take_profit_pct": [0.10, 0.15],
        "max_vol_ratio": [2.5, 3.5, 5.0],
        "min_confidence": [0.40, 0.50],
    },
    # v0.6.2 新增：云策略 + 游资策略参数网格
    "cloud_trend": {
        "ma_short": [8, 10, 12],
        "ma_trend": [15, 20, 25],
        "hh_ll_window": [15, 20, 25],
        "breakout_vol_ratio": [1.3, 1.5, 2.0],
        "trailing_stop_pct": [0.03, 0.05, 0.07],
        "time_stop_days": [10, 15, 20],
    },
    "hot_money": {
        "limit_up_window": [5, 7, 10],
        "shrink_ratio": [0.3, 0.4, 0.5, 0.6],
        "expand_ratio": [1.5, 2.0, 2.5, 3.0],
        "max_track_days": [15, 20, 30, 45],
        "hard_stop_loss_pct": [0.03, 0.05, 0.07],
        "trailing_stop_pct": [0.05, 0.07, 0.10],
    },
}

# 策略 → 其策略类需要哪些参数（pyproject.toml 中定义的策略类）
# 使用 strategy_defaults.yaml 中的 key 名
STRATEGY_CONFIG_KEYS: dict[str, list[str]] = {
    "turtle": ["breakout_window", "exit_window", "atr_window", "risk_per_trade", "max_units"],
    "bollinger": ["window", "num_std", "stop_loss_atr"],
    "dual_ma": ["short_window", "long_window"],
    "box_breakout": ["box_window", "volume_confirm", "volume_ratio"],
    "rsi_reversal": ["window", "oversold", "overbought"],
    "macd": ["short_window", "long_window", "signal_window"],
    "kdj": ["window", "oversold", "overbought"],
    "dual_thrust": ["lookback", "k_up", "k_down"],
    # P0 补全：与新策略网格同步
    "money_flow_follow": [
        "new_high_window",
        "new_high_lookback",
        "pullback_max_pct",
        "volume_expand_ratio",
        "volume_shrink_days",
        "recent_gain_threshold",
        "abnormal_volume_ratio",
    ],
    "chen_jiangting": [
        "resistance_window",
        "volume_ratio",
        "vol_ma_window",
        "ma_short",
        "ma_long",
        "max_daily_gain_pct",
    ],
    "double_yin": [
        "ma_slope_window",
        "first_yin_vol_ratio_max",
        "second_yin_vol_ratio_max",
        "doji_body_max",
        "small_yang_max_gain",
        "breakout_vol_ratio_min",
        "take_profit_pct",
        "stagnation_vol_ratio",
    ],
    # v0.6.2 新增：主力资金跟随策略配置键
    "north_force_long": [
        "ma_trend_window",
        "ma_long_window",
        "ma_slope_window",
        "pullback_to_ma_pct",
        "recent_momentum_window",
        "recent_momentum_pct",
        "recent_spike_window",
        "recent_spike_pct",
        "max_daily_gain_pct",
        "max_vol_ratio",
        "vol_ma_window",
        "stop_loss_pct",
        "take_profit_pct",
        "stagnation_vol_ratio",
        "max_upper_shadow_pct",
        "time_stop_window",
        "time_stop_threshold",
        "min_confidence",
    ],
    "dragon_tiger_short": [
        "ma_trend_window",
        "ma_slope_window",
        "limit_up_pct",
        "limit_up_lookback",
        "max_break_days",
        "stop_loss_pct",
        "take_profit_pct",
        "stagnation_vol_ratio",
        "max_upper_shadow_pct",
        "time_stop_window",
        "time_stop_threshold",
        "max_vol_ratio",
        "vol_ma_window",
        "min_confidence",
    ],
    # v0.6.2 新增：云策略 + 游资策略配置键
    "cloud_trend": [
        "ma_short",
        "ma_trend",
        "hh_ll_window",
        "hh_ll_compare",
        "vol_compare_window",
        "breakout_vol_ratio",
        "pullback_tolerance",
        "doji_body_max",
        "small_yang_max_gain",
        "stop_loss_pct",
        "max_hold_days",
        "trailing_stop_pct",
        "time_stop_days",
    ],
    "hot_money": [
        "limit_up_window",
        "limit_up_threshold",
        "big_yang_threshold",
        "shrink_ratio",
        "expand_ratio",
        "vol_ma_window",
        "min_volume",
        "max_hold_days",
        "max_track_days",
        "hard_stop_loss_pct",
        "trailing_stop_pct",
    ],
}


# P1 修复：参数约束函数字典
# 第一性原理：参数语义要求某些参数之间有大小关系（如 dual_ma 的 short < long），
# 否则会产出语义错误的"金叉/死叉"（短均线比长均线还长，失去均线交叉的意义）
# 对抗性审查：在策略类 __init__ 中校验更彻底，但改动基类影响范围大；
# 在 grid search 中过滤是最小侵入方案，仅优化路径生效，运行时仍依赖策略自身逻辑
def _dual_ma_constraints(p: dict) -> bool:
    """dual_ma: 短均线必须严格小于长均线。"""
    sw = p.get("short_window", 0)
    lw = p.get("long_window", 0)
    return sw < lw


def _macd_constraints(p: dict) -> bool:
    """macd: 短均线必须严格小于长均线；信号线必须短于短均线。"""
    sw = p.get("short_window", 0)
    lw = p.get("long_window", 0)
    sigw = p.get("signal_window", 0)
    return sw < lw and sigw < sw


def _turtle_constraints(p: dict) -> bool:
    """turtle: 突破窗口必须严格大于出场窗口（出场信号应早于突破信号触发）。"""
    bw = p.get("breakout_window", 0)
    ew = p.get("exit_window", 0)
    return bw > ew


def _rsi_reversal_constraints(p: dict) -> bool:
    """rsi_reversal: 超卖必须严格小于超买。"""
    return p.get("oversold", 0) < p.get("overbought", 0)


def _kdj_constraints(p: dict) -> bool:
    """kdj: 超卖必须严格小于超买。"""
    return p.get("oversold", 0) < p.get("overbought", 0)


def _chen_jiangting_constraints(p: dict) -> bool:
    """chen_jiangting: 短均线必须严格小于长均线。"""
    return p.get("ma_short", 0) < p.get("ma_long", 0)


def _money_flow_follow_constraints(p: dict) -> bool:
    """money_flow_follow: 创高窗口必须小于回看窗口（创高是近期现象，回看是历史基线）。"""
    nw = p.get("new_high_window", 0)
    nl = p.get("new_high_lookback", 0)
    return nw < nl


def _double_yin_constraints(p: dict) -> bool:
    """double_yin: 第二根阴量应弱于第一根阴量，止盈和滞涨阈值必须为正。"""
    first = p.get("first_yin_vol_ratio_max", 0)
    second = p.get("second_yin_vol_ratio_max", 0)
    return (
        second <= first and p.get("take_profit_pct", 0) > 0 and p.get("stagnation_vol_ratio", 0) > 0
    )


def _north_force_long_constraints(p: dict) -> bool:
    """north_force_long: 趋势均线必须严格小于长期均线；止损必须小于止盈。"""
    return (
        p.get("ma_trend_window", 0) < p.get("ma_long_window", 0)
        and p.get("stop_loss_pct", 0) < p.get("take_profit_pct", 0)
        and p.get("recent_spike_window", 0) <= p.get("recent_momentum_window", 0)
    )


def _dragon_tiger_short_constraints(p: dict) -> bool:
    """dragon_tiger_short: 止损必须小于止盈；断板窗口不超过涨停回看窗口。"""
    return (
        p.get("stop_loss_pct", 0) < p.get("take_profit_pct", 0)
        and p.get("max_break_days", 0) <= p.get("limit_up_lookback", 0)
        and p.get("time_stop_threshold", 0) < p.get("take_profit_pct", 0)
    )


STRATEGY_PARAM_CONSTRAINTS: dict[str, "callable[[dict], bool]"] = {
    "dual_ma": _dual_ma_constraints,
    "macd": _macd_constraints,
    "turtle": _turtle_constraints,
    "rsi_reversal": _rsi_reversal_constraints,
    "kdj": _kdj_constraints,
    "chen_jiangting": _chen_jiangting_constraints,
    "money_flow_follow": _money_flow_follow_constraints,
    "double_yin": _double_yin_constraints,
    # v0.6.2 新增：主力资金跟随策略约束
    "north_force_long": _north_force_long_constraints,
    "dragon_tiger_short": _dragon_tiger_short_constraints,
}


def load_defaults() -> dict:
    """加载当前 strategy_defaults.yaml。"""
    path = PROJECT_ROOT / "config" / "strategy_defaults.yaml"
    if not path.exists():
        logger.warning(f"策略默认参数文件不存在: {path}")
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_defaults(defaults: dict) -> None:
    """写回 strategy_defaults.yaml，保留注释结构。"""
    path = PROJECT_ROOT / "config" / "strategy_defaults.yaml"
    # 读取现有文件，保留注释
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    # 构建新内容：用 YAML.dump 重新生成（简单策略），尽量保留注释
    if existing:
        # 按策略逐个替换值，保留文件结构
        lines = existing.split("\n")
        new_lines: list[str] = []
        current_strategy = ""
        current_keys: list[str] = []

        for line in lines:
            stripped = line.strip()
            # 检测策略段标题
            if stripped and not stripped.startswith("#"):
                for s in defaults:
                    if stripped == f"{s}:" or stripped.startswith(f"{s}:"):
                        current_strategy = s
                        current_keys = STRATEGY_CONFIG_KEYS.get(s, [])
                        new_lines.append(line)
                        break
                else:
                    # 检测参数行
                    if current_strategy and current_keys:
                        for key in current_keys:
                            if stripped.startswith(f"{key}:"):
                                param_defaults = defaults.get(current_strategy, {})
                                if key in param_defaults:
                                    new_val = param_defaults[key]
                                    indent = line[: len(line) - len(line.lstrip())]
                                    # 保留注释
                                    comment = ""
                                    if "#" in line:
                                        comment = "  #" + line.split("#", 1)[1]
                                    new_lines.append(f"{indent}{key}: {new_val}{comment}")
                                break
                        else:
                            new_lines.append(line)
                    else:
                        new_lines.append(line)
            else:
                new_lines.append(line)

        path.write_text("\n".join(new_lines), encoding="utf-8")
    else:
        with path.open("w", encoding="utf-8") as f:
            yaml.dump(defaults, f, default_flow_style=False, allow_unicode=True)

    logger.info(f"策略参数已写回: {path}")


def show_current_params() -> None:
    """显示当前策略默认参数。"""
    defaults = load_defaults()
    if not defaults:
        print("(空)")
        return
    print("=" * 60)
    print("当前策略默认参数 (config/strategy_defaults.yaml)")
    print("=" * 60)
    for strategy_name, params in defaults.items():
        if isinstance(params, dict):
            print(f"\n{strategy_name}:")
            for k, v in params.items():
                print(f"  {k}: {v}")


def build_param_grid(strategy_name: str, defaults: dict) -> dict[str, list]:
    """为指定策略构建参数网格。

    从预定义网格中选取参数，确保参数在 reasonable 范围内。
    """
    strat_defaults = defaults.get(strategy_name, {})
    grid = STRATEGY_PARAM_GRIDS.get(strategy_name, {})
    if not grid:
        logger.warning(f"策略 {strategy_name} 无预定义参数网格")
        return {}
    return grid


async def run_optimization(
    strategy_name: str,
    symbol: str,
    days: int,
    n_splits: int,
    write: bool,
    force: bool = False,
) -> dict | None:
    """对单个策略运行 Walk-Forward 优化。

    P1 修复：参数写回解锁条件门控
    - write=True 时必须通过 4 项解锁条件，否则拒绝写回
    - force=True 跳过解锁检查（仅紧急情况使用，需在 PR 评审中说明理由）
    """
    from gugu.strategies.registry import get_strategy_cls

    strategy_cls = get_strategy_cls(strategy_name)
    if strategy_cls is None:
        logger.error(f"策略类 {strategy_name} 未注册")
        return None

    dm = data_manager()
    df = await dm.fetch_stock_history(symbol, days=days)

    if df.empty:
        logger.error(f"无法获取 {symbol} 数据（days={days}）")
        return None

    if len(df) < 120:
        logger.error(f"数据不足（{len(df)}行），Walk-Forward 至少需要 120 行")
        return None

    defaults = load_defaults()
    param_grid = build_param_grid(strategy_name, defaults)
    if not param_grid:
        logger.warning(f"策略 {strategy_name} 无参数网格，跳过")
        return None

    logger.info(
        f"开始 Walk-Forward 优化: {strategy_name} @ {symbol} 数据={len(df)}行 窗口={n_splits}"
    )

    results = walk_forward_optimize(
        strategy_cls=strategy_cls,
        df=df,
        symbol=symbol,
        param_grid=param_grid,
        train_ratio=0.6,
        n_splits=n_splits,
        param_constraints=STRATEGY_PARAM_CONSTRAINTS.get(strategy_name),
    )

    if not results or "error" in results[0]:
        error_msg = results[0].get("error", "未知错误") if results else "无结果"
        logger.error(f"Walk-Forward 失败: {error_msg}")
        return None

    # 汇总各窗口最优参数
    param_votes: dict[str, dict] = {}
    for r in results:
        for param_name, param_val in r.get("best_params", {}).items():
            if param_name not in param_votes:
                param_votes[param_name] = {}
            key = str(param_val)
            param_votes[param_name][key] = param_votes[param_name].get(key, 0) + 1

    # 投票选出最优参数（多数窗口一致的参数）
    best_params: dict[str, float] = {}
    for param_name, votes in param_votes.items():
        best_val_str = max(votes, key=votes.get)
        best_val = float(best_val_str) if "." in best_val_str else int(best_val_str)
        best_params[param_name] = best_val

    # 计算平均测试 Sharpe
    avg_sharpe = sum(r.get("test_sharpe", 0) for r in results) / max(len(results), 1)
    avg_return = sum(r.get("test_return", 0) for r in results) / max(len(results), 1)
    avg_max_dd = sum(r.get("test_max_dd", 0) for r in results) / max(len(results), 1)
    avg_win_rate = sum(r.get("test_win_rate", 0) for r in results) / max(len(results), 1)

    print(f"\n{'=' * 60}")
    print(f"策略: {strategy_name}")
    print(f"{'=' * 60}")
    print(f"数据: {symbol}, {len(df)} 行, {n_splits} 窗口")
    print(f"平均测试 Sharpe:   {avg_sharpe:.4f}")
    print(f"平均样本外收益:    {avg_return:.2%}")
    print(f"平均最大回撤:      {avg_max_dd:.2%}")
    print(f"平均胜率:           {avg_win_rate:.2%}")
    print(f"\n最优参数（多窗口投票）:")
    for k, v in best_params.items():
        default_val = defaults.get(strategy_name, {}).get(k, "?")
        print(f"  {k}: {v} (原值: {default_val})")
    print(f"\n各窗口详情:")
    for r in results:
        print(
            f"  窗口{r.get('window', '?')}: "
            f"训练Sharpe={r.get('train_sharpe', 0):.4f}, "
            f"测试Sharpe={r.get('test_sharpe', 0):.4f}, "
            f"收益={r.get('test_return', 0):.2%}, "
            f"参数={r.get('best_params', {})}"
        )

    # P1 修复：参数写回解锁条件检查
    if write:
        unlock_report = _check_unlock_conditions(
            strategy_name=strategy_name,
            results=results,
            avg_sharpe=avg_sharpe,
            avg_return=avg_return,
            avg_max_dd=avg_max_dd,
            param_votes=param_votes,
        )

        if unlock_report["unlocked"] or force:
            if force and not unlock_report["unlocked"]:
                print(
                    f"\n⚠️  --force 强制写回（跳过 {len(unlock_report['failed_checks'])} 项解锁条件）"
                )
                logger.warning(
                    f"强制写回 {strategy_name} 参数，跳过解锁检查: {unlock_report['failed_checks']}"
                )
            # 更新 defaults 并写回
            if strategy_name not in defaults:
                defaults[strategy_name] = {}
            for k, v in best_params.items():
                defaults[strategy_name][k] = v
            write_defaults(defaults)
            print(f"\n优化参数已写回 config/strategy_defaults.yaml")
        else:
            print(
                f"\n❌ 参数写回被拒绝（未通过 {len(unlock_report['failed_checks'])} 项解锁条件）:"
            )
            for check in unlock_report["checks"]:
                status = "✓" if check["passed"] else "✗"
                print(f"  {status} {check['name']}: {check['detail']}")
            print(f"\n建议:")
            print(f"  1. 扩大样本（当前 {symbol} 单股优化易过拟合，建议多股聚合）")
            print(f"  2. 增加数据量（--days 1000+）")
            print(f"  3. 紧急情况可用 --force 强制写回（需 PR 评审说明理由）")

        # 闭环修复：输出优化报告 + 更新 manifest validation 字段
        report_path = write_optimization_report(
            strategy_name=strategy_name,
            unlock_report=unlock_report,
            avg_sharpe=avg_sharpe,
            avg_return=avg_return,
            avg_max_dd=avg_max_dd,
            param_votes=param_votes,
            best_params=best_params,
            n_windows=len(results),
        )
        print(f"\n📋 优化报告: {report_path}")
        failed_reason = (
            "; ".join(unlock_report["failed_checks"]) if unlock_report["failed_checks"] else ""
        )
        apply_optimization_result(
            strategy_name=strategy_name,
            passed=unlock_report["unlocked"],
            reason=failed_reason,
        )

    return {
        "strategy": strategy_name,
        "best_params": best_params,
        "avg_sharpe": avg_sharpe,
        "avg_return": avg_return,
        "avg_max_dd": avg_max_dd,
        "avg_win_rate": avg_win_rate,
        "n_splits": n_splits,
        "results": results,
    }


def _check_unlock_conditions(
    strategy_name: str,
    results: list[dict],
    avg_sharpe: float,
    avg_return: float,
    avg_max_dd: float,
    param_votes: dict[str, dict],
) -> dict:
    """P1 修复：4 项参数写回解锁条件检查。

    基于对抗性审查，每项条件都对应一类典型过拟合陷阱：
    1. 样本外 Sharpe > 0 — 防止"训练神勇、测试亏损"的过拟合
    2. 参数稳定性 — 防止"每个窗口最优参数都不同"的偶然性
    3. 最大回撤可控 — 防止"高收益但破产风险"的赌徒参数
    4. 收益非负 — 防止"系统性亏损参数"被写回

    Returns:
        {
            "unlocked": bool,        # 是否全部通过
            "checks": [...],         # 4 项检查详情
            "failed_checks": [str],  # 失败的检查名称
        }
    """
    checks: list[dict] = []
    failed: list[str] = []

    # 检查1：样本外 Sharpe > 0
    sharpe_passed = avg_sharpe > 0
    checks.append(
        {
            "name": "样本外 Sharpe > 0",
            "passed": sharpe_passed,
            "detail": f"avg_test_sharpe={avg_sharpe:.4f} ({'通过' if sharpe_passed else '失败'})",
        }
    )
    if not sharpe_passed:
        failed.append("样本外 Sharpe ≤ 0")

    # 检查2：参数稳定性 — 同一参数在 ≥50% 窗口被选中
    stability_passed = True
    stability_details: list[str] = []
    n_windows = len(results)
    for param_name, votes in param_votes.items():
        if not votes:
            continue
        max_votes = max(votes.values())
        stability_ratio = max_votes / n_windows if n_windows > 0 else 0
        if stability_ratio < 0.5:
            stability_passed = False
            stability_details.append(f"{param_name} 一致性 {stability_ratio:.0%}")
    checks.append(
        {
            "name": "参数稳定性 ≥50%",
            "passed": stability_passed,
            "detail": (
                f"全部参数一致性 ≥50% ({'通过' if stability_passed else '失败: ' + ', '.join(stability_details)})"
            ),
        }
    )
    if not stability_passed:
        failed.append("参数稳定性 <50%")

    # 检查3：最大回撤可控（avg_max_dd < 25%）
    dd_threshold = 0.25
    dd_passed = avg_max_dd < dd_threshold
    checks.append(
        {
            "name": f"最大回撤 < {dd_threshold:.0%}",
            "passed": dd_passed,
            "detail": f"avg_max_dd={avg_max_dd:.2%} ({'通过' if dd_passed else '失败'})",
        }
    )
    if not dd_passed:
        failed.append(f"最大回撤 ≥ {dd_threshold:.0%}")

    # 检查4：收益非负
    return_passed = avg_return >= 0
    checks.append(
        {
            "name": "平均样本外收益 ≥ 0",
            "passed": return_passed,
            "detail": f"avg_return={avg_return:.2%} ({'通过' if return_passed else '失败'})",
        }
    )
    if not return_passed:
        failed.append("平均样本外收益 < 0")

    return {
        "unlocked": len(failed) == 0,
        "checks": checks,
        "failed_checks": failed,
    }


def write_optimization_report(
    strategy_name: str,
    unlock_report: dict,
    avg_sharpe: float,
    avg_return: float,
    avg_max_dd: float,
    param_votes: dict[str, dict],
    best_params: dict,
    n_windows: int,
) -> Path:
    """输出优化结果到 data/optimization_report_{strategy}_{date}.json。

    闭环修复：优化结果不再仅打印到控制台，同时落盘为结构化 JSON，
    供后续审计/对比/仪表盘消费。包含：
    - 策略名、4 项解锁条件结果
    - 样本外 Sharpe、参数稳定性
    - 是否通过、建议动作
    """
    from gugu.utils.atomic import atomic_write_json

    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    date_display = now.strftime("%Y-%m-%d")

    # 计算参数稳定性（每个参数在多窗口中被选中的最高一致性比例）
    param_stability: dict[str, dict] = {}
    for param_name, votes in param_votes.items():
        if votes and n_windows > 0:
            max_votes = max(votes.values())
            param_stability[param_name] = {
                "stability_ratio": round(max_votes / n_windows, 4),
                "votes": votes,
            }

    passed = unlock_report["unlocked"]

    # 建议动作：通过 → 重跑全市场回测；未通过 → 扩大样本/数据量
    if passed:
        recommended_action = "参数已通过 Walk-Forward 验证，建议重跑全市场回测验证新参数"
    else:
        recommended_action = (
            f"未通过 {len(unlock_report['failed_checks'])} 项解锁条件"
            f"（{'; '.join(unlock_report['failed_checks'])}），"
            "建议扩大样本/增加数据量，或紧急情况用 --force 强制写回（需 PR 评审说明理由）"
        )

    report = {
        "strategy": strategy_name,
        "date": date_display,
        "passed": passed,
        "unlock_conditions": unlock_report["checks"],
        "oos_sharpe": round(avg_sharpe, 4),
        "avg_return": round(avg_return, 4),
        "avg_max_dd": round(avg_max_dd, 4),
        "param_stability": param_stability,
        "best_params": best_params,
        "recommended_action": recommended_action,
    }

    output_path = PROJECT_ROOT / "data" / f"optimization_report_{strategy_name}_{date_str}.json"
    atomic_write_json(output_path, report, ensure_ascii=False, indent=2)
    logger.info(f"优化报告已写入: {output_path}")
    return output_path


def apply_optimization_result(strategy_name: str, passed: bool, reason: str) -> None:
    """更新 strategy_manifest.yaml 的 validation 字段。

    闭环修复：优化结果不再仅写 strategy_defaults.yaml，同步更新 manifest
    的 validation 字段，使策略注册中心感知到最新的 Walk-Forward 状态。

    - 通过：标记 "Walk-Forward 通过 {date}"
    - 未通过：标记 "Walk-Forward 未通过 {date} - {reason}"
    - 不自动禁用策略（避免误伤），仅标记状态供人工决策
    """
    from gugu.utils.atomic import atomic_write_text

    date_str = datetime.now().strftime("%Y-%m-%d")
    if passed:
        new_validation = f"Walk-Forward 通过 {date_str}"
    else:
        new_validation = f"Walk-Forward 未通过 {date_str} - {reason}"

    path = PROJECT_ROOT / "config" / "strategy_manifest.yaml"
    if not path.exists():
        logger.warning(f"策略清单文件不存在: {path}，跳过 validation 更新")
        return

    content = path.read_text(encoding="utf-8")
    new_content, updated = _replace_manifest_validation(content, strategy_name, new_validation)

    if not updated:
        logger.warning(f"策略 {strategy_name} 未在 manifest 中找到，跳过 validation 更新")
        return

    # 原子写入（同目录 tmp → fsync → os.replace），防止崩溃导致文件截断
    atomic_write_text(path, new_content)
    logger.info(f"策略 {strategy_name} validation 已更新: {new_validation}")


def _replace_manifest_validation(
    content: str,
    strategy_name: str,
    new_validation: str,
) -> tuple[str, bool]:
    """行级替换 manifest 中的 validation 字段，保留注释结构。

    manifest 结构：signal_strategies: → {strategy_name}: → validation: value
    策略键位于 2 空格缩进，validation 字段位于 4 空格缩进。

    Returns:
        (new_content, updated) — updated=False 表示未找到目标策略的 validation 行
    """
    lines = content.split("\n")
    new_lines: list[str] = []
    current_strategy: str | None = None
    updated = False

    for line in lines:
        stripped = line.strip()

        if stripped and not stripped.startswith("#"):
            indent_len = len(line) - len(line.lstrip())

            # 策略键位于 2 空格缩进（如 "  turtle:"）
            if indent_len == 2:
                if stripped == f"{strategy_name}:":
                    current_strategy = strategy_name
                else:
                    current_strategy = None
            elif indent_len == 0:
                # 新分类（signal_strategies: / risk_strategies: 等）
                current_strategy = None

        # 在目标策略段内匹配 validation 行
        if current_strategy == strategy_name and not updated and stripped.startswith("validation:"):
            indent = line[: len(line) - len(line.lstrip())]
            # 保留行尾注释
            comment = ""
            if "#" in line:
                comment = "  #" + line.split("#", 1)[1]
            new_lines.append(f"{indent}validation: {new_validation}{comment}")
            updated = True
            current_strategy = None  # 已替换，避免重复匹配
            continue

        new_lines.append(line)

    return "\n".join(new_lines), updated


async def run_multi_stock_optimization(
    strategy_name: str,
    symbols: list[str],
    days: int,
    n_splits: int,
    write: bool,
    force: bool = False,
) -> dict | None:
    """多股聚合 Walk-Forward 优化（解决单股过拟合问题）。

    第一性原理：单股在 600519 上训练 Sharpe 5.01 vs 测试 -6.28 = 过拟合
    修复：用 5-10 只不同行业代表股聚合训练，找跨股票稳健参数

    Args:
        strategy_name: 策略名
        symbols: 股票代码列表（建议 5-10 只）
        days: 每只股票的历史数据天数
        n_splits: Walk-Forward 窗口数
        write: 是否写回参数（需通过 4 项解锁条件）
        force: 是否强制写回
    """
    from gugu.strategies.registry import get_strategy_cls

    strategy_cls = get_strategy_cls(strategy_name)
    if strategy_cls is None:
        logger.error(f"策略类 {strategy_name} 未注册")
        return None

    dm = data_manager()
    print(f"\n并发批量获取 {len(symbols)} 只股票历史数据...")
    stock_dfs = await dm.fetch_stock_history_batch(symbols, days=days)
    # 过滤空数据
    stock_dfs = {s: df for s, df in stock_dfs.items() if not df.empty and len(df) >= 120}
    if len(stock_dfs) < 3:
        logger.error(f"有效股票数 {len(stock_dfs)} < 3，多股聚合无意义")
        return None

    print(f"成功获取 {len(stock_dfs)}/{len(symbols)} 只（≥120 行）")

    defaults = load_defaults()
    param_grid = build_param_grid(strategy_name, defaults)
    if not param_grid:
        logger.warning(f"策略 {strategy_name} 无参数网格，跳过")
        return None

    print(f"\n开始多股聚合 Walk-Forward 优化: {strategy_name}")
    print(f"  股票数: {len(stock_dfs)}")
    constraint_fn = STRATEGY_PARAM_CONSTRAINTS.get(strategy_name)
    # 统计合法参数组合数（约束过滤后）
    all_combos = list(__import__("itertools").product(*param_grid.values()))
    keys = list(param_grid.keys())
    legal_combos = [
        c
        for c in all_combos
        if constraint_fn is None or constraint_fn(dict(zip(keys, c, strict=True)))
    ]
    print(
        f"  参数组合数: {len(all_combos)} 总 / {len(legal_combos)} 合法（约束过滤 {len(all_combos) - len(legal_combos)} 个）"
    )
    print(f"  预计耗时: ~{len(stock_dfs) * len(legal_combos) * n_splits * 0.1:.0f}s")

    results = walk_forward_optimize_multi_stock(
        strategy_cls=strategy_cls,
        stock_dfs=stock_dfs,
        param_grid=param_grid,
        train_ratio=0.6,
        n_splits=n_splits,
        param_constraints=constraint_fn,
    )

    if not results or "error" in results[0]:
        error_msg = results[0].get("error", "未知错误") if results else "无结果"
        logger.error(f"多股 Walk-Forward 失败: {error_msg}")
        return None

    # 投票选出最优参数
    param_votes: dict[str, dict] = {}
    for r in results:
        for param_name, param_val in r.get("best_params", {}).items():
            if param_name not in param_votes:
                param_votes[param_name] = {}
            key = str(param_val)
            param_votes[param_name][key] = param_votes[param_name].get(key, 0) + 1

    best_params: dict[str, float] = {}
    for param_name, votes in param_votes.items():
        best_val_str = max(votes, key=votes.get)
        best_val = float(best_val_str) if "." in best_val_str else int(best_val_str)
        best_params[param_name] = best_val

    # 聚合统计
    avg_sharpe = sum(r.get("test_avg_sharpe", 0) for r in results) / max(len(results), 1)
    avg_return = sum(r.get("test_avg_return", 0) for r in results) / max(len(results), 1)
    avg_max_dd = sum(r.get("test_avg_max_dd", 0) for r in results) / max(len(results), 1)
    avg_win_rate = sum(r.get("test_avg_win_rate", 0) for r in results) / max(len(results), 1)

    print(f"\n{'=' * 70}")
    print(f"策略: {strategy_name} (多股聚合)")
    print(f"{'=' * 70}")
    print(f"股票: {list(stock_dfs.keys())}")
    print(f"窗口: {n_splits}, 训练比例: 60%")
    print(f"平均训练 Sharpe:   {results[0].get('train_avg_sharpe', 0):.4f}")
    print(f"平均测试 Sharpe:   {avg_sharpe:.4f}")
    print(f"平均样本外收益:    {avg_return:.2%}")
    print(f"平均最大回撤:      {avg_max_dd:.2%}")
    print(f"平均胜率:           {avg_win_rate:.2%}")
    print(f"\n最优参数（多窗口投票）:")
    for k, v in best_params.items():
        default_val = defaults.get(strategy_name, {}).get(k, "?")
        print(f"  {k}: {v} (原值: {default_val})")
    print(f"\n各窗口聚合详情:")
    for r in results:
        print(
            f"  窗口{r.get('window', '?')}: "
            f"训练avg_sharpe={r.get('train_avg_sharpe', 0):.4f}, "
            f"测试avg_sharpe={r.get('test_avg_sharpe', 0):.4f}, "
            f"收益={r.get('test_avg_return', 0):.2%}, "
            f"参数={r.get('best_params', {})}"
        )
        # 显示各股明细
        for d in r.get("test_details", [])[:3]:  # 只显示前3只
            print(
                f"    {d['symbol']}: sharpe={d['test_sharpe']:.4f} ret={d['test_return']:.2%} dd={d['test_max_dd']:.2%} trades={d['trades']}"
            )

    if write:
        unlock_report = _check_unlock_conditions(
            strategy_name=strategy_name,
            results=results,
            avg_sharpe=avg_sharpe,
            avg_return=avg_return,
            avg_max_dd=avg_max_dd,
            param_votes=param_votes,
        )

        if unlock_report["unlocked"] or force:
            if force and not unlock_report["unlocked"]:
                print(
                    f"\n⚠️  --force 强制写回（跳过 {len(unlock_report['failed_checks'])} 项解锁条件）"
                )
                logger.warning(f"强制写回 {strategy_name} 参数: {unlock_report['failed_checks']}")
            if strategy_name not in defaults:
                defaults[strategy_name] = {}
            for k, v in best_params.items():
                defaults[strategy_name][k] = v
            write_defaults(defaults)
            print(f"\n优化参数已写回 config/strategy_defaults.yaml")
        else:
            print(
                f"\n❌ 参数写回被拒绝（未通过 {len(unlock_report['failed_checks'])} 项解锁条件）:"
            )
            for check in unlock_report["checks"]:
                status = "✓" if check["passed"] else "✗"
                print(f"  {status} {check['name']}: {check['detail']}")
            print(f"\n建议:")
            print(f"  1. 扩大样本（当前 {len(stock_dfs)} 只，建议 10+ 只不同行业代表股）")
            print(f"  2. 增加数据量（--days 1000+）")
            print(f"  3. 紧急情况可用 --force 强制写回（需 PR 评审说明理由）")

        # 闭环修复：输出优化报告 + 更新 manifest validation 字段
        report_path = write_optimization_report(
            strategy_name=strategy_name,
            unlock_report=unlock_report,
            avg_sharpe=avg_sharpe,
            avg_return=avg_return,
            avg_max_dd=avg_max_dd,
            param_votes=param_votes,
            best_params=best_params,
            n_windows=len(results),
        )
        print(f"\n📋 优化报告: {report_path}")
        failed_reason = (
            "; ".join(unlock_report["failed_checks"]) if unlock_report["failed_checks"] else ""
        )
        apply_optimization_result(
            strategy_name=strategy_name,
            passed=unlock_report["unlocked"],
            reason=failed_reason,
        )

    return {
        "strategy": strategy_name,
        "best_params": best_params,
        "avg_sharpe": avg_sharpe,
        "avg_return": avg_return,
        "avg_max_dd": avg_max_dd,
        "avg_win_rate": avg_win_rate,
        "n_splits": n_splits,
        "n_stocks": len(stock_dfs),
        "results": results,
    }


async def main(
    strategy_name: str | None,
    symbol: str,
    days: int,
    n_splits: int,
    write: bool,
    show: bool,
    force: bool,
    multi_stock: str | None,
    use_default_pool: bool = False,
) -> None:
    """主入口。"""
    if show:
        show_current_params()
        return

    # P1-2 新增：使用默认50只代表性股票池
    if use_default_pool and not multi_stock:
        symbols = DEFAULT_REPRESENTATIVE_STOCKS
        print(f"\n使用默认代表性股票池（{len(symbols)} 只）进行多股聚合优化")
        print(f"覆盖行业：白酒/医药/银行/保险/券商/房地产/家电/汽车/电子/新能源/互联网/消费")
        print(f"选股标准：各行业龙头 + 不同市值 + 不同估值水平\n")

        if strategy_name:
            await run_multi_stock_optimization(strategy_name, symbols, days, n_splits, write, force)
        else:
            from gugu.config import get_config

            cfg = get_config()
            enabled = cfg.strategy.enabled
            if not enabled:
                logger.error("无启用策略")
                return
            print(f"将对 {len(enabled)} 个策略运行多股聚合 Walk-Forward 优化")
            print(f"股票池: {symbols}")
            if write:
                if force:
                    print("⚠️  --force 强制写回（跳过解锁条件检查）")
                else:
                    print("写回前将执行 4 项解锁条件检查")
            print()
            for s_name in enabled:
                try:
                    await run_multi_stock_optimization(s_name, symbols, days, n_splits, write, force)
                    print()
                except Exception as e:
                    logger.error(f"策略 {s_name} 多股优化失败: {e}")
                    continue
        return

    # P2 新增：多股聚合模式（解决单股过拟合）
    if multi_stock:
        # 解析股票列表（逗号分隔）
        symbols = [s.strip().zfill(6) for s in multi_stock.split(",") if s.strip()]
        if len(symbols) < 3:
            logger.error("--multi-stock 至少需要 3 只股票，用逗号分隔，如 600519,000858,002714")
            return

        if strategy_name:
            await run_multi_stock_optimization(strategy_name, symbols, days, n_splits, write, force)
        else:
            from gugu.config import get_config

            cfg = get_config()
            enabled = cfg.strategy.enabled
            if not enabled:
                logger.error("无启用策略")
                return
            print(f"\n将对 {len(enabled)} 个策略运行多股聚合 Walk-Forward 优化")
            print(f"股票池: {symbols}")
            if write:
                if force:
                    print("⚠️  --force 强制写回（跳过解锁条件检查）")
                else:
                    print("写回前将执行 4 项解锁条件检查")
            print()
            for s_name in enabled:
                try:
                    await run_multi_stock_optimization(
                        s_name, symbols, days, n_splits, write, force
                    )
                    print()
                except Exception as e:
                    logger.error(f"策略 {s_name} 多股优化失败: {e}")
                    continue
        return

    if strategy_name:
        await run_optimization(strategy_name, symbol, days, n_splits, write, force)
    else:
        # 对所有启用策略运行
        from gugu.config import get_config

        cfg = get_config()
        enabled = cfg.strategy.enabled

        if not enabled:
            logger.error("无启用策略")
            return

        print(f"\n将对以下 {len(enabled)} 个策略运行 Walk-Forward 优化: {enabled}")
        print(f"基准: {symbol}, 数据量: {days} 天, 窗口: {n_splits}")
        if write:
            if force:
                print("⚠️  --force 强制写回（跳过解锁条件检查）")
            else:
                print("写回前将执行 4 项解锁条件检查（Sharpe>0/参数稳定性/回撤可控/收益非负）")
        print()

        for s_name in enabled:
            try:
                await run_optimization(s_name, symbol, days, n_splits, write, force)
                print()
            except Exception as e:
                logger.error(f"策略 {s_name} 优化失败: {e}")
                continue


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-Forward 参数优化")
    parser.add_argument("--strategy", type=str, default=None, help="策略名称（默认=所有启用策略）")
    parser.add_argument(
        "--symbol", type=str, default="600519", help="股票代码（默认=600519 贵州茅台）"
    )
    parser.add_argument("--days", type=int, default=500, help="历史数据天数（默认=500）")
    parser.add_argument("--splits", type=int, default=3, help="Walk-Forward 窗口数（默认=3）")
    parser.add_argument(
        "--write",
        action="store_true",
        help="将最优参数写回 strategy_defaults.yaml（需通过解锁条件）",
    )
    parser.add_argument(
        "--force", action="store_true", help="强制写回，跳过解锁条件检查（紧急情况使用）"
    )
    parser.add_argument(
        "--multi-stock",
        type=str,
        default=None,
        help="多股聚合模式：逗号分隔的股票代码（如 600519,000858,002714），解决单股过拟合",
    )
    parser.add_argument(
        "--default-pool",
        action="store_true",
        help="P1-2 新增：使用默认50只代表性股票池进行多股聚合优化（覆盖多个行业，解决单股过拟合）",
    )
    parser.add_argument("--show", action="store_true", help="显示当前策略参数")
    parser.add_argument("--version", action="version", version="gugu 0.1.0")
    args = parser.parse_args()

    asyncio.run(
        main(
            args.strategy,
            args.symbol,
            args.days,
            args.splits,
            args.write,
            args.show,
            args.force,
            args.multi_stock,
            args.default_pool,
        )
    )
