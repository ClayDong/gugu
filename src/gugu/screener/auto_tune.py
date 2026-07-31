"""尾盘选股参数自动调优。

基于近 N 个交易日的验证历史（data/screener_validation.jsonl），
动态调整 ScreenerParams 中的选股阈值。调优过程受硬边界约束，
不会把参数推到非理性区间。

调优逻辑（第一性原理）：
- 选股效果差（开盘超额低 + 胜率低 + 止损命中率高）→ 收紧条件，
  提高信号质量，减少误选。
- 选股效果好（开盘超额高 + 胜率高 + 回撤可控）→ 放宽条件，
  增加样本数量，避免过度拟合。
- 每次调整幅度小（步进），防止参数抖动。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from gugu.screener.params import ScreenerParams
from gugu.utils.log import get_logger

logger = get_logger()

_DEFAULT_DATA_DIR = Path(__file__).parent.parent.parent.parent / "data"
_VALIDATION_FILE = _DEFAULT_DATA_DIR / "screener_validation.jsonl"

# 最小样本数，低于此值不执行调优
_MIN_SAMPLES = 5


@dataclass
class TuneMetrics:
    """用于自动调优的验证统计指标。"""

    avg_open_excess: float  # 平均开盘超额收益（%）
    win_rate: float  # 开盘溢价 > 0 的比例
    stop_loss_rate: float  # 止损命中比例
    avg_drawdown: float  # 平均盘中最低回撤（%）
    sample_size: int  # 样本数


def load_recent_validation(days: int | None = None) -> list[dict[str, Any]]:
    """加载最近 N 个交易日的验证记录。

    按 validation_date 去重，每个交易日只取最新一次验证结果，
    避免同一交易日多次验证导致样本重复。
    """
    file_path = _VALIDATION_FILE
    if not file_path.exists():
        return []

    lookback = days or 10
    cutoff = (datetime.now().date() - timedelta(days=lookback * 2)).isoformat()

    records_by_date: dict[str, list[dict[str, Any]]] = {}
    try:
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                val_date = d.get("validation_date", "")
                if not val_date or val_date < cutoff:
                    continue
                records_by_date.setdefault(val_date, []).append(d)
    except Exception as e:
        logger.warning("读取验证历史失败: {}", e)
        return []

    # 每个交易日取全部记录（因为一天可能选多只股票）
    # 按日期倒序取最近 lookback 个交易日
    sorted_dates = sorted(records_by_date.keys(), reverse=True)[:lookback]
    result: list[dict[str, Any]] = []
    for date in sorted_dates:
        result.extend(records_by_date[date])
    return result


def compute_metrics(records: list[dict[str, Any]]) -> TuneMetrics:
    """计算验证记录的调优指标。

    P2-3 升级（第一性原理）：
    - 优先使用真实策略收益（sim_return_pct > 0 为胜），而非仅开盘溢价。
      sim 字段由 validator 在 T+1 日生成（开盘低吸 → 止盈/止损/收盘平仓）。
    - 大盘门控阻断的记录（market_gate_blocked=True）未实际买入，不参与统计。
    - 向后兼容：无 sim 字段的旧记录回退到 open_premium。
    """
    # 过滤：门控阻断 = 未买入，排除
    tradable = [r for r in records if not r.get("market_gate_blocked", False)]
    n = len(tradable)
    if n == 0:
        return TuneMetrics(0.0, 0.0, 0.0, 0.0, 0)

    # 有 sim 字段的记录用 sim_return_pct，否则回退 open_premium
    def _has_sim(r: dict[str, Any]) -> bool:
        return bool(r.get("sim_exit_reason")) and r.get("sim_return_pct") is not None

    open_excess = [
        r.get("open_premium", 0.0) - r.get("benchmark_open_change", 0.0)
        for r in tradable
        if not _has_sim(r)
    ]
    # 基准：优先 sim 收益均值，无 sim 时用开盘超额
    sim_returns = [r.get("sim_return_pct", 0.0) for r in tradable if _has_sim(r)]
    if sim_returns:
        avg_open_excess = sum(sim_returns) / n
    else:
        avg_open_excess = sum(open_excess) / n if open_excess else 0.0

    win_rate = (
        sum(1 for r in tradable if _has_sim(r) and r.get("sim_return_pct", 0) > 0)
        + sum(1 for r in tradable if not _has_sim(r) and r.get("open_premium", 0.0) > 0)
    ) / n
    stop_loss_rate = (
        sum(
            1
            for r in tradable
            if r.get("stop_loss_hit", False)
            or (_has_sim(r) and r.get("sim_exit_reason") == "stop_loss")
        )
        / n
    )
    avg_drawdown = sum(r.get("intraday_max_drawdown", 0.0) for r in tradable) / n

    return TuneMetrics(
        avg_open_excess=round(avg_open_excess, 2),
        win_rate=round(win_rate, 2),
        stop_loss_rate=round(stop_loss_rate, 2),
        avg_drawdown=round(avg_drawdown, 2),
        sample_size=n,
    )


def generate_adjustments(
    metrics: TuneMetrics,
    params: ScreenerParams,
    vol_ratio_step: float = 0.05,
    turnover_step: float = 0.5,
    chg_step: float = 0.2,
) -> dict[str, float]:
    """基于验证指标生成参数调整增量。

    返回空 dict 表示无需调整。
    """
    adjustments: dict[str, float] = {}

    # 情况 A：效果差 → 收紧
    poor_performance = (
        metrics.avg_open_excess < params.tune_tighten_open_excess
        and metrics.win_rate < params.tune_tighten_win_rate
    )
    too_much_stop_loss = metrics.stop_loss_rate > params.tune_tighten_stop_loss_rate

    if poor_performance or too_much_stop_loss:
        # 更缩量（降低量比上限）
        adjustments["max_vol_ratio"] = -vol_ratio_step
        # 要求更活跃（提高换手率下限）
        adjustments["min_turnover"] = turnover_step
        # 跌幅要求更深（避免弱势反抽）
        adjustments["min_chg_pct"] = -chg_step
        logger.info(
            f"自动调优: 效果偏弱（超额{metrics.avg_open_excess:+.2f}% "
            f"胜率{metrics.win_rate:.0%} 止损率{metrics.stop_loss_rate:.0%}），收紧条件"
        )
        return adjustments

    # 情况 B：效果好 → 放宽
    good_performance = (
        metrics.avg_open_excess > params.tune_loosen_open_excess
        and metrics.win_rate > params.tune_loosen_win_rate
        and metrics.avg_drawdown > -2.0  # 回撤可控才放宽
    )
    if good_performance:
        adjustments["max_vol_ratio"] = vol_ratio_step
        adjustments["min_turnover"] = -turnover_step
        adjustments["min_chg_pct"] = chg_step
        logger.info(
            f"自动调优: 效果良好（超额{metrics.avg_open_excess:+.2f}% "
            f"胜率{metrics.win_rate:.0%} 回撤{metrics.avg_drawdown:+.2f}%），放宽条件"
        )
        return adjustments

    logger.info(
        f"自动调优: 指标处于中性区间（超额{metrics.avg_open_excess:+.2f}% "
        f"胜率{metrics.win_rate:.0%} 止损率{metrics.stop_loss_rate:.0%}），不调整"
    )
    return adjustments


def auto_tune_screener_params(days: int | None = None) -> ScreenerParams:
    """执行尾盘选股参数自动调优并保存。

    Args:
        days: 回看交易日数，默认使用 params.tune_lookback_days

    Returns:
        调优后的 ScreenerParams（如果未启用或样本不足则返回原参数）
    """
    params = ScreenerParams.load()
    if not params.auto_tune_enabled:
        logger.info("自动调优已禁用，跳过")
        return params

    lookback = days or params.tune_lookback_days
    records = load_recent_validation(lookback)
    if len(records) < _MIN_SAMPLES:
        logger.info(
            f"自动调优: 近 {lookback} 日验证样本 {len(records)} 条，不足 {_MIN_SAMPLES}，跳过"
        )
        return params

    metrics = compute_metrics(records)
    logger.info(
        f"自动调优: 近 {lookback} 日样本 {metrics.sample_size} 只 | "
        f"平均开盘超额 {metrics.avg_open_excess:+.2f}% | "
        f"胜率 {metrics.win_rate:.0%} | "
        f"止损率 {metrics.stop_loss_rate:.0%} | "
        f"平均回撤 {metrics.avg_drawdown:+.2f}%"
    )

    adjustments = generate_adjustments(metrics, params)
    if not adjustments:
        return params

    new_params = params.apply_adjustment(adjustments)
    new_params.save()
    logger.info(
        f"自动调优已应用并保存: "
        f"max_vol_ratio {params.max_vol_ratio:.2f}→{new_params.max_vol_ratio:.2f}, "
        f"min_turnover {params.min_turnover:.1f}→{new_params.min_turnover:.1f}, "
        f"min_chg_pct {params.min_chg_pct:.1f}→{new_params.min_chg_pct:.1f}"
    )
    return new_params
