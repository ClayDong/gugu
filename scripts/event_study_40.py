#!/usr/bin/env python3
"""linjudaye 扩池事件研究（40只，2026-08-01）。

现为 scripts/event_study.py 的封装，使用统一事件研究框架。
等价命令:
    python scripts/event_study.py --strategy linjudaye --from-strategy \\
        --pool 002230 300308 ... --entry-mode close --output data/linjudaye_40.json
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from gugu.analysis.event_study import EventStudy, StudyConfig
from gugu.strategies.registry import get_strategy

# 40 只跨板块活跃股（覆盖科技/消费/金融/新能源/周期）
POOL_40 = [
    "002230",
    "300308",
    "300502",
    "688041",
    "002371",
    "603501",
    "000977",
    "300661",
    "002049",
    "300496",
    "300033",
    "002475",
    "300059",
    "002415",
    "300418",
    "300750",
    "002594",
    "300274",
    "002460",
    "300014",
    "600519",
    "000858",
    "600809",
    "002304",
    "603288",
    "601318",
    "600036",
    "600030",
    "601166",
    "000001",
    "600276",
    "300760",
    "000538",
    "300015",
    "002007",
    "600760",
    "002179",
    "600893",
    "601989",
    "600150",
]


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="linjudaye 40股扩池事件研究")
    ap.add_argument("--strategy", default="linjudaye", help="策略名")
    ap.add_argument("--output", default=None, help="结果 JSON 路径")
    args = ap.parse_args()

    config = StudyConfig(
        strategy_name=args.strategy,
        stock_pool=POOL_40,
        output_path=args.output or f"data/{args.strategy}_event_study_40.json",
        entry_mode="close",
    )
    study = EventStudy(config)
    strategy = get_strategy(args.strategy)
    result = study.run_from_strategy(strategy.__class__)
    study.print_summary(result)


if __name__ == "__main__":
    main()
