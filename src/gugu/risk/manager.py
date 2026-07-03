"""Risk manager with three-level risk control.

This module is the single source of truth for risk enforcement.
Risk rules are iron laws and cannot be bypassed by business logic.

Levels:
- L1 single position: per-stock position ratio <= max_position_ratio (default 30%)
- L2 daily loss: warn at daily_loss_warn (default 3%), halt at daily_loss_halt (default 5%)
- L3 system: T+1 settlement, price limit (涨跌停), suspension (停牌)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gugu.config import PROJECT_ROOT, get_config
from gugu.config.models import AppConfig
from gugu.filters.industry_constraint import IndustryConstraint
from gugu.utils.log import get_logger

from .rules import Position, RiskAction, RiskCheckResult, RiskLevel

# Price limit ratios by board
_MAIN_BOARD_LIMIT = 0.10  # 主板 ±10%
_GEM_KCB_LIMIT = 0.20  # 创业板 / 科创板 ±20%
_BSE_LIMIT = 0.30  # 北交所 ±30%（P1-c 修复）
_ST_LIMIT = 0.05  # ST ±5%

# Symbol prefixes
_GEM_PREFIXES = ("300", "301")  # 创业板
_KCB_PREFIXES = ("688", "689")  # 科创板
_BSE_PREFIXES = ("43", "83", "87", "88", "920")  # 北交所（P1-c 修复）

# L2 熔断状态持久化文件（P0-2 修复）
RISK_STATE_FILE: Path = PROJECT_ROOT / "data" / "risk_state.json"


class RiskManager:
    """Three-level risk manager.

    Once L2 halt is triggered, all orders are blocked until ``reset()`` is called.
    This is an iron law and cannot be bypassed.

    P0-2 修复：L2 熔断状态持久化到 data/risk_state.json，
    进程重启后自动恢复，防止绕过熔断铁律。
    """

    def __init__(self, config: dict[str, Any] | AppConfig | None = None) -> None:
        if isinstance(config, AppConfig):
            rc = config.risk
            self.max_position_ratio = rc.max_position_ratio
            self.daily_loss_warn = rc.daily_loss_warn
            self.daily_loss_halt = rc.daily_loss_halt
            self.max_total_positions = rc.max_total_positions
            self.t_plus_1 = rc.t_plus_1
            cfg = config.flatten().get("risk", {})
        elif config is not None:
            cfg = config
            self.max_position_ratio = float(cfg.get("max_position_ratio", 0.30))
            self.daily_loss_warn = float(cfg.get("daily_loss_warn", 0.03))
            self.daily_loss_halt = float(cfg.get("daily_loss_halt", 0.05))
            self.max_total_positions = int(cfg.get("max_total_positions", 5))
            self.t_plus_1 = bool(cfg.get("t_plus_1", True))
        else:
            rc = get_config().risk
            self.max_position_ratio = rc.max_position_ratio
            self.daily_loss_warn = rc.daily_loss_warn
            self.daily_loss_halt = rc.daily_loss_halt
            self.max_total_positions = rc.max_total_positions
            self.t_plus_1 = rc.t_plus_1
            cfg = get_config().flatten().get("risk", {})
        self._logger = get_logger()
        self._halted: bool = False
        self._last_reset_date: str = ""
        # 组合级风控（实例级别，避免跨实例共享）
        self._portfolio_peak: float = 0.0
        self._max_drawdown_halt: float = 0.15
        # 行业分散约束
        self._industry_constraint = IndustryConstraint(config=cfg)
        # P0-2 修复：启动时从磁盘加载熔断状态
        self._load_halt_state()

    @property
    def is_halted(self) -> bool:
        """Whether the manager is in halt state (all trading blocked)."""
        return self._halted

    def reset(self) -> None:
        """Reset halt state. Call at the start of a new trading day."""
        if self._halted:
            self._logger.info("Risk manager halt state reset")
        self._halted = False
        from datetime import date as _date

        self._last_reset_date = _date.today().isoformat()
        self._save_halt_state()

    def clear_halt_only(self) -> None:
        """仅清除 L2 熔断状态，不重置日初净值。

        用于盘中人工解除熔断后恢复交易，但保留当日亏损累计，
        避免掩盖当日已发生的亏损（铁律：L2 熔断不可被绕过）。
        """
        if self._halted:
            self._logger.warning(
                "L2 熔断状态人工解除（clear_halt_only），日初净值保留不变，当日亏损继续累计"
            )
        self._halted = False
        self._save_halt_state()

    def _load_halt_state(self) -> None:
        """P0-2 修复：从磁盘加载熔断状态，防止进程重启绕过熔断。

        同时加载 last_reset_date，用于确定是否为新交易日（与 _last_cycle_date 同步）。

        P0 修复：fail-safe — 文件损坏时保守保持 halted=False 但记录 ERROR。
        （熔断状态丢失比误熔断影响小，可由人工核查；若误熔断会阻断所有交易）
        """
        try:
            if RISK_STATE_FILE.exists():
                data = json.loads(RISK_STATE_FILE.read_text(encoding="utf-8"))
                halted = data.get("halted", False)
                halt_date = data.get("halt_date", "")
                from datetime import date as _date

                today_str = _date.today().isoformat()
                # 仅恢复当日熔断状态，跨日熔断自动失效（需新交易日 reset）
                if halted and halt_date == today_str:
                    self._halted = True
                    self._logger.warning(
                        f"P0-2: 恢复当日 L2 熔断状态（halt_date={halt_date}），"
                        f"交易已被阻断，需人工 reset_halt 后恢复"
                    )
                self._last_reset_date = data.get("last_reset_date", "")
        except Exception as e:
            # P0 修复：fail-safe 但记录 ERROR，便于人工介入
            self._logger.error(
                f"加载熔断状态失败({e})，文件可能损坏。"
                f"保守恢复为非熔断状态，请人工核查 {RISK_STATE_FILE}"
            )

    @property
    def last_reset_date(self) -> str:
        """上次 reset() 日期（用于 TradingEngine 的 _last_cycle_date 同步）。"""
        return self._last_reset_date

    def _save_halt_state(self) -> None:
        """P0-2 修复：将熔断状态持久化到磁盘，含 last_reset_date 用于跨日同步。
        P0 修复：同时保存 _portfolio_peak，防止重启后组合回撤熔断失效。
        P0 修复：原子写入（tmp + os.replace），防止崩溃导致文件截断。
        """
        try:
            RISK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            from datetime import date as _date

            data = {
                "halted": self._halted,
                "halt_date": _date.today().isoformat(),
                "last_reset_date": self._last_reset_date,
                "portfolio_peak": self._portfolio_peak,  # P0: 持久化组合峰值
                "updated_at": __import__("pandas").Timestamp.now().isoformat(),
            }
            # P0 修复：原子写入
            from gugu.utils.atomic import atomic_write_json
            atomic_write_json(RISK_STATE_FILE, data, ensure_ascii=False, indent=2)
        except Exception as e:
            self._logger.warning(f"保存熔断状态失败: {e}")

    def check_order(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        price: float,
        portfolio: dict[str, Position],
        *,
        cash: float = 0.0,
        prev_close: float | None = None,
        is_st: bool = False,
        is_suspended: bool = False,
    ) -> RiskCheckResult:
        """Check an order against all risk rules.

        Args:
            symbol: Stock code, e.g. "600519".
            direction: "buy" or "sell".
            quantity: Order quantity in shares.
            price: Order price.
            portfolio: Current positions keyed by symbol.
            cash: Available cash, needed for position ratio calculation.
            prev_close: Previous close price, needed for price limit check.
            is_st: Whether the stock is ST (affects price limit).
            is_suspended: Whether the stock is suspended (停牌).

        Returns:
            RiskCheckResult. HALT means the order must be rejected.
        """
        # P0 修复：熔断时仅阻断买入，卖出/止损豁免（防止熔断后无法止损导致亏损扩大）
        if self._halted and direction.lower().strip() == "buy":
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.HALT,
                message="Daily loss halt is active, buy blocked. Sell/stop-loss still allowed. Call reset() to resume buy.",
            )

        direction = direction.lower().strip()
        if direction not in ("buy", "sell"):
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"Invalid direction: {direction!r}, must be 'buy' or 'sell'",
            )

        if quantity <= 0:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"Invalid quantity: {quantity}, must be positive",
            )

        if price <= 0:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"Invalid price: {price}, must be positive",
            )

        # L3: suspension check
        if is_suspended:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"{symbol} is suspended, trading not allowed",
            )

        # L3: price limit check (涨跌停) - 区分买卖方向
        if (
            prev_close is not None
            and prev_close > 0
            and not self.is_tradable(symbol, price, prev_close, is_st=is_st, direction=direction)
        ):
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"{symbol} at price limit, trading not allowed",
            )

        if direction == "buy":
            return self._check_buy(symbol, quantity, price, portfolio, cash)
        return self._check_sell(symbol, quantity, price, portfolio)

    def _check_buy(
        self,
        symbol: str,
        quantity: int,
        price: float,
        portfolio: dict[str, Position],
        cash: float,
    ) -> RiskCheckResult:
        """L1 checks for buy orders: max positions, industry constraint, and single position ratio."""
        # L1: max total positions (only when opening a new position)
        if symbol not in portfolio and len(portfolio) >= self.max_total_positions:
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.HALT,
                message=(
                    f"Max total positions ({self.max_total_positions}) reached, "
                    f"cannot open new position in {symbol}"
                ),
            )

        # 行业分散约束：同行业持仓不超过上限
        if symbol not in portfolio:
            result = self._industry_constraint.check_buy(symbol, portfolio)
            if not result["allowed"]:
                return RiskCheckResult(
                    level=RiskLevel.L1_POSITION,
                    action=RiskAction.HALT,
                    message=result["reason"],
                )

        # L1: single position ratio limit
        current_pos = portfolio.get(symbol)
        existing_qty = current_pos.quantity if current_pos else 0

        # Post-trade position value for this symbol (valued at order price)
        new_symbol_value = (existing_qty + quantity) * price
        # Other positions valued at their current price
        other_value = sum(p.quantity * p.current_price for s, p in portfolio.items() if s != symbol)
        # Post-trade total = cash - buy_cost + new_symbol_value + other_value
        # 买入后现金减少 quantity * price（未含手续费，保守估算）
        post_trade_total = cash - quantity * price + new_symbol_value + other_value

        if post_trade_total <= 0:
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.HALT,
                message=(
                    f"Cannot determine total portfolio value ({post_trade_total}), "
                    f"pass cash= to check_order for position ratio check"
                ),
            )

        new_ratio = new_symbol_value / post_trade_total
        if new_ratio > self.max_position_ratio:
            return RiskCheckResult(
                level=RiskLevel.L1_POSITION,
                action=RiskAction.HALT,
                message=(
                    f"{symbol} position ratio {new_ratio:.1%} exceeds limit "
                    f"{self.max_position_ratio:.1%}"
                ),
            )

        return RiskCheckResult(
            level=RiskLevel.L1_POSITION,
            action=RiskAction.ALLOW,
            message=f"Buy {symbol} {quantity}@{price} approved, ratio={new_ratio:.1%}",
        )

    def _check_sell(
        self,
        symbol: str,
        quantity: int,
        price: float,
        portfolio: dict[str, Position],
    ) -> RiskCheckResult:
        """L3 checks for sell orders: holding and T+1."""
        position = portfolio.get(symbol)
        if position is None or position.quantity <= 0:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"No position in {symbol} to sell",
            )

        if quantity > position.quantity:
            return RiskCheckResult(
                level=RiskLevel.L3_SYSTEM,
                action=RiskAction.HALT,
                message=f"Sell quantity {quantity} exceeds holding {position.quantity} for {symbol}",
            )

        # L3: T+1 settlement check
        if self.t_plus_1:
            sellable = position.available
            if quantity > sellable:
                return RiskCheckResult(
                    level=RiskLevel.L3_SYSTEM,
                    action=RiskAction.HALT,
                    message=(
                        f"T+1 rule: sell {quantity} exceeds sellable {sellable} "
                        f"(available) for {symbol}"
                    ),
                )

        return RiskCheckResult(
            level=RiskLevel.L3_SYSTEM,
            action=RiskAction.ALLOW,
            message=f"Sell {symbol} {quantity}@{price} approved",
        )

    def check_daily_loss(self, loss_pct: float) -> RiskCheckResult:
        """L2: Check daily loss against circuit breaker thresholds.

        Args:
            loss_pct: Daily loss as a non-negative fraction (0.03 = 3% loss).

        Returns:
            RiskCheckResult. HALT triggers a permanent block until reset().
        """
        if loss_pct < 0:
            loss_pct = 0.0

        if loss_pct >= self.daily_loss_halt:
            self._halted = True
            self._save_halt_state()  # P0-2: 持久化熔断状态
            self._logger.warning(
                f"L2 HALT: daily loss {loss_pct:.2%} >= {self.daily_loss_halt:.2%}, "
                f"trading halted until reset()"
            )
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.HALT,
                message=(
                    f"Daily loss {loss_pct:.2%} reached halt threshold "
                    f"{self.daily_loss_halt:.2%}, trading halted"
                ),
            )

        if loss_pct >= self.daily_loss_warn:
            self._logger.warning(
                f"L2 WARN: daily loss {loss_pct:.2%} >= {self.daily_loss_warn:.2%}"
            )
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.WARN,
                message=(
                    f"Daily loss {loss_pct:.2%} reached warn threshold {self.daily_loss_warn:.2%}"
                ),
            )

        return RiskCheckResult(
            level=RiskLevel.L2_DAILY_LOSS,
            action=RiskAction.ALLOW,
            message=f"Daily loss {loss_pct:.2%} within limits",
        )

    def is_tradable(
        self,
        symbol: str,
        price: float,
        prev_close: float,
        *,
        is_st: bool = False,
        is_suspended: bool = False,
        direction: str = "",
    ) -> bool:
        """L3: Check if a stock is tradable (not at price limit, not suspended).

        Price limit ratios:
        - Main board (600/601/603/605/000/001/002/003): ±10%
        - ChiNext (300/301) / STAR (688/689): ±20%
        - ST stocks: ±5% (overrides board-based limit)

        Direction-aware: 涨停时允许卖出（有人排队买），跌停时允许买入（有人排队卖）。

        Args:
            symbol: Stock code.
            price: Current / order price.
            prev_close: Previous close price.
            is_st: Whether the stock is ST.
            is_suspended: Whether the stock is suspended.
            direction: "buy" or "sell". Empty string = old behavior (both blocked).

        Returns:
            True if tradable, False if at price limit or suspended.
        """
        if is_suspended:
            return False

        if prev_close <= 0:
            self._logger.warning(
                f"Cannot determine price limit for {symbol}: prev_close={prev_close}"
            )
            return True

        limit_ratio = self._price_limit_ratio(symbol, is_st)
        limit_up = round(prev_close * (1 + limit_ratio), 2)
        limit_down = round(prev_close * (1 - limit_ratio), 2)

        # 涨停：不可买入，但可以卖出
        if price >= limit_up and direction != "sell":
            return False
        # 跌停：不可卖出，但可以买入
        if price <= limit_down and direction != "buy":  # noqa: SIM103
            return False
        return True

    @staticmethod
    def _price_limit_ratio(symbol: str, is_st: bool) -> float:
        """Determine price limit ratio based on board and ST status.

        P1-c 修复：增加北交所（BSE）±30% 涨跌停限制。
        """
        if is_st:
            return _ST_LIMIT
        if symbol.startswith(_BSE_PREFIXES):  # P1-c: 北交所优先判断
            return _BSE_LIMIT
        if symbol.startswith(_GEM_PREFIXES) or symbol.startswith(_KCB_PREFIXES):
            return _GEM_KCB_LIMIT
        return _MAIN_BOARD_LIMIT

    # ── P1 新增：组合级风控 ─────────────────────────────────────────

    def update_portfolio_peak(self, total_value: float) -> None:
        """更新组合历史最高净值（每日循环结束时调用）。
        P0 修复：峰值更新时持久化到磁盘，防止重启后回撤熔断失效。
        """
        if total_value > self._portfolio_peak:
            self._portfolio_peak = total_value
            self._save_halt_state()  # P0: 持久化峰值防止重启绕过回撤铁律

    def check_portfolio_drawdown(self, total_value: float) -> RiskCheckResult:
        """检查组合从高点回撤是否触发熔断。

        回撤 ≥ 15% 时触发熔断（设置 _halted），阻止所有交易。
        """
        if self._portfolio_peak <= 0:
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.ALLOW,
                message="组合峰值未初始化",
            )
        dd = (self._portfolio_peak - total_value) / self._portfolio_peak
        if dd >= self._max_drawdown_halt:
            self._halted = True
            self._save_halt_state()
            self._logger.warning(
                f"L2 HALT: 组合回撤 {dd:.2%} >= {self._max_drawdown_halt:.0%}, "
                f"trading halted until reset()"
            )
            return RiskCheckResult(
                level=RiskLevel.L2_DAILY_LOSS,
                action=RiskAction.HALT,
                message=(
                    f"组合回撤 {dd:.2%} 触发熔断（阈值 {self._max_drawdown_halt:.0%}），仅允许减仓"
                ),
            )
        return RiskCheckResult(
            level=RiskLevel.L2_DAILY_LOSS,
            action=RiskAction.ALLOW,
            message=f"组合回撤 {dd:.2%} 在安全范围内",
        )

    @staticmethod
    async def check_correlation(
        portfolio: dict[str, Position], data_mgr: Any, threshold: float = 0.85
    ) -> list[dict]:
        """检查持仓间高相关性风险（P1 新增，async 修复 FastAPI 兼容）。

        基于 historical daily returns 计算两两相关系数，
        返回相关系数 > threshold 的高相关股票对列表。

        Args:
            portfolio: 当前持仓 {symbol: Position}
            data_mgr: DataManager 实例（用于拉取历史数据）
            threshold: 相关系数告警阈值

        Returns:
            高相关股票对列表 [{"pair": (s1,s2), "correlation": float}, ...]
        """
        import numpy as np
        import pandas as pd

        symbols = list(portfolio.keys())
        if len(symbols) < 2:
            return []

        returns: dict[str, pd.Series] = {}
        for sym in symbols:
            try:
                df = await data_mgr.fetch_stock_history(sym, days=90)
                if not df.empty and len(df) >= 30:
                    ret = df["close"].pct_change().dropna()
                    if len(ret) >= 30:
                        returns[sym] = ret
            except Exception:
                pass

        alerts: list[dict] = []
        syms = list(returns.keys())
        for i in range(len(syms)):
            for j in range(i + 1, len(syms)):
                s1, s2 = syms[i], syms[j]
                try:
                    aligned = pd.concat([returns[s1], returns[s2]], axis=1).dropna()
                    if len(aligned) < 20:
                        continue
                    corr = float(np.corrcoef(aligned.iloc[:, 0], aligned.iloc[:, 1])[0, 1])
                    if abs(corr) > threshold:
                        alerts.append({"pair": (s1, s2), "correlation": round(corr, 4)})
                except Exception:
                    continue
        return alerts
