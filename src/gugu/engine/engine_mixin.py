"""TradingEngine signal processing, dedup, and utility methods.

Extracted from engine/main.py to keep the core engine class focused on
orchestration while signal processing lives in this mixin.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from gugu.analysis.signal_tracker import SignalTracker
from gugu.config import PROJECT_ROOT
from gugu.engine.signal_pipeline import record_signal_history
from gugu.utils.atomic import append_jsonl, atomic_write_json, atomic_write_text
from gugu.utils.log import get_logger

logger = get_logger()


class EngineOpsMixin:
    """Mixin providing signal processing, dedup, and utility methods.

    Expects the following attributes on the hosting class:
    - self._broker: Broker instance (PaperBroker or LivePositionTracker)
    - self._dm: DataManager instance
    - self._notifier: FeishuNotifier instance
    - self._risk: RiskManager instance
    - self._pipeline: SignalPipeline instance
    - self._watchlist: list[str]
    - self._exec_mode: str
    - self._dedup_lock: asyncio.Lock
    - self._notified_signals: dict[str, str]
    - self._notified_signals_file: Path
    - self._filtered_signals_today: list[dict]
    - self._stop_loss_escalator: Any | None
    - self._regime_detector: MultiPeriodRegimeDetector
    - self._market_filter: MarketFilter
    - self._strategies: list
    - self._position_controller: PositionController
    - self._router: SignalRouter
    - self._event_engine: EventEngine
    - self._order_manager: OrderManager
    - self._should_alert(key: str) -> bool (method)
    - self._alert_history: dict[str, float] (class attribute)
    """

    # v0.7.5 P0-10修复：策略级去重配置
    _DEDUP_CONFIG: dict[str, dict] = {
        # v0.9.7: hot_money 已移除（2026-08-01 证伪）
    }
    _DEFAULT_DEDUP: dict = {"min_interval_minutes": 0, "dedup_key_include_strategy": False}

    def _get_dedup_config(self, strategy: str) -> dict:
        """获取策略的去重配置。"""
        return self._DEDUP_CONFIG.get(strategy, self._DEFAULT_DEDUP)

    def _mark_notified(self, signal: dict[str, Any]) -> None:
        """记录信号已通知，防止多轮扫描重复发送。"""
        symbol = signal["symbol"]
        direction = signal["direction"]
        strategy = signal.get("strategy", "")
        dedup_config = self._get_dedup_config(strategy)
        if dedup_config["dedup_key_include_strategy"]:
            dedup_key = f"{symbol}_{direction}_{strategy}"
        else:
            dedup_key = f"{symbol}_{direction}"
        self._notified_signals[dedup_key] = date.today().isoformat()
        self._save_notified_signals()

    def _load_notified_signals(self) -> dict[str, str]:
        """从文件加载已通知信号记录（持久化去重）。

        重要：本方法只读文件、只清理内存中的过期记录。
        不写入文件（避免与 _mark_notified 并发写入竞态）。
        过期清理由 _save_notified_signals 在写入时顺便完成。
        """
        try:
            if self._notified_signals_file.exists():
                with open(self._notified_signals_file, encoding="utf-8") as f:
                    data = json.load(f)

                today = date.today()
                cutoff_date = (today - timedelta(days=7)).isoformat()
                # v0.7.6 P0-13修复：保留所有格式key（包括带策略名的key）
                cleaned = {k: v for k, v in data.items() if v >= cutoff_date}
                if len(cleaned) != len(data):
                    logger.info("清理过期去重 key: {} → {} 条", len(data), len(cleaned))
                return cleaned
        except Exception as e:
            logger.warning("加载已通知信号记录失败: {}", e)
        return {}

    def _save_notified_signals(self) -> None:
        """保存已通知信号记录到文件（原子写入）。"""
        if not self._notified_signals:
            if self._notified_signals_file.exists():
                try:
                    with open(self._notified_signals_file, encoding="utf-8") as f:
                        existing = json.load(f)
                    if existing:
                        logger.info("内存去重数据为空，使用文件已有数据 ({} 条)", len(existing))
                        self._notified_signals = existing
                        return
                except Exception:
                    pass
            return
        try:
            today = date.today()
            cutoff_date = (today - timedelta(days=7)).isoformat()
            # v0.7.6 P0-13修复：保留所有格式key（包括带策略名的key）
            self._notified_signals = {
                k: v for k, v in self._notified_signals.items() if v >= cutoff_date
            }
            atomic_write_json(
                self._notified_signals_file,
                self._notified_signals,
                ensure_ascii=False,
            )
        except Exception as e:
            logger.warning("保存已通知信号记录失败: {}", e)

    def _clear_today_notified_signals(self) -> None:
        """清除当日的信号去重记录。"""
        today_str = date.today().isoformat()
        keys_to_remove = [k for k, v in self._notified_signals.items() if v == today_str]
        for k in keys_to_remove:
            del self._notified_signals[k]
        if keys_to_remove:
            logger.info("已清除 {} 条当日信号去重记录，补发熔断期间信号", len(keys_to_remove))
            self._save_notified_signals()

    async def _scan_signals(self, extra_symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """扫描自选股策略信号，经过滤链后输出。"""
        scan_list = list(self._watchlist)
        for s in extra_symbols or []:
            if s not in scan_list:
                scan_list.append(s)

        regime = await self._regime_detector.detect()
        current_regime = regime.get("regime", "unknown")
        self._current_regime = current_regime
        logger.info("市场择时: {}", regime["reason"])

        market_status = await self._market_filter.check_market()
        if not regime.get("buy_signal_allowed", False):
            logger.warning(
                "大市过滤: {} / 市场择时={}，"
                "禁止买入 — {}，"
                "跳过自动选股候选，仅扫描 watchlist（{} 只自选股）",
                market_status["regime"],
                current_regime,
                market_status["reason"],
                len(self._watchlist),
            )
            scan_list = list(self._watchlist)
            extra_symbols = []
        elif not market_status["bullish"]:
            logger.info(
                "大市过滤: {} 非 bull，但市场择时={} 允许买入，保留自动选股候选",
                market_status["regime"],
                current_regime,
            )

        try:
            from gugu.config import get_config as _get_cfg

            _cfg = _get_cfg().strategy
            if _cfg.enabled_by_regime:
                regime_strategies = get_enabled_strategies_for_regime(current_regime)
                _new_names = {s.name for s in regime_strategies}
                _old_names = {s.name for s in self._strategies} if self._strategies else set()
                if _new_names != _old_names:
                    self._router = SignalRouter(regime_strategies)
                    logger.info(
                        "v0.5.6 按 regime={} 动态加载策略: {}", current_regime, sorted(_new_names)
                    )
        except Exception as _e:
            logger.warning("v0.5.6 按 regime 加载策略失败，回退默认: {}", _e)

        budget = self._position_controller.calculate(
            regime=regime,
            account=self._broker.get_account(),
            is_halted=self._risk.is_halted,
        )
        logger.info(
            "仓位预算: 总上限={:.0%}, 单股上限={:.0%}, {}",
            budget.total_limit,
            budget.single_limit,
            budget.reason,
        )

        self._event_engine.put(
            "market_regime",
            {
                "regime": current_regime,
                "reason": regime.get("reason", ""),
                "total_limit": budget.total_limit,
                "single_limit": budget.single_limit,
            },
        )

        signals = []
        try:
            rt_all = await self._dm.fetch_stock_realtime(scan_list) if scan_list else None
        except Exception:
            rt_all = None

        # v0.7.8 数据时效性：记录扫描时间和数据源
        scan_timestamp = datetime.now().isoformat()
        data_source = self._dm.current_source
        adjust_type = self._dm.current_adjust_type

        from gugu.config import get_config as _get_config

        max_concurrency = getattr(_get_config().execution, "scan_concurrency", 10)
        semaphore = asyncio.Semaphore(max_concurrency)

        if scan_list and len(scan_list) > 5:
            logger.info("批量预取 {} 只股票历史数据（并发 {}）...", len(scan_list), max_concurrency)
            batch_history = {}

            async def _prefetch_one(sym: str) -> tuple[str, Any] | None:
                try:
                    df = await self._dm.fetch_stock_history(sym, days=120)
                    if df is not None and not df.empty:
                        return sym, df
                except Exception as e:
                    logger.warning("获取 {} 历史数据失败，跳过: {}", sym, e)
                return None

            async def _prefetch_with_sem(sym: str) -> tuple[str, Any] | None:
                async with semaphore:
                    return await _prefetch_one(sym)

            prefetch_tasks = [_prefetch_with_sem(sym) for sym in scan_list]
            prefetch_results = await asyncio.gather(*prefetch_tasks)
            for r in prefetch_results:
                if r is not None:
                    sym, df = r
                    batch_history[sym] = df
            logger.info("批量预取完成: {}/{}", len(batch_history), len(scan_list))
        else:
            batch_history = {}

        # 2026-09-03：跨股龙头分析（钱多多"只做真龙头"）
        # 用批量预取的板块数据识别每只股票的龙头/中军/跟随龙身份与板块情绪，
        # 注入每只股票的信号上下文，供 dragon_head 策略和入场 Gate 使用。
        dragon_head_info: dict[str, dict] = {}
        if len(batch_history) >= 2:
            try:
                from gugu.analysis.dragon_head import DragonHeadDetector

                dragon_head_info = DragonHeadDetector().detect(batch_history)
                if dragon_head_info:
                    _dragon_count = sum(1 for v in dragon_head_info.values() if v.get("is_dragon"))
                    logger.info(
                        "龙头识别完成: {} 只参与分析, {} 个板块龙头, 情绪={}",
                        len(dragon_head_info),
                        _dragon_count,
                        sorted({v.get("sentiment", "?") for v in dragon_head_info.values()}),
                    )
            except Exception as e:
                logger.warning("龙头识别失败，跳过跨股分析: {}", e)
                dragon_head_info = {}

        # 市场环境一次性预计算并显式注入 pipeline。
        market_env: dict[str, Any] = {
            "detector_status": "unavailable",
            "sample_count": len(batch_history),
            "mode": "unavailable",
            "asof": datetime.now().isoformat(timespec="seconds"),
        }
        if batch_history:
            try:
                from gugu.analysis.market_env import MarketEnvironment

                market_env = MarketEnvironment().assess(batch_history)
            except Exception as e:
                logger.warning("市场环境评估失败，买入 fail-closed: {}", e)

        async def _scan_single(symbol: str) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    df = batch_history.get(symbol)
                    if df is None:
                        df = await self._dm.fetch_stock_history(symbol, days=120)
                    if df is None or df.empty:
                        return None

                    # P0 前视偏差修复（2026-08-07, AUDIT_LOOKAHEAD_20260807）:
                    # 盘中时段(收盘前) 当日K线未完成(high/low/close为盘中瞬时值),
                    # 用未完成K线生成信号 = 盘中信号≠回测信号 → 实盘信号无法用回测验证。
                    # 修复: 盘中只基于"已收盘历史K线"判断形态; 收盘后(15:00)用完整K线重算。
                    # 判断收盘: 15:00后(含)视为已收盘K线可用; 早于15:00视为盘中。
                    # ⚠️ 2026-08-11 修复（AUDIT_LOOKAHEAD 副作用，告警评估员发现）:
                    # 原实现盘中无条件 iloc[:-1] 假设"最后一行=当日未完成K线"，但 A股
                    # 日K源盘中通常不含当日bar（最后一行=昨日已收盘bar，见
                    # ParquetPersistentCache 新鲜度语义: 盘中允许昨日bar）→ 把昨日
                    # 完整bar也删掉 → 策略基于 2 个交易日前数据计算（实证:
                    # 2026-08-11 10:19 日志 最后bar=2026-08-07，而缓存最后bar=08-10）。
                    # 正确语义: 仅当最后一行日期==今日（数据源确实含当日未完成bar）才剔除。
                    from datetime import datetime as _dt

                    _now = _dt.now()
                    if _now.strftime("%H:%M") < "15:00" and "date" in df.columns:
                        _last_bar_date = pd.Timestamp(df["date"].iloc[-1]).date()
                        if _last_bar_date == _now.date():
                            df = df.iloc[:-1].copy()  # 仅剔除当日未完成K线(确实存在时)
                            if len(df) < 2:  # 剔除后数据不足(上市初期/停牌), 放弃本轮
                                return None

                    meta = await self._dm.fetch_stock_meta(symbol)
                    portfolio = self._broker.get_portfolio()
                    account = self._broker.get_account()

                    yege_context = None
                    if "yege_mainline_explosion" in {
                        strategy.name for strategy in (self._strategies or [])
                    }:
                        # 当前数据链尚未提供完整板块事件快照；显式标记不可用，
                        # 由策略和 Pipeline fail-closed，仅保留观察语义。
                        yege_context = {
                            "evidence_status": "unavailable",
                            "data_quality": "insufficient",
                            "passed": False,
                            "entry_ready": False,
                        }

                    signal = await self._pipeline.process(
                        symbol=symbol,
                        df=df,
                        meta=meta,
                        budget=budget,
                        rt_all=rt_all,
                        watchlist=self._watchlist,
                        portfolio=portfolio,
                        account=account,
                        regime=regime["regime"],
                        dragon_head_info=dragon_head_info.get(symbol),
                        market_env=market_env,
                        yege_context=yege_context,
                    )
                    if signal is None:
                        return None

                    signal["market_context"]["regime"] = regime["regime"]
                    # v0.7.8 数据时效性：标注数据来源和时间戳
                    signal["data_timestamp"] = scan_timestamp
                    signal["data_source"] = data_source
                    signal["adjust_type"] = adjust_type
                    # 盘中实时数据标记（14:30扫描的盘中价）
                    signal["is_intraday"] = True
                    try:
                        atr_series = df["close"].pct_change().rolling(14).std()
                        signal["_atr"] = float(atr_series.iloc[-1]) * float(df["close"].iloc[-1])
                    except Exception:
                        signal["_atr"] = 0.0

                    if (
                        signal["direction"] == "buy"
                        and not signal.get("wisdom_filtered")
                        and regime.get("sell_signal_required")
                    ):
                        logger.info(
                            "{} 市场状态 {} 建议减仓，仓位上限已由 budget 限制为 {:.2%}",
                            symbol,
                            regime["regime"],
                            signal["suggested_position_ratio"],
                        )

                    return signal
                except Exception as e:
                    logger.error("扫描 {} 失败: {}", symbol, e)
                    return None

        results: list[dict[str, Any] | None] = []
        if scan_list:
            tasks = [_scan_single(sym) for sym in scan_list]
            results = await asyncio.gather(*tasks)
            signals = [s for s in results if s is not None]

        self._collect_filtered_signals(results, scan_list)
        self._log_daily_signal_summary(signals, scan_list)
        flow_results = [r.get("money_flow", {}) for r in results if r and r.get("money_flow")]
        flow_available = all(not r.get("data_missing", False) for r in flow_results)
        flow_errors = [r["data_error"] for r in flow_results if r.get("data_error")]
        self._write_signal_scan_status(
            len(signals),
            len(scan_list),
            data_health="degraded" if flow_errors else "ok",
            data_errors=flow_errors,
            flow_data_available=flow_available if flow_results else None,
        )

        return signals

    def _collect_filtered_signals(
        self,
        results: list[dict[str, Any] | None],
        scan_list: list[str],
    ) -> None:
        """收集被过滤的信号摘要。"""
        filtered: list[dict[str, Any]] = []
        for r in results:
            if r is None:
                continue
            if r.get("wisdom_filtered"):
                filtered.append(
                    {
                        "symbol": r.get("symbol", ""),
                        "name": r.get("name", ""),
                        "direction": r.get("direction", ""),
                        "strategy": r.get("strategy", ""),
                        "filter_reason": r.get("filter_reason", "未知原因"),
                        "confidence": r.get("confidence", 0),
                        "price": r.get("price", 0),
                    }
                )
        self._filtered_signals_today = filtered
        if filtered:
            _filter_summary = ", ".join(
                f"{s['symbol']}({s['filter_reason'][:20]})" for s in filtered[:5]
            )
            logger.info("今日被过滤信号: {} 条 — {}", len(filtered), _filter_summary)

    def _log_daily_signal_summary(
        self,
        signals: list[dict[str, Any]],
        scan_list: list[str],
    ) -> None:
        """记录当日信号统计摘要：累计信号数、通过/拒绝数、按策略分组。"""
        total_signals = len(signals)
        filtered_count = len(self._filtered_signals_today)

        strategy_stats: dict[str, int] = {}
        for sig in signals:
            sname = sig.get("strategy", sig.get("strategies", ["unknown"]))
            if isinstance(sname, list):
                for s in sname:
                    strategy_stats[s] = strategy_stats.get(s, 0) + 1
            else:
                strategy_stats[sname] = strategy_stats.get(sname, 0) + 1

        if strategy_stats:
            strategy_detail = " | ".join(f"{k}={v}" for k, v in sorted(strategy_stats.items()))
        else:
            strategy_detail = "无"

        logger.info(
            "📊 当日信号统计: 扫描 {} 只 | 通过 {} 个 | 过滤 {} 个 | {}",
            len(scan_list),
            total_signals,
            filtered_count,
            strategy_detail,
        )

    async def _process_signal(self, signal: dict[str, Any]) -> None:
        """处理单个信号：signal_only/track_live 模式只通知不下单，paper/live 模式走风控+下单+通知。"""
        symbol = signal["symbol"]
        direction = signal["direction"]
        _price = signal.get("price", 0)

        if not hasattr(self, "_notified_signals"):
            self._notified_signals = {}
        _sig_strategy = signal.get("strategy", "")
        _dedup_cfg = self._get_dedup_config(_sig_strategy)
        if _dedup_cfg["dedup_key_include_strategy"]:
            dedup_key = f"{symbol}_{direction}_{_sig_strategy}"
        else:
            dedup_key = f"{symbol}_{direction}"
        today_str = date.today().isoformat()
        async with self._get_dedup_lock():
            if self._notified_signals.get(dedup_key) == today_str:
                logger.info("{} {} 信号已通知过，跳过重复发送", symbol, direction)
                return
            # v0.7.6 P0-13修复：先不标记，发送成功后再标记，避免发送失败但已标记导致无法重试

        if self._exec_mode in ("signal_only", "track_live"):
            mode_label = "track_live" if self._exec_mode == "track_live" else "signal_only"
            logger.info("{} {} 信号（{} 模式，仅通知不下单）", symbol, direction, mode_label)
            try:
                sig_price = (
                    float(signal.get("price", 0)) if signal.get("price") is not None else 0.0
                )
            except (TypeError, ValueError):
                sig_price = 0.0
            sig_confidence = signal.get("confidence")
            sig_strategies = signal.get("strategies", [])
            if sig_price <= 1.0:
                if self._should_alert(f"{symbol}_price_anomaly"):
                    logger.error(
                        "{} {} 模式价格异常 price={}，拒绝通知并跳过历史记录",
                        symbol,
                        mode_label,
                        sig_price,
                    )
                return
            if sig_confidence is None or (
                isinstance(sig_confidence, (int, float)) and sig_confidence <= 0
            ):
                logger.error(
                    "{} {} 模式置信度异常 confidence={}，拒绝通知并跳过历史记录",
                    symbol,
                    mode_label,
                    sig_confidence,
                )
                return
            if not sig_strategies or (
                isinstance(sig_strategies, list) and len(sig_strategies) == 0
            ):
                logger.error("{} {} 模式策略为空，拒绝通知并跳过历史记录", symbol, mode_label)
                return

            try:
                record_signal_history(signal, None, None)
            except Exception as e:
                logger.warning("记录信号历史失败: {}", e)
            self._inject_performance_summary(signal)
            notify_ok = await self._notifier.notify_signal(signal)
            if notify_ok:
                self._mark_notified(signal)
            else:
                logger.error("{} 信号通知失败，请检查飞书配置", symbol)
            return

        if signal.get("wisdom_filtered"):
            logger.info("{} 信号被 wisdom 入场过滤，仅通知不下单", symbol)
            try:
                record_signal_history(signal, None, None)
            except Exception as e:
                logger.warning("记录 wisdom 过滤信号历史失败: {}", e)
            notify_ok = await self._notifier.notify_signal(signal)
            if notify_ok:
                self._mark_notified(signal)
            else:
                logger.error("{} wisdom 过滤信号通知失败，请检查飞书配置与网络", symbol)
            return

        result_dict = await self._order_manager.execute(
            signal=signal,
            broker=self._broker,
            risk=self._risk,
            notifier=self._notifier,
            event_engine=self._event_engine,
            data_mgr=self._dm,
        )

        if result_dict and result_dict.get("notify_ok"):
            self._mark_notified(signal)

        return

    async def _check_stop_loss_escalation(self) -> None:
        """检查止损告警是否需要升级（track_live 模式）。

        PR1 修复（2026-08-14）：
        - 改用 send_stop_loss_escalation 专用方法（带语义化 dedup_key，不走 send_text 降级路径）
        - 发送成功才调用 confirm_escalation_sent（未送达不推进级别）
        - 增加恢复清除：破位持仓价格回到止损线上方 → clear 状态，不再骚扰用户
        """
        if not self._stop_loss_escalator:
            return
        pending = self._stop_loss_escalator.get_pending_alerts()
        for alert in pending:
            # 恢复清除：价格回到止损线上方 → 清状态，不再打扰
            try:
                if self._broker:
                    pos = self._broker.get_position(alert.symbol)
                    if pos is not None:
                        cur = getattr(pos, "current_price", 0) or 0
                        stop = getattr(pos, "stop_loss_price", 0) or 0
                        if cur > stop and stop > 0:
                            self._stop_loss_escalator.clear(alert.symbol)
                            logger.info(
                                "止损告警 {} 已恢复（价格 {} > 止损价 {}），清除告警状态",
                                alert.symbol,
                                cur,
                                stop,
                            )
                            continue
            except Exception:
                pass

            needs_alert, level, urgency_label = self._stop_loss_escalator.check_escalation(
                alert.symbol
            )
            if needs_alert:
                logger.warning("止损告警升级 {}: level={} {}", alert.symbol, level, urgency_label)
                try:
                    sent = await self._notifier.send_stop_loss_escalation(
                        symbol=alert.symbol,
                        level=level,
                        urgency_label=urgency_label,
                        first_alert_ts=alert.first_alert_ts,
                    )
                    if sent:
                        self._stop_loss_escalator.confirm_escalation_sent(alert.symbol, level)
                    else:
                        logger.warning(
                            "止损升级告警 {} level={} 未送达（去重或失败），等待下轮重试",
                            alert.symbol,
                            level,
                        )
                except Exception as e:
                    logger.warning("止损升级告警发送失败: {}", e)

    async def _check_holdings_sell_signals(self, portfolio: dict) -> list[dict]:
        """收盘后检查持仓股卖出信号。"""
        if not portfolio:
            return []

        symbols = list(portfolio.keys())
        logger.info("持仓卖出信号检查: {} 只持仓股", len(symbols))

        signals = await self._scan_signals(extra_symbols=symbols)
        if not signals:
            return []

        sell_alerts = []
        for sig in signals:
            sym = sig.get("symbol", "")
            if sym not in portfolio:
                continue
            direction = str(sig.get("direction", "")).lower()
            if direction != "sell":
                continue
            pos = portfolio[sym]
            if not isinstance(pos, dict) and not hasattr(pos, "avg_cost"):
                continue
            cost = float(
                pos.get("avg_cost", 0) if isinstance(pos, dict) else getattr(pos, "avg_cost", 0)
            )
            price = sig.get("price", 0) or 0
            profit_pct = ((price - cost) / cost * 100) if cost and price else 0

            signal_strategies = (
                sig.get("strategies", [])
                if isinstance(sig.get("strategies"), list)
                else [sig.get("strategy", "?")]
            )
            sell_alerts.append(
                {
                    "name": sig.get("name", sym),
                    "symbol": sym,
                    "price": price,
                    "profit_pct": round(profit_pct, 2),
                    "sell_count": len(signal_strategies),
                    "buy_count": 1 if sig.get("buy_signals") else 0,
                    "sell_signals": [
                        {"strategy_name": s, "signal_strength": sig.get("confidence", 0.5)}
                        for s in signal_strategies
                    ],
                }
            )

        if sell_alerts:
            logger.info("持仓卖出信号: {} 只股票需关注", len(sell_alerts))
        return sell_alerts

    @staticmethod
    def _inject_performance_summary(signal: dict[str, Any]) -> None:
        """注入同类信号历史表现摘要到信号 dict 中。"""
        try:
            tracker = SignalTracker()
            records = tracker.load_history(days=30)
            if len(records) < 3:
                return

            strategies = set()
            sig_strategies = signal.get("strategies", [])
            if isinstance(sig_strategies, str):
                strategies.add(sig_strategies)
            elif isinstance(sig_strategies, list):
                strategies.update(sig_strategies)
            if not strategies:
                return

            relevant_records = [
                r
                for r in records
                if set(
                    r.get("strategies", [])
                    if isinstance(r.get("strategies"), list)
                    else [r.get("strategy", "")]
                )
                & strategies
            ]
            buy_records = [r for r in relevant_records if r.get("direction") == "buy"]
            if len(buy_records) < 3:
                return

            valid = [r for r in buy_records if r.get("return_5d") is not None]
            if len(valid) < 3:
                return
            wins = [r for r in valid if r["return_5d"] > 0]
            returns = [r["return_5d"] for r in valid]
            closed_trades = [r for r in relevant_records if r.get("outcome_status") == "closed"]
            realized_pnls = []
            for trade in closed_trades:
                pnl = trade.get("realized_pnl")
                if pnl is None:
                    pnl = (trade.get("outcome_metrics") or {}).get("realized_pnl")
                try:
                    realized_pnls.append(float(pnl))
                except (TypeError, ValueError):
                    continue

            signal["performance_summary"] = {
                "sample_count": len(valid),
                "forward_5d_signal_hit_rate": round(len(wins) / len(valid), 3),
                "avg_return_5d": round(sum(returns) / len(valid), 4),
                "best_return": round(max(returns), 4),
                "worst_return": round(min(returns), 4),
                "closed_trade_count": len(realized_pnls),
                "closed_trade_win_rate": round(
                    sum(pnl > 0 for pnl in realized_pnls) / len(realized_pnls), 3
                )
                if realized_pnls
                else 0.0,
                "realized_pnl": round(sum(realized_pnls), 2),
                "strategies": list(strategies),
            }
        except Exception as e:
            logger.debug("注入信号绩效摘要失败: {}", e)

    @staticmethod
    def _rotate_jsonl(path: Path, max_lines: int = 5000) -> None:
        """JSONL 文件行数超过 max_lines 时截断。"""
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > max_lines:
                    keep = lines[-max_lines // 2 :]
                    with path.open("w", encoding="utf-8") as f:
                        f.writelines(keep)
                    logger.info("历史文件轮转: {} {}→{} 行", path.name, len(lines), len(keep))
        except Exception as e:
            logger.warning("历史文件轮转失败: {}", e)

    def _write_signal_scan_status(
        self,
        signals_count: int,
        scanned_count: int,
        data_health: str = "unknown",
        data_errors: list[str] | None = None,
        flow_data_available: bool | None = None,
    ) -> None:
        path = PROJECT_ROOT / "data" / "signal_scan.json"
        payload = {
            "last_scan_at": pd.Timestamp.now().isoformat(),
            "signals_count": signals_count,
            "scanned_count": scanned_count,
            "data_health": data_health,
            "data_errors": data_errors or [],
            "flow_data_available": flow_data_available,
        }
        try:
            atomic_write_json(path, payload, ensure_ascii=False)
        except Exception as e:
            logger.warning("写入 signal_scan.json 失败: {}", e)

    def _write_heartbeat(self, status: str) -> None:
        """写入心跳文件，便于外部监控。"""
        hb_dir = PROJECT_ROOT / "data"
        hb_dir.mkdir(exist_ok=True)
        path = hb_dir / "heartbeat.json"
        history_path = hb_dir / "heartbeat_history.jsonl"

        try:
            account = self._broker.get_account()
            total_value = account.total_value
            cash = account.cash
            positions_count = len(account.positions)
        except Exception as e:
            logger.warning("心跳获取账户信息失败，使用 fallback 值: {}", e)
            total_value = 0.0
            cash = 0.0
            positions_count = 0

        payload = {
            "last_cycle_at": pd.Timestamp.now().isoformat(),
            "status": status,
            "halted": self._risk.is_halted,
            "total_value": total_value,
            "cash": cash,
            "positions_count": positions_count,
        }
        payload_json = json.dumps(payload, ensure_ascii=False)

        try:
            atomic_write_text(path, payload_json)
        except Exception as e:
            logger.warning("写入 heartbeat.json 失败: {}", e)

        try:
            append_jsonl(history_path, payload, max_lines=5000)
        except Exception as e:
            logger.warning("写入 heartbeat_history.jsonl 失败: {}", e)

    def _on_risk_alert(self, event: dict[str, Any]) -> None:
        """风险事件处理器。"""
        data = event.get("data", {})
        logger.warning("风险事件: level={}, message={}", data.get("level"), data.get("message", ""))

    def _on_stop_loss_triggered(self, event: dict[str, Any]) -> None:
        """止损触发事件处理器。"""
        data = event.get("data", {})
        symbol = data.get("symbol", "")
        if self._should_alert(f"{symbol}_stop_loss"):
            logger.warning(
                "止损触发事件: symbol={}, price={}, quantity={}",
                symbol,
                data.get("price"),
                data.get("quantity"),
            )
        if self._stop_loss_escalator and symbol:
            self._stop_loss_escalator.record_stop_loss_triggered(symbol)

    def _on_order_filled(self, event: dict[str, Any]) -> None:
        """订单成交事件处理器。"""
        data = event.get("data", {})
        logger.info(
            "订单成交: {} {} {}@{}",
            data.get("symbol"),
            data.get("direction"),
            data.get("quantity"),
            data.get("price"),
        )


# Need to import SignalRouter at module level to avoid circular imports
from gugu.engine.signal_router import SignalRouter  # noqa: E402
from gugu.strategies.registry import get_enabled_strategies_for_regime  # noqa: E402
