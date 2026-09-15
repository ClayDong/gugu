"""事件研究统计与结果输出 mixin。"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from gugu.analysis.event_study_models import (
    DEFAULT_CONCENTRATION_THRESHOLD,
    HorizonResult,
    StudyEvent,
    StudyResult,
)
from gugu.utils.atomic import atomic_write_json
from gugu.utils.log import get_logger

logger = get_logger()


def _welch_t(values: np.ndarray) -> tuple[float, float]:
    from scipy import stats

    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 3:
        return 0.0, 1.0
    result = stats.ttest_1samp(values, 0.0, nan_policy="omit")
    t_stat, p_value = float(result.statistic), float(result.pvalue)
    if not math.isfinite(t_stat) or not math.isfinite(p_value):
        return 0.0, 1.0
    return t_stat, p_value


class EventStudyReportMixin:
    @staticmethod
    def _check_concentration(
        events: list[StudyEvent],
        *,
        threshold: float = DEFAULT_CONCENTRATION_THRESHOLD,
    ) -> list[dict[str, Any]]:
        """检查单股信号集中度。"""
        if not events:
            return []
        counts = Counter(event.symbol for event in events)
        total = len(events)
        return [
            {
                "symbol": symbol,
                "count": count,
                "ratio": round(count / total, 4),
                "action": "集中度过高，建议剔除后重验",
            }
            for symbol, count in counts.items()
            if count / total > threshold
        ]

    def _aggregate_by_horizon(self, events: list[StudyEvent]) -> list[HorizonResult]:
        results: list[HorizonResult] = []
        for horizon in self.config.horizons:
            key = str(horizon)
            returns = [event.returns[key] for event in events if key in event.returns]
            excess = [event.excess[key] for event in events if key in event.excess]
            if len(returns) < self.config.min_samples:
                continue

            return_arr = np.asarray(returns, dtype=float)
            excess_arr = np.asarray(excess, dtype=float)
            avg_return = float(np.mean(return_arr))
            std_return = float(np.std(return_arr, ddof=1)) if len(return_arr) > 1 else 0.0
            avg_excess = float(np.mean(excess_arr)) if len(excess_arr) else 0.0
            std_excess = float(np.std(excess_arr, ddof=1)) if len(excess_arr) > 1 else 0.0
            t_stat, p_value = (
                _welch_t(excess_arr) if len(excess_arr) >= self.config.min_samples else (0.0, 1.0)
            )
            results.append(
                HorizonResult(
                    horizon=horizon,
                    n_events=len(returns),
                    avg_return=round(avg_return, 4),
                    avg_excess=round(avg_excess, 4),
                    std_excess=round(std_excess, 4),
                    t_stat=round(t_stat, 4),
                    p_value=round(p_value, 4),
                    win_rate=round(float(np.mean(return_arr > 0)), 4),
                    sharpe=round(avg_return / std_return, 4) if std_return > 0 else 0.0,
                    excess_sharpe=round(avg_excess / std_excess, 4) if std_excess > 0 else 0.0,
                    significant=p_value < 0.05,
                    excess_win_rate=round(float(np.mean(excess_arr > 0)), 4)
                    if len(excess_arr)
                    else 0.0,
                    std_return=round(std_return, 4),
                    n_returns=len(returns),
                    n_excess=len(excess),
                )
            )
        return results

    @staticmethod
    def _horizon_dict(result: HorizonResult) -> dict[str, Any]:
        return {
            "horizon": result.horizon,
            "n_events": result.n_events,
            "n_returns": result.n_returns or result.n_events,
            "n_excess": result.n_excess,
            "avg_return": result.avg_return,
            "avg_excess": result.avg_excess,
            "std_return": result.std_return,
            "std_excess": result.std_excess,
            "t_stat": result.t_stat,
            "p_value": result.p_value,
            "win_rate": result.win_rate,
            "excess_win_rate": result.excess_win_rate,
            "sharpe": result.sharpe,
            "excess_sharpe": result.excess_sharpe,
            "significant": result.significant,
        }

    @staticmethod
    def _event_dict(event: StudyEvent) -> dict[str, Any]:
        return {
            "symbol": event.symbol,
            "date": event.date,
            "entry": event.entry,
            "entry_date": event.entry_date,
            "entry_source": event.entry_source,
            "period": event.period,
            "year": event.year,
            "returns": event.returns,
            "excess": event.excess,
        }

    def _save(self, result: StudyResult) -> None:
        if not self.config.output_path:
            return
        atomic_write_json(Path(self.config.output_path), self._to_dict(result), indent=2)
        logger.info("事件研究结果已写入: {}", self.config.output_path)

    @classmethod
    def _to_dict(cls, result: StudyResult) -> dict[str, Any]:
        return {
            "schema_version": "event-study-v2",
            "strategy": result.strategy_name,
            "config": {
                "horizons": result.config.horizons,
                "index_code": result.config.index_code,
                "entry_mode": result.config.entry_mode,
                "cost_rate": result.config.cost_rate,
                "train_period": [result.config.train_start, result.config.train_end],
                "val_period": [result.config.val_start, result.config.val_end],
                "concentration_threshold": result.config.concentration_threshold,
            },
            "summary": {
                "n_events": result.n_events,
                "n_symbols": result.n_symbols,
                "benchmark_available": result.benchmark_available,
                "concentration_issues": result.concentration_issues,
            },
            "per_horizon": [cls._horizon_dict(item) for item in result.per_horizon],
            "per_year": {
                str(year): [cls._horizon_dict(item) for item in values]
                for year, values in result.per_year.items()
            },
            "per_period": {
                period: [cls._horizon_dict(item) for item in values]
                for period, values in result.per_period.items()
            },
            "events": [cls._event_dict(event) for event in result.events],
        }

    def _empty_result(self) -> StudyResult:
        return StudyResult(
            strategy_name=self.config.strategy_name,
            config=self.config,
            n_events=0,
            n_symbols=0,
            concentration_issues=[],
            per_horizon=[],
            per_year={},
            per_period={},
            events=[],
            benchmark_available=False,
        )

    def print_summary(self, result: StudyResult) -> None:
        """打印易读摘要。"""
        header = f"═ 事件研究: {result.strategy_name} ═"
        print("\n" + "═" * len(header))
        print(header)
        print("═" * len(header))
        print(f"总事件: {result.n_events} | 股票数: {result.n_symbols}")
        print(f"入场: {self.config.entry_mode} | 成本: {self.config.cost_rate:.2%}")
        print(
            f"基准: {self.config.index_code} | 基准数据: {'可用' if result.benchmark_available else '不可用'}"
        )
        print(f"训练期: {self.config.train_start} ~ {self.config.train_end}")
        print(f"验证期: {self.config.val_start} ~ {self.config.val_end}\n")

        if result.concentration_issues:
            print("⚠ 集中度警告:")
            for issue in result.concentration_issues:
                print(
                    f"  {issue['symbol']}: {issue['ratio']:.0%} ({issue['count']}/{result.n_events})"
                )
            print()
        if not result.per_horizon:
            print("无满足最小样本要求的有效事件\n")
            return

        print(
            f"{'维度':>6} {'样本':>6} {'超额收益':>10} {'t统计':>8} {'p值':>8} {'胜率':>7} {'夏普':>8} {'显著':>6}"
        )
        print("-" * 68)
        for item in result.per_horizon:
            mark = "✅" if item.significant else "❌"
            print(
                f"{item.horizon:>4}d {item.n_events:>6} {item.avg_excess:>+9.2%} "
                f"{item.t_stat:>+8.3f} {item.p_value:>8.4f} {item.win_rate:>6.0%} "
                f"{item.sharpe:>8.2f} {mark:>4}"
            )

        if result.per_period:
            print("\n时期分桶:")
            for period in ("train", "val", "outside"):
                items = result.per_period.get(period, [])
                if not items:
                    continue
                text = " | ".join(
                    f"{item.horizon}d: {item.avg_excess:+.2%} (n={item.n_events})" for item in items
                )
                print(f"  {period}: {text}")
        print()
