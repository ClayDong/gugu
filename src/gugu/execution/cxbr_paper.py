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
    code: str
    action: str
    signal_asof: str | None
    order_time: str
    planned_price: float | None
    fill_price: float
    quantity: int
    requested_quantity: int
    partial_fill: bool
    slippage: float
    commission: float
    stamp_tax: float
    transfer_fee: float
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
                    code=signal.code,
                    action=signal.action,
                    signal_asof=signal.asof,
                    order_time=now,
                    planned_price=snapshot.price,
                    fill_price=0.0,
                    quantity=0,
                    requested_quantity=0,
                    partial_fill=False,
                    slippage=0.0,
                    commission=0.0,
                    stamp_tax=0.0,
                    transfer_fee=0.0,
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
        execution = CXBRPaperExecution(
            event_id=self._event_id(signal_id, event_type),
            signal_id=signal_id,
            event_type=event_type,
            code=result.symbol,
            action=signal.action,
            signal_asof=signal.asof,
            order_time=now,
            planned_price=snapshot.price,
            fill_price=result.price if result.success else 0.0,
            quantity=result.quantity,
            requested_quantity=order_quantity,
            partial_fill=result.success and result.quantity < order_quantity,
            slippage=(result.price / snapshot.price - 1.0) if result.success else 0.0,
            commission=result.commission,
            stamp_tax=result.stamp_tax,
            transfer_fee=result.transfer_fee,
            success=result.success,
            message=result.message,
        )
        if result.success:
            self._processed_signals.add(signal_id)
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
                signal.rule_version,
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


__all__ = ["CXBRMarketSnapshot", "CXBRPaperExecution", "CXBRPaperExecutor"]
