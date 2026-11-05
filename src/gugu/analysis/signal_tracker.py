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
from gugu.utils.atomic import atomic_write_text
from gugu.utils.calendar import last_trading_day
from gugu.utils.evidence_guard import guard_evidence_path
from gugu.utils.log import get_logger

logger = get_logger()

# 追踪周期（交易日）
# 20/60 日为中长期验证：短周期验证信号即时性，长周期验证信号是否具备中期趋势价值
TRACK_PERIODS = [1, 3, 5, 10, 20, 60]
# 最小样本数（低于此数不生成报告）
MIN_SAMPLE_SIZE = 3
# 各周期独立最小样本数（不足标记 insufficient，不阻塞其他周期）
PERIOD_MIN_SAMPLE = 3
# 牛熊分层基准指数（沪深300）
REGIME_INDEX = "000300"
# 牛熊判定均线窗口
REGIME_MA_WINDOW = 60


def _normalize_timestamp(value: Any) -> pd.Timestamp | None:
    """Parse a history timestamp into a timezone-naive value or return None."""
    try:
        timestamp = pd.to_datetime(value, errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    if timestamp is None or pd.isna(timestamp):
        return None
    if getattr(timestamp, "tzinfo", None) is not None:
        timestamp = timestamp.tz_localize(None)
    return pd.Timestamp(timestamp)


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
            logger.warning("信号历史文件不存在: {}", self._history_path)
            return []

        records: list[dict[str, Any]] = []
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=days)) if days > 0 else None

        try:
            with self._history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if not isinstance(rec, dict):
                            continue
                        ts = _normalize_timestamp(rec.get("timestamp"))
                        if ts is None:
                            logger.debug("跳过无效时间戳: {}", rec.get("timestamp"))
                            continue
                        if cutoff is None or ts >= cutoff:
                            records.append(rec)
                    except (json.JSONDecodeError, TypeError, ValueError) as e:
                        logger.debug("跳过无效行: {}", e)
                        continue
        except Exception as e:
            logger.error("读取信号历史失败: {}", e)
            return []

        logger.info("加载 {} 条信号记录（最近 {} 天）", len(records), days)
        return records

    @staticmethod
    def _record_key(record: dict[str, Any]) -> str:
        """生成历史记录的稳定键；兼容旧记录没有 signal_id 的情况。"""
        signal_id = str(record.get("signal_id") or record.get("execution_id") or "")
        if signal_id:
            return signal_id
        ts = _normalize_timestamp(record.get("timestamp"))
        if ts is None:
            return ""
        return f"{ts.date().isoformat()}_{record.get('symbol', '')}_{record.get('direction', '')}"

    def _apply_tracked_updates(
        self, records: list[dict[str, Any]], tracked: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把追踪收益原子地写回原始历史记录。"""
        tracked_map: dict[str, dict[str, Any]] = {}
        for tracked_record in tracked:
            key = self._record_key(tracked_record)
            if key:
                tracked_map[key] = tracked_record

        updated: list[dict[str, Any]] = []
        changed = 0
        for record in records:
            key = self._record_key(record)
            if key and key in tracked_map:
                tracked_record = tracked_map[key]
                merged = {**record, **{k: v for k, v in tracked_record.items() if k != "error"}}
                if merged != record:
                    changed += 1
                updated.append(merged)
            else:
                updated.append(record)
        if changed:
            logger.info("信号历史回填 {} 条追踪收益", changed)
        return updated

    def _write_history(self, records: list[dict[str, Any]]) -> None:
        """以原子写方式更新信号历史，防止回填过程中断留下半截数据。

        2026-11-05：本方法是测试夹具污染生产证据文件的实际通道——
        ``self._history_path`` 在 ``__init__`` 时由导入期绑定的 ``PROJECT_ROOT``
        算出，conftest patch ``gugu.config.PROJECT_ROOT`` 影响不到它，
        于是测试里回填的前瞻收益被直接写进了仓库 ``data/signals_history.jsonl``
        （实测残留 3 条 symbol 000001-000003 的合成信号）。写入前统一过
        evidence_guard，测试模式下自动重定向到临时目录。
        """
        path = guard_evidence_path(self._history_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            for record in records
        )
        atomic_write_text(path, payload)

    async def generate_report(self, days: int = 30) -> dict[str, Any]:
        """生成信号绩效报告。

        Args:
            days: 分析最近 N 天的信号

        Returns:
            报告字典，包含：
            - total_signals: 信号总数
            - buy_count/sell_count: 买入/卖出信号数
            - executed_count: 实际下单数
            - forward_5d_signal_hit_rate: 前瞻 5 日信号命中率
            - avg_return_5d: 前瞻 5 日平均收益
            - closed_trade_win_rate: 已平仓交易胜率
            - realized_pnl: 已实现盈亏
            - by_strategy: 按策略分组的绩效
            - recent_signals: 最近 5 条信号及追踪结果
        """
        records = self.load_history(days)
        # 去重后再判断样本门槛；多个退出事件必须保留独立 event/signal ID。
        records = self._dedupe(records)
        if len(records) < MIN_SAMPLE_SIZE:
            return {
                "total_signals": len(records),
                "message": f"样本数不足（{len(records)}<{MIN_SAMPLE_SIZE}），无法生成报告",
            }

        buy_signals = [r for r in records if r.get("direction") == "buy"]
        sell_signals = [r for r in records if r.get("direction") == "sell"]
        executed = [r for r in records if r.get("order_success")]

        # 追踪买入信号收益（卖出信号收益需要持仓数据，暂不追踪）
        tracked = await self._track_signals(buy_signals)
        if tracked:
            try:
                updated_records = self._apply_tracked_updates(records, tracked)
                if updated_records != records:
                    self._write_history(updated_records)
                    logger.info("已回填 {} 条信号的前瞻收益", len(tracked))
            except Exception as exc:
                logger.warning("信号收益回填失败: {}", exc)

        # 按策略分组统计
        by_strategy = self._stats_by_strategy(tracked)

        # 按周期独立统计（20/60 日样本少，单独标注 insufficient）
        by_period = self._stats_by_period(tracked)

        # 按牛熊分层统计（沪深300 相对 MA60）
        by_regime = await self._stats_by_regime(tracked)

        # 最近 5 条信号
        recent = tracked[-5:] if len(tracked) >= 5 else tracked

        # 整体前瞻命中率：仅衡量买入信号后 5 日收益，不代表已实现交易胜率
        valid = [t for t in tracked if t.get("return_5d") is not None]
        wins = [t for t in valid if t["return_5d"] > 0]
        forward_5d_signal_hit_rate = len(wins) / len(valid) if valid else 0
        closed_trades = [t for t in records if t.get("outcome_status") == "closed"]
        realized_pnls: list[float] = []
        for trade in closed_trades:
            pnl = trade.get("realized_pnl")
            if pnl is None:
                pnl = (trade.get("outcome_metrics") or {}).get("realized_pnl")
            try:
                realized_pnls.append(float(pnl))
            except (TypeError, ValueError):
                continue
        closed_trade_wins = [pnl for pnl in realized_pnls if pnl > 0]
        closed_trade_win_rate = len(closed_trade_wins) / len(realized_pnls) if realized_pnls else 0
        realized_pnl = sum(realized_pnls) if realized_pnls else 0

        return {
            "period_days": days,
            "total_signals": len(records),
            "buy_count": len(buy_signals),
            "sell_count": len(sell_signals),
            "execution_success": len(executed),
            "executed_count": len(executed),
            "tracked_count": len(valid),
            "forward_5d_signal_hit_rate": round(forward_5d_signal_hit_rate, 4),
            "closed_trade_count": len(realized_pnls),
            "closed_trade_win_rate": round(closed_trade_win_rate, 4),
            "realized_pnl": round(realized_pnl, 2),
            "by_strategy": by_strategy,
            "by_period": by_period,
            "by_regime": by_regime,
            "recent_signals": recent,
            "generated_at": datetime.now().isoformat(),
        }

    def _dedupe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按事件 ID 去重；旧记录退回同日同股同方向兼容键。"""
        seen: dict[str, dict[str, Any]] = {}
        for r in records:
            event_id = str(r.get("signal_id") or r.get("execution_id") or "")
            if not event_id:
                timestamp = _normalize_timestamp(r.get("timestamp"))
                if timestamp is None:
                    continue
                event_id = (
                    f"{timestamp.date().isoformat()}_{r.get('symbol', '')}_{r.get('direction', '')}"
                )
            seen[event_id] = r
        return list(seen.values())

    async def _track_signals(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                logger.debug("追踪信号失败: {}", r)

        return results

    async def _track_one(self, sig: dict[str, Any]) -> dict[str, Any]:
        """追踪单条信号的收益。"""
        symbol = sig.get("symbol", "")
        ts = sig.get("timestamp", "")

        try:
            signal_date = pd.to_datetime(ts)
        except Exception:
            return {**sig, "error": "invalid_timestamp"}

        # 信号产生距今的天数
        days_since = (datetime.now() - signal_date.to_pydatetime()).days
        if days_since < 1:
            return {**sig, "error": "too_recent"}

        try:
            # 拉取信号日到现在的 K 线（需覆盖最长 60 交易日 ≈ 84 自然日 + 信号日距今 + 缓冲）
            fetch_days = max(days_since + 95, 120)
            df = await self._dm.fetch_stock_history(symbol, days=fetch_days)
            if df.empty or len(df) < 2:
                return {**sig, "error": "no_data"}

            # 按交易日排序并使用位置索引，避免非连续索引/乱序行情取错日期。
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            mask = df["date"] >= signal_date
            if not mask.any():
                return {**sig, "error": "date_not_found"}

            idx = int(mask.to_numpy().nonzero()[0][0])

            # 优先使用实际成交价，否则用信号日收盘价
            order_result = sig.get("order_result")
            if order_result and order_result.get("success"):
                entry_price = float(order_result["price"])
                price_source = "fill"
                logger.info(
                    "使用实际成交价 {} 追踪 {} 信号绩效（信号日: {}）",
                    entry_price,
                    symbol,
                    signal_date.date(),
                )
            else:
                entry_price = float(df.iloc[idx]["close"])
                price_source = "close"

            # 计算各周期收益
            result = {
                **sig,
                "entry_price": round(entry_price, 3),
                "price_source": price_source,
            }
            for n in TRACK_PERIODS:
                target_idx = idx + n
                if target_idx < len(df):
                    exit_price = float(df.iloc[target_idx]["close"])
                    ret = (exit_price - entry_price) / entry_price
                    result[f"return_{n}d"] = round(ret, 4)
                    result[f"close_{n}d"] = round(exit_price, 3)
                else:
                    result[f"return_{n}d"] = None

            return result
        except Exception as e:
            logger.debug("追踪 {} 失败: {}", symbol, e)
            return {**sig, "error": str(e)}

    def _stats_by_strategy(self, tracked: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            result.append(
                {
                    "strategy": s,
                    "count": len(returns),
                    "win_rate": round(len(wins) / len(returns), 4),
                    "avg_return": round(sum(returns) / len(returns), 4),
                    "best_return": round(max(returns), 4),
                    "worst_return": round(min(returns), 4),
                }
            )

        result.sort(key=lambda x: x["avg_return"], reverse=True)
        return result

    def _stats_by_period(self, tracked: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按追踪周期独立统计绩效。

        20/60 日样本天然少于短周期（需要信号发出足够久），
        样本不足的周期标记 insufficient，不阻塞其他周期统计。
        """
        result = []
        for n in TRACK_PERIODS:
            field = f"return_{n}d"
            returns = [t[field] for t in tracked if t.get(field) is not None]
            entry: dict[str, Any] = {
                "period": n,
                "count": len(returns),
            }
            if len(returns) < PERIOD_MIN_SAMPLE:
                entry["status"] = "insufficient"
                entry["win_rate"] = None
                entry["avg_return"] = None
            else:
                wins = [r for r in returns if r > 0]
                entry["status"] = "ok"
                entry["win_rate"] = round(len(wins) / len(returns), 4)
                entry["avg_return"] = round(sum(returns) / len(returns), 4)
            result.append(entry)
        return result

    async def _stats_by_regime(self, tracked: list[dict[str, Any]]) -> dict[str, Any]:
        """按信号发出时的市场状态（牛/熊/震荡）分层统计。

        用沪深300（000300）历史 K 线判定每个信号日的市场状态：
        - close > MA60 × (1 + 1%) → bull
        - close < MA60 × (1 - 1%) → bear
        - 其余 → sideways
        指数数据不可用 → 全部标记 unknown，不阻塞报告。
        """
        # 收集需要判定的信号日期
        signal_dates: set[str] = set()
        for t in tracked:
            ts = t.get("timestamp", "")
            try:
                signal_dates.add(pd.to_datetime(ts).date().isoformat())
            except Exception:
                continue
        if not signal_dates:
            return {}

        # 拉取沪深300 K 线（覆盖最早信号日往前 REGIME_MA_WINDOW 天 + 缓冲）
        earliest = min(signal_dates)
        try:
            earliest_dt = date.fromisoformat(earliest)
        except ValueError:
            earliest_dt = date.today()
        fetch_days = max((datetime.now().date() - earliest_dt).days + REGIME_MA_WINDOW + 20, 120)
        try:
            df = await self._dm.fetch_stock_history(REGIME_INDEX, days=fetch_days)
        except Exception as e:
            logger.debug("拉取牛熊基准指数 {} 失败: {}", REGIME_INDEX, e)
            df = pd.DataFrame()

        if df.empty or len(df) < REGIME_MA_WINDOW:
            # 指数数据不可用：全部标记 unknown
            return {"unknown": {"count": len(tracked), "status": "insufficient"}}

        # 计算 MA60
        df = df.sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        df["ma60"] = df["close"].rolling(REGIME_MA_WINDOW).mean()

        # 每个信号日 → 最近一个交易日 → 市场状态
        regime_of_date: dict[str, str] = {}
        index_dates = df["date"].dt.date.tolist()
        for d in signal_dates:
            try:
                d_date = date.fromisoformat(d)
            except ValueError:
                continue
            # 找 <= d 的最近交易日
            eligible = [x for x in index_dates if x <= d_date]
            if not eligible:
                continue
            row = df[df["date"].dt.date == max(eligible)].iloc[0]
            close = float(row["close"])
            ma60 = float(row["ma60"])
            if pd.isna(ma60):
                continue
            if close > ma60 * 1.01:
                regime_of_date[d] = "bull"
            elif close < ma60 * 0.99:
                regime_of_date[d] = "bear"
            else:
                regime_of_date[d] = "sideways"

        # 分层统计（用 return_5d 作为统一比较口径）
        buckets: dict[str, list[float]] = {}
        for t in tracked:
            ret = t.get("return_5d")
            if ret is None:
                continue
            ts = t.get("timestamp", "")
            try:
                d = pd.to_datetime(ts).date().isoformat()
            except Exception:
                continue
            regime = regime_of_date.get(d, "unknown")
            buckets.setdefault(regime, []).append(ret)

        result: dict[str, Any] = {}
        for regime, returns in buckets.items():
            entry: dict[str, Any] = {
                "count": len(returns),
                "status": "ok" if len(returns) >= PERIOD_MIN_SAMPLE else "insufficient",
            }
            if entry["status"] == "ok":
                wins = [r for r in returns if r > 0]
                entry["win_rate"] = round(len(wins) / len(returns), 4)
                entry["avg_return"] = round(sum(returns) / len(returns), 4)
            else:
                entry["win_rate"] = None
                entry["avg_return"] = None
            result[regime] = entry
        return result

    async def generate_daily_validation_report(
        self, target_date: date | str | None = None
    ) -> dict[str, Any]:
        """生成信号 T+1 次日验证报告。

        对上一交易日（或指定日期）发出的买入信号，计算次日真实可执行收益：
        - T+1 收盘价收益（相对信号入场价）
        - 盘中最低回撤（从开盘价到最低价）
        - 止损命中率（最低价触及信号止损价）

        Args:
            target_date: 指定信号日期，默认上一交易日。

        Returns:
            报告字典，含 overall / by_strategy / signals 三个维度。
        """
        if target_date is None:
            target_date = last_trading_day()
        if isinstance(target_date, str):
            target_date = date.fromisoformat(target_date)

        target_str = target_date.isoformat()
        records = self._load_signals_by_date(target_date)
        buy_signals = [r for r in records if r.get("direction") == "buy"]

        if len(buy_signals) < MIN_SAMPLE_SIZE:
            return {
                "date": target_str,
                "total_signals": len(buy_signals),
                "message": f"{target_str} 买入信号不足（{len(buy_signals)}<{MIN_SAMPLE_SIZE}），无法生成次日验证",
            }

        validated = await self._validate_t1_signals(buy_signals)
        valid = [v for v in validated if not v.get("error")]

        if len(valid) < MIN_SAMPLE_SIZE:
            return {
                "date": target_str,
                "total_signals": len(buy_signals),
                "valid_count": len(valid),
                "message": f"{target_str} 可追踪信号不足（{len(valid)}<{MIN_SAMPLE_SIZE}）",
            }

        close_returns = [v["close_return"] for v in valid]
        drawdowns = [v["max_drawdown"] for v in valid]
        stop_hits = [v["stop_loss_hit"] for v in valid if v.get("stop_loss_price", 0) > 0]

        win_count = sum(1 for r in close_returns if r > 0)
        avg_return = sum(close_returns) / len(close_returns)
        avg_drawdown = sum(drawdowns) / len(drawdowns) if drawdowns else 0
        stop_hit_rate = sum(stop_hits) / len(stop_hits) if stop_hits else 0

        by_strategy = self._stats_by_strategy_for_t1(valid)

        return {
            "date": target_str,
            "total_signals": len(buy_signals),
            "valid_count": len(valid),
            "win_rate": round(win_count / len(valid), 4),
            "avg_close_return": round(avg_return, 4),
            "avg_max_drawdown": round(avg_drawdown, 4),
            "stop_hit_rate": round(stop_hit_rate, 4),
            "by_strategy": by_strategy,
            "signals": valid,
            "generated_at": datetime.now().isoformat(),
        }

    async def generate_weekly_report(self) -> dict[str, Any]:
        """生成最近 7 日信号绩效周报。

        聚合近 7 日所有买入信号的前瞻收益、胜率、策略贡献，
        并分日统计信号密度，帮助用户感知策略系统运行状态。
        """
        records = self.load_history(days=7)
        buy_signals = self._dedupe([r for r in records if r.get("direction") == "buy"])

        if len(buy_signals) < MIN_SAMPLE_SIZE:
            return {
                "period": "近7日",
                "total_signals": len(buy_signals),
                "message": f"近7日买入信号不足（{len(buy_signals)}<{MIN_SAMPLE_SIZE}），暂无周报",
            }

        tracked = await self._track_signals(buy_signals)
        valid = [t for t in tracked if t.get("return_5d") is not None]

        if len(valid) < MIN_SAMPLE_SIZE:
            return {
                "period": "近7日",
                "total_signals": len(buy_signals),
                "valid_count": len(valid),
                "message": f"近7日可追踪信号不足（{len(valid)}<{MIN_SAMPLE_SIZE}），暂无周报",
            }

        returns = [t["return_5d"] for t in valid]
        wins = [r for r in returns if r > 0]
        by_strategy = self._stats_by_strategy(valid)

        # 分日统计
        daily_stats: dict[str, dict[str, Any]] = {}
        for t in valid:
            ts = t.get("timestamp", "")
            try:
                d = pd.to_datetime(ts).date().isoformat()
            except Exception:
                continue
            if d not in daily_stats:
                daily_stats[d] = {"count": 0, "wins": 0, "returns": []}
            daily_stats[d]["count"] += 1
            ret = t["return_5d"]
            daily_stats[d]["returns"].append(ret)
            if ret > 0:
                daily_stats[d]["wins"] += 1

        daily_summary = []
        for d, s in sorted(daily_stats.items()):
            rets = s["returns"]
            daily_summary.append(
                {
                    "date": d,
                    "count": s["count"],
                    "win_rate": round(s["wins"] / len(rets), 4) if rets else 0,
                    "avg_return": round(sum(rets) / len(rets), 4) if rets else 0,
                }
            )

        return {
            "period": "近7日",
            "start_date": (date.today() - timedelta(days=6)).isoformat(),
            "end_date": date.today().isoformat(),
            "total_signals": len(buy_signals),
            "valid_count": len(valid),
            "win_rate": round(len(wins) / len(valid), 4),
            "avg_return_5d": round(sum(returns) / len(returns), 4),
            "best_return": round(max(returns), 4),
            "worst_return": round(min(returns), 4),
            "by_strategy": by_strategy,
            "daily_summary": daily_summary,
            "generated_at": datetime.now().isoformat(),
        }

    def _load_signals_by_date(self, target_date: date) -> list[dict[str, Any]]:
        """加载指定日期的所有信号。"""
        if not self._history_path.exists():
            return []
        target_str = target_date.isoformat()
        records: list[dict[str, Any]] = []
        try:
            with self._history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = rec.get("timestamp", "")
                        if ts.startswith(target_str):
                            records.append(rec)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception as e:
            logger.error("读取信号历史失败: {}", e)
        return records

    async def _validate_t1_signals(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """批量计算信号的 T+1 次日验证指标。"""
        results: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(5)

        async def validate_one(sig: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await self._validate_t1_one(sig)

        done = await asyncio.gather(*[validate_one(s) for s in signals], return_exceptions=True)
        for r in done:
            if isinstance(r, dict):
                results.append(r)
            elif isinstance(r, Exception):
                logger.debug("T+1 验证失败: {}", r)
        return results

    async def _validate_t1_one(self, sig: dict[str, Any]) -> dict[str, Any]:
        """计算单条信号的 T+1 指标。"""
        symbol = sig.get("symbol", "")
        ts = sig.get("timestamp", "")
        try:
            signal_date = pd.to_datetime(ts)
        except Exception:
            return {**sig, "error": "invalid_timestamp"}

        try:
            # 拉取包含信号日及之后若干交易日的 K 线
            df = await self._dm.fetch_stock_history(symbol, days=30)
            if df.empty or len(df) < 2:
                return {**sig, "error": "no_data"}

            df["date"] = pd.to_datetime(df["date"])
            signal_date_norm = pd.Timestamp(signal_date.date())
            mask = df["date"] == signal_date_norm
            if not mask.any():
                # 信号日期可能非交易日，找最近的前一交易日
                mask = df["date"] <= signal_date_norm
                if not mask.any():
                    return {**sig, "error": "date_not_found"}

            idx = df[mask].index[-1]
            next_idx = idx + 1
            if next_idx >= len(df):
                return {**sig, "error": "no_next_day"}

            entry_price = self._resolve_entry_price(sig, df, idx)
            next_row = df.iloc[next_idx]
            next_open = float(next_row["open"])
            next_high = float(next_row["high"])
            next_low = float(next_row["low"])
            next_close = float(next_row["close"])

            stop_loss = float(sig.get("stop_loss_price", 0) or 0)
            stop_hit = stop_loss > 0 and next_low <= stop_loss

            return {
                **sig,
                "entry_price": round(entry_price, 3),
                "next_date": next_row["date"].strftime("%Y-%m-%d"),
                "open_return": round((next_open - entry_price) / entry_price, 4),
                "high_return": round((next_high - entry_price) / entry_price, 4),
                "close_return": round((next_close - entry_price) / entry_price, 4),
                "max_drawdown": round((next_low - next_open) / next_open, 4),
                "stop_loss_price": stop_loss,
                "stop_loss_hit": stop_hit,
            }
        except Exception as e:
            logger.debug("T+1 验证 {} 失败: {}", symbol, e)
            return {**sig, "error": str(e)}

    def _resolve_entry_price(self, sig: dict[str, Any], df: pd.DataFrame, idx: int) -> float:
        """优先使用实际成交价，否则使用信号日收盘价。"""
        order_result = sig.get("order_result")
        if order_result and order_result.get("success"):
            return float(order_result["price"])
        return float(df.loc[idx, "close"])

    def _stats_by_strategy_for_t1(self, validated: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按策略分组统计 T+1 验证结果。"""
        strategy_stats: dict[str, list[float]] = {}
        for v in validated:
            strategies = v.get("strategies", [])
            if isinstance(strategies, str):
                strategies = [strategies]
            ret = v.get("close_return")
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
            result.append(
                {
                    "strategy": s,
                    "count": len(returns),
                    "win_rate": round(len(wins) / len(returns), 4),
                    "avg_return": round(sum(returns) / len(returns), 4),
                    "best_return": round(max(returns), 4),
                    "worst_return": round(min(returns), 4),
                }
            )
        result.sort(key=lambda x: x["avg_return"], reverse=True)
        return result
