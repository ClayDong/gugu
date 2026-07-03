"""Walk-Forward 参数优化脚本：对所有启用策略运行 Walk-Forward 优化，结果写回 strategy_defaults.yaml。

用法：
    # 显示所有启用策略的当前参数
    python scripts/optimize_params.py --show

    # 对所有启用策略运行 Walk-Forward 优化（基于 600519 基准数据）
    python scripts/optimize_params.py --symbol 600519 --days 500 --splits 3

    # 对指定策略运行优化
    python scripts/optimize_params.py --strategy turtle --symbol 600519

    # 优化并写回 strategy_defaults.yaml
    python scripts/optimize_params.py --symbol 600519 --days 500 --write
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from copy import deepcopy
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest.engine import walk_forward_optimize  # noqa: E402
from gugu.config import PROJECT_ROOT  # noqa: E402
from gugu.data import data_manager  # noqa: E402
from gugu.strategies.registry import get_strategy, list_strategies  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

logger = get_logger()

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
        "new_high_window", "new_high_lookback", "pullback_max_pct",
        "volume_expand_ratio", "volume_shrink_days", "recent_gain_threshold",
        "abnormal_volume_ratio",
    ],
    "chen_jiangting": [
        "resistance_window", "volume_ratio", "vol_ma_window",
        "ma_short", "ma_long", "max_daily_gain_pct",
    ],
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
) -> dict | None:
    """对单个策略运行 Walk-Forward 优化。"""
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
        f"开始 Walk-Forward 优化: {strategy_name} @ {symbol} "
        f"数据={len(df)}行 窗口={n_splits}"
    )

    results = walk_forward_optimize(
        strategy_cls=strategy_cls,
        df=df,
        symbol=symbol,
        param_grid=param_grid,
        train_ratio=0.6,
        n_splits=n_splits,
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

    if write:
        # 更新 defaults 并写回
        if strategy_name not in defaults:
            defaults[strategy_name] = {}
        for k, v in best_params.items():
            defaults[strategy_name][k] = v
        write_defaults(defaults)
        print(f"\n优化参数已写回 config/strategy_defaults.yaml")

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


async def main(
    strategy_name: str | None,
    symbol: str,
    days: int,
    n_splits: int,
    write: bool,
    show: bool,
) -> None:
    """主入口。"""
    if show:
        show_current_params()
        return

    if strategy_name:
        await run_optimization(strategy_name, symbol, days, n_splits, write)
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
            print("结果将写回 config/strategy_defaults.yaml")
        print()

        for s_name in enabled:
            try:
                await run_optimization(s_name, symbol, days, n_splits, write)
                print()
            except Exception as e:
                logger.error(f"策略 {s_name} 优化失败: {e}")
                continue


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-Forward 参数优化")
    parser.add_argument("--strategy", type=str, default=None, help="策略名称（默认=所有启用策略）")
    parser.add_argument("--symbol", type=str, default="600519", help="股票代码（默认=600519 贵州茅台）")
    parser.add_argument("--days", type=int, default=500, help="历史数据天数（默认=500）")
    parser.add_argument("--splits", type=int, default=3, help="Walk-Forward 窗口数（默认=3）")
    parser.add_argument("--write", action="store_true", help="将最优参数写回 strategy_defaults.yaml")
    parser.add_argument("--show", action="store_true", help="显示当前策略参数")
    parser.add_argument("--version", action="version", version="gugu 0.1.0")
    args = parser.parse_args()

    asyncio.run(main(args.strategy, args.symbol, args.days, args.splits, args.write, args.show))
