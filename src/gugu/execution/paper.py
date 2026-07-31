"""模拟盘 broker。

内存中维护现金、持仓、交易记录。模拟真实交易成本。
状态持久化到 JSON 文件，重启后自动恢复。

P3 优化：
- 部分成交模拟（大单批量分拆）
- 流动性感知滑点模型（小盘股滑点更高）
- 涨跌停成交概率模型
"""

from __future__ import annotations

import fcntl
import json
import math
import random
import threading
from datetime import date
from typing import Any

from gugu.config import PROJECT_ROOT, get_config
from gugu.execution.base import AccountInfo, BaseBroker, Direction, OrderResult
from gugu.models import Position
from gugu.utils.log import get_logger

logger = get_logger()

# 持久化文件路径
STATE_FILE = PROJECT_ROOT / "data" / "paper_broker_state.json"

# 流动性分级（按日均成交额大致估算）
# 格式：(threshold_market_value_billions, slippage_bps, partial_fill_probability)
# 市值越小，滑点越大，部分成交概率越高
LIQUIDITY_TIERS = [
    (100, 5, 0.02),  # 超大市值（>1000亿）：滑点0.05%，部分成交概率2%
    (30, 10, 0.05),  # 大市值（300-1000亿）：滑点0.1%，部分成交概率5%
    (10, 20, 0.10),  # 中市值（100-300亿）：滑点0.2%，部分成交概率10%
    (3, 40, 0.20),  # 小市值（30-100亿）：滑点0.4%，部分成交概率20%
    (1, 60, 0.30),  # 微小市值（10-30亿）：滑点0.6%，部分成交概率30%
]

# P1-3 修复：涨跌停改为硬阻断（与回测一致），概率常量已不再使用
# 保留注释作为历史记录
# 原概率常量：
# LIMIT_UP_FILL_PROB = 0.3   # 涨停时买入成交概率 30%
# LIMIT_DOWN_FILL_PROB = 0.3 # 跌停时卖出成交概率 30%


