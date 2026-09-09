"""野哥主线爆发事件的离线 T+1 验证。

输入为 JSON/JSONL 事件记录，记录中必须保留 T 日事件证据和 T+1
可执行行情字段。本脚本不联网、不补造缺失值，也不会修改策略准入状态。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.analysis.yege_battle import validate_next_day


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        return rows
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("events", [])
    return [row for row in value if isinstance(row, dict)]


def validate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐事件执行 T+1 验证，缺字段交给规则层 fail-closed。"""
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            validate_next_day(
                row,
                next_open=row.get("next_open"),
                next_high=row.get("next_high"),
                next_low=row.get("next_low"),
                next_close=row.get("next_close"),
                first_board_survival_rate=row.get("first_board_survival_rate"),
                anchor_negative_feedback=bool(row.get("anchor_negative_feedback_t1", False)),
                min_open_premium=float(row.get("min_open_premium", 0.0) or 0.0),
                min_survival_rate=float(row.get("min_survival_rate", 0.5) or 0.5),
                benchmark_open_return=row.get("benchmark_open_return"),
                benchmark_close_return=row.get("benchmark_close_return"),
            )
        )
    return result


def summarize_validations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总 T+1 验证，不把失败或不足证据计入平均收益。"""
    available = [row for row in rows if row.get("validation_status") == "available"]
    passed = [row for row in available if row.get("validated") is True]
    rule_failed = [row for row in rows if row.get("validation_status") == "rule_failed"]
    insufficient = [row for row in rows if row.get("validation_status") == "insufficient_evidence"]

    def finite_mean(field: str) -> float | None:
        values: list[float] = []
        for row in passed:
            value = row.get(field)
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                values.append(number)
        return round(sum(values) / len(values), 6) if values else None

    return {
        "rule_version": "yege-mainline-v1",
        "event_count": len(rows),
        "available_count": len(available),
        "validated_count": len(passed),
        "rule_failed_count": len(rule_failed),
        "insufficient_evidence_count": len(insufficient),
        "avg_open_premium": finite_mean("open_premium"),
        "avg_close_premium": finite_mean("close_premium"),
        "avg_excess_open_return": finite_mean("excess_open_return"),
        "avg_excess_close_return": finite_mean("excess_close_return"),
        "status": "insufficient_evidence" if len(passed) < 30 else "available",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--ledger",
        type=Path,
        help="可选：将本次 T+1 验证摘要追加到 research_ledger.jsonl",
    )
    args = parser.parse_args()

    rows = validate_rows(_read_rows(args.events))
    report = summarize_validations(rows)
    payload = {"report": report, "events": rows}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    if args.ledger:
        from gugu.research.ledger import append_record, build_record

        append_record(
            build_record(
                kind="yege_next_day_validation",
                entrypoint="scripts/validate_yege_next_day.py",
                strategy="yege_mainline_explosion",
                event_input=str(args.events),
                report=report,
            ),
            path=args.ledger,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
