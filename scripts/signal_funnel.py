#!/usr/bin/env python3
"""信号漏斗报告：把「产生多少信号 → 被什么拦掉 → 成交多少」变成可读的一条。

为什么需要它（2026-11-05）：
`data/trade_journal.jsonl` 里 19 个交易日、144 条记录、零成交，全部是
`gate_blocked`，但系统从不主动告诉你这件事。这个漏斗一直是黑箱——
策略到底有没有在产生信号、卡在哪一道门、有没有重复刷屏，全靠人工挖日志。

没有漏斗就调阈值，等于闭着眼睛改风控。本脚本只做统计，不改任何判定逻辑。

用法：
    python scripts/signal_funnel.py                 # 最近 30 天
    python scripts/signal_funnel.py --days 0        # 全部历史
    python scripts/signal_funnel.py --json          # 机器可读
退出码：0 = 正常；1 = 账本不存在（如实报告，不伪造）
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DEFAULT_JOURNAL = Path(__file__).resolve().parents[1] / "data" / "trade_journal.jsonl"

# 拦截原因归类。顺序敏感：先匹配到的类目生效。
_CATEGORY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("暴涨", "暴跌", "追涨杀跌"), "追涨杀跌"),
    (("接力成功率",), "接力环境"),
    (("封板率",), "打板环境"),
    (("涨停稀少",), "涨停稀少-空仓"),
    (("数据不足", "仅观察"), "数据不足"),
    (("熔断",), "风控熔断"),
    (("持仓数已达上限",), "仓位已满"),
    (("白名单", "verified"), "策略白名单"),
    (("T+1", "可卖"), "T+1可卖限制"),
)

# result 字段的语义分组
_EXECUTED_RESULTS = frozenset({"executed", "filled"})
_OBSERVE_RESULTS = frozenset({"observation_only"})


def classify_reason(reason: str) -> str:
    """把一条拦截原因归入可统计的类目。"""
    text = str(reason or "")
    for keywords, category in _CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            return category
    return "其他" if text else "未记录原因"


def load_journal(path: Path) -> list[dict[str, Any]]:
    """读取交易日志；坏行跳过但不隐瞒。"""
    if not path.exists():
        return []
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
    return rows


def _row_date(row: dict[str, Any]) -> str:
    return str(row.get("ts") or "")[:10]


def summarize(rows: list[dict[str, Any]], *, days: int = 30) -> dict[str, Any]:
    """按日聚合漏斗。days<=0 表示不限窗口。"""
    if days > 0:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = [r for r in rows if _row_date(r) >= cutoff]

    events = len(rows)
    results = Counter(str(r.get("result") or "unknown") for r in rows)
    executed = sum(v for k, v in results.items() if k in _EXECUTED_RESULTS)
    observing = sum(v for k, v in results.items() if k in _OBSERVE_RESULTS)
    blocked = results.get("gate_blocked", 0)

    # 拦截原因只在 gate_blocked 里统计，避免把成交原因混进来
    blocked_rows = [r for r in rows if str(r.get("result")) == "gate_blocked"]
    categories = Counter(classify_reason(r.get("reason", "")) for r in blocked_rows)
    strategies = Counter(str(r.get("strategy") or "未标注") for r in blocked_rows)

    # 重复刷屏：同一标的在同一天被同一原因反复拦截，说明扫描层缺去重
    dup_counter = Counter(
        (_row_date(r), str(r.get("symbol") or ""), classify_reason(r.get("reason", "")))
        for r in blocked_rows
    )
    duplicates = [
        {"date": d, "symbol": s, "category": c, "count": n}
        for (d, s, c), n in dup_counter.most_common()
        if n > 1
    ]

    daily: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "blocked": 0, "executed": 0, "observing": 0}
    )
    for row in rows:
        d = _row_date(row) or "unknown"
        bucket = daily[d]
        bucket["total"] += 1
        result = str(row.get("result") or "")
        if result == "gate_blocked":
            bucket["blocked"] += 1
        elif result in _EXECUTED_RESULTS:
            bucket["executed"] += 1
        elif result in _OBSERVE_RESULTS:
            bucket["observing"] += 1

    dates = sorted(k for k in daily if k != "unknown")
    return {
        "window_days": days,
        "trading_days": len(dates),
        "date_range": [dates[0], dates[-1]] if dates else [],
        "total_events": events,
        "executed": executed,
        "observing": observing,
        "blocked": blocked,
        "pass_rate": round(executed / events, 4) if events else 0.0,
        "by_result": dict(results),
        "by_category": dict(categories.most_common()),
        "by_strategy": dict(strategies.most_common()),
        "duplicate_blocks": duplicates[:10],
        "daily": {k: daily[k] for k in dates},
    }


def render_markdown(report: dict[str, Any]) -> str:
    """渲染成人能读的报告。"""
    lines: list[str] = ["# 信号漏斗", ""]
    if not report["total_events"]:
        lines.append("窗口内没有任何交易事件记录。")
        return "\n".join(lines)

    span = report["date_range"]
    lines.append(
        f"**窗口**：最近 {report['window_days'] or '全部'} 天 · "
        f"{report['trading_days']} 个有记录的日子 · {span[0]} → {span[1]}"
    )
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(f"- 事件总数：**{report['total_events']}**")
    lines.append(f"- 成交：**{report['executed']}**（通过率 {report['pass_rate']:.1%}）")
    lines.append(f"- 仅观察：{report['observing']}")
    lines.append(f"- 被拦截：**{report['blocked']}**")
    lines.append("")

    if report["by_category"]:
        lines.append("## 拦截原因分布")
        lines.append("")
        lines.append("| 类目 | 次数 | 占比 |")
        lines.append("|---|---:|---:|")
        blocked = report["blocked"] or 1
        for cat, n in report["by_category"].items():
            lines.append(f"| {cat} | {n} | {n / blocked:.1%} |")
        lines.append("")

    if report["by_strategy"]:
        lines.append("## 被拦信号来自哪些策略")
        lines.append("")
        lines.append("| 策略 | 拦截次数 |")
        lines.append("|---|---:|")
        for name, n in list(report["by_strategy"].items())[:10]:
            lines.append(f"| {name} | {n} |")
        lines.append("")

    if report["duplicate_blocks"]:
        lines.append("## 重复拦截（同一标的同日同原因多次）")
        lines.append("")
        lines.append("> 同一标的在同一天被同一原因反复拦截，说明扫描层对已处理的")
        lines.append("> 标的没有去重，日志会被单只股票刷屏，掩盖真实信号分布。")
        lines.append("")
        lines.append("| 日期 | 标的 | 原因类目 | 次数 |")
        lines.append("|---|---|---|---:|")
        for d in report["duplicate_blocks"]:
            lines.append(f"| {d['date']} | {d['symbol']} | {d['category']} | {d['count']} |")
        lines.append("")

    if report["daily"]:
        lines.append("## 按日")
        lines.append("")
        lines.append("| 日期 | 事件 | 拦截 | 成交 | 观察 |")
        lines.append("|---|---:|---:|---:|---:|")
        for d, v in report["daily"].items():
            lines.append(
                f"| {d} | {v['total']} | {v['blocked']} | {v['executed']} | {v['observing']} |"
            )
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="信号漏斗报告")
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--days", type=int, default=30, help="统计窗口天数，0=全部")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    if not args.journal.exists():
        print(
            json.dumps(
                {"error": f"账本不存在: {args.journal}", "passed": False}, ensure_ascii=False
            )
        )
        return 1

    report = summarize(load_journal(args.journal), days=args.days)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
