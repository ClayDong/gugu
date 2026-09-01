"""回测报告生成器，汇总多策略对比指标。

功能：
- 接收 BacktestResult 列表，按策略分组
- 计算每个策略跨股票的分布统计（均值/中位数/std/p25/p75）
- 单样本 t 检验（收益是否显著 > 0）
- 生成多维度排序 Markdown 报告

用法:
    from gugu.backtest import BacktestResult
    from scripts.backtest_report_generator import generate_report

    results: list[BacktestResult] = [...]
    md = generate_report(results)

    或通过 JSON 数据生成:
    from scripts.backtest_report_generator import generate_report_from_json
    md = generate_report_from_json(data)
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from gugu.backtest import BacktestResult
from gugu.utils.atomic import atomic_write_text

# 需要计算分布统计的核心指标
_CORE_METRICS = [
    "total_return",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "total_trades",
    "avg_hold_days",
]

# 用于排序的指标（带中文名）
_SORT_KEYS: dict[str, str] = {
    "sharpe": "夏普比率",
    "win_rate": "胜率",
    "total_return": "总收益",
    "profit_factor": "盈亏比",
}


def generate_report(results: list[BacktestResult]) -> str:
    """生成多策略对比 Markdown 报告。

    Args:
        results: BacktestResult 对象列表，来自多股票多策略回测。

    Returns:
        Markdown 格式的完整报告字符串。
    """
    if not results:
        return "# 回测对比报告\n\n*无数据*"

    today = date.today()

    # 按策略分组
    strategy_groups: dict[str, list[BacktestResult]] = {}
    for r in results:
        strategy_groups.setdefault(r.strategy_name, []).append(r)

    # 构建策略汇总数据
    summaries: dict[str, dict[str, Any]] = {}
    for sname, group in strategy_groups.items():
        summaries[sname] = _compute_strategy_summary(sname, group)

    lines: list[str] = []
    lines.append(f"# 多策略回测对比报告")
    lines.append(f"")
    lines.append(f"**生成日期**: {today}")
    lines.append(f"**股票样本数**: {len(results)} 条记录（{len(strategy_groups)} 个策略）")
    lines.append(f"")

    # ── 1. 策略总览汇总表 ──
    lines.append("## 1. 策略总览汇总表")
    lines.append("")
    lines.append(
        "| 策略 | 样本数 | 总收益(中位数) | 年化(中位数) | 夏普(中位数) | 最大回撤(中位数) | 胜率(中位数) | 盈亏比(中位数) | 交易次数(均值) | 持仓天数(均值) | 收益>0显著性 |"
    )
    lines.append(
        "|------|--------|---------------|-------------|-------------|-----------------|-------------|---------------|--------------|--------------|-------------|"
    )
    for sname in sorted(summaries.keys()):
        s = summaries[sname]
        n = s["n"]
        med_ret = s["total_return"]["median"]
        med_ann = s["annual_return"]["median"]
        med_sharpe = s["sharpe"]["median"]
        med_dd = s["max_drawdown"]["median"]
        med_wr = s["win_rate"]["median"]
        med_pf = s["profit_factor"]["median"]
        mean_tt = s["total_trades"]["mean"]
        mean_hd = s["avg_hold_days"]["mean"]
        sig = s["significance"]
        sig_str = f"t={sig['t_stat']:.2f}, p={sig['p_value']:.4f}"
        if sig["significant"]:
            sig_str += " ✅"
        else:
            sig_str += " ❌"
        lines.append(
            f"| {sname} | {n} "
            f"| {_pct(med_ret)} "
            f"| {_pct(med_ann)} "
            f"| {med_sharpe:.3f} "
            f"| {_pct(med_dd)} "
            f"| {_pct(med_wr)} "
            f"| {_pf_str(med_pf)} "
            f"| {mean_tt:.0f} "
            f"| {mean_hd:.1f} "
            f"| {sig_str} |"
        )
    lines.append("")

    # ── 2-5. 多维度排序表 ──
    for sort_key, sort_label in _SORT_KEYS.items():
        lines.append(f"## 2. 按 {sort_label} 排序")
        lines.append("")
        sorted_names = sorted(
            summaries.keys(),
            key=lambda n: summaries[n][sort_key]["median"],
            reverse=(sort_key != "max_drawdown"),
        )
        header = (
            f"| {'策略':<20} | {'样本':<6} | "
            f"{'均值':>10} | {'中位数':>10} | {'Std':>10} | {'P25':>10} | {'P75':>10} |"
        )
        sep = (
            "|"
            + "-" * 22
            + "|"
            + "-" * 8
            + "|"
            + "-" * 12
            + "|"
            + "-" * 12
            + "|"
            + "-" * 12
            + "|"
            + "-" * 12
            + "|"
            + "-" * 12
            + "|"
        )
        lines.append(header)
        lines.append(sep)
        for sname in sorted_names:
            s = summaries[sname][sort_key]
            lines.append(
                f"| {sname:<20} | {s['n']:<6} "
                f"| {s['mean']:>10.4f} "
                f"| {s['median']:>10.4f} "
                f"| {s['std']:>10.4f} "
                f"| {s['p25']:>10.4f} "
                f"| {s['p75']:>10.4f} |"
            )
        lines.append("")

    # ── 6. 各策略指标分布（boxplot 文本信息） ──
    lines.append("## 6. 各策略指标分布")
    lines.append("")
    for metric_name in ["total_return", "sharpe", "win_rate", "max_drawdown"]:
        lines.append(f"### {_metric_label(metric_name)} 分布")
        lines.append("")
        lines.append("| 策略 | 最小值 | P25 | 中位数 | P75 | 最大值 | 均值 | Std |")
        lines.append("|------|--------|-----|--------|-----|--------|------|-----|")
        for sname in sorted(summaries.keys()):
            s = summaries[sname][metric_name]
            lines.append(
                f"| {sname} "
                f"| {s['min']:.4f} "
                f"| {s['p25']:.4f} "
                f"| {s['median']:.4f} "
                f"| {s['p75']:.4f} "
                f"| {s['max']:.4f} "
                f"| {s['mean']:.4f} "
                f"| {s['std']:.4f} |"
            )
        lines.append("")

    # ── 结论 ──
    lines.append("## 结论与建议")
    lines.append("")
    best_sharpe = max(summaries.keys(), key=lambda n: summaries[n]["sharpe"]["median"])
    best_wr = max(summaries.keys(), key=lambda n: summaries[n]["win_rate"]["median"])
    best_ret = max(summaries.keys(), key=lambda n: summaries[n]["total_return"]["median"])
    best_pf = max(summaries.keys(), key=lambda n: summaries[n]["profit_factor"]["median"])
    lines.append(
        f"- **夏普最佳**: {best_sharpe} (中位数 {summaries[best_sharpe]['sharpe']['median']:.3f})"
    )
    lines.append(
        f"- **胜率最佳**: {best_wr} (中位数 {_pct(summaries[best_wr]['win_rate']['median'])})"
    )
    lines.append(
        f"- **收益最佳**: {best_ret} (中位数 {_pct(summaries[best_ret]['total_return']['median'])})"
    )
    lines.append(
        f"- **盈亏比最佳**: {best_pf} (中位数 {_pf_str(summaries[best_pf]['profit_factor']['median'])})"
    )
    lines.append("")

    for sname in sorted(summaries.keys()):
        sig = summaries[sname]["significance"]
        ret_med = summaries[sname]["total_return"]["median"]
        sharpe_med = summaries[sname]["sharpe"]["median"]
        if sig["significant"] and ret_med > 0 and sharpe_med > 0.5:
            lines.append(
                f"- ✅ **{sname}**: 收益显著为正 (p={sig['p_value']:.4f}), 夏普 {sharpe_med:.2f} → 建议保留"
            )
        elif not sig["significant"]:
            lines.append(f"- ⚠️ **{sname}**: 收益不显著 (p={sig['p_value']:.4f}) → 需更多数据验证")
        elif ret_med < 0 or sharpe_med < 0:
            lines.append(
                f"- ❌ **{sname}**: 收益中位数 {_pct(ret_med)}, 夏普 {sharpe_med:.2f} → 建议禁用或调优"
            )
        else:
            lines.append(
                f"- ? **{sname}**: 收益 {_pct(ret_med)}, 夏普 {sharpe_med:.2f} → 边缘，需观察"
            )
    lines.append("")

    return "\n".join(lines)


def generate_report_from_metrics_dict(
    per_strategy_metrics: dict[str, list[dict[str, float]]],
) -> str:
    """从按策略分组后的 metrics 字典生成报告。

    Args:
        per_strategy_metrics: {strategy_name: [metrics_dict, ...]} 格式的数据。

    Returns:
        Markdown 报告字符串。
    """
    # 构建模拟的 BacktestResult 列表（保留必要的 metrics 信息）
    results: list[BacktestResult] = []
    for sname, metrics_list in per_strategy_metrics.items():
        for i, m in enumerate(metrics_list):
            # 使用占位 symbol（真实 symbol 不影响分布统计）
            results.append(
                BacktestResult(
                    symbol=f"stock_{i}",
                    strategy_name=sname,
                    trades=[],
                    equity_curve=__import__("pandas").Series(dtype=float),
                    metrics=m,
                )
            )
    return generate_report(results)


def save_report(md_content: str, path: str | Path) -> None:
    """保存 Markdown 报告到文件。

    Args:
        md_content: Markdown 字符串。
        path: 输出文件路径。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, md_content)


