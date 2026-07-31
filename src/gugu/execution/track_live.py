"""实盘持仓跟踪器（track_live 模式）。

与 PaperBroker 的根本区别：
- PaperBroker 会自动执行买卖订单，修改持仓状态（用于模拟盘）
- LivePositionTracker 拒绝任何自动下单，只读取用户手动维护的持仓文件
- 用户真实持仓数据存于 data/live_positions.json，系统只读不写交易，仅更新现价/止损价

适用场景：
- 用户有真实持仓（券商账户），希望系统跟踪止损/止盈提醒
- 系统产生信号时推送"建议"卡片，但不会自动下单
- 用户手动调整持仓后，需手动更新 live_positions.json

设计原则：
- 安全优先：order() 永远返回失败，防止系统误操作用户真实持仓
- 只读持仓：positions 从文件加载，update_price 仅更新现价（用于止损检查）
- 持久化止损价调整：当系统调整止损价时（如移动止损），持久化到文件
"""

from __future__ import annotations

import fcntl
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from gugu.config import PROJECT_ROOT
from gugu.execution.base import AccountInfo, BaseBroker, Direction, OrderResult
from gugu.models import Position
from gugu.utils.log import get_logger

logger = get_logger()

# 实盘持仓文件路径（独立于 paper_broker_state.json）
STATE_FILE = PROJECT_ROOT / "data" / "live_positions.json"


