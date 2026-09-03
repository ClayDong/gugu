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

    # --force 仅保留命令兼容性，不得绕过生产参数写回门禁。
    python scripts/optimize_params.py --symbol 600519 --days 500 --write --force

P1 修复：参数写回解锁条件（防止过拟合参数污染生产）
基于第一性原理：参数验证的本质是"证明参数不会让用户亏钱"，
而非"找到最优参数"。Walk-Forward 训练 Sharpe 5.01 vs 测试 Sharpe -6.28
明显过拟合，必须建立门控机制。

5 项解锁条件（全部满足才能写回）：
1. 样本外 Sharpe > 0（不过拟合基础条件）
2. 参数稳定性：同一参数在 ≥80% 窗口被选中
3. 最大回撤可控：avg_max_dd < 25%
4. 收益非负：avg_return ≥ 0（允许持平但不允许系统性亏损）
5. 样本外交易次数 ≥ 30
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.analysis.optimization_pools import (
    DEFAULT_REPRESENTATIVE_STOCKS,
    get_strategy_stock_pool,
)  # noqa: E402
from gugu.backtest.engine import (
    walk_forward_optimize,
    walk_forward_optimize_multi_stock,
    BacktestEngine,
)  # noqa: E402
from gugu.config import PROJECT_ROOT  # noqa: E402
from gugu.data import data_manager  # noqa: E402
from gugu.research.ledger import append_record, build_record  # noqa: E402
from gugu.strategies.registry import get_strategy, list_strategies  # noqa: E402
from gugu.utils.atomic import atomic_write_text  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402
from gugu.validation.gate_policy import GatePolicy  # noqa: E402

logger = get_logger()


def _data_range(df: Any) -> dict[str, Any]:
    """Return a compact, serializable data-range summary for a research run."""
    if not hasattr(df, "columns") or "date" not in df.columns:
        return {"row_count": len(df) if hasattr(df, "__len__") else 0}
    dates = df["date"]
    return {
        "row_count": len(df),
        "data_start": str(dates.min()),
        "data_end": str(dates.max()),
    }


def _record_optimization_ledger(
    *,
    entrypoint: str,
    strategy_name: str,
    symbols: list[str],
    stock_dfs: dict[str, Any],
    requested_days: int,
    param_grid: dict[str, list],
    results: list[dict] | None,
    best_params: dict,
    write_requested: bool,
    writeback_applied: bool,
    unlock_report: dict | None,
    algorithm: str = "walk_forward",
    n_splits: int = 0,
    seed: int | None = None,
    population_size: int | None = None,
    generations: int | None = None,
    artifact_path: Path | None = None,
) -> None:
    """Record one top-level optimization run without affecting its outcome."""
    effective_symbols = list(stock_dfs)
    grid_size = 1
    for values in param_grid.values():
        grid_size *= max(1, len(values))
    record = build_record(
        kind="optimization",
        entrypoint=entrypoint,
        strategy=strategy_name,
        symbols=symbols,
        requested_days=requested_days,
        effective_symbols=effective_symbols,
        excluded_symbols=sorted(set(symbols) - set(effective_symbols)),
        data_ranges={symbol: _data_range(df) for symbol, df in stock_dfs.items()},
        algorithm=algorithm,
        seed=seed,
        population_size=population_size,
        generations=generations,
        train_ratio=0.6,
        n_splits=n_splits,
        parameter_grid=param_grid,
        parameter_grid_size=grid_size if param_grid else 0,
        best_params=best_params,
        results=results or [],
        gate_status=(unlock_report.get("version") if unlock_report else "not_evaluated"),
        gate=unlock_report,
        write_requested=write_requested,
        writeback_applied=writeback_applied,
        artifact_path=artifact_path,
    )
    append_record(record)