# ── 内部辅助函数 ──


def _compute_strategy_summary(
    strategy_name: str,
    results: list[BacktestResult],
) -> dict[str, Any]:
    """计算单个策略跨股票的汇总统计。

    Args:
        strategy_name: 策略名称。
        results: 该策略的所有回测结果。

    Returns:
        包含各指标分布统计和显著性检验的字典。
    """
    summary: dict[str, Any] = {
        "strategy_name": strategy_name,
        "n": len(results),
    }

    # 提取各指标值
    metric_values: dict[str, list[float]] = {}
    for m_key in _CORE_METRICS:
        values = [
            value
            for r in results
            for value in [r.metrics.get(m_key, 0.0)]
            if value is not None and math.isfinite(float(value))
        ]
        metric_values[m_key] = values

    # 计算分布统计
    for m_key, values in metric_values.items():
        summary[m_key] = _distribution_stats(values)

    # 单样本 t 检验：总收益是否显著 > 0
    summary["significance"] = _one_sample_ttest(metric_values.get("total_return", []))

    return summary


def _distribution_stats(values: list[float]) -> dict[str, float]:
    """计算一组值的分布统计，跳过 NaN/无穷值。"""
    finite_values = [value for value in values if math.isfinite(float(value))]
    if not finite_values:
        return {
            "mean": 0.0,
            "median": 0.0,
            "std": 0.0,
            "p25": 0.0,
            "p75": 0.0,
            "min": 0.0,
            "max": 0.0,
            "n": 0,
        }
    arr = np.array(finite_values, dtype=float)
    std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std": std,
        "p25": float(np.percentile(arr, 25)),
        "p75": float(np.percentile(arr, 75)),
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "n": len(finite_values),
    }


