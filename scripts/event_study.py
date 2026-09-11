#!/usr/bin/env python3
"""统一事件研究 CLI — 替代 scripts/ 下 9 个独立事件研究脚本。

用法:
    # 从 signals_history.jsonl 分析已有策略绩效
    python scripts/event_study.py --strategy double_yin --from-history

    # 直接运行策略+股票池离线验证
    python scripts/event_study.py --strategy box_breakout --from-strategy

    # 指定股票池
    python scripts/event_study.py --strategy rsi_reversal --pool 600519 000858

    # 自定义参数
    python scripts/event_study.py --strategy two_b_reversal --horizons 5,10 \\
        --entry-mode close --cost 0.002 --output data/my_study.json

方法论（gugu-strategy-backtest skill 铁律）：
1. 事件研究先行：信号后 N 日收益 vs 沪深300 超额，Welch t 检验
2. 训练期选参 / 验证期冻结（Walk-Forward）
3. 触发股集中度检查（单股>30%信号=不可信，剔除后重验）
4. 超额收益检验（赚钱≠有alpha）
5. 年份分桶（防单一年份/暴跌日污染）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu.analysis.event_study import EventStudy, StudyConfig


def main() -> None:
    ap = argparse.ArgumentParser(description="统一事件研究")
    ap.add_argument("--strategy", required=True, help="策略名")
    ap.add_argument("--from-history", action="store_true", help="从 signals_history.jsonl 分析")
    ap.add_argument("--from-strategy", action="store_true", help="直接运行策略离线验证")
    ap.add_argument("--pool", nargs="*", default=None, help="股票池（默认 15 只跨行业）")
    ap.add_argument("--horizons", default="3,5,10", help="逗号分隔的周期")
    ap.add_argument("--entry-mode", default="next_open", choices=["close", "next_open"])
    ap.add_argument("--cost", type=float, default=0.004, help="双向交易成本")
    ap.add_argument("--index", default="000300", help="基准指数代码")
    ap.add_argument("--output", default=None, help="结果 JSON 路径")
    ap.add_argument("--days", type=int, default=600, help="K 线天数")
    args = ap.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]

    config = StudyConfig(
        strategy_name=args.strategy,
        horizons=horizons,
        index_code=args.index,
        entry_mode=args.entry_mode,
        cost_rate=args.cost,
        output_path=args.output,
        stock_pool=args.pool,
    )
    study = EventStudy(config)

    if args.from_history:
        result = study.run_from_history()
    elif args.from_strategy:
        # 从 registry 加载策略
        from gugu.strategies.registry import get_strategy

        strategy = get_strategy(args.strategy)
        result = study.run_from_strategy(type(strategy), days=args.days)
    else:
        # 默认：先尝试 history，若无信号则 fallback 到 strategy
        try:
            result = study.run_from_history()
        except FileNotFoundError:
            result = study._empty_result()

        if result.n_events == 0:
            print("⚠️ 历史信号中未找到，尝试直接运行策略...")
            from gugu.strategies.registry import get_strategy

            strategy = get_strategy(args.strategy)
            result = study.run_from_strategy(type(strategy), days=args.days)

    study.print_summary(result)

    # 集中度检查后的剔除重验
    if result.concentration_issues and result.n_events > 0:
        print("\n⚠️ 发现集中度问题，进行剔除重验...")
        for issue in result.concentration_issues:
            sym = issue["symbol"]
            filtered_events = [e for e in result.events if e.symbol != sym]
            if len(filtered_events) < 10:
                print(f"  剔除 {sym} 后样本不足 ({len(filtered_events)} < 10)，跳过")
                continue
            # 重新统计
            filtered_result = StudyResult(
                strategy_name=result.strategy_name,
                config=result.config,
                n_events=len(filtered_events),
                n_symbols=len({e.symbol for e in filtered_events}),
                concentration_issues=[],
                per_horizon=study._aggregate_by_horizon(filtered_events),
                per_year={},
                per_period={},
                events=filtered_events,
            )
            print(f"  剔除 {sym}({issue['count']} 条) 后重验:")
            for rh in filtered_result.per_horizon:
                if rh.horizon in (5, 10):
                    sig = "✅" if rh.significant else ""
                    print(
                        f"    [{rh.horizon}d] 超额 {rh.avg_excess:+>.1%} "
                        f"p={rh.p_value:.4f} n={rh.n_events} {sig}"
                    )


if __name__ == "__main__":
    main()
