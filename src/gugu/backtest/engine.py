"""Backtest engine with realistic A-share transaction costs.

Transaction cost model:
- Buy:  commission_rate (default 0.025%) + slippage (default 0.2%)
- Sell: commission_rate + stamp_tax (default 0.05%) + slippage

Position management respects the L1 single-position ratio from risk config.
P1-m: L3 price limit checks (涨跌停) integrated via RiskManager.is_tradable().

Cognitive engine integration (enable_cognitive_engine=True):
- StageDetector: 疯狂/最后阶段不入场
- DangerSignalDetector: medium+ 危险信号不入场
- NoAverageDownChecker: 亏损仓位不加码
- TrailingStopEngine: 浪谷递进动态止损（替代纯策略卖出信号）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from gugu.config import settings
from gugu.risk import RiskManager
from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

from .metrics import calc_metrics

# A-share board lot: must trade in multiples of 100 shares
_BOARD_LOT = 100


@dataclass
class Trade:
    """Single trade record.

    Attributes:
        date: Trade date (pd.Timestamp / str / datetime).
        direction: "buy" or "sell".
        price: Execution price after slippage.
        quantity: Number of shares traded.
        commission: Brokerage commission.
        profit: Realized profit (only for sells, 0.0 for buys).
        stamp_tax: Stamp tax (only for sells, 0.0 for buys).
    """

    date: Any
    direction: str
    price: float
    quantity: int
    commission: float
    profit: float = 0.0
    stamp_tax: float = 0.0


@dataclass
class BacktestResult:
    """Backtest result.

    Attributes:
        symbol: Stock code.
        strategy_name: Strategy name.
        trades: List of Trade records (both buys and sells).
        equity_curve: Daily equity values indexed by date.
        metrics: Performance metrics dict from calc_metrics.
        cognitive_log: List of cognitive engine decisions (stage/danger/trailing stop).
    """

    symbol: str
    strategy_name: str
    trades: list[Trade]
    equity_curve: pd.Series
    metrics: dict[str, float]
    cognitive_log: list[dict] = field(default_factory=list)


class BacktestEngine:
    """Backtest engine with realistic A-share transaction costs.

    Position sizing is capped by ``position_ratio`` (default from risk config)
    to stay consistent with the L1 single-position limit.
    P1-m 修复：已接入 L3 风控（涨跌停检查），涨停时不可买入，跌停时不可卖出。

    Example:
        >>> engine = BacktestEngine()
        >>> result = engine.run(strategy, df, symbol="600519")
        >>> print(result.metrics["total_return"])
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        commission_rate: float = 0.00025,
        stamp_tax: float = 0.0005,  # P1-b: 2023-08-28 起减半为万5
        slippage: float = 0.002,
        position_ratio: float | None = None,
        enable_wisdom: bool = False,
        enable_cognitive_engine: bool = False,
    ) -> None:
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax = stamp_tax
        self.slippage = slippage
        # 默认与风控 L1 保持一致，避免回测与模拟盘行为脱节
        if position_ratio is None:
            position_ratio = float(
                settings().get("risk", {}).get("max_position_ratio", 0.30)
            )
        self.position_ratio = max(0.0, min(1.0, position_ratio))
        # P1-m 修复：回测接入 L3 风控（涨跌停检查），与模拟盘行为一致
        self._risk = RiskManager()
        self._logger = get_logger()

        # BIZ-02 修复：回测引擎可选接入 wisdom 决策层
        self.enable_wisdom = enable_wisdom
        self._wisdom = None
        if enable_wisdom:
            try:
                from gugu.wisdom import WisdomAdvisor

                self._wisdom = WisdomAdvisor()
                self._logger.info("回测引擎已启用 wisdom 决策层")
            except Exception as e:
                self._logger.warning(f"回测引擎加载 wisdom 失败，回退到纯策略模式: {e}")
                self.enable_wisdom = False

        # P0 认知引擎集成：四阶段判断 / 危险信号 / 向下摊平 / 移动止损
        self.enable_cognitive_engine = enable_cognitive_engine
        self._stage_detector = None
        self._danger_detector = None
        self._no_avg_down_checker = None
        self._trailing_stop_engine = None
        if enable_cognitive_engine:
            try:
                from gugu.analysis.stage_detector import StageDetector
                from gugu.analysis.danger_signal import DangerSignalDetector
                from gugu.analysis.no_average_down import NoAverageDownChecker
                from gugu.analysis.trailing_stop import TrailingStopEngine

                self._stage_detector = StageDetector()
                self._danger_detector = DangerSignalDetector()
                self._no_avg_down_checker = NoAverageDownChecker()
                self._trailing_stop_engine = TrailingStopEngine()
                self._logger.info("回测引擎已启用 P0 认知引擎（四阶段/危险信号/移动止损）")
            except Exception as e:
                self._logger.warning(f"回测引擎加载认知引擎失败，回退到纯策略模式: {e}")
                self.enable_cognitive_engine = False

    def run(self, strategy: Strategy, df: pd.DataFrame, symbol: str) -> BacktestResult:
        """Run backtest for a single symbol.

        Iterates row by row, generates signals on the expanding window
        ``df.iloc[:i+1]`` to avoid lookahead bias, and executes trades
        based on the signal at each bar.

        Args:
            strategy: Strategy instance with generate_signals method.
            df: OHLCV data with columns date, open, high, low, close, volume, amount.
            symbol: Stock code.

        Returns:
            BacktestResult with trades, equity curve, and metrics.
        """
        if len(df) == 0:
            self._logger.warning(f"Empty data for {symbol}, returning empty result")
            empty_curve = pd.Series(dtype=float)
            return BacktestResult(
                symbol=symbol,
                strategy_name=strategy.name,
                trades=[],
                equity_curve=empty_curve,
                metrics=calc_metrics(empty_curve, []),
            )

        cash = self.initial_capital
        position_qty = 0
        position_cost = 0.0  # total cost basis for current position
        buy_date: pd.Timestamp | None = None  # T+1: 记录买入日期
        trades: list[Trade] = []
        equity_values: list[float] = []
        dates: list[pd.Timestamp] = []
        cognitive_log: list[dict] = []

        # 认知引擎状态
        trailing_state = None  # TrailingStopState

        # 一次性对全 df 生成信号，避免 O(n²) 重算（D-05 修复）
        # 策略内部使用 .shift(1) 避免前视偏差，全量生成与逐行 expanding 结果一致
        signals_df = strategy.generate_signals(df)

        for i in range(len(df)):
            row = df.iloc[i]
            current_date = row["date"]
            close = float(row["close"])
            # P1-m: L3 涨跌停检查需要前一日收盘价
            prev_close = float(df.iloc[i - 1]["close"]) if i > 0 else close

            # 从预计算的信号 DataFrame 中读取第 i 行信号
            signal = int(signals_df["signal"].iloc[i])
            # P0-5 修复：从信号 DataFrame 读取真实置信度，而非硬编码 1.0
            confidence = 1.0
            if "confidence" in signals_df.columns:
                try:
                    confidence = float(signals_df["confidence"].iloc[i])
                except (KeyError, ValueError, TypeError):
                    confidence = 1.0

            # P0-3/P0-4/P1-j/P1-k 修复：支持加仓，正确传递 has_position/current_position_ratio
            # ===== 认知引擎：移动止损检查（每日）=====
            cognitive_skip_sell = False
            if (
                self.enable_cognitive_engine
                and self._trailing_stop_engine is not None
                and trailing_state is not None
                and position_qty > 0
            ):
                try:
                    from gugu.analysis.trailing_stop import TrailingStopSignal

                    window_df = df.iloc[max(0, i - 60):i + 1]
                    new_state, ts_signal = self._trailing_stop_engine.update(
                        trailing_state, window_df
                    )
                    trailing_state = new_state

                    if ts_signal == TrailingStopSignal.EXIT:
                        cognitive_log.append({
                            "date": str(current_date),
                            "action": "trailing_stop_exit",
                            "stop_price": trailing_state.current_stop_price,
                            "close": close,
                            "signal": ts_signal.value,
                        })
                        # 执行止损卖出
                        if buy_date is not None and pd.Timestamp(current_date) <= buy_date:
                            pass  # T+1
                        elif self._risk.is_tradable(
                            symbol, close, prev_close, direction="sell"
                        ):
                            trade = self._execute_sell(
                                symbol, current_date, close, position_qty, position_cost
                            )
                            trades.append(trade)
                            proceeds = (
                                trade.price * trade.quantity
                                - trade.commission
                                - trade.stamp_tax
                            )
                            cash += proceeds
                            position_qty = 0
                            position_cost = 0.0
                            buy_date = None
                            trailing_state = None
                            cognitive_skip_sell = True
                except Exception as e:
                    self._logger.debug(f"回测移动止损更新失败: {e}")

            # Execute trades based on signal
            if signal == 1 and not cognitive_skip_sell:
                # 认知引擎：入场前检查
                cognitive_skip_buy = False
                if self.enable_cognitive_engine and self._stage_detector is not None:
                    try:
                        from gugu.analysis.stage_detector import MarketStage

                        window_df = df.iloc[max(0, i - 80):i + 1]
                        stage_result = self._stage_detector.detect(window_df)
                        if stage_result.stage in (MarketStage.FRENZY, MarketStage.FINAL):
                            cognitive_log.append({
                                "date": str(current_date),
                                "action": "stage_filter",
                                "stage": stage_result.stage.value,
                                "reason": stage_result.description,
                            })
                            cognitive_skip_buy = True
                    except Exception:
                        pass

                # 认知引擎：危险信号检查
                if not cognitive_skip_buy and self._danger_detector is not None:
                    try:
                        window_df = df.iloc[max(0, i - 20):i + 1]
                        danger_result = self._danger_detector.detect(
                            window_df, prev_close=prev_close
                        )
                        if danger_result.has_signal and danger_result.severity in (
                            "medium",
                            "high",
                        ):
                            cognitive_log.append({
                                "date": str(current_date),
                                "action": "danger_filter",
                                "signals": danger_result.signals,
                                "severity": danger_result.severity,
                            })
                            cognitive_skip_buy = True
                    except Exception:
                        pass

                # 认知引擎：向下摊平检查
                if not cognitive_skip_buy and self._no_avg_down_checker is not None:
                    if position_qty > 0 and position_cost > 0:
                        avg_cost = position_cost / position_qty
                        avg_down_result = self._no_avg_down_checker.check(
                            symbol=symbol,
                            has_position=True,
                            cost_price=avg_cost,
                            current_price=close,
                            quantity=position_qty,
                        )
                        if not avg_down_result.allowed:
                            cognitive_log.append({
                                "date": str(current_date),
                                "action": "no_avg_down",
                                "reason": avg_down_result.reason,
                            })
                            cognitive_skip_buy = True

                if cognitive_skip_buy:
                    pass  # 认知引擎拦截，跳过买入
                # 启用 wisdom 时，先经过决策层过滤
                elif self.enable_wisdom and self._wisdom is not None:
                    # P1-j 修复：仓位基准与模拟盘一致（max_ratio * 0.8）
                    max_ratio = float(
                        settings().get("risk", {}).get("max_position_ratio", 0.30)
                    )
                    base_ratio = max_ratio * 0.8
                    # P0-3 修复：正确传递 has_position 和 current_position_ratio
                    current_position_ratio = (
                        position_qty * close / (cash + position_qty * close)
                        if (cash + position_qty * close) > 0
                        else 0.0
                    )
                    wisdom_signal = {
                        "symbol": symbol,
                        "direction": "buy",
                        "price": close,
                        "confidence": confidence,  # P0-5: 真实置信度
                        "strategy": strategy.name,
                        "strategies": [strategy.name],
                        "reason": "backtest",
                        "suggested_position_ratio": base_ratio,  # P1-j: 与模拟盘一致
                        "has_position": position_qty > 0,  # P0-3: 真实持仓状态
                        "current_position_ratio": current_position_ratio,
                    }
                    enhanced = self._wisdom.advise(wisdom_signal)
                    # wisdom 过滤则跳过买入
                    if enhanced.get("wisdom_filtered"):
                        continue
                    # P1-j 修复：仓位计算与模拟盘一致，按总资产 * adjusted_ratio
                    adjusted_ratio = enhanced.get("suggested_position_ratio", base_ratio)
                    equity = cash + position_qty * close
                    buy_cash = equity * adjusted_ratio
                else:
                    buy_cash = cash

                # P1-m: L3 涨跌停检查（涨停时不可买入，与模拟盘一致）
                if not self._risk.is_tradable(
                    symbol, close, prev_close, direction="buy"
                ):
                    self._logger.debug(
                        f"SKIP BUY {symbol}: 涨停 close={close} prev_close={prev_close}"
                    )
                else:
                    # Buy with adjusted cash (wisdom may reduce position)
                    trade = self._execute_buy(symbol, current_date, close, buy_cash)
                    if trade is not None:
                        trades.append(trade)
                        cash -= trade.price * trade.quantity + trade.commission
                        # P1-k 修复：支持加仓，正确更新 avg_cost 和 position_qty
                        if position_qty > 0:
                            # 加仓：更新加权平均成本
                            position_cost = (
                                (position_cost + trade.price * trade.quantity + trade.commission)
                            )
                        else:
                            position_cost = trade.price * trade.quantity + trade.commission
                            buy_date = pd.Timestamp(current_date)
                            # 认知引擎：初始化移动止损
                            if (
                                self.enable_cognitive_engine
                                and self._trailing_stop_engine is not None
                            ):
                                trailing_state = self._trailing_stop_engine.init_stop(
                                    entry_price=trade.price
                                )
                        position_qty += trade.quantity

            elif signal == -1 and position_qty > 0 and not cognitive_skip_sell:
                # T+1: 买入当天不能卖出
                if buy_date is not None and pd.Timestamp(current_date) <= buy_date:
                    pass  # 跳过当天卖出
                # P1-m: L3 涨跌停检查（跌停时不可卖出，与模拟盘一致）
                elif not self._risk.is_tradable(
                    symbol, close, prev_close, direction="sell"
                ):
                    self._logger.debug(
                        f"SKIP SELL {symbol}: 跌停 close={close} prev_close={prev_close}"
                    )
                else:
                    # Sell all shares (clear position)
                    trade = self._execute_sell(symbol, current_date, close, position_qty, position_cost)
                    trades.append(trade)
                    proceeds = trade.price * trade.quantity - trade.commission - trade.stamp_tax
                    cash += proceeds
                    position_qty = 0
                    position_cost = 0.0
                    buy_date = None
                    trailing_state = None  # 清除移动止损状态

            # Record daily equity (mark-to-market at close)
            equity = cash + position_qty * close
            equity_values.append(equity)
            dates.append(pd.Timestamp(current_date))

        equity_curve = pd.Series(equity_values, index=dates, name="equity")
        metrics = calc_metrics(equity_curve, trades)

        self._logger.info(
            f"Backtest {symbol} / {strategy.name}: "
            f"trades={len(trades)} "
            f"total_return={metrics['total_return']:.2%} "
            f"sharpe={metrics['sharpe']:.4f} "
            f"max_drawdown={metrics['max_drawdown']:.2%}"
        )

        return BacktestResult(
            symbol=symbol,
            strategy_name=strategy.name,
            trades=trades,
            equity_curve=equity_curve,
            metrics=metrics,
            cognitive_log=cognitive_log,
        )

    def _execute_buy(
        self,
        symbol: str,
        date: Any,
        close: float,
        cash: float,
    ) -> Trade | None:
        """Execute a buy order with slippage and commission.

        Buy price = close * (1 + slippage)
        Quantity is rounded down to the nearest board lot (100 shares).
        买入金额受 position_ratio 限制，与风控 L1 单股上限保持一致。
        保底机制：常规计算为 0 但现金够买 100 股时，买入 100 股
        （与 TradingEngine._process_signal 的行为一致）。

        Returns None if not enough cash for even one lot.
        """
        buy_price = close * (1 + self.slippage)
        cost_per_share = buy_price * (1 + self.commission_rate)
        max_value = min(cash, self.initial_capital * self.position_ratio)
        max_qty = int(max_value / cost_per_share)
        quantity = (max_qty // _BOARD_LOT) * _BOARD_LOT
        if quantity <= 0:
            # 保底：常规计算为 0 但现金够买 100 股时，买入 100 股
            if cash >= buy_price * _BOARD_LOT:
                quantity = _BOARD_LOT
                self._logger.debug(
                    f"MIN LOT BUY {symbol}: regular calc=0, using 1 lot at {buy_price:.2f}"
                )
            else:
                self._logger.debug(
                    f"SKIP BUY {symbol}: cash={cash:.2f} not enough for 1 lot at {buy_price:.2f}"
                )
                return None

        commission = buy_price * quantity * self.commission_rate
        self._logger.debug(
            f"BUY {symbol} {quantity}@{buy_price:.2f} commission={commission:.2f}"
        )
        return Trade(
            date=date,
            direction="buy",
            price=buy_price,
            quantity=quantity,
            commission=commission,
        )

    def _execute_sell(
        self,
        symbol: str,
        date: Any,
        close: float,
        quantity: int,
        cost_basis: float,
    ) -> Trade:
        """Execute a sell order with slippage, commission, and stamp tax.

        Sell price = close * (1 - slippage)
        Stamp tax is only applied to sells (A-share rule).
        """
        sell_price = close * (1 - self.slippage)
        commission = sell_price * quantity * self.commission_rate
        stamp_tax = sell_price * quantity * self.stamp_tax
        proceeds = sell_price * quantity - commission - stamp_tax
        profit = proceeds - cost_basis

        self._logger.debug(
            f"SELL {symbol} {quantity}@{sell_price:.2f} "
            f"commission={commission:.2f} tax={stamp_tax:.2f} profit={profit:.2f}"
        )
        return Trade(
            date=date,
            direction="sell",
            price=sell_price,
            quantity=quantity,
            commission=commission,
            profit=profit,
            stamp_tax=stamp_tax,
        )
