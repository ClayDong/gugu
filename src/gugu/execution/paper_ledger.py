"""统一 paper 成交账本。

PaperBroker 保持原有交易 API；OrderManager 通过可选 ExecutionContext 注入
信号/策略身份，PaperBroker 成交后写入本账本。账本是成交证据源，不把
signals_history.jsonl 当作成交账本。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gugu import config as gugu_config
from gugu.utils.atomic import append_jsonl


@dataclass(frozen=True)
class ExecutionContext:
    """成交上下文，所有字段可缺省以兼容直接调用 PaperBroker 的旧代码。"""

    signal_id: str = ""
    strategy: str = ""
    strategies: tuple[str, ...] = field(default_factory=tuple)
    attribution_method: str = "primary"
    regime: str = ""
    data_quality: str = "unknown"
    market_env_snapshot: dict[str, Any] | None = None

    @classmethod
    def from_signal(cls, signal: dict[str, Any]) -> ExecutionContext:
        raw = signal.get("strategies") or []
        if isinstance(raw, str):
            strategies = tuple(x.strip() for x in raw.split(",") if x.strip())
        else:
            strategies = tuple(str(x) for x in raw if x)
        strategy = str(signal.get("strategy") or "")
        if not strategy and strategies:
            strategy = ",".join(strategies)
        return cls(
            signal_id=str(signal.get("signal_id") or uuid.uuid4().hex),
            strategy=strategy,
            strategies=strategies,
            attribution_method=str(signal.get("attribution_method") or "primary"),
            regime=str(signal.get("regime") or signal.get("market_context", {}).get("regime", "")),
            data_quality=str(signal.get("data_quality") or "unknown"),
            market_env_snapshot=signal.get("market_env") or signal.get("_market_env"),
        )


def new_execution_id() -> str:
    return uuid.uuid4().hex


def ledger_path() -> Path:
    return gugu_config.PROJECT_ROOT / "data" / "paper_execution_ledger.jsonl"


def append_fill(record: dict[str, Any]) -> None:
    """追加一笔成交，写失败不阻塞 PaperBroker 成交。"""
    try:
        append_jsonl(ledger_path(), record)
    except Exception:
        # 成交状态已在 broker 持久化，账本失败由 watchdog/日志暴露，不回滚模拟成交。
        return


def read_fills() -> list[dict[str, Any]]:
    path = ledger_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = __import__("json").loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except (TypeError, ValueError):
            continue
    return rows


def summarize_fills(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return account/strategy paper fill evidence without double counting PnL."""
    rows = rows if rows is not None else read_fills()
    closed = [r for r in rows if r.get("direction") == "sell" and r.get("realized_pnl") is not None]
    by_strategy: dict[str, dict[str, Any]] = {}
    for row in closed:
        names = [str(x) for x in (row.get("strategies") or []) if x]
        if not names:
            raw_strategy = str(row.get("strategy") or "unknown")
            names = [x.strip() for x in raw_strategy.split(",") if x.strip()] or ["unknown"]
        method = str(row.get("attribution_method") or "primary")
        if method == "equal":
            allocations = {
                name: float(row.get("realized_pnl") or 0.0) / len(names) for name in names
            }
        else:
            # 默认只归因给融合信号的第一主策略，账户总 PnL 只计算一次。
            allocations = {names[0]: float(row.get("realized_pnl") or 0.0)}
        for name, share in allocations.items():
            bucket = by_strategy.setdefault(name, {"closed_trades": 0, "realized_pnl": 0.0})
            bucket["closed_trades"] += 1
            bucket["realized_pnl"] += share
    return {
        "fills": len(rows),
        "closed_trades": len(closed),
        "realized_pnl": sum(float(r.get("realized_pnl") or 0.0) for r in closed),
        "by_strategy": by_strategy,
    }


def build_fill_record(
    *,
    execution_id: str,
    context: ExecutionContext | None,
    symbol: str,
    direction: str,
    requested_quantity: int,
    filled_quantity: int,
    requested_price: float,
    fill_price: float,
    commission: float,
    stamp_tax: float,
    transfer_fee: float,
    entry_execution_id: str | None,
    cost_basis_consumed: float,
    realized_pnl: float | None,
    outcome_status: str,
) -> dict[str, Any]:
    ctx = context or ExecutionContext()
    data = {
        "execution_id": execution_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "signal_id": ctx.signal_id,
        "strategy": ctx.strategy,
        "strategies": list(ctx.strategies),
        "attribution_method": ctx.attribution_method,
        "regime": ctx.regime,
        "data_quality": ctx.data_quality,
        "market_env_snapshot": ctx.market_env_snapshot,
        "symbol": symbol,
        "direction": direction,
        "requested_quantity": requested_quantity,
        "filled_quantity": filled_quantity,
        "requested_price": requested_price,
        "fill_price": fill_price,
        "commission": commission,
        "stamp_tax": stamp_tax,
        "transfer_fee": transfer_fee,
        "entry_execution_id": entry_execution_id,
        "cost_basis_consumed": cost_basis_consumed,
        "realized_pnl": realized_pnl,
        "outcome_status": outcome_status,
    }
    # asdict-like normalization is intentionally shallow; append_jsonl sanitizes nested values.
    return data
