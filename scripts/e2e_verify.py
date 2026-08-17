#!/usr/bin/env python3
"""gugu E2E 验证脚本（2026-08-05 P0 防护）。

每次代码上线/配置变更后运行，验证三件事：
1. 单实例锁：确认没有双 scheduler 并行（杜绝 8/5 双实例事故）
2. 真实扫描：跑一轮真实 _scan_signals，核对信号产出 + signal_scan.json 更新
3. 通知链路：飞书双通道（系统→Hermes 对话 / 业务→开悟之路群）连通性

用法：
    uv run python scripts/e2e_verify.py             # 完整 E2E（含真实扫描）
    uv run python scripts/e2e_verify.py --no-scan   # 只检查锁+心跳+通知配置，不扫描
    uv run python scripts/e2e_verify.py --no-notify # 扫描但跳过真实飞书测试卡片
    uv run python scripts/e2e_verify.py --check     # 只读检查（不扫描、不发送）

退出码：0=全部通过 1=有失败项
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gugu.config import PROJECT_ROOT  # noqa: E402

RESULTS: list[dict] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "ok": ok, "detail": detail})
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))


async def check_single_instance() -> None:
    """验证单实例锁：运行中的 scheduler 应持有锁且为唯一实例。"""
    lock_path = PROJECT_ROOT / "data" / "scheduler.lock"
    if not lock_path.exists():
        report("单实例锁", False, "锁文件不存在（scheduler 可能未运行，或 lock 已被清理）")
        return

    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception as e:
        report("单实例锁", False, f"锁文件读取失败: {e}")
        return

    pid = int(data.get("pid", 0) or 0)
    if pid <= 0:
        report("单实例锁", False, f"锁文件 pid 无效: {data}")
        return

    # 检查进程存活
    try:
        import os

        os.kill(pid, 0)
    except ProcessLookupError:
        report("单实例锁", False, f"锁持有进程已死 (PID={pid})，存在残留锁")
        return
    except PermissionError:
        pass

    # 检查是否只有一个 scheduler 进程
    import subprocess

    proc = subprocess.run(
        ["pgrep", "-fl", "run_scheduler|engine.scheduler|run_live.py"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    scheduler_pids = [line.split()[0] for line in (proc.stdout or "").splitlines() if line.strip()]
    if len(scheduler_pids) > 1:
        report(
            "单实例锁",
            False,
            f"发现 {len(scheduler_pids)} 个 scheduler 进程: {scheduler_pids}（双实例风险！）",
        )
        return
    if len(scheduler_pids) == 0:
        report("单实例锁", False, "没有运行中的 scheduler 进程（launchd 未拉起？）")
        return

    report(
        "单实例锁",
        True,
        f"锁持有 PID={pid}，唯一 scheduler 进程 PID={scheduler_pids[0]}",
    )


async def check_heartbeat() -> None:
    """验证心跳新鲜度。"""
    hb_path = PROJECT_ROOT / "data" / "heartbeat.json"
    if not hb_path.exists():
        report("心跳", False, "heartbeat.json 不存在")
        return
    try:
        data = json.loads(hb_path.read_text(encoding="utf-8"))
        last = data.get("last_cycle_at", "")
        if not last:
            report("心跳", False, "heartbeat.json 无 last_cycle_at")
            return
        dt = datetime.fromisoformat(last)
        age_min = (datetime.now() - dt).total_seconds() / 60.0
        if age_min > 15:
            report("心跳", False, f"心跳 {age_min:.0f} 分钟前（>15min，scheduler 可能挂死）")
            return
        report("心跳", True, f"{age_min:.1f} 分钟前，status={data.get('status')}")
    except Exception as e:
        report("心跳", False, f"心跳解析失败: {e}")


async def run_real_scan() -> None:
    """跑一轮真实扫描，验证信号产出。"""
    try:
        from gugu.engine.main import TradingEngine

        engine = TradingEngine()
        # 记录扫描前 signal_scan.json 状态
        scan_path = PROJECT_ROOT / "data" / "signal_scan.json"
        before = None
        if scan_path.exists():
            try:
                before = json.loads(scan_path.read_text(encoding="utf-8"))
            except Exception:
                before = None

        signals = await engine._scan_signals()
        count = len(signals) if signals else 0

        # 验证 signal_scan.json 已更新
        after = None
        if scan_path.exists():
            try:
                after = json.loads(scan_path.read_text(encoding="utf-8"))
            except Exception:
                after = None

        updated = False
        if after and before:
            updated = after.get("last_scan_at") != before.get("last_scan_at")
        elif after and not before:
            updated = True

        # 计算通过过滤的信号数
        passed = sum(1 for s in signals if s and s.get("confidence", 0) > 0) if signals else 0

        detail = f"扫描完成: {count} 个候选信号, {passed} 个通过置信度; signal_scan.json {'已更新' if updated else '未更新'}"
        report("真实扫描", updated, detail)
    except Exception as e:
        report("真实扫描", False, f"扫描异常: {e}")


async def verify_notification_channels(send_test: bool) -> None:
    """验证飞书双通道配置 + 可选发送测试卡片。"""
    from gugu.notifier.feishu import FeishuNotifier

    notifier = FeishuNotifier()
    sys_chat = notifier._chat_id
    biz_chat = notifier._business_chat_id

    if not sys_chat or not notifier._app_id or not notifier._app_secret:
        report("飞书系统通道", False, "系统对话凭证缺失（FEISHU_APP_ID/SECRET/CHAT_ID）")
    else:
        report("飞书系统通道", True, f"chat_id={sys_chat[:8]}...（Hermes 对话）")

    if not biz_chat:
        report("飞书业务通道", False, "业务群 chat_id 缺失（FEISHU_BUSINESS_CHAT_ID）")
    else:
        report("飞书业务通道", True, f"chat_id={biz_chat[:8]}...（开悟之路群）")

    if not send_test:
        report("测试卡片", True, "跳过（--notify 未开启）")
        return

    # 发送测试卡片验证真实链路（默认跳过，显式 --notify 才发）
    try:
        # 系统通道测试卡片
        sys_card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "🧪 gugu E2E 验证"},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"系统通道连通性测试\n时间: {datetime.now().isoformat(timespec='seconds')}",
                        },
                    },
                ],
            },
        }
        ok_sys = await notifier.send_system_card(sys_card)

        # 业务通道测试卡片
        biz_card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "🧪 gugu E2E 验证"},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"业务通道连通性测试\n时间: {datetime.now().isoformat(timespec='seconds')}",
                        },
                    },
                ],
            },
        }
        ok_biz = await notifier.send_business_card(biz_card)
        report("测试卡片", ok_sys and ok_biz, f"系统={ok_sys} 业务={ok_biz}")
    except Exception as e:
        report("测试卡片", False, f"发送异常: {e}")


async def check_disk_and_sources() -> None:
    """磁盘 + 数据源健康快照（复用 watchdog 检查）。"""
    from gugu.health.watchdog import check_data_source_health, check_disk_health

    disk = check_disk_health(data_dir=PROJECT_ROOT / "data")
    report("磁盘空间", disk.ok, disk.message.replace("⚠️ ", ""))

    src = check_data_source_health(data_dir=PROJECT_ROOT / "data")
    report("数据源健康", src.ok, src.message.replace("⚠️ ", ""))


async def main() -> int:
    parser = argparse.ArgumentParser(description="gugu E2E 验证")
    parser.add_argument("--no-scan", action="store_true", help="跳过真实扫描")
    parser.add_argument("--no-notify", action="store_true", help="跳过测试卡片发送")
    parser.add_argument("--check", action="store_true", help="只读检查（不扫描、不发送）")
    args = parser.parse_args()

    print("=" * 56)
    print("gugu E2E 验证")
    print("=" * 56)

    await check_single_instance()
    await check_heartbeat()
    await check_disk_and_sources()

    if not args.no_scan and not args.check:
        await run_real_scan()
    else:
        report("真实扫描", True, "跳过（--no-scan/--check）")

    await verify_notification_channels(send_test=not args.no_notify and not args.check)

    # 汇总
    print("\n" + "=" * 56)
    failed = [r for r in RESULTS if not r["ok"]]
    if failed:
        print(f"❌ {len(failed)} 项失败:")
        for r in failed:
            print(f"   - {r['name']}: {r['detail']}")
        return 1
    print(f"✅ 全部 {len(RESULTS)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
