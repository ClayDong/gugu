"""策略准入验证脚本。

将回测、Walk-Forward、paper 绩效等指标统一成结构化门槛，避免只凭自然语言结论启用策略。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.config import get_config  # noqa: E402

GATE_THRESHOLDS: dict[str, float] = {
    "total_trades": 100,
    "avg_return": 0.0,
    "median_return": 0.0,
    "avg_sharpe": 0.0,
    "max_drawdown": 0.25,
    "profitable_ratio": 0.50,
    "param_stability": 0.50,
    "paper_signals": 30,
    "paper_return": 0.0,
}


_HIGHER_IS_BETTER = {
    "total_trades",
    "avg_return",
    "median_return",
    "avg_sharpe",
    "profitable_ratio",
    "param_stability",
    "paper_signals",
    "paper_return",
}


_LOWER_IS_BETTER = {"max_drawdown"}


def evaluate_gate(strategy: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """根据统一门槛评估策略是否允许启用。"""
    failed: list[str] = []
    checks: dict[str, dict[str, Any]] = {}

    for key, threshold in GATE_THRESHOLDS.items():
        value = metrics.get(key)
        if value is None:
            passed = False
        elif key in _HIGHER_IS_BETTER:
            passed = float(value) >= threshold
        elif key in _LOWER_IS_BETTER:
            passed = float(value) <= threshold
        else:
            passed = False

        checks[key] = {
            "value": value,
            "threshold": threshold,
            "passed": passed,
        }
        if not passed:
            failed.append(key)

    return {
        "date": str(date.today()),
        "strategy": strategy,
        "passed": not failed,
        "failed_checks": failed,
        "checks": checks,
    }


def _load_metrics(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("metrics JSON 顶层必须是对象")
    return data


def _enabled_strategies() -> list[str]:
    cfg = get_config().strategy
    return cfg.get_all_enabled_unique() if cfg.enabled_by_regime else list(cfg.enabled)


def main() -> None:
    parser = argparse.ArgumentParser(description="策略准入门禁验证")
    parser.add_argument("--strategy", type=str, default="", help="策略名；为空时打印当前启用策略")
    parser.add_argument("--metrics", type=str, default="", help="包含结构化指标的 JSON 文件")
    parser.add_argument("--output", type=str, default="", help="输出验证报告 JSON 路径")
    parser.add_argument("--notify", action="store_true", help="将验证结果推送到飞书")
    args = parser.parse_args()

    if not args.strategy:
        print(json.dumps({"enabled_strategies": _enabled_strategies()}, ensure_ascii=False, indent=2))
        return

    if not args.metrics:
        raise SystemExit("必须提供 --metrics JSON 文件")

    report = evaluate_gate(args.strategy, _load_metrics(args.metrics))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")

    # 飞书通知
    if args.notify:
        import asyncio
        from gugu.notifier.feishu import FeishuNotifier

        metrics = _load_metrics(args.metrics)
        failed_checks = report.get("failed_checks", [])
        issues = [f"{c}: {metrics.get(c, 'N/A')} (阈值: {GATE_THRESHOLDS.get(c, 'N/A')})" for c in failed_checks]
        recs = []
        if "avg_sharpe" in failed_checks:
            recs.append("夏普比率过低，尝试优化参数或检查数据质量")
        if "max_drawdown" in failed_checks:
            recs.append("最大回撤超标，考虑加入止损或降低仓位")
        if "total_trades" in failed_checks:
            recs.append("交易次数不足，需要更长回测周期或更多标的")

        validation_report = {
            "strategy_name": args.strategy,
            "gate_status": "passed" if report["passed"] else "failed",
            "metrics": {k: v["value"] for k, v in report["checks"].items()},
            "issues": issues,
            "recommendations": recs,
        }

        async def _send():
            notifier = FeishuNotifier()
            await notifier.notify_validation(validation_report)

        asyncio.run(_send())

    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
