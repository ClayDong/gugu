"""盈亏归因分析器：消费 signals_history.jsonl 生成每日/每周盈亏归因报告。

核心功能：
1. 读取 signals_history.jsonl 的完整决策链路
2. 追踪每条信号的后续实际盈亏
3. 按过滤原因分组统计（基本面过滤拦截了多少盈利/亏损信号）
4. 按策略分组统计（哪个策略贡献最大）
5. 按决策步骤分组归因（风控拦截、Wisdom 过滤、四阶段过滤等）
6. 输出结构化报告供飞书推送

数据流：
signals_history.jsonl → AttributionAnalyzer → AttributionReport → FeishuNotifier
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from gugu.config import PROJECT_ROOT
from gugu.data import data_manager
from gugu.data.stock_names import lookup_stock_name
from gugu.utils.log import get_logger

logger = get_logger()

# 追踪周期（交易日天数）
TRACK_PERIODS = [1, 3, 5, 10]
# 最小样本数
MIN_SAMPLE_SIZE = 3


class AttributionReport:
    """归因报告数据类。"""

    def __init__(self) -> None:
        self.period_start: str = ""
        self.period_end: str = ""
        self.total_signals: int = 0
        self.executed_signals: int = 0
        self.filtered_signals: int = 0

        # 按过滤步骤归因
        self.by_filter_step: list[dict[str, Any]] = []
        # 按策略归因
        self.by_strategy: list[dict[str, Any]] = []
        # 按方向归因（买入 vs 卖出）
        self.by_direction: dict[str, Any] = {}
        # 按 Wisdom 决策归因
        self.by_wisdom_action: list[dict[str, Any]] = []

        # 整体绩效
        self.total_tracked: int = 0
        self.forward_5d_signal_hit_rate: float = 0.0
        self.avg_return_5d: float = 0.0
        self.closed_trade_win_rate: float = 0.0
        self.realized_pnl: float = 0.0
        self.best_signal: dict[str, Any] | None = None
        self.worst_signal: dict[str, Any] | None = None

        # 最近信号（用于日报）
        self.recent_signals: list[dict[str, Any]] = []

    def to_dict(self) -> dict[str, Any]:
        """转为字典（供飞书推送和 Web 展示）。"""
        return {
            "period": f"{self.period_start} ~ {self.period_end}",
            "total_signals": self.total_signals,
            "executed_signals": self.executed_signals,
            "filtered_signals": self.filtered_signals,
            "by_filter_step": self.by_filter_step,
            "by_strategy": self.by_strategy,
            "by_direction": self.by_direction,
            "by_wisdom_action": self.by_wisdom_action,
            "performance": {
                "total_tracked": self.total_tracked,
                "forward_5d_signal_hit_rate": round(self.forward_5d_signal_hit_rate, 4),
                "avg_return_5d": round(self.avg_return_5d, 4),
                "closed_trade_win_rate": round(self.closed_trade_win_rate, 4),
                "realized_pnl": round(self.realized_pnl, 2),
            },
            "best_signal": self.best_signal,
            "worst_signal": self.worst_signal,
            "recent_signals": self.recent_signals[:10],
        }


class AttributionAnalyzer:
    """盈亏归因分析器。

    读取 signals_history.jsonl，对每条信号追踪后续实际盈亏，
    按多种维度分组归因。

    用法：
        analyzer = AttributionAnalyzer()
        report = await analyzer.analyze(days=7)  # 过去一周
        await notifier.notify_attribution(report.to_dict())
    """

    def __init__(self) -> None:
        self._dm = data_manager()
        self._history_path: Path = PROJECT_ROOT / "data" / "signals_history.jsonl"

    def load_history(self, days: int = 7) -> list[dict[str, Any]]:
        """加载最近 N 天的信号历史。

        Args:
            days: 回溯天数（0=全部）

        Returns:
            信号记录列表（按时间排序）
        """
        if not self._history_path.exists():
            logger.warning(f"信号历史文件不存在: {self._history_path}")
            return []

        records: list[dict[str, Any]] = []
        cutoff = datetime.now() - timedelta(days=days) if days > 0 else None

        try:
            with self._history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = pd.to_datetime(rec.get("timestamp"))
                        if cutoff is None or ts >= cutoff:
                            records.append(rec)
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.debug(f"跳过无效行: {e}")
                        continue
        except Exception as e:
            logger.error(f"读取信号历史失败: {e}")
            return []

        # 按时间排序
        records.sort(key=lambda r: r.get("timestamp", ""))
        logger.info(f"加载 {len(records)} 条信号记录（最近 {days} 天）")
        return records

    async def analyze(self, days: int = 7) -> AttributionReport:
        """生成完整归因报告。

        Args:
            days: 分析最近 N 天的信号

        Returns:
            AttributionReport
        """
        records = self.load_history(days)
        report = AttributionReport()

        if not records:
            report.period_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            report.period_end = datetime.now().strftime("%Y-%m-%d")
            return report

        # 记录周期
        timestamps = [r.get("timestamp", "") for r in records if r.get("timestamp")]
        report.period_start = timestamps[0][:10] if timestamps else ""
        report.period_end = timestamps[-1][:10] if timestamps else ""

        # 去重：同一股票同一天同方向只保留最后一条
        records = self._dedupe(records)
        report.total_signals = len(records)

        executed = [r for r in records if r.get("order_success")]
        filtered = [r for r in records if r.get("wisdom_filtered")]
        report.executed_signals = len(executed)
        report.filtered_signals = len(filtered)

        # 1. 按过滤步骤归因
        report.by_filter_step = self._analyze_filter_steps(records)

        # 2. 按策略归因
        report.by_strategy = self._analyze_by_strategy(records)

        # 3. 按方向归因
        report.by_direction = self._analyze_by_direction(records)

        # 4. 按 Wisdom 决策归因
        report.by_wisdom_action = self._analyze_by_wisdom(records)

        # 前瞻信号统计与闭仓交易统计严格分离。
        buy_signals = [r for r in records if r.get("direction") == "buy"]
        tracked = await self._track_signals(buy_signals) if buy_signals else []
        valid = [t for t in tracked if t.get("return_5d") is not None]
        report.total_tracked = len(valid)
        if valid:
            wins = [t for t in valid if t["return_5d"] > 0]
            report.forward_5d_signal_hit_rate = len(wins) / len(valid)
            report.avg_return_5d = sum(t["return_5d"] for t in valid) / len(valid)
            report.best_signal = max(valid, key=lambda t: t.get("return_5d", -999))
            report.worst_signal = min(valid, key=lambda t: t.get("return_5d", 999))
            report.recent_signals = valid[-10:] if len(valid) >= 10 else valid
            for s in report.recent_signals:
                sym = s.get("symbol", "")
                name = s.get("name", "")
                if not name or name == sym:
                    s["name"] = lookup_stock_name(sym) or sym

        closed_trades = [r for r in records if r.get("outcome_status") == "closed"]
        realized_pnls: list[float] = []
        for trade in closed_trades:
            pnl = trade.get("realized_pnl")
            if pnl is None:
                pnl = (trade.get("outcome_metrics") or {}).get("realized_pnl")
            try:
                realized_pnls.append(float(pnl))
            except (TypeError, ValueError):
                continue
        if realized_pnls:
            report.closed_trade_win_rate = sum(pnl > 0 for pnl in realized_pnls) / len(
                realized_pnls
            )
            report.realized_pnl = sum(realized_pnls)

        return report

    def _dedupe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """去重：同一股票同一天同方向只保留最后一条。"""
        seen: dict[str, dict[str, Any]] = {}
        for r in records:
            ts = r.get("timestamp", "")
            symbol = r.get("symbol", "")
            direction = r.get("direction", "")
            try:
                d = pd.to_datetime(ts).date().isoformat()
            except Exception:
                continue
            key = f"{d}_{symbol}_{direction}"
            seen[key] = r
        return list(seen.values())

    def _analyze_filter_steps(self, records: list[dict]) -> list[dict[str, Any]]:
        """按过滤步骤归因：每一步过滤了多少信号，拦截了什么。"""
        # 决策步骤名称定义
        step_names = {
            "danger_signals": "危险信号检测",
            "fundamental": "基本面过滤",
            "money_flow": "资金流过滤",
            "avg_down_check": "向下摊平检查",
            "industry_check": "行业约束",
            "stage": "四阶段入场过滤",
            "wisdom_decision": "Wisdom 决策",
            "risk_check": "风控检查",
        }

        step_counts: dict[str, dict] = {}
        for step_key in step_names:
            step_counts[step_key] = {"name": step_names[step_key], "count": 0, "symbols": []}

        for r in records:
            if not r.get("order_success") and r.get("filter_reason"):
                reason = r.get("filter_reason", "")
                # 匹配过滤步骤
                matched = False
                for step_key, name in step_names.items():
                    if name in reason or step_key in reason or step_key.replace("_", "") in reason:
                        step_counts[step_key]["count"] += 1
                        sym = r.get("symbol", "")
                        if sym not in step_counts[step_key]["symbols"]:
                            step_counts[step_key]["symbols"].append(sym)
                        matched = True
                        break
                if not matched:
                    # 尝试从 decision_chain 匹配
                    chain = r.get("decision_chain", [])
                    for step in chain:
                        if not step.get("passed", True):
                            step_name = step.get("name", "")
                            for step_key, name in step_names.items():
                                if name in step_name or step_key in step_name:
                                    step_counts[step_key]["count"] += 1
                                    sym = r.get("symbol", "")
                                    if sym not in step_counts[step_key]["symbols"]:
                                        step_counts[step_key]["symbols"].append(sym)
                                    break

        # 格式化为列表
        result = []
        for step_key, info in step_counts.items():
            if info["count"] > 0:
                result.append(
                    {
                        "step": step_key,
                        "name": info["name"],
                        "filtered_count": info["count"],
                        "symbols": info["symbols"],
                    }
                )

        result.sort(key=lambda x: x["filtered_count"], reverse=True)
        return result

    def _analyze_by_strategy(self, records: list[dict]) -> list[dict[str, Any]]:
        """按策略分组统计信号数量。"""
        strategy_counts: dict[str, dict] = {}

        for r in records:
            strategies = r.get("strategies", []) or [r.get("strategy", "unknown")]
            if isinstance(strategies, str):
                strategies = [strategies]

            for s in strategies:
                if s not in strategy_counts:
                    strategy_counts[s] = {
                        "count": 0,
                        "executed": 0,
                        "filtered": 0,
                        "buy": 0,
                        "sell": 0,
                    }
                strategy_counts[s]["count"] += 1
                if r.get("order_success"):
                    strategy_counts[s]["executed"] += 1
                if r.get("wisdom_filtered"):
                    strategy_counts[s]["filtered"] += 1
                if r.get("direction") == "buy":
                    strategy_counts[s]["buy"] += 1
                elif r.get("direction") == "sell":
                    strategy_counts[s]["sell"] += 1

        result = []
        for s, stats in strategy_counts.items():
            result.append(
                {
                    "strategy": s,
                    "total_signals": stats["count"],
                    "executed": stats["executed"],
                    "filtered": stats["filtered"],
                    "buy_signals": stats["buy"],
                    "sell_signals": stats["sell"],
                    "execute_rate": round(stats["executed"] / stats["count"], 4)
                    if stats["count"] > 0
                    else 0,
                }
            )

        result.sort(key=lambda x: x["total_signals"], reverse=True)
        return result

    def _analyze_by_direction(self, records: list[dict]) -> dict[str, Any]:
        """按方向分组统计。"""
        buy_count = sum(1 for r in records if r.get("direction") == "buy")
        sell_count = sum(1 for r in records if r.get("direction") == "sell")
        buy_executed = sum(
            1 for r in records if r.get("direction") == "buy" and r.get("order_success")
        )
        sell_executed = sum(
            1 for r in records if r.get("direction") == "sell" and r.get("order_success")
        )
        buy_filtered = sum(
            1 for r in records if r.get("direction") == "buy" and r.get("wisdom_filtered")
        )
        sell_filtered = sum(
            1 for r in records if r.get("direction") == "sell" and r.get("wisdom_filtered")
        )

        return {
            "buy": {
                "count": buy_count,
                "execution_success": buy_executed,
                "executed": buy_executed,
                "filtered": buy_filtered,
            },
            "sell": {
                "count": sell_count,
                "execution_success": sell_executed,
                "executed": sell_executed,
                "filtered": sell_filtered,
            },
        }

    def _analyze_by_wisdom(self, records: list[dict]) -> list[dict[str, Any]]:
        """按 Wisdom 决策动作分组统计。"""
        action_counts: dict[str, dict] = {}

        for r in records:
            wisdom = r.get("wisdom_decision", {}) or {}
            action = wisdom.get("action", "unknown")
            source = wisdom.get("source", "unknown")

            key = f"{source}/{action}"
            if key not in action_counts:
                action_counts[key] = {"source": source, "action": action, "count": 0, "filtered": 0}
            action_counts[key]["count"] += 1
            if r.get("wisdom_filtered"):
                action_counts[key]["filtered"] += 1

        result = []
        for _key, stats in action_counts.items():
            result.append(
                {
                    "source": stats["source"],
                    "action": stats["action"],
                    "count": stats["count"],
                    "filtered": stats["filtered"],
                }
            )

        result.sort(key=lambda x: x["count"], reverse=True)
        return result

    async def _track_signals(self, signals: list[dict]) -> list[dict[str, Any]]:
        """追踪每条买入信号的实际收益。"""
        sem = asyncio.Semaphore(5)

        async def track_one(sig: dict) -> dict:
            async with sem:
                return await self._track_one(sig)

        tasks = [track_one(s) for s in signals]
        done = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[dict] = []
        for r in done:
            if isinstance(r, dict):
                results.append(r)
        return results

    async def _track_one(self, sig: dict) -> dict[str, Any]:
        """追踪单条信号的实际收益。"""
        symbol = sig.get("symbol", "")
        ts = sig.get("timestamp", "")

        try:
            signal_date = pd.to_datetime(ts)
        except Exception:
            return {**sig, "error": "invalid_timestamp"}

        days_since = (datetime.now() - signal_date.to_pydatetime()).days
        if days_since < 1:
            return {**sig, "error": "too_recent"}

        try:
            df = await self._dm.fetch_stock_history(symbol, days=max(days_since + 10, 30))
            if df.empty or len(df) < 2:
                return {**sig, "error": "no_data"}

            df["date"] = pd.to_datetime(df["date"])
            mask = df["date"] >= signal_date
            if not mask.any():
                return {**sig, "error": "date_not_found"}

            idx = mask.idxmax() if hasattr(mask, "idxmax") else df[mask].index[0]
            entry_price = float(df.loc[idx, "close"])

            result = {**sig, "entry_price": round(entry_price, 3)}
            for n in TRACK_PERIODS:
                target_idx = idx + n
                if target_idx < len(df):
                    exit_price = float(df.loc[target_idx, "close"])
                    ret = (exit_price - entry_price) / entry_price
                    result[f"return_{n}d"] = round(ret, 4)
                else:
                    result[f"return_{n}d"] = None

            return result
        except Exception as e:
            logger.debug("追踪 {} 失败: {}", symbol, e)
            return {**sig, "error": str(e)}