def _one_sample_ttest(values: list[float], popmean: float = 0.0) -> dict[str, Any]:
    """单样本 t 检验：检验 values 的均值是否显著不等于 popmean。

    Returns:
        包含 t_stat, p_value, significant, mean 的字典。
    """
    finite_values = [value for value in values if math.isfinite(float(value))]
    n = len(finite_values)
    if n < 2:
        return {"t_stat": 0.0, "p_value": 1.0, "significant": False, "mean": 0.0}

    arr = np.array(finite_values, dtype=float)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1))

    if std == 0:
        return {
            "t_stat": float("inf") if mean > popmean else float("-inf"),
            "p_value": 0.0,
            "significant": True,
            "mean": mean,
        }

    se = std / math.sqrt(n)
    t_stat = (mean - popmean) / se

    # 使用 scipy 计算 p 值（如果可用），否则用近似
    try:
        from scipy import stats as scipy_stats

        p_value = float(scipy_stats.t.sf(abs(t_stat), df=n - 1) * 2)
    except ImportError:
        # 手动近似（正态近似，大样本下准确）
        from scipy import stats as scipy_stats

        # 如果 scipy 不可用，使用正态近似
        try:
            from scipy.stats import norm

            p_value = float(norm.sf(abs(t_stat)) * 2)
        except ImportError:
            # 极简近似：t 分布自由度大时接近正态
            p_value = _approx_t_pvalue(abs(t_stat), n - 1)

    significant = p_value < 0.05
    return {
        "t_stat": float(t_stat),
        "p_value": p_value,
        "significant": significant,
        "mean": mean,
    }


def _approx_t_pvalue(t_abs: float, df: int) -> float:
    """极简 t 分布 p 值近似（仅当 scipy 不可用时的后备）。"""
    # 自由度为无穷时用正态近似
    # 对常见自由度（df > 30）足够准确
    if df > 100:
        # 正态近似
        return math.erfc(t_abs / math.sqrt(2))
    # 保守估计：用 t >= 2 近似 p < 0.05
    if t_abs >= 2.0:
        return 0.05 / (t_abs / 2.0)
    return 0.5


def _pct(value: float) -> str:
    """格式化百分比。"""
    return f"{value * 100:+.2f}%"


def _pf_str(value: float) -> str:
    """格式化盈亏比。"""
    if math.isinf(value):
        return "inf"
    return f"{value:.2f}"


def _metric_label(key: str) -> str:
    """指标键 → 中文标签。"""
    labels = {
        "total_return": "总收益",
        "annual_return": "年化收益",
        "sharpe": "夏普比率",
        "max_drawdown": "最大回撤",
        "win_rate": "胜率",
        "profit_factor": "盈亏比",
        "total_trades": "交易次数",
        "avg_hold_days": "平均持仓天数",
    }
    return labels.get(key, key)
