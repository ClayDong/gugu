"""离线 Walk-Forward 参数优化脚本（基于本地 parquet 缓存）。

不依赖在线 API，从 data/parquet_cache/ 读取已缓存的历史 K 线数据，
对启用策略运行多股聚合 Walk-Forward 优化，避免单股过拟合。

第一性原理：
- 单股 Walk-Forward 在 600519 上训练 Sharpe 5.01 vs 测试 -6.28 = 典型过拟合
- 根因：单只股票的局部行情特征被误认为"普适规律"
- 修复：在多只股票上聚合训练，找跨股票稳健参数

用法:
    # 默认对 box_breakout 跑多股聚合优化（8 只代表股）
    uv run python scripts/walk_forward_offline.py

    # 显示当前参数
    uv run python scripts/walk_forward_offline.py --show

    # 自定义股票池
    uv run python scripts/walk_forward_offline.py --symbols 600519,000858,002714

    # 写回最优参数到 strategy_defaults.yaml（需通过 4 项解锁条件）
    uv run python scripts/walk_forward_offline.py --write

    # 强制写回（紧急情况，跳过解锁条件）
    uv run python scripts/walk_forward_offline.py --write --force

P1 解锁条件（全部满足才能写回，防止过拟合参数污染生产）：
1. 样本外 Sharpe > 0
2. 参数稳定性 ≥50% 窗口一致
3. 最大回撤 < 25%
4. 平均样本外收益 ≥ 0
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.backtest.engine import walk_forward_optimize_multi_stock  # noqa: E402
from gugu.config import PROJECT_ROOT  # noqa: E402
from gugu.strategies.registry import get_strategy_cls  # noqa: E402
from gugu.utils.log import get_logger  # noqa: E402

# 复用 optimize_params.py 的参数网格、约束、解锁条件检查
from optimize_params import (  # noqa: E402
    STRATEGY_PARAM_GRIDS,
    STRATEGY_PARAM_CONSTRAINTS,
    _check_unlock_conditions,
    load_defaults,
    write_defaults,
)

logger = get_logger()

# 默认 10 只不同行业代表股（如有 500+ 行缓存数据则用，否则从全市场随机抽样）
_DEFAULT_SYMBOLS = [
    "600519",
    "000858",
    "601318",
    "600036",
    "002714",
    "300750",
    "000333",
    "600276",
    "601012",
    "601888",
]

# 默认每只股票要求的最小行数（Walk-Forward 切 3 窗口 × 60% 训练 + 测试，至少需要 200 行）
_MIN_ROWS = 500

# 随机抽样股票数（当代表股不足时从全市场缓存补充）
_RANDOM_FALLBACK_COUNT = 10

_CACHE_DIR = PROJECT_ROOT / "data" / "parquet_cache"


def load_stock_df(symbol: str) -> pd.DataFrame | None:
    """从本地 parquet 缓存读取单股历史数据，优先取最长天数。"""
    for days in [2000, 1000, 500, 365, 250, 200, 120, 60]:
        f = _CACHE_DIR / f"{symbol}_{days}d.parquet"
        if f.exists():
            try:
                df = pd.read_parquet(f)
                if not df.empty:
                    return df
            except Exception as e:
                logger.debug(f"读取 {f} 失败: {e}")
    return None


def select_stocks_from_cache(
    preferred: list[str],
    min_rows: int,
    target_count: int,
) -> list[str]:
    """从缓存中选股：先用偏好列表，不足时从全市场随机补充。

    第一性原理：
    - 多股聚合 Walk-Forward 的目标是找"跨股票稳健参数"
    - 偏好代表股（大盘股、流动性好）数据质量高
    - 但代表股缓存可能不足（早期 datalen=200 截断），需从全市场补充
    - 随机抽样反而更具市场代表性（避免选股偏差）
    """
    import random

    selected: list[str] = []
    for sym in preferred:
        df = load_stock_df(sym)
        if df is not None and len(df) >= min_rows:
            selected.append(sym)
        if len(selected) >= target_count:
            return selected

    # 不足则从全市场缓存随机补充
    if len(selected) < target_count:
        cache_files = list(_CACHE_DIR.glob("*_500d.parquet"))
        random.seed(42)  # 可复现
        random.shuffle(cache_files)

        for f in cache_files:
            if len(selected) >= target_count:
                break
            code = f.stem.split("_")[0]
            if code in selected:
                continue
            try:
                df = pd.read_parquet(f)
                if len(df) >= min_rows and not df.empty:
                    selected.append(code)
            except Exception:
                continue

    return selected


async def run_offline_optimization(
    strategy_name: str,
    symbols: list[str],
    n_splits: int,
    write: bool,
    force: bool = False,
) -> dict | None:
    """对单个策略运行多股聚合 Walk-Forward 优化（离线数据）。"""
    strategy_cls = get_strategy_cls(strategy_name)
    if strategy_cls is None:
        logger.error(f"策略类 {strategy_name} 未注册")
        return None

    # 加载本地缓存数据
    stock_dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = load_stock_df(sym)
        if df is None or df.empty:
            logger.warning(f"{sym} 无缓存数据，跳过")
            continue
        if len(df) < 120:
            logger.warning(f"{sym} 数据仅 {len(df)} 行 < 120，跳过")
            continue
        stock_dfs[sym] = df

    if len(stock_dfs) < 3:
        logger.error(f"有效股票数 {len(stock_dfs)} < 3，多股聚合无意义")
        return None

    print(f"\n成功加载 {len(stock_dfs)}/{len(symbols)} 只股票缓存数据")
    for sym, df in stock_dfs.items():
        print(f"  {sym}: {len(df)} 行, 范围 {df['date'].min()} → {df['date'].max()}")

    defaults = load_defaults()
    param_grid = STRATEGY_PARAM_GRIDS.get(strategy_name, {})
    if not param_grid:
        logger.warning(f"策略 {strategy_name} 无参数网格，跳过")
        return None

    constraint_fn = STRATEGY_PARAM_CONSTRAINTS.get(strategy_name)

    # 统计合法参数组合数
    import itertools

    all_combos = list(itertools.product(*param_grid.values()))
    keys = list(param_grid.keys())
    legal_combos = [
        c
        for c in all_combos
        if constraint_fn is None or constraint_fn(dict(zip(keys, c, strict=True)))
    ]
    print(f"\n开始多股聚合 Walk-Forward 优化: {strategy_name}")
    print(f"  股票数: {len(stock_dfs)}")
    print(
        f"  参数组合: {len(all_combos)} 总 / {len(legal_combos)} 合法（约束过滤 {len(all_combos) - len(legal_combos)} 个）"
    )
    print(f"  窗口数: {n_splits}, 训练比例: 60%")
    print(f"  预计回测次数: ~{len(stock_dfs) * len(legal_combos) * n_splits * 2} 次（训练+测试）")

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
    print(f"策略: {strategy_name} (多股聚合 Walk-Forward)")
    print(f"{'=' * 70}")
    print(f"股票: {list(stock_dfs.keys())}")
    print(f"窗口: {n_splits}, 训练比例: 60%")
    print(f"平均训练 Sharpe:   {results[0].get('train_avg_sharpe', 0):.4f}")
    print(f"平均测试 Sharpe:   {avg_sharpe:.4f}")
    print(f"平均样本外收益:    {avg_return:.2%}")
    print(f"平均最大回撤:      {avg_max_dd:.2%}")
    print(f"平均胜率:           {avg_win_rate:.2%}")
    print(f"最差股票 Sharpe:    {min(r.get('test_min_sharpe', 0) for r in results):.4f}")
    print(f"最差股票收益:       {min(r.get('test_min_return', 0) for r in results):.2%}")
    print(f"最差股票最大回撤:   {max(r.get('test_worst_max_dd', 0) for r in results):.2%}")
    print(f"最大交易笔数集中度: {max(r.get('test_max_trade_share', 0) for r in results):.2%}")
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
            f"最差sharpe={r.get('test_min_sharpe', 0):.4f}, "
            f"收益={r.get('test_avg_return', 0):.2%}, "
            f"最差收益={r.get('test_min_return', 0):.2%}, "
            f"集中度={r.get('test_max_trade_share', 0):.2%}, "
            f"风险标记={r.get('risk_flags', [])}, "
            f"参数={r.get('best_params', {})}"
        )
        for d in r.get("test_details", [])[:3]:
            print(
                f"    {d['symbol']}: sharpe={d['test_sharpe']:.4f} "
                f"ret={d['test_return']:.2%} dd={d['test_max_dd']:.2%} "
                f"trades={d['trades']}"
            )

    # 解锁条件检查
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
            print(f"\n✅ 优化参数已写回 config/strategy_defaults.yaml")
        else:
            print(
                f"\n❌ 参数写回被拒绝（未通过 {len(unlock_report['failed_checks'])} 项解锁条件）:"
            )
            for check in unlock_report["checks"]:
                status = "✓" if check["passed"] else "✗"
                print(f"  {status} {check['name']}: {check['detail']}")
            print(f"\n建议:")
            print(f"  1. 检查策略是否真的有效（box_breakout 已通过全市场回测验证）")
            print(f"  2. 增加股票数（当前 {len(stock_dfs)} 只）")
            print(f"  3. 紧急情况可用 --force 强制写回（需 PR 评审说明理由）")

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


async def main(
    strategy_name: str | None,
    symbols_str: str | None,
    n_splits: int,
    write: bool,
    show: bool,
    force: bool,
    notify: bool = False,
) -> None:
    """主入口。"""
    if show:
        show_current_params()
        return

    if symbols_str:
        symbols = [s.strip().zfill(6) for s in symbols_str.split(",") if s.strip()]
    else:
        # 从代表股 + 全市场缓存补充选股
        symbols = select_stocks_from_cache(
            preferred=_DEFAULT_SYMBOLS,
            min_rows=_MIN_ROWS,
            target_count=_RANDOM_FALLBACK_COUNT,
        )
        print(f"从缓存自动选股: {symbols}")

    if strategy_name:
        await run_offline_optimization(strategy_name, symbols, n_splits, write, force)
    else:
        # 对所有启用策略运行
        from gugu.config import get_config

        cfg = get_config()
        enabled = cfg.strategy.enabled

        if not enabled:
            logger.error("无启用策略")
            return

        print(f"\n将对 {len(enabled)} 个启用策略运行多股聚合 Walk-Forward 优化")
        print(f"股票池: {symbols}")
        if write:
            if force:
                print("⚠️  --force 强制写回（跳过解锁条件检查）")
            else:
                print("写回前将执行 4 项解锁条件检查")
        print()

        for s_name in enabled:
            try:
                await run_offline_optimization(s_name, symbols, n_splits, write, force)
                if notify:
                    await _notify_optimization_result(s_name, symbols)
                print()
            except Exception as e:
                logger.error(f"策略 {s_name} 优化失败: {e}")
                continue


async def _notify_optimization_result(strategy_name: str, symbols: list[str]) -> None:
    """推送 Walk-Forward 优化结果到飞书。"""
    try:
        from gugu.notifier.feishu import FeishuNotifier

        report = {
            "title": f"Walk-Forward 优化: {strategy_name}",
            "strategy": strategy_name,
            "symbols": ",".join(symbols),
            "description": f"多股聚合 Walk-Forward 优化完成",
        }
        notifier = FeishuNotifier()
        await notifier.notify_backtest(report)
    except Exception as e:
        logger.warning(f"飞书推送失败: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="离线 Walk-Forward 参数优化（基于本地缓存）")
    parser.add_argument("--strategy", type=str, default=None, help="策略名称（默认=所有启用策略）")
    parser.add_argument(
        "--symbols", type=str, default=None, help="股票代码列表（逗号分隔，默认=8只代表股）"
    )
    parser.add_argument("--splits", type=int, default=3, help="Walk-Forward 窗口数（默认=3）")
    parser.add_argument(
        "--write",
        action="store_true",
        help="将最优参数写回 strategy_defaults.yaml（需通过解锁条件）",
    )
    parser.add_argument(
        "--force", action="store_true", help="强制写回，跳过解锁条件检查（紧急情况使用）"
    )
    parser.add_argument("--show", action="store_true", help="显示当前策略参数")
    parser.add_argument("--notify", action="store_true", help="优化完成后推送飞书通知")
    parser.add_argument("--version", action="version", version="gugu 0.6.0")
    args = parser.parse_args()

    asyncio.run(
        main(
            args.strategy, args.symbols, args.splits, args.write, args.show, args.force, args.notify
        )
    )
