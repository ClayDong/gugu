"""CXBR 策略级模拟执行适配器。

该模块只负责纸面成交，不注册生产策略，也不调用真实券商接口。
CXBR 的信号、订单和成交事件使用独立账本，避免污染全局模拟盘。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gugu.config import PROJECT_ROOT
from gugu.execution.paper import PaperBroker
from gugu.research.cxbr_v1 import CXBRSignal

DEFAULT_STATE_FILE = PROJECT_ROOT / "data" / "cxbr_paper_broker_state.json"
DEFAULT_LEDGER_FILE = PROJECT_ROOT / "data" / "cxbr_paper_ledger.jsonl"


@dataclass(frozen=True)
class CXBRMarketSnapshot:
    """执行时必须明确的市场事实。"""

    price: float
    prev_close: float
    market_value_billion: float | None = None


@dataclass(frozen=True)
class CXBRPaperExecution:
    """一次 CXBR 模拟执行或拒单结果。"""

    event_id: str
    signal_id: str
    event_type: str
    strategy_name: str
    rule_version: str
    entry_assumption: str
    code: str
    action: str
    signal_asof: str | None
    order_time: str
    fill_time: str | None
    planned_price: float | None
    fill_price: float
    quantity: int
    requested_quantity: int
    gross_amount: float
    net_cash_flow: float
    partial_fill: bool
    slippage: float
    commission: float
    stamp_tax: float
    transfer_fee: float
    total_fees: float
    cost_basis_consumed: float | None
    realized_pnl: float | None
    success: bool
    message: str


class CXBRPaperExecutor:
    """隔离于全局 PaperBroker 的 CXBR 模拟执行层。"""

    def __init__(
        self,
        initial_capital: float | None = None,
        broker: PaperBroker | None = None,
        state_file: Path | None = None,
        ledger_file: Path | None = None,
    ) -> None:
        self._state_file = Path(state_file or DEFAULT_STATE_FILE)
        self._ledger_file = Path(ledger_file or DEFAULT_LEDGER_FILE)
        self._broker = broker or PaperBroker(
            initial_capital=initial_capital,
            state_file=self._state_file,
        )
        self._processed_signals = self._load_signal_ids()
        self._cost_basis = self._load_cost_basis()

    @property
    def broker(self) -> PaperBroker:
        return self._broker

    @property
    def executions(self) -> list[dict[str, Any]]:
        if not self._ledger_file.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self._ledger_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def summarize_performance(self) -> dict[str, float | int]:
        """汇总已闭仓的 CXBR paper 结果，不把未平仓当作收益。"""
        records = self.executions
        filled = [record for record in records if record.get("success")]
        closed = [
            record
            for record in filled
            if record.get("action") == "sell" and record.get("realized_pnl") is not None
        ]
        realized_pnl = sum(float(record["realized_pnl"]) for record in closed)
        realized_cost = sum(
            float(record.get("cost_basis_consumed", 0.0))
            for record in closed
            if record.get("cost_basis_consumed") is not None
        )
        total_fees = sum(float(record.get("total_fees", 0.0)) for record in filled)
        open_quantity = sum(int(basis["quantity"]) for basis in self._cost_basis.values())
        wins = sum(float(record["realized_pnl"]) > 0 for record in closed)
        return {
            "filled_orders": len(filled),
            "closed_trades": len(closed),
            "open_quantity": open_quantity,
            "realized_pnl": round(realized_pnl, 2),
            "realized_return": round(realized_pnl / realized_cost, 6) if realized_cost > 0 else 0.0,
            "closed_win_rate": round(wins / len(closed), 6) if closed else 0.0,
            "total_fees": round(total_fees, 2),
        }

    def settle_t_plus_1(self) -> None:
        """在每个交易日开盘前调用，释放前一日买入的可卖数量。"""
        self._broker.settle_t_plus_1()

    def update_market(self, code: str, snapshot: CXBRMarketSnapshot) -> None:
        """更新持仓价格及下一次订单需要的前收盘价。"""
        self._broker.set_market_context(
            code,
            prev_close=snapshot.prev_close,
            market_value_billion=snapshot.market_value_billion,
        )
        self._broker.update_price(code, snapshot.price)

    def execute_signal(
        self,
        signal: CXBRSignal,
        snapshot: CXBRMarketSnapshot,
        quantity: int | None = None,
    ) -> CXBRPaperExecution | None:
        """对 buy/sell 信号执行一次模拟订单；observe/none 不下单。"""
        if signal.action not in ("buy", "sell"):
            return None
        signal_id = self._signal_id(signal, snapshot)
        now = datetime.now().isoformat(timespec="seconds")
        if signal_id in self._processed_signals:
            return self._record(
                CXBRPaperExecution(
                    event_id=self._event_id(signal_id, "duplicate"),
                    signal_id=signal_id,
                    event_type="order_duplicate",
                    strategy_name=signal.strategy_name,
                    rule_version=signal.rule_version,
                    entry_assumption=signal.entry_assumption,
                    code=signal.code,
                    action=signal.action,
                    signal_asof=signal.asof,
                    order_time=now,
                    fill_time=None,
                    planned_price=snapshot.price,
                    fill_price=0.0,
                    quantity=0,
                    requested_quantity=0,
                    gross_amount=0.0,
                    net_cash_flow=0.0,
                    partial_fill=False,
                    slippage=0.0,
                    commission=0.0,
                    stamp_tax=0.0,
                    transfer_fee=0.0,
                    total_fees=0.0,
                    cost_basis_consumed=None,
                    realized_pnl=None,
                    success=False,
                    message="同一信号已处理，阻止重复模拟下单",
                )
            )

        self._broker.set_market_context(
            signal.code,
            prev_close=snapshot.prev_close,
            market_value_billion=snapshot.market_value_billion,
        )
        order_quantity = quantity or self._default_quantity(signal, snapshot)
        result = self._broker.order(signal.code, signal.action, order_quantity, snapshot.price)
        event_type = "order_filled" if result.success else "order_rejected"
        fill_time = now if result.success else None
        gross_amount = result.price * result.quantity if result.success else 0.0
        total_fees = result.commission + result.stamp_tax + result.transfer_fee
        if result.success and signal.action == "buy":
            net_cash_flow = -(gross_amount + total_fees)
            cost_basis_consumed = None
            realized_pnl = None
        elif result.success and signal.action == "sell":
            net_cash_flow = gross_amount - total_fees
            cost_basis_consumed, realized_pnl = self._consume_cost_basis(
                result.symbol, result.quantity, net_cash_flow
            )
        else:
            net_cash_flow = 0.0
            cost_basis_consumed = None
            realized_pnl = None
        execution = CXBRPaperExecution(
            event_id=self._event_id(signal_id, event_type),
            signal_id=signal_id,
            event_type=event_type,
            strategy_name=signal.strategy_name,
            rule_version=signal.rule_version,
            entry_assumption=signal.entry_assumption,
            code=result.symbol,
            action=signal.action,
            signal_asof=signal.asof,
            order_time=now,
            fill_time=fill_time,
            planned_price=snapshot.price,
            fill_price=result.price if result.success else 0.0,
            quantity=result.quantity,
            requested_quantity=order_quantity,
            gross_amount=round(gross_amount, 2),
            net_cash_flow=round(net_cash_flow, 2),
            partial_fill=result.success and result.quantity < order_quantity,
            slippage=(result.price / snapshot.price - 1.0) if result.success else 0.0,
            commission=result.commission,
            stamp_tax=result.stamp_tax,
            transfer_fee=result.transfer_fee,
            total_fees=round(total_fees, 2),
            cost_basis_consumed=(
                None if cost_basis_consumed is None else round(cost_basis_consumed, 2)
            ),
            realized_pnl=None if realized_pnl is None else round(realized_pnl, 2),
            success=result.success,
            message=result.message,
        )
        if result.success:
            self._processed_signals.add(signal_id)
            if signal.action == "buy":
                self._add_cost_basis(result.symbol, result.quantity, gross_amount + total_fees)
        return self._record(execution)

    def _default_quantity(self, signal: CXBRSignal, snapshot: CXBRMarketSnapshot) -> int:
        position_ratio = signal.suggested_position or 0.0
        cash = self._broker.get_account().cash
        budget = cash * position_ratio
        return max(100, int(budget // snapshot.price // 100) * 100)

    @staticmethod
    def _signal_id(signal: CXBRSignal, snapshot: CXBRMarketSnapshot) -> str:
        raw = "|".join(
            [
                signal.strategy_name,
                signal.rule_version,
                signal.entry_assumption,
                signal.code,
                signal.action,
                signal.stage,
                signal.asof or "",
                signal.buy_point or "",
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    @staticmethod
    def _event_id(signal_id: str, event_type: str) -> str:
        return hashlib.sha256(f"{signal_id}|{event_type}".encode()).hexdigest()[:24]

    def _record(self, execution: CXBRPaperExecution) -> CXBRPaperExecution:
        self._ledger_file.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(execution), ensure_ascii=False) + "\n")
        return execution

    def _add_cost_basis(self, code: str, quantity: int, total_cost: float) -> None:
        basis = self._cost_basis.setdefault(code, {"quantity": 0, "total_cost": 0.0})
        basis["quantity"] += quantity
        basis["total_cost"] += total_cost

    def _consume_cost_basis(
        self, code: str, quantity: int, net_proceeds: float
    ) -> tuple[float, float] | tuple[None, None]:
        basis = self._cost_basis.get(code)
        if not basis or basis["quantity"] < quantity:
            return None, None
        average_cost = basis["total_cost"] / basis["quantity"]
        cost = average_cost * quantity
        basis["quantity"] -= quantity
        basis["total_cost"] -= cost
        if basis["quantity"] == 0:
            self._cost_basis.pop(code, None)
        return cost, net_proceeds - cost

    def _load_signal_ids(self) -> set[str]:
        if not self._ledger_file.exists():
            return set()
        signal_ids: set[str] = set()
        for line in self._ledger_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if record.get("success"):
                    signal_ids.add(record["signal_id"])
        return signal_ids

    def _load_cost_basis(self) -> dict[str, dict[str, float | int]]:
        """从成功成交事件重建含买入费用的移动平均成本。"""
        basis: dict[str, dict[str, float | int]] = {}
        if not self._ledger_file.exists():
            return basis
        for line in self._ledger_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not record.get("success"):
                continue
            code = str(record.get("code", "")).zfill(6)
            quantity = int(record.get("quantity", 0))
            if quantity <= 0:
                continue
            gross_amount = float(
                record.get("gross_amount", float(record.get("fill_price", 0.0)) * quantity)
            )
            total_fees = float(
                record.get(
                    "total_fees",
                    float(record.get("commission", 0.0))
                    + float(record.get("stamp_tax", 0.0))
                    + float(record.get("transfer_fee", 0.0)),
                )
            )
            if record.get("action") == "buy":
                current = basis.setdefault(code, {"quantity": 0, "total_cost": 0.0})
                current["quantity"] += quantity
                current["total_cost"] += gross_amount + total_fees
            elif record.get("action") == "sell":
                current = basis.get(code)
                if not current or current["quantity"] < quantity:
                    continue
                average_cost = current["total_cost"] / current["quantity"]
                current["quantity"] -= quantity
                current["total_cost"] -= average_cost * quantity
                if current["quantity"] == 0:
                    basis.pop(code, None)
        return basis


__all__ = ["CXBRMarketSnapshot", "CXBRPaperExecution", "CXBRPaperExecutor"]
