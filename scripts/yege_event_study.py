"""野哥主线爆发战法离线事件研究摘要。

输入必须是已审计的 JSON/JSONL 事件记录；脚本不联网、不补造缺失数据。
每个事件应包含 ``validated``、``entry_assumption=next_session_open`` 和
1/3/5/10 日收益字段。证据不足时输出 insufficient_evidence。
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


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


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总已验证事件；不把缺失值当成零收益。"""
    valid = [
        event
        for event in events
        if event.get("validated") is True
        and event.get("entry_assumption") == "next_session_open"
        and event.get("evidence_status") == "available"
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
    for event in valid:
        by_sector[str(event.get("sector") or "unknown")] += 1
    concentrated = max(by_sector.values(), default=0) / len(valid) if valid else 0.0
    status = "ok" if len(valid) >= 30 and concentrated <= 0.30 else "insufficient_evidence"
    return {
        "rule_version": "yege-mainline-v1",
        "event_count": len(events),
        "validated_event_count": len(valid),
        "status": status,
        "entry_assumption": "next_session_open",
        "by_horizon": {
            horizon: {"count": len(values), "avg_return": _mean(values)}
            for horizon, values in sorted(horizons.items())
        },
        "sector_counts": dict(by_sector),
        "max_sector_concentration": concentrated,
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
    args = parser.parse_args()
    report = summarize(_read_events(args.events))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
