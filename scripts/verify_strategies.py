#!/usr/bin/env python3
"""策略验证脚本 — 统一验证所有策略的可用性和一致性。

用法：
    # 列出所有策略
    python scripts/verify_strategies.py list

    # 验证策略代码与 manifest 一致性
    python scripts/verify_strategies.py check

    # 推送策略看板到飞书
    python scripts/verify_strategies.py dashboard

    # 按出处查询
    python scripts/verify_strategies.py source "炒股的智慧"

    # 查看策略参数
    python scripts/verify_strategies.py params trailing_stop
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path

# 确保 src 在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def cmd_list(manifest, args):
    """列出所有策略。"""
    s = manifest.stats()
    print(f"\n{'='*60}")
    print(f"策略注册中心 · 全景概览")
    print(f"{'='*60}")
    print(f"总计 {s['total']} 个策略 | 启用 {s['enabled']} | 停用 {s['disabled']}")
    print(f"{'='*60}\n")

    category_names = {
        "signal_strategies": "信号生成策略",
        "risk_strategies": "风控择时策略",
        "take_profit_strategies": "止盈止损策略",
        "skill_strategies": "Skill 策略",
        "dynamic_strategies": "动态策略生成",
    }

    for cat_key, cat_name in category_names.items():
        strategies = manifest.list_by_category(cat_key)
        if not strategies:
            continue
        enabled_count = sum(
            1 for info in strategies.values()
            if isinstance(info, dict) and info.get("enabled", False)
        )
        print(f"【{cat_name}】（{enabled_count}/{len(strategies)}）")
        for key, info in strategies.items():
            if not isinstance(info, dict):
                continue
            name = info.get("name", key)
            source = info.get("source", "")
            summary = info.get("summary", "")
            is_enabled = info.get("enabled", False)
            validation = info.get("validation", "")
            status = "🟢" if is_enabled else "⚪"
            print(f"  {status} {name} ({key})")
            print(f"     出处：{source}")
            print(f"     逻辑：{summary}")
            print(f"     验证：{validation}")
        print()


def cmd_check(manifest, args):
    """验证策略代码与 manifest 一致性。"""
    print(f"\n{'='*60}")
    print("策略一致性检查")
    print(f"{'='*60}\n")

    errors: list[str] = []
    warnings: list[str] = []
    ok_count = 0

    for category, strategies in manifest.list_all().items():
        if not isinstance(strategies, dict):
            continue
        for key, info in strategies.items():
            if not isinstance(info, dict):
                continue
            module_path = info.get("module")
            class_name = info.get("class")
            if not module_path or not class_name:
                # Skill 策略没有 module/class，跳过
                if category == "skill_strategies":
                    continue
                warnings.append(f"{key}: 缺少 module/class 字段")
                continue

            # 验证模块可导入
            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name, None)
                if cls is None:
                    errors.append(f"{key}: 模块 {module_path} 中找不到类 {class_name}")
                else:
                    ok_count += 1
                    print(f"  🟢 {key} → {module_path}.{class_name} OK")
            except ImportError as e:
                errors.append(f"{key}: 模块导入失败 {module_path}: {e}")
            except Exception as e:
                errors.append(f"{key}: 检查异常: {e}")

    print(f"\n{'='*60}")
    print(f"检查完成: {ok_count} OK, {len(warnings)} 警告, {len(errors)} 错误")
    if warnings:
        print(f"\n⚠️ 警告:")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print(f"\n❌ 错误:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("✅ 所有策略代码一致性检查通过")


def cmd_dashboard(manifest, args):
    """推送策略看板到飞书。"""
    from gugu.notifier.feishu import FeishuNotifier

    async def send():
        notifier = FeishuNotifier()
        ok = await notifier.notify_strategy_dashboard()
        print(f"飞书策略看板发送: {'✅ 成功' if ok else '❌ 失败'}")

    asyncio.run(send())


def cmd_source(manifest, args):
    """按出处查询策略。"""
    results = manifest.list_by_source(args.keyword)
    if not results:
        print(f"未找到出处包含 '{args.keyword}' 的策略")
        return
    print(f"\n出处包含 '{args.keyword}' 的策略（{len(results)} 个）:\n")
    for r in results:
        print(f"  {r['name']} ({r['key']})")
        print(f"    出处：{r['source']}")
        print(f"    逻辑：{r['summary']}")
        print(f"    类别：{r['category']}")
        print()


def cmd_params(manifest, args):
    """查看策略参数。"""
    params = manifest.get_params(args.strategy_key)
    info = manifest.get_strategy(args.strategy_key)
    if not info:
        print(f"未找到策略: {args.strategy_key}")
        return
    print(f"\n策略：{info.get('name', args.strategy_key)}")
    print(f"出处：{info.get('source', '')}")
    print(f"逻辑：{info.get('summary', '')}")
    if params:
        print(f"\n参数：")
        for k, v in params.items():
            print(f"  {k}: {v}")
    else:
        print(f"\n参数入口：{info.get('params_entry', '无')}")
        print("（该策略参数未集中到 manifest，请查看类属性）")


def main():
    parser = argparse.ArgumentParser(description="策略验证工具")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出所有策略")
    sub.add_parser("check", help="验证策略代码一致性")
    sub.add_parser("dashboard", help="推送策略看板到飞书")
    sub.add_parser("enabled", help="列出已启用策略")

    sp = sub.add_parser("source", help="按出处查询")
    sp.add_argument("keyword", help="出处关键词")

    sp = sub.add_parser("params", help="查看策略参数")
    sp.add_argument("strategy_key", help="策略 key")

    args = parser.parse_args()

    from gugu.strategies.manifest import get_manifest

    manifest = get_manifest()

    if args.command == "list":
        cmd_list(manifest, args)
    elif args.command == "check":
        cmd_check(manifest, args)
    elif args.command == "dashboard":
        cmd_dashboard(manifest, args)
    elif args.command == "enabled":
        for s in manifest.list_enabled():
            print(f"  🟢 {s['name']} ({s['key']}) — {s['summary']}")
    elif args.command == "source":
        cmd_source(manifest, args)
    elif args.command == "params":
        cmd_params(manifest, args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
