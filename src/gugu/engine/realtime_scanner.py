"""v0.9.0 实时推荐引擎（RealtimeSignalScanner）。

把 gugu 从「离线定时扫描」升级为「盘中实时推荐」：

- 交易时段每分钟扫描 watchlist + 持仓
- 复用 TradingEngine._scan_signals() 策略信号管线（过滤/去重/风控全保留）
- 新信号立即推送飞书（买入/卖出 + 价格）
- 可选自动执行：live 模式下信号直接委托 QMT（带人工确认开关）

设计原则：
- 复用引擎已有管线，不重复实现策略逻辑
- 信号去重：同一 (symbol, direction) 30 分钟内不重复推送（与聚合配置联动）
- 性能自适应：由 IntradayMonitor 的性能模式控制扫描频率
- 安全：signal_only/track_live 只推送观察信号；paper 才允许模拟下单；live 默认人工确认
"""

from __future__ import annotations

import time
from typing import Any

from gugu.config import get_config
from gugu.utils.calendar import is_trading_day, is_trading_time
from gugu.utils.log import get_logger

logger = get_logger()


class RealtimeSignalScanner:
    """盘中实时策略信号扫描器。"""

    # 同一 (symbol, direction) 信号最小推送间隔（秒）
    DEFAULT_COOLDOWN_SEC = 1800
    # 策略全量扫描最小间隔（秒）——单机性能节流，避免每分钟全量扫描
    DEFAULT_SCAN_INTERVAL_SEC = 300

    def __init__(
        self,
        engine: Any,
        cooldown_sec: int | None = None,
        auto_trade: bool | None = None,
        scan_interval_sec: int | None = None,
    ) -> None:
        self._engine = engine
        self._cooldown_sec = cooldown_sec or self.DEFAULT_COOLDOWN_SEC
        self._scan_interval_sec = scan_interval_sec or self.DEFAULT_SCAN_INTERVAL_SEC
        self._last_scan_ts: float = 0.0
        self._last_push: dict[tuple[str, str], float] = {}
        self._paper_broker: Any = None  # 保留兼容字段；提醒模式不使用模拟盘
        # auto_trade: None=跟随配置（execution.mode=live 且 confirm_required=False）
        self._auto_trade = auto_trade

    # ────────────────────── 主入口 ──────────────────────

    async def run_cycle(self) -> list[dict[str, Any]]:
        """执行一次实时信号扫描（仅交易日盘中时段）。

        Returns:
            新推送的信号列表
        """
        if not is_trading_day() or not is_trading_time():
            return []

        # 性能节流：距上次全量扫描不足间隔则跳过本轮
        now_ts = time.time()
        if now_ts - self._last_scan_ts < self._scan_interval_sec:
            return []
        self._last_scan_ts = now_ts

        try:
            cfg = get_config()
            execution_mode = getattr(cfg.execution, "mode", "signal_only")
        except Exception:
            execution_mode = "signal_only"

        # live 模式必须已连接券商
        if execution_mode == "live":
            broker = getattr(self._engine, "_broker", None)
            if broker is None or not getattr(broker, "_connected", False):
                logger.warning("实时扫描: execution.mode=live 但 QMT 未连接，跳过自动执行")
                # 仍推送信号（推荐模式），但不自动下单

        # P0-3 修复（2026-08-05）：实时循环中执行止损检查 + 升级检查。
        # 此前止损升级仅在 run_daily_cycle 的交易时段分支调用（每日 4 轮），
        # RealtimeSignalScanner 每 5 分钟一轮却从不检查止损 → 破位持仓
        # 告警 14:46 后不再升级，用户不知情。track_live 模式尤其关键
        # （系统只提醒不自动交易，止损升级是用户唯一的执行信号）。
        try:
            if getattr(self._engine, "_stop_loss_escalator", None) is not None:
                await self._engine._check_stop_loss_escalation()
            # 非熔断状态下的止损检查（熔断时 run_daily_cycle 已处理）
            if not getattr(self._engine._risk, "is_halted", False):
                await self._engine._check_stop_loss()
        except Exception as e:
            logger.warning("实时止损检查异常: {}", e)

        signals = await self._scan()
        pushed: list[dict[str, Any]] = []
        for signal in signals:
            if not self._should_push(signal):
                continue
            self._mark_pushed(signal)
            pushed.append(signal)
            await self._notify(signal)
            await self._maybe_execute(signal, execution_mode)
        return pushed

    # ────────────────────── 内部 ──────────────────────

    async def _scan(self) -> list[dict[str, Any]]:
        """调用引擎策略管线扫描信号。"""
        try:
            return await self._engine._scan_signals()
        except Exception as e:
            logger.warning("实时信号扫描失败: {}", e)
            return []

    def _should_push(self, signal: dict[str, Any]) -> bool:
        """去重：同一 (symbol, direction) 在冷却期内不重复推送。"""
        symbol = signal.get("symbol", "")
        direction = signal.get("direction", "")
        if not symbol or not direction:
            return False
        key = (symbol, direction)
        now = time.time()
        last = self._last_push.get(key, 0)
        return not (now - last < self._cooldown_sec)

    def _mark_pushed(self, signal: dict[str, Any]) -> None:
        key = (signal.get("symbol", ""), signal.get("direction", ""))
        self._last_push[key] = time.time()
        # 防止字典无限增长：清理 2 倍冷却期前的记录
        stale = [
            k for k, ts in self._last_push.items() if time.time() - ts > self._cooldown_sec * 2
        ]
        for k in stale:
            del self._last_push[k]

    async def _notify(self, signal: dict[str, Any]) -> None:
        """推送信号到飞书。"""
        notifier = getattr(self._engine, "_notifier", None)
        if notifier is None:
            logger.warning("实时信号: notifier 不可用，无法推送")
            return
        try:
            ok = await notifier.notify_signal(signal)
            logger.info(
                "实时信号推送: {} {} {}",
                signal.get("direction"),
                signal.get("symbol"),
                "OK" if ok else "FAIL",
            )
        except Exception as e:
            logger.warning("实时信号推送异常: {}", e)

    async def _maybe_execute(self, signal: dict[str, Any], execution_mode: str) -> None:
        """按运行模式执行信号。

        已被标记为 observation_only/rejected 的买入信号不得进入任何执行器，
        这是流水线门禁之外的第二道防线。

        安全分层：
        - signal_only / track_live：只推送观察信号，不执行委托
        - paper：走引擎完整管线（风控 + PaperBroker 模拟下单 + 通知）
                 —— 模拟盘验证信号效果，不上真钱
        - live + confirm_required=True：只推送，等待人工确认（由外部确认流程下单）
        - live + confirm_required=False：直接委托 QMT
        """
        validation_status = str(
            signal.get("validation_status")
            or (signal.get("capability") or {}).get("validation_status", "")
        ).lower()
        # 2026-09-03 修复（评审 P1）：paper 模式放行 observation_only/experimental，
        # 模拟盘的目的就是成交验证；live/signal_only/track_live 仍严格阻断。
        # gate_blocked（纪律拦截）在任何模式都不得执行。
        is_paper = execution_mode == "paper"
        blocked_observation = signal.get("observation_only") and not is_paper
        blocked_validation = (
            validation_status in {"experimental", "in_review", "unknown"} and not is_paper
        )
        if signal.get("direction") == "buy" and (
            signal.get("gate_blocked")
            or signal.get("rejected")
            or blocked_observation
            or blocked_validation
        ):
            logger.info(
                "实时信号: 买入信号被门禁标记为不可执行: {} {}",
                signal.get("symbol"),
                signal.get("reject_reason") or signal.get("filter_reason", ""),
            )
            return

        if execution_mode in ("signal_only", "track_live"):
            # 提醒模式只推送，不进入任何模拟或实盘执行路径。
            logger.info(
                "实时信号: {} 模式仅观察，不执行委托: {} {}",
                execution_mode,
                signal.get("direction"),
                signal.get("symbol"),
            )
            return

        if execution_mode == "paper":
            await self._execute_paper(signal)
            return

        if execution_mode != "live":
            return

        # auto_trade 显式指定时优先；否则跟随配置
        if self._auto_trade is not None:
            allow_auto = self._auto_trade
        else:
            try:
                cfg = get_config()
                allow_auto = not cfg.execution.confirm_required
            except Exception:
                allow_auto = False

        if not allow_auto:
            logger.info("实时信号: live 模式但要求人工确认，仅推送不下单: {}", signal.get("symbol"))
            return

        broker = getattr(self._engine, "_broker", None)
        if broker is None or not getattr(broker, "_connected", False):
            logger.error("实时信号: 自动交易失败，QMT 未连接")
            return

        direction = signal.get("direction", "")
        symbol = signal.get("symbol", "")
        # 建议仓位 → 股数（默认 1 手起步）
        suggested_qty = signal.get("suggested_quantity") or signal.get("quantity") or 0
        if suggested_qty <= 0:
            suggested_qty = 100
        # 建议价格（限价单）
        price = signal.get("suggested_price") or signal.get("price") or None

        risk = getattr(self._engine, "_risk", None) or getattr(self._engine, "risk", None)
        if risk is None:
            logger.error("实时信号: 自动交易拒绝，风控管理器不可用")
            return
        try:
            account = broker.get_account()
            portfolio = broker.get_portfolio()
            position = portfolio.get(str(symbol).strip().zfill(6))
            risk_result = risk.check_order(
                str(symbol).strip().zfill(6),
                str(direction),
                int(suggested_qty),
                float(price or 0),
                portfolio,
                cash=account.cash,
                prev_close=getattr(position, "prev_close", None),
                is_st=getattr(position, "is_st", False)
                if position
                else bool(signal.get("is_st", False)),
                is_suspended=getattr(position, "is_suspended", False)
                if position
                else bool(signal.get("is_suspended", False)),
            )
            if not risk_result.allowed:
                logger.warning("实时信号: 风控拒绝自动交易 {}: {}", symbol, risk_result.message)
                return
            result = broker.order(symbol, direction, int(suggested_qty), price)
            logger.info(
                "实时信号自动执行: {} {} {}股 -> {}",
                direction,
                symbol,
                suggested_qty,
                "成功" if result.success else f"失败: {result.message}",
            )
        except Exception as e:
            logger.error("实时信号自动执行异常: {} {} {}", direction, symbol, e)

    def _get_sidecar_paper_broker(self):
        """懒加载独立模拟盘（与真实持仓隔离，持久化到 data/paper_broker_state.json）。"""
        if self._paper_broker is None:
            try:
                from gugu.execution.paper import PaperBroker

                self._paper_broker = PaperBroker()
            except Exception as e:
                logger.warning("独立模拟盘构建失败: {}", e)
                self._paper_broker = None
        return self._paper_broker

    async def _execute_sidecar_paper(self, signal: dict[str, Any]) -> None:
        """遗留兼容入口：提醒模式禁止使用 sidecar 模拟盘。"""
        logger.info(
            "忽略遗留 sidecar 模拟执行入口: {} {}",
            signal.get("direction"),
            signal.get("symbol"),
        )

    async def _execute_paper(self, signal: dict[str, Any]) -> None:
        """paper 模式：走引擎完整信号处理管线（风控+模拟下单+通知）。

        复用 _process_signal 而不是直接调 broker.order：
        - 保留风控（仓位/单股上限/日亏熔断）
        - 保留信号去重（_notified_signals）
        - 保留订单历史记录
        - PaperBroker 状态持久化到 data/paper_broker_state.json
        """
        try:
            process = getattr(self._engine, "_process_signal", None)
            if process is None:
                logger.warning("实时信号: 引擎无 _process_signal，模拟盘跳过")
                return
            await process(signal)
            logger.info(
                "实时信号模拟盘执行: {} {}",
                signal.get("direction"),
                signal.get("symbol"),
            )
        except Exception as e:
            logger.error("实时信号模拟盘执行异常: {}", e)

    def describe(self) -> dict[str, Any]:
        """返回扫描器配置描述。"""
        return {
            "cooldown_sec": self._cooldown_sec,
            "scan_interval_sec": self._scan_interval_sec,
            "auto_trade": self._auto_trade,
            "last_push_count": len(self._last_push),
        }


# ────────────────────── 兼容别名 ──────────────────────

RealtimeRecommender = RealtimeSignalScanner
