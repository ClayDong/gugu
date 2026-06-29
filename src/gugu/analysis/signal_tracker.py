"""信号绩效追踪器：验证策略有效性。

核心功能：
1. 读取 signals_history.jsonl 历史信号
2. 追踪信号产生后 N 日的实际收益
3. 生成命中率/收益率/策略对比报告
4. 定期推送飞书验证报告

这是项目"验证"环节的关键：信号发出后，到底赚没赚钱？
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from gugu.config import PROJECT_ROOT
from gugu.data import data_manager
from gugu.utils.log import get_logger

logger = get_logger()

# 追踪周期（天）
TRACK_PERIODS = [1, 3, 5, 10]
# 最小样本数（低于此数不生成报告）
MIN_SAMPLE_SIZE = 3


class SignalTracker:
    """信号绩效追踪器。

    读取 signals_history.jsonl，对每条历史信号：
    - 拉取信号产生后的 K 线数据
    - 计算持有 N 日后的实际收益
    - 汇总命中率、平均收益、策略对比

    用法：
        tracker = SignalTracker()
        report = await tracker.generate_report(days=30)  # 分析最近30天信号
        await notifier.notify_signal_performance(report)
    """

    def __init__(self) -> None:
        self._dm = data_manager()
        self._history_path: Path = PROJECT_ROOT / "data" / "signals_history.jsonl"

    def load_history(self, days: int = 30) -> list[dict[str, Any]]:
        """加载最近 N 天的信号历史。

        Args:
            days: 回溯天数（0=全部）

        Returns:
            信号记录列表
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

        logger.info(f"加载 {len(records)} 条信号记录（最近 {days} 天）")
        return records

    async def generate_report(self, days: int = 30) -> dict[str, Any]:
        """生成信号绩效报告。

        Args:
            days: 分析最近 N 天的信号

        Returns:
            报告字典，包含：
            - total_signals: 信号总数
            - buy_count/sell_count: 买入/卖出信号数
            - executed_count: 实际下单数
            - win_rate: 命中率（按 5 日收益判断）
            - avg_return_5d: 5 日平均收益
            - by_strategy: 按策略分组的绩效
            - recent_signals: 最近 5 条信号及追踪结果
        """
        records = self.load_history(days)
        if len(records) < MIN_SAMPLE_SIZE:
            return {
                "total_signals": len(records),
                "message": f"样本数不足（{len(records)}<{MIN_SAMPLE_SIZE}），无法生成报告",
            }

        # 去重：同一股票同一天同方向的信号只取最后一条
        records = self._dedupe(records)

        buy_signals = [r for r in records if r.get("direction") == "buy"]
        sell_signals = [r for r in records if r.get("direction") == "sell"]
        executed = [r for r in records if r.get("order_success")]

        # 追踪买入信号收益（卖出信号收益需要持仓数据，暂不追踪）
        tracked = await self._track_signals(buy_signals)

        # 按策略分组统计
        by_strategy = self._stats_by_strategy(tracked)

        # 最近 5 条信号
        recent = tracked[-5:] if len(tracked) >= 5 else tracked

        # 整体命中率
        valid = [t for t in tracked if t.get("return_5d") is not None]
        wins = [t for t in valid if t["return_5d"] > 0]
        win_rate = len(wins) / len(valid) if valid else 0
        avg_return = sum(t["return_5d"] for t in valid) / len(valid) if valid else 0

        return {
            "period_days": days,
            "total_signals": len(records),
            "buy_count": len(buy_signals),
            "sell_count": len(sell_signals),
            "executed_count": len(executed),
            "tracked_count": len(valid),
            "win_rate": round(win_rate, 4),
            "avg_return_5d": round(avg_return, 4),
            "by_strategy": by_strategy,
            "recent_signals": recent,
            "generated_at": datetime.now().isoformat(),
        }

    def _dedupe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """去重：同一股票同一天同方向只保留最后一条。"""
        seen: dict[str, dict[str, Any]] = {}
        for r in records:
            ts = r.get("timestamp", "")
            symbol = r.get("symbol", "")
            direction = r.get("direction", "")
            # 按日期+股票+方向去重
            try:
                d = pd.to_datetime(ts).date().isoformat()
            except Exception:
                continue
            key = f"{d}_{symbol}_{direction}"
            seen[key] = r  # 后出现的覆盖前面的
        return list(seen.values())

    async def _track_signals(
        self, signals: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """追踪每条买入信号的实际收益。

        对每条信号拉取 K 线，计算持有 1/3/5/10 日后的收益。
        """
        results: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(5)  # 并发限制

        async def track_one(sig: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await self._track_one(sig)

        tasks = [track_one(s) for s in signals]
        done = await asyncio.gather(*tasks, return_exceptions=True)
        for r in done:
            if isinstance(r, dict):
                results.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"追踪信号失败: {r}")

        return results

    async def _track_one(self, sig: dict[str, Any]) -> dict[str, Any]:
        """追踪单条信号的收益。"""
        symbol = sig.get("symbol", "")
        ts = sig.get("timestamp", "")
        signal_price = sig.get("price", 0) or 0

        try:
            signal_date = pd.to_datetime(ts)
        except Exception:
            return {**sig, "error": "invalid_timestamp"}

        # 信号产生距今的天数
        days_since = (datetime.now() - signal_date.to_pydatetime()).days
        if days_since < 1:
            return {**sig, "error": "too_recent"}

        try:
            # 拉取信号日到现在的 K 线
            df = await self._dm.fetch_stock_history(symbol, days=max(days_since + 10, 30))
            if df.empty or len(df) < 2:
                return {**sig, "error": "no_data"}

            # 找到信号日对应的 K 线索引
            df["date"] = pd.to_datetime(df["date"])
            mask = df["date"] >= signal_date
            if not mask.any():
                return {**sig, "error": "date_not_found"}

            idx = df[mask].index[0]
            entry_price = float(df.loc[idx, "close"])

            # 计算各周期收益
            result = {**sig, "entry_price": round(entry_price, 3)}
            for n in TRACK_PERIODS:
                target_idx = idx + n
                if target_idx < len(df):
                    exit_price = float(df.loc[target_idx, "close"])
                    ret = (exit_price - entry_price) / entry_price
                    result[f"return_{n}d"] = round(ret, 4)
                    result[f"close_{n}d"] = round(exit_price, 3)
                else:
                    result[f"return_{n}d"] = None

            return result
        except Exception as e:
            logger.debug(f"追踪 {symbol} 失败: {e}")
            return {**sig, "error": str(e)}

    def _stats_by_strategy(
        self, tracked: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """按策略分组统计绩效。"""
        strategy_stats: dict[str, list[float]] = {}

        for t in tracked:
            strategies = t.get("strategies", [])
            if isinstance(strategies, str):
                strategies = [strategies]
            ret = t.get("return_5d")
            if ret is None:
                continue

            for s in strategies:
                if s not in strategy_stats:
                    strategy_stats[s] = []
                strategy_stats[s].append(ret)

        result = []
        for s, returns in strategy_stats.items():
            if not returns:
                continue
            wins = [r for r in returns if r > 0]
            result.append({
                "strategy": s,
                "count": len(returns),
                "win_rate": round(len(wins) / len(returns), 4),
                "avg_return": round(sum(returns) / len(returns), 4),
                "best_return": round(max(returns), 4),
                "worst_return": round(min(returns), 4),
            })

        result.sort(key=lambda x: x["avg_return"], reverse=True)
        return result
