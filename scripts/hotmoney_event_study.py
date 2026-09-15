#!/usr/bin/env python3
"""已退役 hot_money 事件研究入口。

``hot_money`` 已从运行时注册表移除，原因是样本外证伪和集中度问题。
保留脚本名用于旧自动任务的明确 fail-closed 提示，不再请求行情或生成新报告。
"""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_POOL = [
    "002472",
    "300033",
    "002230",
    "603501",
    "300308",
    "300502",
    "600519",
    "300750",
    "002594",
    "300059",
    "002475",
    "600050",
    "000977",
    "688041",
    "002371",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="hot_money 退役事件研究入口")
    parser.add_argument("--days", type=int, default=600, help="兼容旧参数，不再使用")
    parser.add_argument("--pool", nargs="*", default=DEFAULT_POOL, help="兼容旧参数，不再使用")
    parser.add_argument("--json", type=Path, default=Path("data/hotmoney_event_study.json"))
    parser.parse_args()
    raise SystemExit(
        "hot_money 已从策略注册表移除，事件研究停止；请使用 "
        "scripts/event_study.py 验证当前已注册策略。"
    )


if __name__ == "__main__":
    main()