# 参数网格定义：策略名 → 参数网格
# 基于 config/strategy_defaults.yaml 中的每个策略
STRATEGY_PARAM_GRIDS: dict[str, dict[str, list]] = {
    "box_breakout": {
        "box_window": [10, 15, 20, 25, 30],
        "volume_ratio": [1.2, 1.5, 1.8, 2.0],
    },
    "rsi_reversal": {
        "window": [9, 14, 20],
        "oversold": [20, 25, 30, 35],
        "overbought": [65, 70, 75, 80],
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
        "support_tolerance": [0.03, 0.05, 0.08],  # 2026-09-02: 支撑接近度（原硬编码3%）
    },
    # 2026-09-01 新增：三个启用策略的参数网格
    "linjudaye": {
        "golden_level": [0.5, 0.618, 0.7, 0.8],
        "vol_shrink_ratio": [0.5, 0.7, 0.9],
        "box_range_threshold": [0.03, 0.05, 0.08],
        "low_vol_ratio": [0.3, 0.5, 0.7],
        "pullback_tolerance": [0.02, 0.03, 0.05],
        "max_hold_days": [5, 10, 15],
    },
    "price_volume_signal": {
        "turnover_min_pct": [0.03, 0.05, 0.08],
        "vol_ratio_min": [1.5, 2.0, 3.0],
        "vol_ratio_max": [3.0, 5.0, 8.0],
        "max_hold_days": [3, 5, 10],
        "big_turnover_pct": [0.08, 0.10, 0.15],
        "big_vol_ratio": [3.0, 5.0, 8.0],
        "float_shares": [5e7, 1e8, 5e8, 1e9],  # 流通股本：5000万~10亿（覆盖中小盘→大盘）
    },
    "regime_cycle_four": {
        "ma_short": [5, 10, 20],
        "ma_long": [20, 30, 60],
        "macd_fast": [8, 12, 16],
        "macd_slow": [20, 26, 30],
        "macd_signal": [5, 9, 12],
        "silver_window": [1, 3, 5, 10],  # 2026-09-02: 金叉后入场窗口（原只允许当天）
    },
    "sector_momentum": {
        "ma_short": [5, 10],
        "ma_mid": [10, 20, 30],
        "momentum_days": [3, 5, 10],
        "vol_ratio": [1.0, 1.2, 1.5],
        "stop_loss_pct": [0.05, 0.08, 0.10],
        "max_hold_days": [10, 20, 30],
    },
    "dragon_head": {
        "ma_short": [5, 10],
        "limit_up_pct": [0.07, 0.09, 0.095],
        "pullback_max": [0.02, 0.03, 0.05],
        "vol_expand_min": [0.8, 1.0, 1.2],
        "vol_shrink_exit": [0.5, 0.7, 0.9],
        "trailing_stop_pct": [0.05, 0.08, 0.10],
    },
}

# 策略 → 其策略类需要哪些参数（pyproject.toml 中定义的策略类）
# 使用 strategy_defaults.yaml 中的 key 名
STRATEGY_CONFIG_KEYS: dict[str, list[str]] = {
    "box_breakout": ["box_window", "volume_confirm", "volume_ratio"],
    "rsi_reversal": ["window", "oversold", "overbought"],
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
    "dragon_head": [
        "ma_short",
        "limit_up_pct",
        "pullback_max",
        "vol_expand_min",
        "vol_shrink_exit",
        "trailing_stop_pct",
    ],
}


# P1 修复：参数约束函数字典
# 第一性原理：参数语义要求某些参数之间有大小关系（如 dual_ma 的 short < long），
# 否则会产出语义错误的"金叉/死叉"（短均线比长均线还长，失去均线交叉的意义）
# 对抗性审查：在策略类 __init__ 中校验更彻底，但改动基类影响范围大；
# 在 grid search 中过滤是最小侵入方案，仅优化路径生效，运行时仍依赖策略自身逻辑
def _rsi_reversal_constraints(p: dict) -> bool:
    """rsi_reversal: 超卖必须严格小于超买。"""
    return p.get("oversold", 0) < p.get("overbought", 0)


def _double_yin_constraints(p: dict) -> bool:
    """double_yin: 第二根阴量应弱于第一根阴量，止盈和滞涨阈值必须为正。"""
    first = p.get("first_yin_vol_ratio_max", 0)
    second = p.get("second_yin_vol_ratio_max", 0)
    return (
        second <= first and p.get("take_profit_pct", 0) > 0 and p.get("stagnation_vol_ratio", 0) > 0
    )