class LivePositionTracker(BaseBroker):
    """实盘持仓跟踪器。

    track_live 模式专用 broker：
    - 读取 data/live_positions.json 用户真实持仓
    - order() 拒绝下单，返回失败（保护用户真实持仓不被系统误操作）
    - update_price() 仅更新现价（用于止损检查）
    - 所有持仓 available = quantity（实盘 T+1 由券商控制，本系统不做T+1限制）
    """

    def __init__(self, state_file: Path | None = None) -> None:
        self._state_file = state_file if state_file is not None else STATE_FILE
        self._lock = threading.RLock()
        self._positions: dict[str, Position] = {}
        self._names: dict[str, str] = {}  # symbol -> name（Position类无name属性，单独管理）
        self._cash: float = 0.0
        self._initial_capital: float = 1_000_000.0
        self._daily_start_value: float | None = None
        self._load_state()

    def _load_state(self) -> None:
        """从 live_positions.json 加载持仓状态。"""
        with self._lock:
            if not self._state_file.exists():
                logger.warning(
                    f"[实盘跟踪] 持仓文件不存在: {self._state_file}，请手动创建并填入真实持仓数据"
                )
                return

            try:
                with self._state_file.open("r", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        data = json.load(f)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

                self._cash = float(data.get("cash", 0.0))
                self._initial_capital = float(data.get("initial_capital", 1_000_000.0))
                self._daily_start_value = data.get("daily_start_value")
                if self._daily_start_value is not None:
                    self._daily_start_value = float(self._daily_start_value)

                positions_data = data.get("positions", {})
                self._positions = {}
                self._names = {}
                for sym, p in positions_data.items():
                    symbol = str(sym).strip().zfill(6)
                    self._positions[symbol] = Position(
                        symbol=symbol,
                        quantity=int(p.get("quantity", 0)),
                        available=int(p.get("available", p.get("quantity", 0))),
                        avg_cost=float(p.get("avg_cost", 0.0)),
                        current_price=float(p.get("current_price", 0.0)),
                        stop_loss_price=float(p.get("stop_loss_price", 0.0)),
                        prev_close=float(p.get("prev_close", 0.0)),
                        is_st=bool(p.get("is_st", False)),
                        is_suspended=bool(p.get("is_suspended", False)),
                        lowest_price=float(p.get("lowest_price", p.get("avg_cost", 0.0))),
                    )
                    # name 单独管理（Position 类无 name 属性）
                    name = p.get("name", "")
                    if name:
                        self._names[symbol] = name

                logger.info(
                    f"[实盘跟踪] 加载 {len(self._positions)} 个真实持仓: "
                    f"{list(self._positions.keys())}"
                )
            except Exception as e:
                logger.error("[实盘跟踪] 加载持仓文件失败: {}", e)
                # 失败时保持空持仓，不抛异常（fail-safe，不阻塞引擎启动）
                self._positions = {}

    def _save_state(self) -> None:
        """持久化到 live_positions.json（原子写入 + 文件锁）。"""
        with self._lock:
            try:
                self._state_file.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "mode": "track_live",
                    "description": "用户真实持仓（track_live模式只读跟踪，系统不自动交易）",
                    "initial_capital": self._initial_capital,
                    "cash": self._cash,
                    "positions": {
                        sym: {
                            "symbol": p.symbol,
                            "name": self._names.get(sym, ""),
                            "quantity": p.quantity,
                            "available": p.available,
                            "avg_cost": p.avg_cost,
                            "current_price": p.current_price,
                            "stop_loss_price": p.stop_loss_price,
                            "prev_close": p.prev_close,
                            "is_st": p.is_st,
                            "is_suspended": p.is_suspended,
                            "lowest_price": p.lowest_price,
                        }
                        for sym, p in self._positions.items()
                    },
                    "daily_start_value": self._daily_start_value,
                    "updated_at": datetime.now().isoformat(),
                    "note": (
                        "此文件由用户手动维护。系统在 track_live 模式下只读此文件，"
                        "用于止损/止盈提醒，不会自动下单修改持仓。"
                        "用户调整持仓后需手动更新此文件。"
                    ),
                }

                # 原子写入：先写临时文件，再 rename
                tmp_file = self._state_file.with_suffix(".json.tmp")
                with tmp_file.open("w", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                        f.flush()
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                tmp_file.replace(self._state_file)
            except Exception as e:
                logger.error("[实盘跟踪] 保存持仓文件失败: {}", e)

    def order(
        self,
        symbol: str,
        direction: Direction,
        quantity: int,
        price: float | None = None,
    ) -> OrderResult:
        """拒绝下单（track_live 模式核心安全机制）。

        track_live 模式下，系统不会自动买卖用户真实持仓。
        策略信号会通过飞书通知用户，由用户手动在券商APP执行。
        """
        logger.warning(
            f"[实盘跟踪] track_live 模式拒绝自动下单: {symbol} {direction} {quantity}股 "
            f"(用户应手动在券商APP执行)"
        )
        return OrderResult(
            success=False,
            symbol=symbol.strip().zfill(6),
            direction=direction,
            price=0.0,
            quantity=0,
            commission=0.0,
            stamp_tax=0.0,
            transfer_fee=0.0,
            message="track_live 模式不自动下单，请手动在券商APP执行",
        )

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
        """更新现价（用于止损检查）。

        与 PaperBroker 不同：
        - 不持久化每次价格更新（避免频繁IO，只在止损价调整时持久化）
        - prev_close 仅在首次更新时设置（避免同日多次调用覆盖）
        - lowest_price 追踪持仓以来最低价
        """
        with self._lock:
            symbol = symbol.strip().zfill(6)
            pos = self._positions.get(symbol)
            if pos:
                if pos.prev_close <= 0 and pos.current_price > 0:
                    pos.prev_close = pos.current_price
                if pos.lowest_price <= 0 or price < pos.lowest_price:
                    pos.lowest_price = price
                pos.current_price = price

    def update_prices(self, prices: dict[str, float]) -> None:
        """批量更新现价（不持久化，避免高频IO）。"""
        with self._lock:
            for sym, price in prices.items():
                self.update_price(sym, price)

    def update_stop_loss(self, symbol: str, stop_loss_price: float) -> None:
        """更新止损价并持久化（移动止损触发时调用）。

        这是 track_live 模式下唯一会修改持仓文件的场景：
        - 系统计算出新的移动止损价
        - 持久化到 live_positions.json，确保重启后止损价不丢失
        """
        with self._lock:
            symbol = symbol.strip().zfill(6)
            pos = self._positions.get(symbol)
            if pos:
                old_stop = pos.stop_loss_price
                pos.stop_loss_price = stop_loss_price
                self._save_state()
                logger.info(
                    f"[实盘跟踪] 更新止损价 {symbol}: {old_stop:.2f} → {stop_loss_price:.2f} (已持久化)"
                )

    def adjust_for_corporate_action(
        self,
        symbol: str,
        adjustment_factor: float,
        old_price: float,
        new_price: float,
        bonus_ratio: float = 0.0,
    ) -> None:
        """除权除息调整（持久化，因为是真实持仓的公司行为）。"""
        with self._lock:
            symbol = symbol.strip().zfill(6)
            pos = self._positions.get(symbol)
            if not pos:
                logger.warning("[实盘跟踪] 尝试调整不存在的持仓 {}，忽略", symbol)
                return

            old_avg_cost = pos.avg_cost
            old_qty = pos.quantity
            price_ratio = new_price / old_price if old_price > 0 else 1.0
            new_avg_cost = old_avg_cost * price_ratio
            pos.avg_cost = round(new_avg_cost, 4)

            if bonus_ratio > 0:
                pos.quantity = int(old_qty * (1 + bonus_ratio))
                pos.available = int(pos.available * (1 + bonus_ratio))

            pos.current_price = new_price
            pos.prev_close = old_price

            logger.info(
                f"[实盘跟踪] 除权除息调整 {symbol}: "
                f"avg_cost {old_avg_cost:.4f}→{pos.avg_cost:.4f}, "
                f"数量 {old_qty}→{pos.quantity}"
            )
            self._save_state()

    def settle_t_plus_1(self) -> None:
        """T+1 结算：实盘持仓全部可卖（T+1由券商控制，本系统不做限制）。"""
        with self._lock:
            for pos in self._positions.values():
                pos.available = pos.quantity
            if self._daily_start_value is None or self._daily_start_value <= 0:
                self._daily_start_value = self.get_account().total_value

    @property
    def daily_start_value(self) -> float:
        """当日开盘时账户净值（用于日亏计算）。"""
        with self._lock:
            if self._daily_start_value is None or self._daily_start_value <= 0:
                return self._initial_capital
            return self._daily_start_value

    def reset_daily_start_value(self) -> None:
        """重置日初净值。"""
        with self._lock:
            self._daily_start_value = self.get_account().total_value
            self._save_state()

    @property
    def trades(self) -> list[dict[str, Any]]:
        """无交易记录（track_live 模式不记录系统交易）。"""
        return []
