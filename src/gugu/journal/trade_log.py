"""交易日志（道④：知己）。

每次信号触发/执行/止损，写一条结构化日志到 data/trade_journal.jsonl。
用于每周复盘：哪些策略在亏钱？哪些止损被反复触发？

格式：
{
  "ts": "2026-09-03T10:30:00",
  "symbol": "002230",
  "strategy": "sector_momentum",
  "direction": "buy",
  "price": 45.2,
  "stop_loss": 42.9,
  "confidence": 0.72,
  "regime": "sideways",
  "reason": "MA5>MA10 + close>MA20 + 动量>0 + 放量",
  "result": "executed" | "gate_blocked" | "observation_only"
}
"""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from gugu.utils.atomic import append_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[3]
JOURNAL_PATH = PROJECT_ROOT / "data" / "trade_journal.jsonl"


def log_trade_event(
    *,
    symbol: str,
    strategy: str,
    direction: str,
    price: float,
    stop_loss: float | None = None,
    confidence: float | None = None,
    regime: str | None = None,
    reason: str = "",
    result: str = "executed",
    extra: dict[str, Any] | None = None,
) -> None:
    """记录一条交易事件到日志。

    Args:
        symbol: 股票代码
        strategy: 策略名称
        direction: "buy" | "sell" | "stop_loss"
        price: 成交价或触发价
        stop_loss: 止损价（买入时）
        confidence: 置信度 0-1
        regime: 市场状态
        reason: 触发原因
        result: 执行结果（executed/gate_blocked/observation_only/stop_loss_triggered）
        extra: 额外字段
    """
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "symbol": str(symbol),
        "strategy": str(strategy),
        "direction": str(direction),
        "price": round(float(price), 2),
        "result": str(result),
    }
    if stop_loss is not None:
        record["stop_loss"] = round(float(stop_loss), 2)
    if confidence is not None:
        record["confidence"] = round(float(confidence), 4)
    if regime:
        record["regime"] = str(regime)
    if reason:
        record["reason"] = str(reason)[:200]
    if extra:
        record["extra"] = extra

    with suppress(Exception):  # 日志写失败不阻塞交易
        append_jsonl(JOURNAL_PATH, record)


def log_signal_blocked(
    symbol: str,
    strategy: str,
    reason: str,
    price: float = 0.0,
    regime: str | None = None,
) -> None:
    """记录一条被拦截的信号（gate_blocked）。"""
    log_trade_event(
        symbol=symbol,
        strategy=strategy,
        direction="buy",
        price=price,
        reason=reason,
        result="gate_blocked",
        regime=regime,
    )


def log_stop_loss(
    symbol: str,
    strategy: str,
    entry_price: float,
    exit_price: float,
    loss_pct: float,
    regime: str | None = None,
) -> None:
    """记录一条止损事件。"""
    log_trade_event(
        symbol=symbol,
        strategy=strategy,
        direction="stop_loss",
        price=exit_price,
        reason=f"从入场价 {entry_price:.2f} 止损，亏损 {loss_pct:.1%}",
        result="stop_loss_triggered",
        regime=regime,
        extra={"entry_price": entry_price, "loss_pct": round(loss_pct, 4)},
    )
