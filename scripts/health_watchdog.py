#!/usr/bin/env python3
"""健康看门狗脚本（P0 静默失效防线，2026-08-01）。

用法:
    uv run python scripts/health_watchdog.py          # 检查并推送告警（自动去重）
    uv run python scripts/health_watchdog.py --check   # 只检查不推送（退出码 0/1）
    uv run python scripts/health_watchdog.py --auto-fix  # 检测→根因分析→自动修复→失败才推送

检查项（每 5 分钟 cron 运行）：
1. 调度器心跳新鲜度：heartbeat.json 超过 15 分钟未更新 → 调度器可能挂死
2. 调度器重启风暴：单日重启 >3 次 → 网络错误风暴/崩溃循环
3. 信号历史新鲜度：调度器在跑但信号历史 >2 天未写入 → "系统活着但失忆了"
4. 磁盘空间：<1GB → 持久化可能静默失败
5. 数据源降级：失败率 >50% → 0 信号/0 选股风险
6. 连续 0 信号：交易时段连续 3 轮扫描无信号

--auto-fix 模式（2026-08-10 P0 自愈闭环）：
检测到告警后先走 AutoRecover 根因分析 + 自动修复，修复成功且验证通过则
不推送（或推送修复摘要），只有「无法自动修复 / 修复后验证失败」的项才
推送带根因分析的飞书告警。用户收到告警 = 系统已尝试自愈但确实需要介入。

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
from gugu.health.auto_recover import AutoRecover, FixResult
from gugu.health.watchdog import dedupe_alerts, run_all_checks


def group_alert_messages(results: list[object]) -> list[str]:
    return [
        result.message for result in results if not result.message.startswith("⚠️ 调度器重启风暴")
    ]


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
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help="自愈模式：检测到告警先自动修复，修复失败才推送人工告警",
    )
    parser.add_argument(
        "--notify-auto-fixed",
        action="store_true",
        help="自愈成功后也推送摘要（默认静默，仅记录审计日志）",
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

    if args.auto_fix:
        return _auto_fix_mode(results, data_dir, args)

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
        group_messages = group_alert_messages(alerts[:3])
        if group_messages:
            print("\n".join(group_messages))
            push_alerts(group_messages)
        else:
            print("\n".join(r.message for r in alerts[:3]))
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
    group_messages = group_alert_messages(alerts[:3])
    if group_messages:
        push_alerts(group_messages)
    return 1


def _auto_fix_mode(results, data_dir: Path, args) -> int:
    """自愈模式主流程：告警 → 根因分析 → 自动修复 → 验证 → 失败才升级人工。

    返回码：0 = 全部健康或全部自愈成功；1 = 有需要人工介入的告警。
    """
    alerts = [r for r in results if not r.ok]
    if not alerts:
        if not args.quiet:
            print("✅ 全部健康，无需修复")
        return 0

    if not args.quiet:
        print("=" * 50)
        print("gugu 自愈模式：检测到告警，开始根因分析 + 自动修复")
        print("=" * 50)

    # 根因分析 + 自动修复
    recover = AutoRecover(data_dir=data_dir)
    fixes: list[FixResult] = recover.run(alerts)

    # 需要升级人工的 = 未修复 或 修复后验证失败
    escalations = [f for f in fixes if f.escalation]
    # 自愈成功的（用于摘要推送）
    auto_fixed = [f for f in fixes if f.ok and not f.escalation]

    lines: list[str] = []
    for fix in fixes:
        status = "✅ 自愈" if fix.ok else "❌ 需人工"
        lines.append(f"{status} [{fix.fingerprint}] 根因: {fix.root_cause}")
        if fix.action:
            lines.append(f"    动作: {fix.action}")
        if not fix.ok:
            lines.append(f"    升级原因: 无法自动修复或修复后验证失败")
    summary = "\n".join(lines)

    if not args.quiet:
        print(summary)
        print(f"\n自愈成功 {len(auto_fixed)} 项，需人工介入 {len(escalations)} 项")

    # 有需要人工介入的 → 推送带根因分析的告警
    if escalations:
        if args.quiet:
            print(summary)  # cron no_agent：非空 stdout 触发投递
        # 推送原始告警 + 根因/修复尝试摘要（去重后）
        alert_msgs = group_alert_messages(alerts[:3])
        for msg in alert_msgs:
            push_alerts([msg + "\n\n🛠️ 自动修复尝试失败，根因分析:\n" + summary])
        return 1

    # 全部自愈成功
    if args.notify_auto_fixed and auto_fixed:
        # 可选：推送自愈摘要（默认静默，仅审计日志）
        push_alerts([f"✅ gugu 自动修复 {len(auto_fixed)} 项问题:\n{summary}"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
