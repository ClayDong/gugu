"""Scheduler job handlers mixin.

Extracted from scheduler.py to keep the main scheduler class focused on
orchestration (setup, start, shutdown) while job handlers live here.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta

from gugu.data.stock_names import lookup_stock_name
from gugu.screener.auto_tune import auto_tune_screener_params
from gugu.screener.terminal_screener import TerminalScreener
from gugu.screener.validator import (
    ScreenerRecord,
    save_screener_records,
    validate_yesterday_screener,
)
from gugu.strategies.registry import get_enabled_strategies
from gugu.utils.calendar import is_trading_day
from gugu.utils.log import get_logger

logger = get_logger()

# 优化股票池统一由 gugu.analysis.optimization_pools 维护，避免与 scripts/optimize_params.py 重复定义


class SchedulerJobsMixin:
    """Mixin providing all _safe_* job handlers for TradingScheduler.

    Expects the following attributes on the hosting class:
    - self._engine: TradingEngine instance
    - self._scheduler: AsyncIOScheduler instance
    - self._task_enabled(name) -> bool
    """

    async def _safe_screener(self) -> None:
        """安全执行尾盘选股（非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            screener = TerminalScreener(dm=self._engine.data_manager)
            scan_result = await screener.scan()
            results = scan_result.results if scan_result else []
            total = scan_result.total_scanned if scan_result else 0
            if results:
                today_str = datetime.now().strftime("%Y-%m-%d")
                records = [
                    ScreenerRecord(
                        symbol=r.symbol,
                        name=r.name,
                        close_price=r.price,
                        change_pct=r.change_pct,
                        volume_ratio=r.volume_ratio,
                        screener_date=today_str,
                        passed=r.passed,
                        failed_conditions=r.failed_conditions,
                        stop_loss_price=r.stop_loss_price,
                    )
                    for r in results
                ]
                save_screener_records(records)
            await self._engine.notifier.notify_screener(
                results,
                total,
                data_quality=scan_result.data_quality if scan_result else None,
                status=scan_result.status if scan_result else None,
                data_errors=scan_result.data_errors if scan_result else None,
            )
        except Exception as e:
            logger.exception("尾盘选股异常: {}", e)
            try:
                await self._engine.notifier.notify_screener([], 0)
            except Exception as notify_err:
                logger.error("尾盘选股异常通知发送失败: {}", notify_err)

    async def _safe_validate_screener(self) -> None:
        """安全执行尾盘选股次日验证（非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            results = await validate_yesterday_screener()
            if results:
                win_open = sum(1 for v in results if v.open_premium > 0)
                win_high = sum(1 for v in results if v.intraday_high_premium > 0)
                stop_hit = sum(1 for v in results if v.stop_loss_hit)
                n = len(results)
                logger.info(
                    "📊 尾盘验证日报: "
                    "昨日选股 {} 只 | "
                    "高开 {}/{} ({:.0f}%) | "
                    "日内冲高 {}/{} ({:.0f}%) | "
                    "止损命中 {}/{} ({:.0f}%) | "
                    "平均开盘溢价 {:+.2f}%",
                    n,
                    win_open,
                    n,
                    win_open / n * 100,
                    win_high,
                    n,
                    win_high / n * 100,
                    stop_hit,
                    n,
                    stop_hit / n * 100,
                    sum(v.open_premium for v in results) / n,
                )
                await self._engine.notifier.notify_screener_validation(results)
            else:
                logger.info("尾盘验证: 无结果（昨日无选股记录或行情获取失败）")
                await self._engine.notifier.notify_screener_validation([])
        except Exception as e:
            logger.exception("尾盘选股验证异常: {}", e)
            try:
                await self._engine.notifier.notify_error(
                    {
                        "message": f"尾盘选股验证异常: {e}",
                        "level": "error",
                        "source": "scheduler._safe_validate_screener",
                    }
                )
            except Exception as ne:
                logger.error("尾盘选股验证异常通知发送失败: {}", ne)

    async def _safe_validate_screener_eod(self) -> None:
        """安全执行尾盘选股收盘验证（15:05，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            results = await validate_yesterday_screener(is_eod=True)
            if results:
                n = len(results)
                avg_close = sum(v.close_premium for v in results) / n
                stop_hit = sum(1 for v in results if v.stop_loss_hit)
                logger.info(
                    "📊 尾盘收盘验证: 昨日选股 {} 只 | "
                    "收盘收益 {:+.2f}% | "
                    "止损命中 {}/{} ({:.0f}%)",
                    n,
                    avg_close,
                    stop_hit,
                    n,
                    stop_hit / n * 100,
                )
        except Exception as e:
            logger.exception("尾盘收盘验证异常: {}", e)
            await self._engine.notifier.notify_error(
                {
                    "message": f"尾盘收盘验证异常: {e}",
                    "level": "error",
                    "source": "scheduler._safe_validate_screener_eod",
                }
            )

    async def _safe_auto_tune_screener(self) -> None:
        """安全执行尾盘选股参数自动调优（15:10，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            new_params = auto_tune_screener_params()
            logger.info(
                "🎛️ 尾盘选股自动调优完成: "
                "max_vol_ratio={:.2f} "
                "min_turnover={:.1f} "
                "min_chg_pct={:.1f}",
                new_params.max_vol_ratio,
                new_params.min_turnover,
                new_params.min_chg_pct,
            )
        except Exception as e:
            logger.exception("尾盘选股自动调优异常: {}", e)

    async def _safe_range_watchlist(self) -> None:
        """震荡市盘中观察列表推送。"""
        if not is_trading_day():
            return
        try:
            market_status = await self._engine.market_filter.check_market()
            regime = market_status.get("regime", "unknown")

            if regime == "bull":
                logger.debug("盘中观察: 牛市，跳过观察列表推送")
                return

            candidates = await self._scan_range_candidates()
            if candidates:
                logger.info("盘中观察: 震荡市推送 {} 只接近 MA20 的候选", len(candidates))
                await self._engine.notifier.notify_range_watchlist(candidates, regime)
            else:
                await self._engine.notifier.notify_range_watchlist([], regime)
        except Exception as e:
            logger.exception("震荡市观察列表异常: {}", e)

    async def _scan_range_candidates(self) -> list[dict]:
        """扫描接近 MA20 支撑位的候选股（用于震荡市观察列表）。"""
        candidates: list[dict] = []
        watchlist = self._engine.watchlist[:20]

        async def check_one(symbol: str) -> dict | None:
            try:
                df = await self._engine.data_manager.fetch_stock_history(symbol, days=60)
                if df is None or df.empty or len(df) < 30:
                    return None
                price = float(df["close"].iloc[-1])
                ma20 = float(df["close"].rolling(20).mean().iloc[-1])
                if ma20 <= 0:
                    return None
                distance_pct = (price - ma20) / ma20 * 100
                if abs(distance_pct) > 5.0:
                    return None

                main_net = None
                main_pct = None
                try:
                    flow_df = await self._engine.data_manager.fetch_stock_flow(symbol)
                    if flow_df is not None and not flow_df.empty:
                        latest_flow = flow_df.iloc[-1]
                        main_net = float(latest_flow.get("main_net", 0) or 0)
                        main_pct = float(latest_flow.get("main_pct", 0) or 0)
                except Exception:
                    pass

                if main_net is not None and main_net < -1.0:
                    return None
                if main_pct is not None and main_pct < -5.0:
                    return None

                return {
                    "symbol": symbol,
                    "name": lookup_stock_name(symbol) or symbol,
                    "price": price,
                    "ma20": round(ma20, 2),
                    "distance_pct": round(distance_pct, 2),
                    "main_net": round(main_net, 2) if main_net is not None else None,
                    "main_pct": round(main_pct, 2) if main_pct is not None else None,
                }
            except Exception:
                return None

        results = await asyncio.gather(*[check_one(s) for s in watchlist], return_exceptions=False)
        for r in results:
            if r is not None:
                candidates.append(r)

        candidates.sort(
            key=lambda x: x.get("main_net") if x.get("main_net") is not None else -999,
            reverse=True,
        )
        return candidates

    async def _safe_afternoon_flow(self) -> None:
        """安全执行盘后资金流决策报告（14:30，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.notifier.flow_report import run_afternoon_report

            result = await run_afternoon_report()
            await self._engine.notifier.notify_flow_report("afternoon", result)
            logger.info("盘后资金流决策报告推送完成（14:30）")
        except Exception as e:
            logger.exception("盘后资金流决策报告异常: {}", e)
            try:
                await self._engine.notifier.notify_error(
                    {
                        "message": f"盘后资金流决策报告异常: {e}",
                        "level": "error",
                        "source": "scheduler._safe_afternoon_flow",
                    }
                )
            except Exception as notify_err:
                logger.error("盘后资金流异常通知发送失败: {}", notify_err)

    async def _safe_altalpha_fetch(self) -> None:
        """安全执行另类因子数据盘后批量采集（18:00，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.data.collectors.altalpha.block_trade import BlockTradeCollector
            from gugu.data.collectors.altalpha.dragon_tiger import DragonTigerCollector
            from gugu.data.collectors.altalpha.insider_trading import InsiderTradingCollector
            from gugu.data.collectors.altalpha.margin_trade import MarginTradeCollector
            from gugu.data.collectors.altalpha.share_unlock import ShareUnlockCollector

            collectors = [
                InsiderTradingCollector(),
                DragonTigerCollector(),
                BlockTradeCollector(),
                MarginTradeCollector(),
                ShareUnlockCollector(),
            ]

            tasks = []
            if hasattr(collectors[0], "fetch_recent"):
                tasks.append(collectors[0].fetch_recent())
            if hasattr(collectors[1], "fetch_detail"):
                tasks.append(collectors[1].fetch_detail())
            if hasattr(collectors[2], "fetch_detail"):
                tasks.append(collectors[2].fetch_detail())
            if hasattr(collectors[3], "fetch_detail"):
                tasks.append(collectors[3].fetch_detail())
            if hasattr(collectors[4], "fetch_upcoming"):
                tasks.append(collectors[4].fetch_upcoming())

            results = await asyncio.gather(*tasks, return_exceptions=True)
            success_count = sum(
                1 for r in results if not isinstance(r, Exception) and r is not None
            )
            logger.info("另类因子采集完成: {}/{} 成功", success_count, len(tasks))
        except Exception as e:
            logger.exception("另类因子采集异常: {}", e)

    async def _safe_alpha_summary(self) -> None:
        """安全执行Alpha因子综合评分报告（18:05，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.analysis.alpha_factory import AlphaFactory
            from gugu.analysis.stock_ranker import StockRanker
            from gugu.config import get_config

            cfg = get_config()
            symbols = cfg.watchlist[:20]
            if not symbols:
                return

            factory = AlphaFactory()
            factor_scores = {}
            try:
                for sym in symbols:
                    df = await self._engine.data_manager.fetch_stock_history(sym, days=60)
                    if df is not None and len(df) >= 30:
                        factors = factory.compute(df)
                        if factors:
                            factor_scores[sym] = factors.composite_score
            except Exception:
                pass

            rankings = []
            try:
                ranker = StockRanker()
                rank_results = ranker.rank(symbols, hot_sectors=[], top_n=20)
                for r in rank_results:
                    name = lookup_stock_name(r.get("symbol", ""))
                    rankings.append(
                        {
                            "symbol": r.get("symbol", ""),
                            "name": name or r.get("symbol", ""),
                            "total_score": r.get("total_score", 0),
                        }
                    )
            except Exception:
                pass

            top_factors = []
            if factor_scores:
                factor_names = (
                    list(next(iter(factor_scores.values())).keys()) if factor_scores else []
                )
                for fname in factor_names[:5]:
                    values = [
                        s.get(fname, 0) for s in factor_scores.values() if isinstance(s, dict)
                    ]
                    if values:
                        top_factors.append(
                            {"name": fname, "avg_value": round(sum(values) / len(values), 4)}
                        )
                top_factors.sort(key=lambda x: abs(x["avg_value"]), reverse=True)

            summary = {
                "rankings": rankings,
                "top_factors": top_factors[:5],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }

            await self._engine.notifier.notify_alpha_summary(summary)
            logger.info(
                "因子综合评分报告推送完成: {}只评分, {}个因子", len(rankings), len(top_factors)
            )
        except Exception as e:
            logger.exception("因子综合评分报告异常: {}", e)
            await self._engine.notifier.notify_error(
                {
                    "message": f"因子综合评分报告异常: {e}",
                    "level": "error",
                    "source": "scheduler._safe_alpha_summary",
                }
            )

    async def _safe_portfolio_report(self) -> None:
        """安全执行持仓认知分析报告（15:35，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from pathlib import Path

            live_path = Path("data/live_positions.json")
            if not live_path.exists():
                return
            with open(live_path, encoding="utf-8") as f:
                live_data = json.load(f)
            raw_positions = live_data.get("positions", {})
            if not raw_positions:
                return

            positions = []
            for sym, p in raw_positions.items():
                entry = p.get("avg_cost", 0)
                current = p.get("current_price", 0)
                profit_pct = ((current - entry) / entry * 100) if entry > 0 else 0
                positions.append(
                    {
                        "symbol": sym,
                        "name": p.get("name", sym),
                        "entry_price": entry,
                        "current_price": current,
                        "quantity": p.get("quantity", 0),
                        "market_value": round(current * p.get("quantity", 0), 2),
                        "profit_pct": round(profit_pct, 2),
                    }
                )
            if not positions:
                return

            from gugu.wisdom.advisor import WisdomAdvisor

            advisor = WisdomAdvisor()
            analysis = await advisor.advise_portfolio_async(
                positions=positions,
                market_summary="",
                macro_summary="",
            )
            if analysis:
                await self._engine.notifier.notify_portfolio_report(analysis, positions)
                logger.info("持仓认知分析报告推送完成: {}只持仓", len(positions))
            else:
                logger.info("持仓认知分析报告: LLM 无输出，跳过推送")
        except Exception as e:
            logger.exception("持仓认知分析报告异常: {}", e)
            await self._engine.notifier.notify_error(
                {
                    "message": f"持仓认知分析报告异常: {e}",
                    "level": "error",
                    "source": "scheduler._safe_portfolio_report",
                }
            )

    async def _safe_concept_daily(self) -> None:
        """安全执行每日概念推送（20:00，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.wisdom.tutor import get_daily_concept

            concept = get_daily_concept()
            if not concept:
                logger.info("每日概念: 无可用概念")
                return

            explanation = ""
            citations: list[dict[str, str]] = []
            try:
                from gugu.wisdom.kb_agent import get_kb_agent

                agent = get_kb_agent()
                answer = await agent.ask(concept.get("kb_query", concept["name"]))
                explanation = answer.answer
                from dataclasses import asdict

                citations = [asdict(c) if not isinstance(c, dict) else c for c in answer.citations]
            except Exception as e:
                logger.debug("KB Agent 检索概念解释失败: {}", e)

            await self._engine.notifier.notify_concept(
                concept["name"], concept["definition"], explanation, citations
            )
            logger.info("每日概念推送完成: {}", concept["name"])
        except Exception as e:
            logger.exception("概念推送异常: {}", e)

    async def _safe_sentiment_daily(self) -> None:
        """安全执行情感日报推送（20:30 交易日）。"""
        if not is_trading_day():
            return
        try:
            from gugu.analysis.sentiment import SentimentSystem

            notifier = self._engine.notifier
            system = SentimentSystem(stock_name="自选股池")
            await system.run_and_notify(notifier)
        except Exception as e:
            logger.exception("情感日报异常: {}", e)

    async def _safe_cleanup_cache(self) -> None:
        """安全执行Parquet 缓存清理。"""
        try:
            from gugu.data.cache import persistent_cache

            count = persistent_cache().cleanup_stale(max_age_days=7)
            if count > 0:
                logger.info("Parquet 缓存清理: 删除 {} 个过期文件", count)
        except Exception as e:
            logger.debug("缓存清理异常: {}", e)

    async def _safe_optimize_params(self) -> None:
        """P1-7: 周参数优化（周日 03:00）。

        按策略类型自动匹配股票池，对每个启用策略分别运行 Walk-Forward，
        避免用统一股票池优化不同 alpha 来源的策略。
        """
        try:
            from gugu.analysis.optimization_pools import get_strategy_stock_pool
            from gugu.config import PROJECT_ROOT

            script = PROJECT_ROOT / "scripts" / "optimize_params.py"
            if not script.exists():
                logger.warning("优化脚本不存在: {}", script)
                return

            enabled = [s.name for s in get_enabled_strategies()]
            if not enabled:
                logger.warning("无启用策略，跳过周参数优化")
                return

            logger.info("启动周参数优化: {} 个策略", len(enabled))
            for s_name in enabled:
                stocks = get_strategy_stock_pool(s_name)
                # v0.9.35：改用 sys.executable 替代 "uv run python"。
                # launchd 环境 PATH 不含 uv（实证 2026-08-23 03:00 每周任务
                # [Errno 2] No such file or directory: 'uv' 静默失败），而调度器
                # 本身运行于项目 venv（sys.executable 即 .venv/bin/python），
                # 直接调用即可，无 uv/PATH 依赖。
                cmd = [
                    sys.executable,
                    str(script),
                    "--strategy",
                    s_name,
                    "--multi-stock",
                    stocks,
                    "--days",
                    "500",
                    "--splits",
                    "3",
                    "--write",
                ]
                logger.info("周参数优化 {}: stocks={}", s_name, stocks)

                try:
                    result = subprocess.run(
                        cmd,
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                        timeout=1200,
                    )
                    if result.returncode == 0:
                        logger.info(
                            "周参数优化 {} 完成: stdout={}",
                            s_name,
                            result.stdout[-300:] if result.stdout else "",
                        )
                    else:
                        logger.error(
                            "周参数优化 {} 失败 rc={}: stderr={}",
                            s_name,
                            result.returncode,
                            result.stderr[-300:] if result.stderr else "",
                        )
                except subprocess.TimeoutExpired:
                    logger.error("周参数优化 {} 超时（>20min），跳过", s_name)
        except subprocess.TimeoutExpired:
            logger.error("周参数优化超时（>1h），已终止")
        except Exception as e:
            logger.exception("周参数优化异常: {}", e)

    async def _safe_heuristic_optimize(self) -> None:
        """启发式参数优化（每半月 04:00，GA/PSO 交替）。

        按策略类型匹配对应股票池，每个策略用最适合的股票优化参数。
        """
        try:
            from datetime import datetime

            from gugu.analysis.optimization_pools import get_strategy_stock_pool
            from gugu.config import PROJECT_ROOT

            script = PROJECT_ROOT / "scripts" / "optimize_params.py"
            if not script.exists():
                logger.warning("优化脚本不存在: {}", script)
                return

            algorithm = "ga" if datetime.now().month % 2 == 1 else "pso"
            enabled = [s.name for s in get_enabled_strategies()]

            for s_name in enabled:
                stocks = get_strategy_stock_pool(s_name)
                cmd = [
                    sys.executable,
                    str(script),
                    "--strategy",
                    s_name,
                    "--multi-stock",
                    stocks,
                    "--days",
                    "500",
                    "--algorithm",
                    algorithm,
                    "--population",
                    "30",
                    "--generations",
                    "20",
                ]
                logger.info("启发式优化 {}: strategy={}, stocks={}", algorithm, s_name, stocks)

                try:
                    result = subprocess.run(
                        cmd,
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                        timeout=900,
                    )
                    if result.returncode == 0:
                        logger.info(
                            "启发式优化 {} 完成: stdout={}",
                            s_name,
                            result.stdout[-300:] if result.stdout else "",
                        )
                    else:
                        logger.error(
                            "启发式优化 {} 失败 rc={}: stderr={}",
                            s_name,
                            result.returncode,
                            result.stderr[-300:] if result.stderr else "",
                        )
                except subprocess.TimeoutExpired:
                    logger.error("启发式优化 {} 超时（>15min），跳过", s_name)
        except Exception as e:
            logger.exception("启发式优化异常: {}", e)

    async def _safe_weekly_review(self) -> None:
        """P2-8: 周日复盘推送（周日 20:00）。"""
        try:
            from gugu.config import PROJECT_ROOT

            history_file = PROJECT_ROOT / "data" / "signals_history.jsonl"
            today = datetime.now().date()
            week_ago = today - timedelta(days=7)
            signals: list[dict] = []
            if not history_file.exists():
                logger.info("周复盘: 无信号历史文件，发送空复盘确认")
            else:
                try:
                    with history_file.open("r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                record = json.loads(line)
                                ts_str = record.get("timestamp", record.get("date", ""))
                                if ts_str:
                                    normalized_ts = str(ts_str).replace("Z", "+00:00")
                                    record_date = datetime.fromisoformat(normalized_ts).date()
                                    if record_date >= week_ago:
                                        signals.append(record)
                            except (ValueError, KeyError, TypeError):
                                continue
                except Exception as e:
                    logger.warning("周复盘: 读取信号历史失败，发送空复盘确认: {}", e)

            if not signals:
                logger.info("周复盘: 本周无信号记录，发送空复盘确认")

            total_signals = len(signals)
            buy_signals = sum(
                1 for s in signals if s.get("signal") == 1 or s.get("action") == "buy"
            )
            sell_signals = sum(
                1 for s in signals if s.get("signal") == -1 or s.get("action") == "sell"
            )

            strategy_stats: dict[str, int] = {}
            for s in signals:
                strategy = s.get("strategy", "unknown")
                strategy_stats[strategy] = strategy_stats.get(strategy, 0) + 1

            try:
                sent = await self._engine.notifier.notify_weekly_review(
                    {
                        "period": f"{week_ago.isoformat()} ~ {today.isoformat()}",
                        "total_signals": total_signals,
                        "buy_signals": buy_signals,
                        "sell_signals": sell_signals,
                        "strategy_stats": strategy_stats,
                        "signals": signals[:20],
                    }
                )
                if sent:
                    logger.info(
                        f"周复盘推送完成: 信号 {total_signals} 条 "
                        f"(买 {buy_signals} / 卖 {sell_signals})"
                    )
                else:
                    logger.warning("周复盘通知未发送，结果为 False")
            except AttributeError:
                logger.debug("周复盘: notifier 未实现 notify_weekly_review 方法")
            except Exception as e:
                logger.warning("周复盘推送失败: {}", e)

        except Exception as e:
            logger.exception("周复盘异常: {}", e)

    async def _safe_perspective_report(self) -> None:
        """十大视角分析日报（08:30，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.analysis.perspectives import (
                PerspectiveEngine,
                StockDataContext,
                build_context_from_kline,
            )
            from gugu.config import get_config
            from gugu.notifier.perspective_report import (
                build_batch_summary_card,
                build_perspective_card,
            )

            cfg = get_config()
            if not cfg.perspective.enabled:
                logger.debug("十大视角分析已禁用，跳过")
                return

            watchlist = cfg.watchlist
            if not watchlist:
                logger.debug("自选股为空，跳过十大视角分析")
                return

            engine = PerspectiveEngine(
                perspectives=cfg.perspective.perspectives or None,
            )

            contexts: list[StockDataContext] = []
            for symbol in watchlist[:10]:
                try:
                    df = await self._engine.data_manager.fetch_stock_history(symbol, days=60)
                    if df is not None and len(df) >= 30:
                        name = lookup_stock_name(symbol) or symbol
                        ctx = build_context_from_kline(symbol, name, df)
                        contexts.append(ctx)
                except Exception as e:
                    logger.debug("十大视角: {} 构建上下文失败: {}", symbol, e)

            if not contexts:
                logger.info("十大视角: 无有效上下文，跳过")
                return

            results = engine.analyze_batch(contexts)

            summary_card = build_batch_summary_card(results)
            await self._engine.notifier.send_business_card(summary_card)

            for result in results:
                try:
                    detail_card = build_perspective_card(result)
                    await self._engine.notifier.send_business_card(detail_card)
                    await asyncio.sleep(1.0)
                except Exception as e:
                    logger.warning("十大视角: {} 详细卡片发送失败: {}", result.symbol, e)

            passed_count = sum(1 for r in results if r.passed)
            logger.info(
                "十大视角日报推送完成: 分析 {} 只, 通过 {} 只，已推送 {} 张卡片",
                len(results),
                passed_count,
                len(results) + 1,
            )
        except Exception as e:
            logger.exception("十大视角分析日报异常: {}", e)

    async def _safe_market_temp(self) -> None:
        """市场温度计推送（08:25，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            card = await self._run_market_temp_subprocess()
            if card is None:
                logger.warning("市场温度计子进程未返回卡片，跳过推送")
                return
            await self._engine.notifier.send_business_card(card)
            logger.info("市场温度计推送完成（子进程隔离执行）")
        except Exception as e:
            logger.exception("市场温度计推送异常: {}", e)

    async def _safe_rsi_scan(self) -> None:
        """RSI 专项扫描（13:30，仅扫描 rsi_reversal 策略，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.engine.signal_router import SignalRouter
            from gugu.strategies.registry import get_runtime_strategy_names, get_strategy

            if "rsi_reversal" not in get_runtime_strategy_names():
                logger.info("RSI 专项扫描跳过：rsi_reversal 未通过当前执行模式准入")
                return
            rsi_strategy = get_strategy("rsi_reversal")
            rsi_router = SignalRouter([rsi_strategy])

            original_router = getattr(self._engine, "_router", None)
            self._engine.router = rsi_router
            try:
                signals = await self._engine.scan_signals()
            finally:
                if original_router is not None:
                    self._engine.router = original_router

            if signals:
                logger.info("RSI 专项扫描: 产生 {} 个信号", len(signals))
                for signal in signals:
                    await self._engine.process_signal(signal)
            else:
                logger.info("RSI 专项扫描: 无信号产生")
        except Exception as e:
            logger.exception("RSI 专项扫描异常: {}", e)

    async def _safe_signal_performance(self) -> None:
        """信号绩效验证推送（15:20，非交易日跳过）。"""
        if not is_trading_day():
            return
        try:
            from gugu.analysis.signal_tracker import SignalTracker
            from gugu.notifier.formatter import build_signal_performance_card

            tracker = SignalTracker()
            report = await tracker.generate_report(days=30)
            card = build_signal_performance_card(report)
            await self._engine.notifier.send_business_card(card)

            total = report.get("total_signals", 0)
            forward_hit_rate = report.get("forward_5d_signal_hit_rate", 0)
            logger.info(
                "信号绩效验证推送完成: {} 条信号, 前瞻5日命中率 {:.1%}",
                total,
                forward_hit_rate,
            )
        except Exception as e:
            logger.exception("信号绩效验证推送异常: {}", e)
            try:
                await self._engine.notifier.notify_error(
                    {
                        "message": f"信号绩效验证异常: {e}",
                        "level": "error",
                        "source": "scheduler._safe_signal_performance",
                    }
                )
            except Exception as ne:
                logger.error("信号绩效验证异常通知发送失败: {}", ne)

    async def _safe_signal_daily_validation(self) -> None:
        """信号 T+1 次日验证推送（15:25，非交易日跳过）。

        验证上一交易日买入信号的次日真实可执行收益、盘中回撤、止损命中率。
        """
        if not is_trading_day():
            return
        try:
            from gugu.analysis.signal_tracker import SignalTracker

            tracker = SignalTracker()
            report = await tracker.generate_daily_validation_report()
            await self._engine.notifier.notify_signal_daily_validation(report)

            total = report.get("total_signals", 0)
            valid = report.get("valid_count", 0)
            win_rate = report.get("win_rate", 0)
            logger.info(
                "信号次日验证推送完成: {} 条买入信号, 可追踪 {}, T+1 胜率 {:.1%}",
                total,
                valid,
                win_rate,
            )
        except Exception as e:
            logger.exception("信号次日验证推送异常: {}", e)
            try:
                await self._engine.notifier.notify_error(
                    {
                        "message": f"信号次日验证异常: {e}",
                        "level": "error",
                        "source": "scheduler._safe_signal_daily_validation",
                    }
                )
            except Exception as ne:
                logger.error("信号次日验证异常通知发送失败: {}", ne)

    async def _safe_signal_weekly(self) -> None:
        """信号绩效周报推送（周日 20:30）。"""
        try:
            from gugu.analysis.signal_tracker import SignalTracker

            tracker = SignalTracker()
            report = await tracker.generate_weekly_report()
            sent = await self._engine.notifier.notify_signal_weekly(report)
            if not sent:
                logger.warning("信号绩效周报通知未发送，结果为 False")

            total = report.get("total_signals", 0)
            win_rate = report.get("win_rate", 0)
            logger.info(
                "信号绩效周报推送完成: {} 条买入信号, 5日胜率 {:.1%}",
                total,
                win_rate,
            )
        except Exception as e:
            logger.exception("信号绩效周报推送异常: {}", e)
            try:
                await self._engine.notifier.notify_error(
                    {
                        "message": f"信号绩效周报异常: {e}",
                        "level": "error",
                        "source": "scheduler._safe_signal_weekly",
                    }
                )
            except Exception as ne:
                logger.error("信号绩效周报异常通知发送失败: {}", ne)

    async def _run_market_temp_subprocess(self) -> dict | None:
        """在子进程中执行市场温度计计算，返回飞书卡片 dict。"""
        script = (
            "import asyncio, json, sys\n"
            "from gugu.analysis.market_temp import calc_market_temp, build_market_temp_card\n"
            "async def main():\n"
            "    result = await calc_market_temp(use_cache=False)\n"
            "    if result.temperature is None:\n"
            "        print(json.dumps({'error': 'no_data'}))\n"
            "        return\n"
            "    card = build_market_temp_card(result)\n"
            "    print(json.dumps(card, ensure_ascii=False, default=str))\n"
            "asyncio.run(main())\n"
        )
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0:
                logger.error(
                    "市场温度计子进程 crash (exit={}): {}", proc.returncode, stderr.decode()[-500:]
                )
                return None
            data = json.loads(stdout.decode().strip())
            if isinstance(data, dict) and data.get("error"):
                logger.warning("市场温度计子进程返回错误: {}", data["error"])
                return None
            return data
        except TimeoutError:
            logger.error("市场温度计子进程超时(180s)")
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return None
        except Exception as e:
            logger.exception("市场温度计子进程异常: {}", e)
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return None