STRATEGY_PARAM_CONSTRAINTS: dict[str, "callable[[dict], bool]"] = {
    "rsi_reversal": _rsi_reversal_constraints,
    "double_yin": _double_yin_constraints,
}


def load_defaults() -> dict:
    """加载当前 strategy_defaults.yaml。"""
    path = PROJECT_ROOT / "config" / "strategy_defaults.yaml"
    if not path.exists():
        logger.warning(f"策略默认参数文件不存在: {path}")
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_defaults(defaults: dict, *, gate_passed: bool = False) -> None:
    """写回 strategy_defaults.yaml；必须由已通过 GatePolicy 的调用方授权。"""
    if not gate_passed:
        raise PermissionError("strategy_defaults.yaml 写回必须先通过 GatePolicy")
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

        existing_strategy_names = {
            line.strip()[:-1]
            for line in new_lines
            if line and not line[0].isspace() and line.strip().endswith(":")
        }
        missing_strategies = [name for name in defaults if name not in existing_strategy_names]
        for name in missing_strategies:
            new_lines.extend(
                [
                    "",
                    yaml.dump(
                        {name: defaults[name]},
                        default_flow_style=False,
                        allow_unicode=True,
                    ).rstrip(),
                ]
            )

    # 原子写回：先写临时文件再替换，防止进程在写入中途崩溃导致参数文件截断。
    if existing:
        atomic_write_text(path, "\n".join(new_lines))
    else:
        atomic_write_text(path, yaml.dump(defaults, default_flow_style=False, allow_unicode=True))

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
    - write=True 时必须通过统一 GatePolicy，否则拒绝写回
    - force=True 仅保留命令兼容性，不得绕过生产写回门禁
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
    unlock_report: dict | None = None
    writeback_applied = False
    report_path: Path | None = None
    if write:
        unlock_report = _check_unlock_conditions(
            strategy_name=strategy_name,
            results=results,
            avg_sharpe=avg_sharpe,
            avg_return=avg_return,
            avg_max_dd=avg_max_dd,
            param_votes=param_votes,
        )

        if unlock_report["unlocked"]:
            # --force 保留为兼容参数，但不得绕过生产参数写回门禁。
            # 只有 GatePolicy 全部通过时才允许更新 strategy_defaults.yaml。
            # 更新 defaults 并写回
            if strategy_name not in defaults:
                defaults[strategy_name] = {}
            for k, v in best_params.items():
                defaults[strategy_name][k] = v
            write_defaults(defaults, gate_passed=True)
            writeback_applied = True
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
            print("  3. --force 不再绕过生产参数写回门禁；如需研究实验，请使用隔离输出目录")

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

    _record_optimization_ledger(
        entrypoint="scripts/optimize_params.py:run_optimization",
        strategy_name=strategy_name,
        symbols=[symbol],
        stock_dfs={symbol: df},
        requested_days=days,
        param_grid=param_grid,
        results=results,
        best_params=best_params,
        write_requested=write,
        writeback_applied=writeback_applied,
        unlock_report=unlock_report,
        n_splits=n_splits,
        artifact_path=report_path,
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
    """使用统一 GatePolicy 检查参数写回解锁条件。"""
    n_windows = len(results)
    stability_ratios = {
        param_name: max(votes.values()) / n_windows
        for param_name, votes in param_votes.items()
        if votes and n_windows > 0
    }
    param_stability = min(stability_ratios.values(), default=1.0)

    total_test_trades = 0
    for result in results:
        # 只消费明确的闭合 round-trip 样本数；旧结果仅兼容 test_trades。
        trades = result.get("closed_trade_count")
        if trades is None:
            trades = result.get("test_trades", 0)
        try:
            total_test_trades += max(0, int(trades or 0))
        except (TypeError, ValueError):
            continue

    gate = GatePolicy.evaluate_writeback(
        {
            "avg_sharpe": avg_sharpe,
            "param_stability": param_stability,
            "max_drawdown": avg_max_dd,
            "avg_return": avg_return,
            "oos_trades": total_test_trades,
        }
    )
    detail_values = {
        "avg_sharpe": f"avg_test_sharpe={avg_sharpe:.4f}",
        "param_stability": f"最低参数一致性={param_stability:.0%}",
        "max_drawdown": f"avg_max_dd={avg_max_dd:.2%}",
        "avg_return": f"avg_return={avg_return:.2%}",
        "oos_trades": f"total_test_trades={total_test_trades}",
    }
    names = {
        "avg_sharpe": "样本外 Sharpe > 0",
        "param_stability": "参数稳定性 ≥80%",
        "max_drawdown": "最大回撤 < 25%",
        "avg_return": "平均样本外收益 ≥ 0",
        "oos_trades": "样本外交易次数 ≥ 30",
    }
    checks = [
        {
            "name": names[key],
            "passed": item["passed"],
            "detail": f"{detail_values[key]} ({'通过' if item['passed'] else '失败'})",
        }
        for key, item in gate["checks"].items()
    ]
    legacy_failed_names = {
        "avg_sharpe": "样本外 Sharpe ≤ 0",
        "param_stability": "参数稳定性 <80%",
        "max_drawdown": "最大回撤 ≥ 25%",
        "avg_return": "平均样本外收益 < 0",
        "oos_trades": "样本外交易次数 < 30",
    }
    return {
        "unlocked": gate["passed"],
        "version": gate["version"],
        "checks": checks,
        "failed_checks": [legacy_failed_names[key] for key in gate["failed_checks"]],
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
            "建议扩大样本/增加数据量；--force 不得绕过生产参数写回门禁"
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


async def run_heuristic_optimization(
    strategy_name: str,
    symbols: list[str],
    days: int,
    algorithm: str,
    population_size: int,
    generations: int,
    write: bool,
    force: bool = False,
    seed: int | None = None,
) -> dict | None:
    """使用启发式算法（GA/PSO）对策略参数进行优化。

    相比网格搜索的 Walk-Forward，启发式算法更适合参数空间较大、
    需要自适应调参的场景。优化目标为夏普、Calmar、胜率、收益的加权评分。
    """
    from gugu.analysis.heuristic_optimizer import StrategyParamOptimizer
    from gugu.strategies.registry import get_strategy_cls

    strategy_cls = get_strategy_cls(strategy_name)
    if strategy_cls is None:
        logger.error(f"策略类 {strategy_name} 未注册")
        return None

    dm = data_manager()
    print(f"\n并发批量获取 {len(symbols)} 只股票历史数据...")
    stock_dfs = await dm.fetch_stock_history_batch(symbols, days=days)
    stock_dfs = {s: df for s, df in stock_dfs.items() if not df.empty and len(df) >= 120}
    if len(stock_dfs) < 3:
        logger.error(f"有效股票数 {len(stock_dfs)} < 3，无法运行启发式优化")
        return None

    defaults = load_defaults()
    param_grid = build_param_grid(strategy_name, defaults)
    if not param_grid:
        logger.warning(f"策略 {strategy_name} 无参数网格，跳过")
        return None

    constraint_fn = STRATEGY_PARAM_CONSTRAINTS.get(strategy_name)

    print(f"\n开始启发式优化: {strategy_name} 算法={algorithm.upper()}")
    print(f"  股票数: {len(stock_dfs)}")
    print(f"  种群大小: {population_size}, 迭代次数: {generations}")

    optimizer = StrategyParamOptimizer(
        strategy_cls=strategy_cls,
        stock_dfs=stock_dfs,
        param_grid=param_grid,
        param_constraints=constraint_fn,
        algorithm=algorithm,
        population_size=population_size,
        generations=generations,
        seed=seed,
    )

    result = optimizer.optimize(verbose=True)

    best_params = result.best_params
    best_score = result.best_score
    best_metrics = result.best_metrics

    print(f"\n{'=' * 70}")
    print(f"策略: {strategy_name} (启发式优化 {algorithm.upper()})")
    print(f"{'=' * 70}")
    print(f"股票: {list(stock_dfs.keys())}")
    print(f"最优综合得分: {best_score:.4f}")
    print(f"最优参数:")
    for k, v in best_params.items():
        default_val = defaults.get(strategy_name, {}).get(k, "?")
        print(f"  {k}: {v} (原值: {default_val})")

    # 用最优参数跑一次完整回测，计算解锁条件所需指标
    engine = BacktestEngine()
    test_sharpes: list[float] = []
    test_returns: list[float] = []
    test_max_dds: list[float] = []
    test_win_rates: list[float] = []
    test_closed_trade_counts: list[int] = []

    for symbol, df in stock_dfs.items():
        try:
            strategy = strategy_cls(best_params)
            bt_result = engine.run(strategy, df, symbol=symbol)
            metrics = bt_result.metrics
            test_sharpes.append(metrics.get("sharpe", 0))
            test_returns.append(metrics.get("total_return", 0))
            test_max_dds.append(metrics.get("max_drawdown", 0))
            test_win_rates.append(metrics.get("win_rate", 0))
            test_closed_trade_counts.append(int(metrics.get("closed_trade_count", 0)))
        except Exception as e:
            logger.warning(f"最终回测失败 {symbol}: {e}")
            continue

    avg_sharpe = sum(test_sharpes) / len(test_sharpes) if test_sharpes else 0
    avg_return = sum(test_returns) / len(test_returns) if test_returns else 0
    avg_max_dd = sum(test_max_dds) / len(test_max_dds) if test_max_dds else 0
    avg_win_rate = sum(test_win_rates) / len(test_win_rates) if test_win_rates else 0

    print(f"\n最终回测指标（多股平均）:")
    print(f"  平均 Sharpe: {avg_sharpe:.4f}")
    print(f"  平均收益:    {avg_return:.2%}")
    print(f"  平均最大回撤: {avg_max_dd:.2%}")
    print(f"  平均胜率:    {avg_win_rate:.2%}")

    # 构造解锁条件检查所需的 results 结构
    closed_trade_count = sum(test_closed_trade_counts)
    results = [
        {
            "best_params": best_params,
            "test_sharpe": avg_sharpe,
            "test_return": avg_return,
            "test_max_dd": avg_max_dd,
            "test_win_rate": avg_win_rate,
            "closed_trade_count": closed_trade_count,
            "test_trades": closed_trade_count,
        }
    ]
    param_votes: dict[str, dict] = {}
    for param_name, param_val in best_params.items():
        param_votes[param_name] = {str(param_val): 1}

    unlock_report: dict | None = None
    writeback_applied = False
    report_path: Path | None = None
    if write:
        unlock_report = _check_unlock_conditions(
            strategy_name=strategy_name,
            results=results,
            avg_sharpe=avg_sharpe,
            avg_return=avg_return,
            avg_max_dd=avg_max_dd,
            param_votes=param_votes,
        )

        if unlock_report["unlocked"]:
            # --force 仅保留命令兼容性，不能绕过 GatePolicy 写入生产默认参数。
            if strategy_name not in defaults:
                defaults[strategy_name] = {}
            for k, v in best_params.items():
                defaults[strategy_name][k] = v
            write_defaults(defaults, gate_passed=True)
            writeback_applied = True
            print(f"\n优化参数已写回 config/strategy_defaults.yaml")
        else:
            print(
                f"\n❌ 参数写回被拒绝（未通过 {len(unlock_report['failed_checks'])} 项解锁条件）:"
            )
            for check in unlock_report["checks"]:
                status = "✓" if check["passed"] else "✗"
                print(f"  {status} {check['name']}: {check['detail']}")

        report_path = write_optimization_report(
            strategy_name=strategy_name,
            unlock_report=unlock_report,
            avg_sharpe=avg_sharpe,
            avg_return=avg_return,
            avg_max_dd=avg_max_dd,
            param_votes=param_votes,
            best_params=best_params,
            n_windows=1,
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

    _record_optimization_ledger(
        entrypoint="scripts/optimize_params.py:run_heuristic_optimization",
        strategy_name=strategy_name,
        symbols=list(symbols),
        stock_dfs=stock_dfs,
        requested_days=days,
        param_grid=param_grid,
        results=results,
        best_params=best_params,
        write_requested=write,
        writeback_applied=writeback_applied,
        unlock_report=unlock_report,
        algorithm=algorithm,
        n_splits=1,
        seed=seed,
        population_size=population_size,
        generations=generations,
        artifact_path=report_path,
    )

    return {
        "strategy": strategy_name,
        "algorithm": algorithm,
        "best_params": best_params,
        "best_score": best_score,
        "best_metrics": best_metrics,
        "avg_sharpe": avg_sharpe,
        "avg_return": avg_return,
        "avg_max_dd": avg_max_dd,
        "avg_win_rate": avg_win_rate,
        "n_stocks": len(stock_dfs),
    }


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
        write: 是否写回参数（需通过统一 GatePolicy）
        force: 兼容旧命令的参数，不得绕过生产写回门禁
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

    unlock_report: dict | None = None
    writeback_applied = False
    report_path: Path | None = None

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

        if unlock_report["unlocked"]:
            # --force 仅保留命令兼容性，不能绕过 GatePolicy 写入生产默认参数。
            if strategy_name not in defaults:
                defaults[strategy_name] = {}
            for k, v in best_params.items():
                defaults[strategy_name][k] = v
            write_defaults(defaults, gate_passed=True)
            writeback_applied = True
            print(f"\n优化参数已写回 config/strategy_defaults.yaml")
        else:
            print(f"\n❌ 参数写回被拒绝（未通过 {len(unlock_report['failed_checks'])} 项解锁条件):")
            for check in unlock_report["checks"]:
                status = "✓" if check["passed"] else "✗"
                print(f"  {status} {check['name']}: {check['detail']}")
            print(f"\n建议:")
            print(f"  1. 扩大样本（当前 {len(stock_dfs)} 只股票，建议增加不同行业代表股）")
            print(f"  2. 增加数据量（--days 1000+）")
            print("  3. --force 不再绕过生产参数写回门禁；如需研究实验，请使用隔离输出目录")

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

    _record_optimization_ledger(
        entrypoint="scripts/optimize_params.py:run_multi_stock_optimization",
        strategy_name=strategy_name,
        symbols=list(symbols),
        stock_dfs=stock_dfs,
        requested_days=days,
        param_grid=param_grid,
        results=results,
        best_params=best_params,
        write_requested=write,
        writeback_applied=writeback_applied,
        unlock_report=unlock_report,
        n_splits=n_splits,
        artifact_path=report_path,
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
    algorithm: str = "walk_forward",
    population_size: int = 20,
    generations: int = 10,
    seed: int | None = None,
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
                    print("--force 仅保留兼容性，不绕过生产参数写回门禁")
                print("写回前将执行统一 GatePolicy 解锁条件检查")
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

    # P2 新增：多股聚合模式（解决单股过拟合）
    if multi_stock or strategy_name:
        # 解析股票列表（逗号分隔）；若未指定则按策略类型自动匹配
        if multi_stock:
            symbols = [s.strip().zfill(6) for s in multi_stock.split(",") if s.strip()]
            if len(symbols) < 3:
                logger.error("--multi-stock 至少需要 3 只股票，用逗号分隔，如 600519,000858,002714")
                return
        else:
            symbols = None  # 按策略自动选择

        if strategy_name:
            defaults = load_defaults()
            pool_symbols = (
                symbols
                if symbols is not None
                else [
                    s.strip().zfill(6)
                    for s in get_strategy_stock_pool(strategy_name, defaults).split(",")
                    if s.strip()
                ]
            )
            if len(pool_symbols) < 3:
                logger.error(f"策略 {strategy_name} 自动股票池不足 3 只: {pool_symbols}")
                return
            await run_multi_stock_optimization(
                strategy_name, pool_symbols, days, n_splits, write, force
            )
        else:
            from gugu.config import get_config

            cfg = get_config()
            enabled = cfg.strategy.enabled
            if not enabled:
                logger.error("无启用策略")
                return
            print(f"\n将对 {len(enabled)} 个策略运行多股聚合 Walk-Forward 优化")
            defaults = load_defaults()
            for s_name in enabled:
                try:
                    pool_symbols = (
                        symbols
                        if symbols is not None
                        else [
                            s.strip().zfill(6)
                            for s in get_strategy_stock_pool(s_name, defaults).split(",")
                            if s.strip()
                        ]
                    )
                    if len(pool_symbols) < 3:
                        logger.warning(f"策略 {s_name} 自动股票池不足 3 只，跳过")
                        continue
                    print(f"  {s_name} 股票池: {','.join(pool_symbols)}")
                    await run_multi_stock_optimization(
                        s_name, pool_symbols, days, n_splits, write, force
                    )
                    print()
                except Exception as e:
                    logger.error(f"策略 {s_name} 多股优化失败: {e}")
                    continue
        return

    # v0.7.3 新增：启发式优化模式（GA/PSO）
    if algorithm in ("ga", "pso"):
        # 解析股票列表：优先用户指定 → 默认股票池（按策略类型匹配）→ 50只代表池 → 单股
        if multi_stock:
            symbols = [s.strip().zfill(6) for s in multi_stock.split(",") if s.strip()]
        elif use_default_pool:
            symbols = DEFAULT_REPRESENTATIVE_STOCKS
        else:
            symbols = None  # 标记为按策略自动选择

        if strategy_name:
            # 单策略：按策略类型自动匹配股票池
            defaults = load_defaults()
            pool_symbols = (
                symbols
                if symbols is not None
                else [
                    s.strip().zfill(6)
                    for s in get_strategy_stock_pool(strategy_name, defaults).split(",")
                    if s.strip()
                ]
            )
            await run_heuristic_optimization(
                strategy_name=strategy_name,
                symbols=pool_symbols,
                days=days,
                algorithm=algorithm,
                population_size=population_size,
                generations=generations,
                write=write,
                force=force,
                seed=seed,
            )
        else:
            from gugu.config import get_config

            cfg = get_config()
            enabled = cfg.strategy.enabled
            if not enabled:
                logger.error("无启用策略")
                return
            print(f"\n将对 {len(enabled)} 个策略运行启发式优化 ({algorithm.upper()})")
            defaults = load_defaults()
            for s_name in enabled:
                try:
                    pool_symbols = (
                        symbols
                        if symbols is not None
                        else [
                            s.strip().zfill(6)
                            for s in get_strategy_stock_pool(s_name, defaults).split(",")
                            if s.strip()
                        ]
                    )
                    print(f"  {s_name} 股票池: {','.join(pool_symbols)}")
                    await run_heuristic_optimization(
                        strategy_name=s_name,
                        symbols=pool_symbols,
                        days=days,
                        algorithm=algorithm,
                        population_size=population_size,
                        generations=generations,
                        write=write,
                        force=force,
                        seed=seed,
                    )
                    print()
                except Exception as e:
                    logger.error(f"策略 {s_name} 启发式优化失败: {e}")
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
                print("--force 仅保留兼容性，不绕过生产参数写回门禁")
            print("写回前将执行统一 GatePolicy 解锁条件检查")
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
        "--force",
        action="store_true",
        help="兼容旧命令参数；不得绕过生产参数写回门禁",
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
    parser.add_argument(
        "--algorithm",
        type=str,
        default="walk_forward",
        choices=["walk_forward", "ga", "pso"],
        help="优化算法：walk_forward=网格搜索 Walk-Forward（默认），ga=遗传算法，pso=粒子群优化",
    )
    parser.add_argument(
        "--population",
        type=int,
        default=20,
        help="启发式算法种群大小（默认=20，仅 GA/PSO 生效）",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=10,
        help="启发式算法迭代次数（默认=10，仅 GA/PSO 生效）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="随机种子（默认=None，仅 GA/PSO 生效，用于结果复现）",
    )
    parser.add_argument("--version", action="version", version="gugu 0.6.0")
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
            args.algorithm,
            args.population,
            args.generations,
            args.seed,
        )
    )
