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

from gugu.config import get_config
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
        strict_confirmation: bool = False,
    ) -> None:
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.stamp_tax = stamp_tax
        self.slippage = slippage
        # 默认与风控 L1 保持一致，避免回测与模拟盘行为脱节
        if position_ratio is None:
            position_ratio = get_config().risk.max_position_ratio
        self.position_ratio = max(0.0, min(1.0, position_ratio))
        # P1-m 修复：回测接入 L3 风控（涨跌停检查），与模拟盘行为一致
        self._risk = RiskManager()
        self._logger = get_logger()
        # 缺陷B 修复：strict_confirmation=True 时回测使用 is_backtest=False，
        # 模拟生产环境的次日确认机制，消除回测与生产行为不一致
        self.strict_confirmation = strict_confirmation

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
                from gugu.analysis.danger_signal import DangerSignalDetector
                from gugu.analysis.no_average_down import NoAverageDownChecker
                from gugu.analysis.stage_detector import StageDetector
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
        # P1 修复：shift(1) 确保第 i 天信号基于 <= i-1 天的数据，
        # 消除 rolling 窗口含当日收盘价引入的隐性前视偏差。
        # 即使策略内部使用 .shift(1)，移动平均类指标（SMA/布林带等）
        # 默认含当日值，shift(1) 信号列保证回测不"看到未来"。
        # 缺陷B 修复：strict_confirmation=True 时用 is_backtest=False，
        # 让回测走生产环境的次日确认流程，保证回测与生产行为一致
        is_bt = not self.strict_confirmation
        signals_df = strategy.generate_signals(df, is_backtest=is_bt)
        signals_df["signal"] = signals_df["signal"].shift(1).fillna(0)
        # P1 修复：验证信号值在 {-1, 0, 1} 范围内，非标准值强制截断到合法区间
        raw = signals_df["signal"]
        invalid = ~raw.isin([-1, 0, 1])
        if invalid.any():
            self._logger.warning(
                f"策略 {strategy.name} 返回非标准信号值，"
                f"已截断到 [-1,1]: {raw[invalid].head(5).tolist()}"
            )
            raw[raw > 1] = 1
            raw[raw < -1] = -1
        signals_df["signal"] = raw.astype(int)

        # P1-9 性能修复：预提取列到 numpy 数组，避免循环内 df.iloc[i] 触发
        # pandas 内部 ExtensionArray + isinstance/typing 重新评估（cProfile 显示占 57% 耗时）
        # 原代码每次 df.iloc[i] 耗时 ~0.4ms（含 200+ 次内部 isinstance），
        # 改为 numpy 索引后单次 ~1μs，预期 30x 加速
        n_rows = len(df)
        close_arr = df["close"].to_numpy(dtype=float)
        date_arr = df["date"].to_numpy()
        signal_arr = signals_df["signal"].to_numpy(dtype=int)
        if "confidence" in signals_df.columns:
            confidence_arr = signals_df["confidence"].to_numpy(dtype=float)
        else:
            confidence_arr = None

        for i in range(n_rows):
            current_date = date_arr[i]
            close = float(close_arr[i])
            # P1-m: L3 涨跌停检查需要前一日收盘价
            prev_close = float(close_arr[i - 1]) if i > 0 else close

            # 从预计算的 numpy 数组中读取第 i 行信号
            signal = int(signal_arr[i])
            # P0-5 修复：从信号 DataFrame 读取真实置信度，而非硬编码 1.0
            confidence = (
                float(confidence_arr[i])
                if confidence_arr is not None
                else 1.0
            )

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

                    window_df = df.iloc[max(0, i - 60) : i + 1]
                    # P0 修复：回测中同步检测危险信号并传入移动止损引擎
                    # 确保回测与实盘移动止损行为一致（实盘在 _check_stop_loss 中传入 pos.danger_signals）
                    ts_danger_signals: list[str] = []
                    try:
                        ts_danger_result = self._danger_detector.detect(
                            window_df, prev_close=prev_close
                        )
                        if ts_danger_result.has_signal:
                            ts_danger_signals = ts_danger_result.signals
                    except Exception:
                        pass
                    new_state, ts_signal = self._trailing_stop_engine.update(
                        trailing_state, window_df, ts_danger_signals
                    )
                    trailing_state = new_state

                    if ts_signal == TrailingStopSignal.EXIT:
                        cognitive_log.append(
                            {
                                "date": str(current_date),
                                "action": "trailing_stop_exit",
                                "stop_price": trailing_state.current_stop_price,
                                "close": close,
                                "signal": ts_signal.value,
                            }
                        )
                        # 执行止损卖出
                        if buy_date is not None and pd.Timestamp(current_date) <= buy_date:
                            pass  # T+1
                        elif self._risk.is_tradable(symbol, close, prev_close, direction="sell"):
                            trade = self._execute_sell(
                                symbol, current_date, close, position_qty, position_cost
                            )
                            trades.append(trade)
                            proceeds = (
                                trade.price * trade.quantity - trade.commission - trade.stamp_tax
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

                        window_df = df.iloc[max(0, i - 80) : i + 1]
                        stage_result = self._stage_detector.detect(window_df)
                        if stage_result.stage in (MarketStage.FRENZY, MarketStage.FINAL):
                            cognitive_log.append(
                                {
                                    "date": str(current_date),
                                    "action": "stage_filter",
                                    "stage": stage_result.stage.value,
                                    "reason": stage_result.description,
                                }
                            )
                            cognitive_skip_buy = True
                    except Exception as e:
                        self._logger.debug(f"Stage detection failed for {symbol}: {e}")

                # 认知引擎：危险信号检查
                if not cognitive_skip_buy and self._danger_detector is not None:
                    try:
                        window_df = df.iloc[max(0, i - 20) : i + 1]
                        danger_result = self._danger_detector.detect(
                            window_df, prev_close=prev_close
                        )
                        if danger_result.has_signal and danger_result.severity in (
                            "medium",
                            "high",
                        ):
                            cognitive_log.append(
                                {
                                    "date": str(current_date),
                                    "action": "danger_filter",
                                    "signals": danger_result.signals,
                                    "severity": danger_result.severity,
                                }
                            )
                            cognitive_skip_buy = True
                    except Exception as e:
                        self._logger.debug(f"Danger detection failed for {symbol}: {e}")

                # 认知引擎：向下摊平检查
                if (
                    not cognitive_skip_buy
                    and self._no_avg_down_checker is not None
                    and position_qty > 0
                    and position_cost > 0
                ):
                    avg_cost = position_cost / position_qty
                    avg_down_result = self._no_avg_down_checker.check(
                        symbol=symbol,
                        has_position=True,
                        cost_price=avg_cost,
                        current_price=close,
                        quantity=position_qty,
                    )
                    if not avg_down_result.allowed:
                        cognitive_log.append(
                            {
                                "date": str(current_date),
                                "action": "no_avg_down",
                                "reason": avg_down_result.reason,
                            }
                        )
                        cognitive_skip_buy = True

                if cognitive_skip_buy:
                    continue  # P1 修复：认知引擎拦截，跳过买入（避免后续 buy_cash 未定义错误）
                # 启用 wisdom 时，先经过决策层过滤
                elif self.enable_wisdom and self._wisdom is not None:
                    # P1-j 修复：仓位基准与模拟盘一致（max_ratio * 0.8）
                    max_ratio = get_config().risk.max_position_ratio
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
                if not self._risk.is_tradable(symbol, close, prev_close, direction="buy"):
                    self._logger.debug(
                        f"SKIP BUY {symbol}: 涨停 close={close} prev_close={prev_close}"
                    )
                else:
                    # Buy with adjusted cash (wisdom may reduce position)
                    equity = cash + position_qty * close
                    trade = self._execute_buy(
                        symbol, current_date, close, buy_cash, current_equity=equity
                    )
                    if trade is not None:
                        trades.append(trade)
                        cash -= trade.price * trade.quantity + trade.commission
                        # P1-k 修复：支持加仓，正确更新 avg_cost 和 position_qty
                        if position_qty > 0:
                            # 加仓：更新加权平均成本
                            position_cost = (
                                position_cost + trade.price * trade.quantity + trade.commission
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
                elif not self._risk.is_tradable(symbol, close, prev_close, direction="sell"):
                    self._logger.debug(
                        f"SKIP SELL {symbol}: 跌停 close={close} prev_close={prev_close}"
                    )
                else:
                    # Sell all shares (clear position)
                    trade = self._execute_sell(
                        symbol, current_date, close, position_qty, position_cost
                    )
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
        *,
        current_equity: float | None = None,
    ) -> Trade | None:
        """Execute a buy order with slippage and commission.

        Buy price = close * (1 + slippage)
        Quantity is rounded down to the nearest board lot (100 shares).
        买入金额受 position_ratio 限制，与风控 L1 单股上限保持一致。
        保底机制：常规计算为 0 但现金够买 100 股时，买入 100 股
        （与 TradingEngine._process_signal 的行为一致）。

        Args:
            current_equity: 当前账户总净值。若未传则退回到 initial_capital。

        Returns None if not enough cash for even one lot.
        """
        buy_price = close * (1 + self.slippage)
        cost_per_share = buy_price * (1 + self.commission_rate)
        ref = current_equity if current_equity is not None else self.initial_capital
        max_value = min(cash, ref * self.position_ratio)
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
        self._logger.debug(f"BUY {symbol} {quantity}@{buy_price:.2f} commission={commission:.2f}")
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


def walk_forward_optimize(
    strategy_cls: type,
    df: pd.DataFrame,
    symbol: str = "",
    param_grid: dict[str, list] | None = None,
    train_ratio: float = 0.6,
    n_splits: int = 3,
    param_constraints: callable[[dict], bool] | None = None,
) -> list[dict]:
    """Walk-forward 参数优化 + 滚动前向验证。

    将历史数据按时间顺序分为 n_splits 个滑窗，
    每窗用前 60% 训练（grid search 最优参数），
    后 40% 测试（记录真实样本外表现）。

    Args:
        strategy_cls: Strategy 类（非实例）。
        df: 完整历史 OHLCV 数据。
        symbol: 股票代码。
        param_grid: 参数网格（如 {"breakout_window": [10,20,30]}）。
        train_ratio: 训练集占比。
        n_splits: 滚动窗口数。
        param_constraints: 可选的参数约束函数 (params_dict) -> bool，
            返回 False 的参数组合将被跳过（如 short_window >= long_window）。

    Returns:
        每个窗口的优化结果列表 [{window, best_params, train_metrics, test_metrics}, ...]
    """
    import itertools

    if param_grid is None:
        param_grid = {}

    n = len(df)
    if n < 120:
        return [{"error": "数据不足 120 行，无法做 walk-forward"}]

    window_size = n // n_splits
    step = max(1, n // (n_splits * 2))
    results: list[dict] = []

    logger = get_logger()

    for w in range(n_splits):
        split_start = w * step
        train_end = split_start + int(window_size * train_ratio)
        test_start = train_end
        test_end = min(split_start + window_size, n)

        if test_end - test_start < 20:
            break

        train_df = df.iloc[split_start:train_end]
        test_df = df.iloc[test_start:test_end]

        best_params: dict[str, Any] = {}
        best_sharpe = -999.0

        # Grid search on train set
        keys = list(param_grid.keys())
        if keys:
            for values in itertools.product(*param_grid.values()):
                params = dict(zip(keys, values, strict=True))
                # P1 修复：参数约束过滤（如 short_window >= long_window 跳过）
                if param_constraints is not None and not param_constraints(params):
                    continue
                try:
                    strategy = strategy_cls(params)
                    engine = BacktestEngine()
                    result = engine.run(strategy, train_df, symbol)
                    sharpe = result.metrics.get("sharpe", 0)
                    if sharpe > best_sharpe:
                        best_sharpe = sharpe
                        best_params = dict(params)
                except Exception as e:
                    logger.debug(f"Grid search failed for {params}: {e}")
                    continue
        else:
            best_params = {}
            try:
                strategy = strategy_cls({})
                engine = BacktestEngine()
                result = engine.run(strategy, train_df, symbol)
                best_sharpe = result.metrics.get("sharpe", 0)
            except Exception:
                best_sharpe = 0.0

        # Test on out-of-sample
        test_strategy = strategy_cls(best_params)
        engine = BacktestEngine()
        test_result = engine.run(test_strategy, test_df, symbol)

        results.append(
            {
                "window": w + 1,
                "train_start": split_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
                "best_params": best_params,
                "train_sharpe": round(best_sharpe, 4),
                "test_sharpe": round(test_result.metrics.get("sharpe", 0), 4),
                "test_return": round(test_result.metrics.get("total_return", 0), 4),
                "test_max_dd": round(test_result.metrics.get("max_drawdown", 0), 4),
                "test_win_rate": round(test_result.metrics.get("win_rate", 0), 4),
                "trades": len(test_result.trades),
            }
        )

    logger.info(
        f"Walk-forward 完成: {len(results)} 窗口, "
        f"平均测试 Sharpe {sum(r['test_sharpe'] for r in results) / max(len(results), 1):.4f}"
    )
    return results


def walk_forward_optimize_multi_stock(
    strategy_cls: type,
    stock_dfs: dict[str, pd.DataFrame],
    param_grid: dict[str, list] | None = None,
    train_ratio: float = 0.6,
    n_splits: int = 3,
    param_constraints: callable[[dict], bool] | None = None,
) -> list[dict]:
    """多股聚合 Walk-Forward 优化（解决单股过拟合问题）。

    第一性原理：
    - 单股 Walk-Forward 在 600519 上训练 Sharpe 5.01 vs 测试 -6.28 = 典型过拟合
    - 根因：单只股票的局部行情特征被误认为"普适规律"
    - 修复：在多只股票上聚合训练（每窗口对每只股票独立回测，
      取平均 Sharpe 作为该参数组的训练表现），找到跨股票稳健的参数

    对抗性审查：
    - 副作用1：多股训练耗时 = 单股 × N（N=股票数）
      缓解：限制股票数 ≤10，且并发执行（asyncio 无效，因为 BacktestEngine 是 CPU 密集型，
      改用 ProcessPoolExecutor 可加速，但本实现先保持串行，后续可优化）
    - 副作用2：多股聚合可能掩盖单股极端表现
      缓解：返回各股明细，便于诊断
    - 副作用3：参数稳定性判定变复杂
      缓解：投票机制改为"在 ≥50% 股票的 ≥50% 窗口被选中"

    Args:
        strategy_cls: 策略类
        stock_dfs: {symbol: DataFrame}，建议 5-10 只不同行业代表股
        param_grid: 参数网格
        train_ratio: 训练集占比
        n_splits: 滚动窗口数

    Returns:
        每个窗口的聚合优化结果列表（含各股明细）
    """
    import itertools

    if param_grid is None:
        param_grid = {}

    if not stock_dfs:
        return [{"error": "stock_dfs 为空"}]

    logger = get_logger()
    symbols = list(stock_dfs.keys())
    n_stocks = len(symbols)
    logger.info(f"多股聚合 Walk-Forward: {n_stocks} 只股票, {n_splits} 窗口")

    # 对齐所有股票的数据长度（取最短的）
    min_len = min(len(df) for df in stock_dfs.values())
    if min_len < 120:
        return [{"error": f"最短股票数据 {min_len} 行 < 120，无法做 walk-forward"}]

    # 用同一时间索引切分窗口
    window_size = min_len // n_splits
    step = max(1, min_len // (n_splits * 2))
    results: list[dict] = []

    for w in range(n_splits):
        split_start = w * step
        train_end = split_start + int(window_size * train_ratio)
        test_start = train_end
        test_end = min(split_start + window_size, min_len)

        if test_end - test_start < 20:
            break

        # 对每只股票切分训练/测试数据
        train_dfs = {s: df.iloc[split_start:train_end] for s, df in stock_dfs.items()}
        test_dfs = {s: df.iloc[test_start:test_end] for s, df in stock_dfs.items()}

        best_params: dict[str, Any] = {}
        best_avg_sharpe = -999.0

        # Grid search：对每组参数，在所有股票上训练并取平均 Sharpe
        keys = list(param_grid.keys())
        if keys:
            for values in itertools.product(*param_grid.values()):
                params = dict(zip(keys, values, strict=True))
                # P1 修复：参数约束过滤（如 short_window >= long_window 跳过）
                if param_constraints is not None and not param_constraints(params):
                    continue
                try:
                    train_sharpes: list[float] = []
                    for sym, tdf in train_dfs.items():
                        strategy = strategy_cls(params)
                        engine = BacktestEngine()
                        result = engine.run(strategy, tdf, symbol=sym)
                        train_sharpes.append(result.metrics.get("sharpe", 0))
                    # 聚合指标：平均 Sharpe（避免单股极端值影响）
                    avg_train_sharpe = sum(train_sharpes) / len(train_sharpes)
                    if avg_train_sharpe > best_avg_sharpe:
                        best_avg_sharpe = avg_train_sharpe
                        best_params = dict(params)
                except Exception as e:
                    logger.debug(f"多股 grid search failed for {params}: {e}")
                    continue
        else:
            best_params = {}
            train_sharpes = []
            for sym, tdf in train_dfs.items():
                try:
                    strategy = strategy_cls({})
                    engine = BacktestEngine()
                    result = engine.run(strategy, tdf, symbol=sym)
                    train_sharpes.append(result.metrics.get("sharpe", 0))
                except Exception:
                    train_sharpes.append(0.0)
            best_avg_sharpe = sum(train_sharpes) / max(len(train_sharpes), 1)

        # 在测试集上验证最优参数（每只股票独立测试，记录明细）
        test_details: list[dict] = []
        test_sharpes: list[float] = []
        test_returns: list[float] = []
        test_max_dds: list[float] = []
        test_win_rates: list[float] = []

        for sym, tdf in test_dfs.items():
            try:
                test_strategy = strategy_cls(best_params)
                engine = BacktestEngine()
                test_result = engine.run(test_strategy, tdf, symbol=sym)
                sharpe = test_result.metrics.get("sharpe", 0)
                ret = test_result.metrics.get("total_return", 0)
                max_dd = test_result.metrics.get("max_drawdown", 0)
                win_rate = test_result.metrics.get("win_rate", 0)
                test_sharpes.append(sharpe)
                test_returns.append(ret)
                test_max_dds.append(max_dd)
                test_win_rates.append(win_rate)
                test_details.append({
                    "symbol": sym,
                    "test_sharpe": round(sharpe, 4),
                    "test_return": round(ret, 4),
                    "test_max_dd": round(max_dd, 4),
                    "test_win_rate": round(win_rate, 4),
                    "trades": len(test_result.trades),
                })
            except Exception as e:
                logger.debug(f"多股测试 {sym} 失败: {e}")

        n = max(len(test_sharpes), 1)
        results.append({
            "window": w + 1,
            "best_params": best_params,
            "train_avg_sharpe": round(best_avg_sharpe, 4),
            "test_avg_sharpe": round(sum(test_sharpes) / n, 4),
            "test_avg_return": round(sum(test_returns) / n, 4),
            "test_avg_max_dd": round(sum(test_max_dds) / n, 4),
            "test_avg_win_rate": round(sum(test_win_rates) / n, 4),
            "test_stock_count": len(test_details),
            "test_details": test_details,
        })

    logger.info(
        f"多股 Walk-forward 完成: {len(results)} 窗口 × {n_stocks} 股, "
        f"平均测试 Sharpe {sum(r['test_avg_sharpe'] for r in results) / max(len(results), 1):.4f}"
    )
    return results
