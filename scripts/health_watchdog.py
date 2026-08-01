#!/usr/bin/env python3
"""健康看门狗脚本（P0 静默失效防线，2026-08-01）。

用法:
    uv run python scripts/health_watchdog.py          # 检查并推送告警（自动去重）
    uv run python scripts/health_watchdog.py --check   # 只检查不推送（退出码 0/1）

检查项（每 5 分钟 cron 运行）：
1. 调度器心跳新鲜度：heartbeat.json 超过 15 分钟未更新 → 调度器可能挂死
2. 调度器重启风暴：单日重启 >3 次 → 网络错误风暴/崩溃循环
3. 信号历史新鲜度：调度器在跑但信号历史 >2 天未写入 → "系统活着但失忆了"

告警去重（P2）：同一问题在 suppress_minutes（默认 120 分钟）内只推送一次，
防持续故障时每 5 分钟刷屏。恢复健康后状态清除，下次故障立即重新告警。

退出码：--check 模式下 0=健康 1=有告警
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gugu.config import PROJECT_ROOT
from gugu.health.watchdog import dedupe_alerts, run_all_checks


def push_alerts(messages: list[str]) -> None:
    """推送告警到飞书（异步）。"""

    async def _push() -> None:
        from gugu.notifier.feishu import FeishuNotifier

        notifier = FeishuNotifier()
        for msg in messages:
            try:
                await notifier.notify_error({"message": msg})
                print(f"✅ 已推送告警: {msg[:60]}...")
            except Exception as e:
                print(f"❌ 告警推送失败: {e}")

    asyncio.run(_push())


def main() -> int:
    parser = argparse.ArgumentParser(description="gugu 健康看门狗")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查不推送（退出码 0=健康 1=有告警）",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="静默模式（cron watchdog 用）：健康时无输出，告警时输出告警文本",
    )
    parser.add_argument(
        "--suppress-minutes",
        type=int,
        default=120,
        help="同一问题抑制窗口（分钟），默认 120（2 小时内同一问题只告警一次）",
    )
    args = parser.parse_args()

    data_dir = PROJECT_ROOT / "data"
    results = run_all_checks(data_dir=data_dir)

    if args.check:
        # 只检查模式：不去重、不写状态文件，如实反映当前健康状态
        alerts = [r for r in results if not r.ok]
        healthy = [r for r in results if r.ok]

        print("=" * 50)
        print("gugu 健康看门狗检查")
        print("=" * 50)
        for r in healthy:
            print(f"  ✅ {r.message}")
        for r in alerts:
            print(f"  ❌ [{r.severity}] {r.message}")

        if not alerts:
            print("\n✅ 全部健康，无需告警")
            return 0
        print(f"\n⚠️ 发现 {len(alerts)} 个问题")
        return 1

    # 告警去重：同一问题在抑制窗口内不重复推送（防持续故障刷屏）
    alerts = dedupe_alerts(
        results,
        data_dir=data_dir,
        suppress_minutes=args.suppress_minutes,
    )

    if args.quiet:
        # cron no_agent 模式：空 stdout = 静默；非空 = 投递
        if not alerts:
            return 0
        print("\n".join(r.message for r in alerts[:3]))
        push_alerts([r.message for r in alerts[:3]])
        return 1

    healthy = [r for r in results if r.ok]

    print("=" * 50)
    print("gugu 健康看门狗检查")
    print("=" * 50)
    for r in healthy:
        print(f"  ✅ {r.message}")
    for r in alerts:
        print(f"  ❌ [{r.severity}] {r.message}")

    if not alerts:
        print("\n✅ 全部健康，无需告警")
        return 0

    print(f"\n⚠️ 发现 {len(alerts)} 个问题（去重后）")
    # 推送告警（最多 3 条，避免刷屏）
    push_alerts([r.message for r in alerts[:3]])
    return 1


if __name__ == "__main__":
    sys.exit(main())
