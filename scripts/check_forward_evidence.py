#!/usr/bin/env python3
"""前向证据门槛：任何策略声称「通过」之前，必须有真实已实现盈亏样本。

2026-11-04 评审 P0-5：项目零 verified 策略、零前向已实现盈亏，却存在
「+2.4%~+3.7% p<0.01」这类无数据支撑的声明。本脚本只**检查**账本，
绝不生成或填充任何模拟样本。

用法：python scripts/check_forward_evidence.py [--ledger PATH] [--min-samples 30]
退出码：0 = 达标；1 = 未达标（此时不得把任何策略标为 enabled）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "data" / "paper_execution_ledger.jsonl"


def evaluate_ledger(
    path: Path,
    *,
    min_samples: int = 30,
    min_distinct_dates: int = 10,
) -> dict[str, Any]:
    """统计账本中真实已实现盈亏样本数与跨日期覆盖。"""
    if not path.exists():
        return {
            "ledger": str(path),
            "total_rows": 0,
            "realized_samples": 0,
            "distinct_dates": 0,
            "passed": False,
            "reasons": ["账本文件不存在（禁止用空账本声称已验证）"],
        }
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except json.JSONDecodeError:
            continue
    realized = [
        row
        for row in rows
        if row.get("realized_pnl") not in (None, "", 0) or row.get("pnl") not in (None, "", 0)
    ]
    dates = {
        str(row.get("date") or row.get("timestamp") or row.get("closed_at") or "")[:10]
        for row in realized
    }
    dates.discard("")
    reasons: list[str] = []
    if len(realized) < min_samples:
        reasons.append(f"已实现盈亏样本 {len(realized)} < 要求 {min_samples}")
    if len(dates) < min_distinct_dates:
        reasons.append(f"跨日期覆盖 {len(dates)} < 要求 {min_distinct_dates}")
    return {
        "ledger": str(path),
        "total_rows": len(rows),
        "realized_samples": len(realized),
        "distinct_dates": len(dates),
        "min_samples": min_samples,
        "min_distinct_dates": min_distinct_dates,
        "passed": not reasons,
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="前向已实现盈亏证据门槛")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--min-distinct-dates", type=int, default=10)
    args = parser.parse_args(argv)
    report = evaluate_ledger(
        args.ledger,
        min_samples=args.min_samples,
        min_distinct_dates=args.min_distinct_dates,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
