"""野哥主线爆发战法离线事件研究摘要。

输入必须是已审计的 JSON/JSONL 事件记录；脚本不联网、不补造缺失数据。
每个事件应包含 ``validated``、``entry_assumption=next_session_open`` 和
1/3/5/10 日收益字段。证据不足时输出 insufficient_evidence。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.analysis.yege_battle import audit_event_records


def _read_events(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
        return rows
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("events", [])
    return [row for row in value if isinstance(row, dict)]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def dedupe_events(events: list[dict[str, Any]], cooldown_days: int = 3) -> list[dict[str, Any]]:
    """按事件 ID 和股票冷却窗口去重，避免同一行情阶段重复计样本。"""
    ordered = sorted(events, key=lambda row: str(row.get("signal_date", "")))
    seen_ids: set[str] = set()
    last_by_symbol: dict[str, date] = {}
    result: list[dict[str, Any]] = []
    for event in ordered:
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen_ids:
            continue
        symbol = str(event.get("candidate_symbol") or event.get("symbol") or "")
        try:
            event_date = datetime.fromisoformat(str(event["signal_date"])).date()
        except (KeyError, TypeError, ValueError):
            event_date = None
        if event_date is not None and symbol:
            previous = last_by_symbol.get(symbol)
            if previous is not None and (event_date - previous).days < cooldown_days:
                continue
            last_by_symbol[symbol] = event_date
        if event_id:
            seen_ids.add(event_id)
        result.append(event)
    return result


def add_forward_returns(
    event: dict[str, Any],
    closes: list[float],
    *,
    entry_index: int,
    cost: float = 0.004,
) -> dict[str, Any]:
    """从 T+1 开盘入场价计算可审计的 forward returns。"""
    result = dict(event)
    if entry_index < 0 or entry_index >= len(closes) or closes[entry_index] <= 0:
        result["data_quality"] = "insufficient"
        result["return_status"] = "insufficient_evidence"
        return result
    entry = float(closes[entry_index])
    result["entry_price"] = entry
    result["entry_price_source"] = "next_open"
    for horizon in (1, 3, 5, 10):
        target = entry_index + horizon
        if target >= len(closes) or closes[target] <= 0:
            result[f"return_{horizon}d"] = None
        else:
            result[f"return_{horizon}d"] = round((closes[target] / entry) * (1 - cost) - 1, 6)
    result["return_status"] = (
        "available" if result.get("return_1d") is not None else "insufficient_evidence"
    )
    return result


def summarize(events: list[dict[str, Any]], *, cooldown_days: int = 3) -> dict[str, Any]:
    """汇总已验证事件；不把缺失值当成零收益。"""
    deduped = dedupe_events(events, cooldown_days=cooldown_days)
    valid = [
        event
        for event in deduped
        if event.get("validated") is True
        and event.get("entry_assumption") == "next_session_open"
        and event.get("evidence_status") == "available"
        and event.get("return_status", "available") == "available"
        and event.get("return_1d") is not None
    ]
    horizons: dict[str, list[float]] = defaultdict(list)
    for event in valid:
        for horizon in (1, 3, 5, 10):
            value = event.get(f"return_{horizon}d")
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                horizons[f"{horizon}d"].append(value)

    by_sector: dict[str, int] = defaultdict(int)
    by_symbol: dict[str, int] = defaultdict(int)
    for event in valid:
        by_sector[str(event.get("sector") or "unknown")] += 1
        by_symbol[str(event.get("candidate_symbol") or event.get("symbol") or "unknown")] += 1
    concentrated = max(by_sector.values(), default=0) / len(valid) if valid else 0.0
    symbol_concentrated = max(by_symbol.values(), default=0) / len(valid) if valid else 0.0
    status = (
        "ok"
        if len(valid) >= 30 and concentrated <= 0.30 and symbol_concentrated <= 0.30
        else "insufficient_evidence"
    )
    insufficient = [
        event
        for event in deduped
        if event not in valid and event.get("return_status", "available") != "available"
    ]
    rule_failed = [event for event in deduped if event.get("rule_status") == "failed"]
    return {
        "rule_version": "yege-mainline-v1",
        "event_count": len(events),
        "deduped_event_count": len(deduped),
        "validated_event_count": len(valid),
        "insufficient_evidence_count": len(insufficient),
        "rule_failed_count": len(rule_failed),
        "status": status,
        "entry_assumption": "next_session_open",
        "by_horizon": {
            horizon: {"count": len(values), "avg_return": _mean(values)}
            for horizon, values in sorted(horizons.items())
        },
        "sector_counts": dict(by_sector),
        "symbol_counts": dict(by_symbol),
        "max_sector_concentration": concentrated,
        "max_symbol_concentration": symbol_concentrated,
        "input_audit": audit_event_records(events),
        "next_action": (
            "继续收集跨板块/跨年份事件并完成连续持仓、Walk-Forward和paper对账"
            if status != "ok"
            else "进入完整 GatePolicy 审核，不自动升级 manifest 或 verified"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--ledger",
        type=Path,
        help="可选：将本次研究摘要追加到 research_ledger.jsonl",
    )
    args = parser.parse_args()
    events = _read_events(args.events)
    report = summarize(events)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    if args.ledger:
        from gugu.research.ledger import append_record, build_record

        append_record(
            build_record(
                kind="yege_event_study",
                entrypoint="scripts/yege_event_study.py",
                strategy="yege_mainline_explosion",
                event_input=str(args.events),
                report=report,
            ),
            path=args.ledger,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
