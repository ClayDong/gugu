"""回测报告兼容包装模块。

供 scripts/backtest_all_strategies.py 的 --report 选项导入。
提供 generate_markdown_report 和 save_report 两个接口。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.backtest_report_generator import (
    generate_report_from_metrics_dict,
    save_report as _save_report,
)


def generate_markdown_report(data: dict[str, Any]) -> str:
    """从 JSON 数据生成 Markdown 对比报告。

    Args:
        data: backtest_all_strategies.py 保存的 JSON 数据字典。
              格式: { "strategies": { "strategy_name": { "total_return": {"mean": ..., ...}, ... } } }

    Returns:
        Markdown 报告字符串。
    """
    # 从聚合数据中提取 per-strategy metrics 列表
    strategies_data = data.get("strategies", {})
    per_strategy_metrics: dict[str, list[dict[str, float]]] = {}

    for sname, summary in strategies_data.items():
        if not summary:
            continue
        # 报告统计必须基于真实的逐样本值，不能从均值伪造样本。
        metrics_list: list[dict[str, float]] = []
        sample_count = summary.get("_sample_count", 0) or summary.get("total_return", {}).get(
            "n", 0
        )
        metric_keys = [
            "total_return",
            "annual_return",
            "sharpe",
            "max_drawdown",
            "win_rate",
            "profit_factor",
            "total_trades",
            "avg_hold_days",
        ]
        raw_by_key = {key: summary.get(key, {}).get("raw_values") for key in metric_keys}
        if sample_count <= 0 or any(
            not isinstance(values, list) or len(values) < sample_count
            for values in raw_by_key.values()
        ):
            raise ValueError(f"策略 {sname} 缺少完整逐样本 raw_values，拒绝生成显著性报告")
        for i in range(sample_count):
            metrics_list.append({key: raw_by_key[key][i] for key in metric_keys})
        per_strategy_metrics[sname] = metrics_list

    return generate_report_from_metrics_dict(per_strategy_metrics)


def save_report(md_content: str, path: str | Path) -> None:
    """保存 Markdown 报告到文件。"""
    _save_report(md_content, path)
