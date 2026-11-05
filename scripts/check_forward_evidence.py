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

# 只有真实每日运行产生的成交才算前向证据；回放/夹具/未标注一律不计入。
FORWARD_EVIDENCE_CLASS = "forward_paper"

# 账本由 paper_ledger.build_fill_record 写入，成交时间字段是 created_at。
# 之前只读 date/timestamp/closed_at，导致真实成交的跨日期覆盖恒为 0，
# 门槛无论如何都过不了（2026-11-05 修复）。
_DATE_FIELDS = ("created_at", "date", "timestamp", "closed_at")


def _row_date(row: dict[str, Any]) -> str:
    for key in _DATE_FIELDS:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)[:10]
    return ""


def _is_forward_realized_sell(row: dict[str, Any], *, evidence_class: str) -> bool:
    """一条成交是否构成可计入前向验证的已实现盈亏样本。

    四个条件缺一不可，任一不满足都不得计入：
    1. ``evidence_class`` 必须是 forward_paper——回放、测试夹具、未标注的行
       一律排除，防止用手工构造的数据冒充前向验证；
    2. ``direction`` 必须是 sell——买入没有已实现盈亏；
    3. ``realized_pnl`` 必须是数字（0 元保本交易同样是一个有效样本，
       不能用真值判断把它当成缺失）；
    4. ``execution_id`` 与 ``symbol`` 必须非空，排除手写的占位行。
    """
    if str(row.get("evidence_class") or "") != evidence_class:
        return False
    if str(row.get("direction") or "").lower() != "sell":
        return False
    pnl = row.get("realized_pnl")
    if isinstance(pnl, bool) or not isinstance(pnl, int | float):
        return False
    if not str(row.get("execution_id") or "").strip():
        return False
    return bool(str(row.get("symbol") or "").strip())


def evaluate_ledger(
    path: Path,
    *,
    min_samples: int = 30,
    min_distinct_dates: int = 10,
    evidence_class: str = FORWARD_EVIDENCE_CLASS,
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
        row for row in rows if _is_forward_realized_sell(row, evidence_class=evidence_class)
    ]
    excluded = len(rows) - len(realized)
    dates = {_row_date(row) for row in realized}
    dates.discard("")
    reasons: list[str] = []
    if len(realized) < min_samples:
        reasons.append(f"已实现盈亏样本 {len(realized)} < 要求 {min_samples}")
    if len(dates) < min_distinct_dates:
        reasons.append(f"跨日期覆盖 {len(dates)} < 要求 {min_distinct_dates}")
    # 被排除的行只作为信息展示，不单独判失败——只要有效样本达标就可以通过，
    # 否则长期累积的回放记录会永久卡死门槛。
    notes = (
        [f"{excluded} 行不计入前向证据（非 {evidence_class} 的卖出成交：回放/夹具/未平仓）"]
        if excluded
        else []
    )
    return {
        "ledger": str(path),
        "total_rows": len(rows),
        "realized_samples": len(realized),
        "excluded_rows": excluded,
        "distinct_dates": len(dates),
        "realized_pnl": sum(float(row["realized_pnl"]) for row in realized),
        "min_samples": min_samples,
        "min_distinct_dates": min_distinct_dates,
        "passed": not reasons,
        "reasons": reasons,
        "notes": notes,
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
