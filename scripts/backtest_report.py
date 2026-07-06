"""回测报告兼容包装模块。

供 scripts/backtest_all_strategies.py 的 --report 选项导入。
提供 generate_markdown_report 和 save_report 两个接口。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.backtest_report_generator import generate_report_from_metrics_dict, save_report as _save_report


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
        # 从分布统计重建 metric values（使用 raw_values 如果是够小）
        # 否则从分布统计生成模拟数据
        metrics_list: list[dict[str, float]] = []
        sample_count = summary.get("_sample_count", 0) or summary.get("total_return", {}).get("n", 0)
        for i in range(sample_count):
            m = {}
            for key in ["total_return", "annual_return", "sharpe", "max_drawdown", "win_rate", "profit_factor", "total_trades", "avg_hold_days"]:
                dist = summary.get(key, {})
                raw_values = dist.get("raw_values", [])
                if raw_values and i < len(raw_values):
                    m[key] = raw_values[i]
                else:
                    # 用均值作为回退
                    m[key] = dist.get("mean", 0.0)
            metrics_list.append(m)
        if metrics_list:
            per_strategy_metrics[sname] = metrics_list

    return generate_report_from_metrics_dict(per_strategy_metrics)


def save_report(md_content: str, path: str | Path) -> None:
    """保存 Markdown 报告到文件。"""
    _save_report(md_content, path)