class PaperBroker(BaseBroker):
    """模拟盘 broker。"""

    def __init__(
        self,
        initial_capital: float | None = None,
        commission_rate: float | None = None,
        stamp_tax: float | None = None,
        slippage: float | None = None,
    ) -> None:
        cfg = get_config().execution.paper
        self._cash = float(initial_capital or cfg.initial_capital)
        self._commission_rate = float(commission_rate or cfg.commission_rate)
        self._stamp_tax = float(stamp_tax or cfg.stamp_tax)
        self._slippage = float(slippage or cfg.slippage)
        self._positions: dict[str, Position] = {}
        self._trades: list[dict[str, Any]] = []
        self._daily_start_value: float | None = None
        # 部分成交状态缓存 {symbol: market_value_in_billions}
        self._market_value_cache: dict[str, float] = {}
        # P1 修复：线程安全锁，保护 _cash / _positions / _trades
        self._lock = threading.RLock()
        # 从磁盘恢复状态
        self._load_state()

    def _get_liquidity_tier(self, symbol: str) -> tuple[int, float, float]:
        """获取股票的流动性等级参数。

        Returns:
            (slippage_bps, partial_fill_probability) 滑点基点（bps）和部分成交概率
        """
        if symbol in self._market_value_cache:
            market_value = self._market_value_cache[symbol]
        else:
            # 如果没有缓存，使用默认中等流动性
            # 实际可通过行情数据获取市值
            return 30, 0.15

        for threshold_billion, slippage_bps, partial_prob in LIQUIDITY_TIERS:
            if market_value >= threshold_billion:
                return slippage_bps, partial_prob
        # 最小市值（<10亿）
        return 80, 0.35

    def _simulate_partial_fill(
        self, quantity: int, fill_price: float, partial_prob: float
    ) -> tuple[int, float]:
        """模拟部分成交。

        Args:
            quantity: 请求数量
            fill_price: 成交价
            partial_prob: 部分成交概率

        Returns:
            (实际成交数量, 成交价)
            部分成交时价格按比例偏移（模拟抢筹/砸盘效应）
        """
        if partial_prob <= 0 or random.random() > partial_prob:
            return quantity, fill_price

        # 部分成交比例在 30%-80% 之间随机
        fill_ratio = random.uniform(0.3, 0.8)
        partial_qty = int(math.floor(quantity * fill_ratio / 100) * 100)
        if partial_qty < 100:
            partial_qty = 100 if quantity >= 100 else 0

        if partial_qty == 0:
            # 完全未成交（极低概率）
            logger.info("[模拟盘] 订单完全未成交（流动性不足），请求量={}", quantity)
            return 0, fill_price

        logger.info(
            "[模拟盘] 部分成交: 请求{}股 → 实际{}股 (比例={:.0%})",
            quantity,
            partial_qty,
            fill_ratio,
        )
        return partial_qty, fill_price

    def _estimate_market_value(self, symbol: str, price: float, quantity: int) -> None:
        """估算股票市值（用于滑点计算）。"""
        trade_value = price * quantity
        market_value = trade_value / 0.01  # 简单估算：一次交易量约为流通市值的 1%
        if symbol not in self._market_value_cache:
            self._market_value_cache[symbol] = market_value
        # 更新缓存（指数移动平滑）
        old_val = self._market_value_cache[symbol]
        self._market_value_cache[symbol] = old_val * 0.7 + market_value * 0.3

    def _check_limit_tradable(
        self, direction: str, price: float, prev_close: float | None, symbol: str = ""
    ) -> bool:
        """检查涨跌停附近是否可成交。

        P1-3 修复：统一为硬阻断逻辑（与 BacktestEngine 的 RiskManager.is_tradable() 一致），
        消除回测-实盘涨跌停检查不一致的问题。

        Args:
            direction: 买卖方向
            price: 当前价格
            prev_close: 前收盘价
            symbol: 股票代码（用于判断板块）

        Returns:
            是否可成交（硬阻断：涨停不能买入，跌停不能卖出）
        """
        if prev_close is None or prev_close <= 0:
            return True

        change_pct = (price - prev_close) / prev_close

        limit_pct = self._get_limit_pct(symbol)

        # 涨停附近（>= limit_pct * 0.95）— 买入硬阻断
        if direction == "buy" and change_pct >= limit_pct * 0.95:
            return False

        # 跌停附近（<= -limit_pct * 0.95）— 卖出硬阻断
        if direction == "sell" and change_pct <= -limit_pct * 0.95:
            return False

        return True

    @staticmethod
    def _get_limit_pct(symbol: str) -> float:
        """根据股票代码前缀返回涨跌停幅度（P0 修复新增）。

        - 主板（60/00/30 开头）：±10%
        - 创业板（300/301）：±20%
        - 科创板（688/689）：±20%
        - 北交所（43/83/87/88/920）：±30%
        - ST/*ST：±5%（需外部标记 is_st，此处保守按 10%）
        """
        if not symbol:
            return 0.10  # 默认主板
        code = symbol.strip().zfill(6)
        if code.startswith(("300", "301", "688", "689")):
            return 0.20  # 创业板/科创板
        if code.startswith(("43", "83", "87", "88", "920")):
            return 0.30  # 北交所
        return 0.10  # 主板

    @staticmethod
    def _calc_transfer_fee(symbol: str, trade_value: float) -> float:
        """P0 修复：计算过户费（沪市+北交所双向 0.001%，2022年4月起减半）。

        沪市股票（600/601/603/605/688/689/9 开头）双向收取过户费 0.001%。
        北交所股票（8/4 开头）同样双向收取过户费 0.001%（与沪市规则一致）。
        深市不收取。

        与 BacktestEngine._calc_transfer_fee 保持一致。
        """
        if not symbol or trade_value <= 0:
            return 0.0
        code = symbol.strip().zfill(6)
        # 沪市 + 北交所前缀
        if code.startswith(("600", "601", "603", "605", "688", "689", "9", "8", "4")):
            return round(trade_value * 0.00001, 2)  # 0.001%
        return 0.0  # 深市无过户费

    def order(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        price: float | None = None,
    ) -> OrderResult:
        """下单。模拟成交，扣手续费/印花税/滑点，支持部分成交。"""
        with self._lock:
            return self._order_impl(symbol, direction, quantity, price)

    def _order_impl(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        price: float | None = None,
    ) -> OrderResult:
        symbol = symbol.strip().zfill(6)
        direction = direction.lower().strip()  # 规范化，与 RiskManager 保持一致
        if quantity <= 0 or quantity % 100 != 0:
            return OrderResult(
                False, symbol, direction, 0, 0, 0, message="数量必须为 100 的正整数倍"
            )

        # 获取现价
        cur_price = price
        if cur_price is None:
            pos = self._positions.get(symbol)
            cur_price = pos.current_price if pos else 0.0
        if cur_price <= 0:
            return OrderResult(False, symbol, direction, 0, 0, 0, message="无可用价格")

        # BUG-04 价格合理性防护：拒绝明显错误的价格
        if cur_price < 0.5:
            return OrderResult(
                False, symbol, direction, 0, 0, 0, message=f"价格异常低 ({cur_price:.2f})，拒绝下单"
            )
        if direction == "buy":
            pos = self._positions.get(symbol)
            if pos and pos.avg_cost > 0:
                deviation = max(cur_price, pos.avg_cost) / min(cur_price, pos.avg_cost)
                if deviation > 10.0:
                    return OrderResult(
                        False,
                        symbol,
                        direction,
                        0,
                        0,
                        0,
                        message=f"价格偏离持仓成本 {deviation:.1f}x (现价{cur_price:.2f} vs 成本{pos.avg_cost:.2f})，拒绝下单",
                    )

        # 获取前收盘价（T+1 / 涨跌停检查用）
        prev_close = None
        existing_pos = self._positions.get(symbol)
        if existing_pos:
            prev_close = existing_pos.prev_close or existing_pos.current_price

        # 流动性感知滑点
        slippage_bps, partial_prob = self._get_liquidity_tier(symbol)
        effective_slippage = max(self._slippage, slippage_bps / 10000)

        if direction == "buy":
            # 涨跌停成交检查
            if not self._check_limit_tradable("buy", cur_price, prev_close, symbol):
                logger.info(f"[模拟盘] 涨停附近{cur_price:.2f}，买入{quantity}股未成交(概率)")
                return OrderResult(False, symbol, direction, 0, 0, 0, message="涨停附近买入未成交")

            fill_price = cur_price * (1 + effective_slippage)
            # 大单部分成交模拟
            actual_quantity, fill_price = self._simulate_partial_fill(
                quantity, fill_price, partial_prob
            )
            if actual_quantity <= 0:
                return OrderResult(False, symbol, direction, 0, 0, 0, message="流动性不足，未成交")

            # 估算市值
            self._estimate_market_value(symbol, fill_price, actual_quantity)

            # P0 修复：过户费（沪市双向 0.001%，2022年4月起减半）
            transfer_fee = self._calc_transfer_fee(symbol, fill_price * actual_quantity)
            commission = fill_price * actual_quantity * self._commission_rate
            # P1 修复：最低佣金 5 元/笔
            commission = max(commission, 5.0)
            total_cost = fill_price * actual_quantity + commission + transfer_fee
            if total_cost > self._cash:
                # 如果是部分成交导致的资金不足，尝试进一步缩小数量
                if actual_quantity < quantity:
                    affordable = int(
                        math.floor(self._cash / (fill_price * (1 + self._commission_rate)) / 100)
                        * 100
                    )
                    if affordable >= 100:
                        actual_quantity = affordable
                        # P0修复：重新计算费用时包含过户费和最低佣金
                        transfer_fee = self._calc_transfer_fee(symbol, fill_price * actual_quantity)
                        commission = max(fill_price * actual_quantity * self._commission_rate, 5.0)
                        total_cost = fill_price * actual_quantity + commission + transfer_fee
                    else:
                        return OrderResult(
                            False,
                            symbol,
                            direction,
                            0,
                            0,
                            0,
                            message="资金不足（部分成交调整后仍不足）",
                        )
                else:
                    return OrderResult(False, symbol, direction, 0, 0, 0, message="资金不足")

            self._cash -= total_cost
            pos = self._positions.get(symbol)
            if pos:
                new_qty = pos.quantity + actual_quantity
                pos.avg_cost = (
                    pos.avg_cost * pos.quantity + fill_price * actual_quantity
                ) / new_qty
                pos.quantity = new_qty
                # 加仓后同步更新现价，避免止损检查用旧价
                pos.current_price = cur_price
                # T+1：追加买入的部分当日仍不可卖，旧持仓的 available 不变
            else:
                self._positions[symbol] = Position(
                    symbol=symbol,
                    quantity=actual_quantity,
                    available=0,  # T+1
                    avg_cost=fill_price,
                    current_price=cur_price,
                )

            result = OrderResult(
                True,
                symbol,
                direction,
                fill_price,
                actual_quantity,
                commission,
                0,
                transfer_fee,
                f"买入成功{'（部分成交）' if actual_quantity < quantity else ''}",
            )
            logger.info(
                f"[模拟盘] 买入 {symbol} {actual_quantity}股 @ {fill_price:.2f} "
                f"佣金 {commission:.2f} 过户费 {transfer_fee:.2f} 滑点={effective_slippage:.4f}"
                f"{'（部分成交）' if actual_quantity < quantity else ''}"
            )

        elif direction == "sell":
            pos = self._positions.get(symbol)
            if not pos or pos.available < 100:
                return OrderResult(False, symbol, direction, 0, 0, 0, message="可卖数量不足（T+1）")

            # 跌停成交检查
            if not self._check_limit_tradable("sell", cur_price, prev_close, symbol):
                logger.info("[模拟盘] 跌停附近{:.2f}，卖出{}股未成交(概率)", cur_price, quantity)
                return OrderResult(False, symbol, direction, 0, 0, 0, message="跌停附近卖出未成交")

            fill_price = cur_price * (1 - effective_slippage)
            # 限制卖出不超出可卖数量
            actual_quantity = min(quantity, pos.available)
            # 部分成交模拟
            actual_quantity, fill_price = self._simulate_partial_fill(
                actual_quantity, fill_price, partial_prob
            )
            if actual_quantity <= 0:
                return OrderResult(False, symbol, direction, 0, 0, 0, message="流动性不足，未成交")

            # P0 修复：过户费（沪市双向 0.001%）+ P1 最低佣金5元
            transfer_fee = self._calc_transfer_fee(symbol, fill_price * actual_quantity)
            commission = fill_price * actual_quantity * self._commission_rate
            commission = max(commission, 5.0)
            stamp_tax = fill_price * actual_quantity * self._stamp_tax
            proceeds = fill_price * actual_quantity - commission - stamp_tax - transfer_fee

            self._cash += proceeds
            pos.quantity -= actual_quantity
            pos.available -= actual_quantity
            if pos.quantity <= 0:
                del self._positions[symbol]

            result = OrderResult(
                True,
                symbol,
                direction,
                fill_price,
                actual_quantity,
                commission,
                stamp_tax,
                transfer_fee,
                f"卖出成功{'（部分成交）' if actual_quantity < quantity else ''}",
            )
            logger.info(
                f"[模拟盘] 卖出 {symbol} {actual_quantity}股 @ {fill_price:.2f} "
                f"佣金 {commission:.2f} 印花税 {stamp_tax:.2f} 过户费 {transfer_fee:.2f}"
                f"{'（部分成交）' if actual_quantity < quantity else ''}"
            )
        else:
            return OrderResult(False, symbol, direction, 0, 0, 0, message=f"未知方向: {direction}")

        self._trades.append(
            {
                "date": date.today().isoformat(),
                "symbol": symbol,
                "direction": direction,
                "price": result.price,
                "quantity": result.quantity,
                "commission": result.commission,
                "stamp_tax": result.stamp_tax,
                "transfer_fee": result.transfer_fee,
            }
        )
        self._save_state()
        return result

    def get_position(self, symbol: str) -> Position | None:
        with self._lock:
            return self._positions.get(symbol.strip().zfill(6))

    def get_portfolio(self) -> dict[str, Position]:
        with self._lock:
            return dict(self._positions)

    def get_account(self) -> AccountInfo:
        with self._lock:
            positions_value = sum(p.market_value for p in self._positions.values())
            return AccountInfo(
                cash=self._cash,
                total_value=self._cash + positions_value,
                positions=dict(self._positions),
                positions_value=positions_value,  # P0-3 修复：补全缺失字段
            )

    def update_price(self, symbol: str, price: float) -> None:
        """更新现价，同步将旧现价赋给 prev_close（P-06+D-08 修复）。

        P1-h 修复：prev_close 仅在每日首次 update_price 时更新，
        避免同日多次调用（进程重启、调度器重入）导致 prev_close 被盘中价覆盖，
        进而使 L3 涨跌停检查基准错误。

        注意：除权除息检测——调用方在每次更新价格时应检查价格是否因除权除息
        发生了大幅跳空（如跳空幅度超过 -5% 且成交量异常），如是则应调用
        adjust_for_corporate_action 调整持仓成本与数量，而非通过 update_price
        简单更新现价。
        """
        with self._lock:
            return self._update_price_impl(symbol, price)

    def _update_price_impl(self, symbol: str, price: float) -> None:
        symbol = symbol.strip().zfill(6)
        pos = self._positions.get(symbol)
        if pos:
            # P1-h 修复：仅当 prev_close 未设置（==0）或与当前价相同（首次更新）时设置
            # 避免同日多次调用把 prev_close 覆盖为盘中价
            if pos.prev_close <= 0 and pos.current_price > 0:
                pos.prev_close = pos.current_price
            # P1 修复：追踪持仓以来最低价（用于 NoAverageDownChecker 累计浮亏检查）
            if pos.lowest_price <= 0 or price < pos.lowest_price:
                pos.lowest_price = price
            pos.current_price = price

    def update_prices(self, prices: dict[str, float]) -> None:
        """批量更新现价。"""
        for sym, price in prices.items():
            self.update_price(sym, price)

    def adjust_for_corporate_action(
        self,
        symbol: str,
        adjustment_factor: float,
        old_price: float,
        new_price: float,
        bonus_ratio: float = 0.0,
    ) -> None:
        """除权除息调整持仓成本与数量。

        当股票发生分红、送股、配股等公司行为时，股价会发生跳空，
        持仓的实际价值不变，但需要调整 avg_cost 和 quantity 以反映真实盈亏。

        调用方应在发现股价跳空幅度超过 -5% 且成交量异常时主动调用此方法。

        Args:
            symbol: 股票代码。
            adjustment_factor: 除权除息调整因子（复权因子），用于记录。
            old_price: 除权除息前一日收盘价。
            new_price: 除权除息后开盘价（理论除权价）。
            bonus_ratio: 送转股比例，如 10 送 5 则传入 0.5。
                         仅当发生送转股时 quantity 需要调整，纯现金分红时传 0。
        """
        symbol = symbol.strip().zfill(6)
        pos = self._positions.get(symbol)
        if not pos:
            logger.warning("[模拟盘] 尝试调整不存在的持仓 {}，忽略", symbol)
            return

        # 记录调整前状态
        old_avg_cost = pos.avg_cost
        old_qty = pos.quantity
        old_available = pos.available
        price_ratio = new_price / old_price if old_price > 0 else 1.0

        # 调整 avg_cost：按价格比例缩放
        new_avg_cost = old_avg_cost * price_ratio
        pos.avg_cost = round(new_avg_cost, 4)

        # 送转股调整 quantity
        if bonus_ratio > 0:
            new_qty = int(old_qty * (1 + bonus_ratio))
            # 送转股同样增加可卖数量
            new_available = int(old_available * (1 + bonus_ratio))
            pos.quantity = new_qty
            pos.available = new_available

        # 同步更新当前价
        pos.current_price = new_price
        # P1修复：除权除息日 prev_close 应设为除权前价格(old_price)，
        # 这样涨跌停检查基准正确（涨停价=old_price*1.1，而非new_price*1.1）
        pos.prev_close = old_price

        # 标记已调整
        pos.dividend_adjusted = True

        logger.info(
            f"[模拟盘] 除权除息调整 {symbol}: "
            f"调整因子={adjustment_factor:.4f}, "
            f"价格 {old_price:.2f}→{new_price:.2f}, "
            f"avg_cost {old_avg_cost:.4f}→{pos.avg_cost:.4f}, "
            f"数量 {old_qty}→{pos.quantity}, "
            f"送转比例={bonus_ratio:.2%}"
        )
        self._save_state()

    def settle_t_plus_1(self) -> None:
        """T+1 结算：每日开盘前调用，持仓全部变为可卖，并记录日初净值。"""
        with self._lock:
            for pos in self._positions.values():
                pos.available = pos.quantity
            if self._daily_start_value is None or self._daily_start_value <= 0:
                self._daily_start_value = self.get_account().total_value
            self._save_state()

    @property
    def trades(self) -> list[dict[str, Any]]:
        """历史交易记录。"""
        return list(self._trades)

    @property
    def daily_start_value(self) -> float:
        """当日开盘时账户净值（用于日亏计算）。"""
        if self._daily_start_value is None or self._daily_start_value <= 0:
            # P0-7 修复：fallback 应返回 initial_capital 而非当前 total_value
            # 原逻辑返回 total_value 会导致"日初净值"语义被污染为"当前净值"，
            # 使日亏率计算永远≈0（start_value≈total_value），熔断形同虚设
            # 改为返回 initial_capital（与 main.py 日亏 fallback 一致），
            # 跨日 reset_daily_start_value() 触发后会更新为真实日初总资产
            cfg = get_config().execution.paper
            return float(cfg.initial_capital)
        return self._daily_start_value

    def reset_daily_start_value(self) -> None:
        """重置日初净值（用于人工恢复或跨日）。"""
        self._daily_start_value = self.get_account().total_value
        self._save_state()

    def _save_state(self) -> None:
        """将当前状态持久化到 JSON 文件（原子写入 + 文件锁，双进程安全）。

        写入流程：文件锁 → 先写临时文件 → 原子 rename 覆盖目标文件。
        双进程安全：使用 fcntl.flock 互斥，引擎和 Web 进程不会同时写。
        线程安全：使用 self._lock（RLock，可重入）。
        """
        with self._lock:
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "cash": self._cash,
                    "positions": {
                        sym: {
                            "symbol": p.symbol,
                            "quantity": p.quantity,
                            "available": p.available,
                            "avg_cost": p.avg_cost,
                            "current_price": p.current_price,
                            "stop_loss_price": p.stop_loss_price,
                            "prev_close": p.prev_close,
                            "is_st": p.is_st,
                            "is_suspended": p.is_suspended,
                            "lowest_price": p.lowest_price,
                            "dividend_adjusted": p.dividend_adjusted,
                            "trailing_stop": p.trailing_stop,
                            "danger_signals": p.danger_signals,
                        }
                        for sym, p in self._positions.items()
                    },
                    "trades": self._trades,
                    "daily_start_value": self._daily_start_value,
                }
                content = json.dumps(data, ensure_ascii=False, indent=2)
                # 备份现有文件（保留上一版本，便于回滚）
                if STATE_FILE.exists():
                    backup = STATE_FILE.with_suffix(STATE_FILE.suffix + ".bak")
                    try:
                        backup.write_bytes(STATE_FILE.read_bytes())
                    except Exception as e:
                        logger.warning(f"备份状态文件失败: {e}")
                # 文件锁 + 原子写入：避免引擎和Web进程并发覆盖
                # fcntl.LOCK_EX 是排他锁，其他进程读/写会阻塞等待
                with open(STATE_FILE, "a") as lock_f:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                    try:
                        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
                        tmp.write_text(content, encoding="utf-8")
                        tmp.replace(STATE_FILE)
                    finally:
                        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                logger.warning(f"保存模拟盘状态失败: {e}")

    def _load_state(self) -> None:
        """从 JSON 文件恢复状态（P0 修复：fail-fast + .bak 回退）。

        原逻辑：解析失败时静默用默认值（100万空仓），用户真实持仓凭空消失。
        修复：
        1. 主文件解析失败 → 尝试 .bak 备份
        2. .bak 也失败 → 抛 RuntimeError（fail-fast），拒绝用空状态继续
        """
        if not STATE_FILE.exists():
            # 尝试 .bak（可能主文件被误删但 .bak 还在）
            bak = STATE_FILE.with_suffix(STATE_FILE.suffix + ".bak")
            if bak.exists():
                logger.warning("主状态文件 {} 不存在，尝试 .bak 备份", STATE_FILE)
                self._load_from_path(bak)
                return
            return

        try:
            self._load_from_path(STATE_FILE)
        except Exception as e:
            # 主文件损坏 → 尝试 .bak 回退
            bak = STATE_FILE.with_suffix(STATE_FILE.suffix + ".bak")
            if bak.exists():
                logger.error(f"主状态文件损坏({e})，尝试从 .bak 备份恢复: {bak}")
                try:
                    self._load_from_path(bak)
                    logger.warning("已从 .bak 恢复，建议人工核查持仓准确性")
                    return
                except Exception as bak_err:
                    logger.error(f".bak 备份也损坏: {bak_err}")
            # P0 修复：fail-fast，拒绝用默认 100万空仓继续
            raise RuntimeError(
                f"模拟盘状态文件损坏且无可用备份。主文件: {STATE_FILE}，错误: {e}。"
                f"拒绝用空状态继续（防止用户真实持仓凭空消失）。"
                f"请人工修复 {STATE_FILE} 或从 {bak} 恢复后重启。"
            ) from e

    def _load_from_path(self, path) -> None:
        """从指定路径加载状态（内部方法，带文件锁）。"""
        # 文件锁（共享锁）：确保读取时没有写入操作
        with open(path) as lock_f:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_SH)
                data = json.load(lock_f)
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        self._cash = data.get("cash", self._cash)
        # P1-f 修复：显式循环 + per-symbol try/except，避免单条失败丢全部持仓
        positions_data = data.get("positions", {})
        for sym, p in positions_data.items():
            try:
                self._positions[sym] = Position(
                    symbol=p["symbol"],
                    quantity=p["quantity"],
                    available=p["available"],
                    avg_cost=p["avg_cost"],
                    current_price=p["current_price"],
                    stop_loss_price=p.get("stop_loss_price", 0.0),
                    prev_close=p.get("prev_close", 0.0),
                    is_st=p.get("is_st", False),
                    is_suspended=p.get("is_suspended", False),
                    lowest_price=p.get("lowest_price", 0.0),
                    dividend_adjusted=p.get("dividend_adjusted", False),
                    trailing_stop=p.get("trailing_stop"),
                    danger_signals=p.get("danger_signals"),
                )
            except (KeyError, TypeError, ValueError) as pos_err:
                logger.warning("恢复持仓 {} 失败，跳过该持仓: {}；原始数据: {}", sym, pos_err, p)
        self._trades = data.get("trades", [])
        self._daily_start_value = data.get("daily_start_value")
        if self._positions:
            logger.info(
                "恢复模拟盘状态: 现金 ¥{:.2f}, 持仓 {} 只, 交易 {} 笔",
                self._cash,
                len(self._positions),
                len(self._trades),
            )
