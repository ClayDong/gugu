#!/usr/bin/env python3
"""统一事件研究 CLI。

普通策略统一使用 ``gugu.analysis.event_study.EventStudy``；有专用证据契约的
策略（如野哥 T+1、隔夜套利）继续使用各自的审计脚本。

示例::

    python scripts/event_study.py --strategy box_breakout --from-strategy
    python scripts/event_study.py --strategy double_yin --from-history
    python scripts/event_study.py --strategy linjudaye --from-strategy \
        --pool 002230 300308 --entry-mode close
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.analysis.event_study import EventStudy, StudyConfig  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="统一事件研究")
    parser.add_argument("--strategy", required=True, help="已注册的策略名")
    parser.add_argument("--history", default="data/signals_history.jsonl")
    parser.add_argument("--from-history", action="store_true", help="从信号历史分析")
    parser.add_argument("--from-strategy", action="store_true", help="直接运行策略离线验证")
    parser.add_argument("--pool", nargs="*", default=None, help="六位股票代码列表")
    parser.add_argument("--horizons", default="3,5,10", help="逗号分隔的持有周期")
    parser.add_argument("--entry-mode", default="next_open", choices=["close", "next_open"])
    parser.add_argument("--cost", type=float, default=0.004, help="往返交易成本")
    parser.add_argument("--index", default="000300", help="基准指数代码")
    parser.add_argument("--output", "--json", dest="output", default=None)
    parser.add_argument("--days", type=int, default=600)
    parser.add_argument(
        "--need-fundamentals",
        action="store_true",
        help="兼容旧参数；实时基本面不纳入可复现事件研究",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.from_history and args.from_strategy:
        raise SystemExit("--from-history 与 --from-strategy 不能同时使用")

    config = StudyConfig(
        strategy_name=args.strategy,
        horizons=[int(value) for value in args.horizons.split(",")],
        index_code=args.index,
        entry_mode=args.entry_mode,
        cost_rate=args.cost,
        output_path=args.output or f"data/{args.strategy}_event_study.json",
        stock_pool=args.pool,
        data_days=args.days,
    )
    study = EventStudy(config)

    if args.from_history:
        result = study.run_from_history(args.history, max_days=args.days)
    else:
        from gugu.strategies.registry import get_strategy

        result = study.run_from_strategy(get_strategy(args.strategy), days=args.days)

    study.print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
