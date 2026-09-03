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
        "risk_strategies": "风控/择时/止盈策略",
        "skill_strategies": "Skill 策略",
        "dynamic_strategies": "动态策略生成",
    }

    for cat_key, cat_name in category_names.items():
        strategies = manifest.list_by_category(cat_key)
        if not strategies:
            continue
        enabled_count = sum(
            1
            for info in strategies.values()
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


def cmd_param_audit(manifest, args):
    """参数闭环审计：strategy_defaults.yaml 每个参数必须被策略代码读取。

    2026-08-01 新增（P0 修复）：发现 16/27 策略存在"defaults 写了参数但代码
    不读"的断裂——GA/PSO 动态调优写回的参数永远不生效。此命令建立闭环校验，
    防止参数名漂移再犯。

    用法：
        python scripts/verify_strategies.py param-audit          # 全量审计
        python scripts/verify_strategies.py param-audit --strict  # 断裂即退出码 1
    """
    import re

    import yaml

    from gugu.config import PROJECT_ROOT

    defaults_path = PROJECT_ROOT / "config" / "strategy_defaults.yaml"
    defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}

    # 基类/引擎层通用参数（由 base.py 或引擎读取，不在策略源码中）
    BASE_COMMON = {
        "max_hold_days",
        "confirm_days",
        "stop_loss_pct",
        "take_profit_pct",
        "time_stop_threshold",
        # 2026-09-03: 移动止损由基类 _apply_max_hold_exit/_apply_trailing_stop_exit 读取，
        # 各策略源码不直接引用（同 max_hold_days 性质）
        "trailing_stop_pct",
    }
    # 每个策略独有的参数命名空间（跨策略同名但含义不同，不交叉校验）
    # 策略 key → 其源码文件（含 blogger_rules 子目录）
    strategies = manifest.list_all().get("signal_strategies", {})

    print(f"\n{'='*60}")
    print("参数闭环审计（defaults 参数名 vs 代码读取名）")
    print(f"{'='*60}\n")

    errors, warnings, ok = [], [], 0
    for key, info in strategies.items():
        if not isinstance(info, dict):
            continue
        module = info.get("module", "")
        # 定位源码文件（注意：module 可能是包名（如 ml_gradient_boost），
        # 此时优先探测包内同名主策略文件，再通配扫描包目录找 name = "策略key"
        # 的定义文件，__init__.py 仅作最后兜底）
        candidates = [
            PROJECT_ROOT / "src" / Path(*module.split(".")) / f"{module.rsplit('.', 1)[-1]}.py",
            PROJECT_ROOT / "src" / f"{module.replace('.', '/')}.py",
            PROJECT_ROOT / "src" / "gugu" / "strategies" / f"{key}.py",
            PROJECT_ROOT / "src" / "gugu" / "strategies" / "blogger_rules" / f"{key}.py",
        ]
        src_path = next((p for p in candidates if p.exists()), None)
        if src_path is None and module:
            # 通配扫描：包目录内所有 .py，找定义 name = "<key>" 的策略文件
            pkg_dir = PROJECT_ROOT / "src" / Path(*module.split("."))
            if pkg_dir.is_dir():
                for py in sorted(pkg_dir.glob("*.py")):
                    if py.name == "__init__.py":
                        continue
                    if f'name = "{key}"' in py.read_text(encoding="utf-8"):
                        src_path = py
                        break
        if src_path is None:
            # 最后兜底：包 __init__.py（策略直接定义在包入口）
            init_path = PROJECT_ROOT / "src" / Path(*module.split(".")) / "__init__.py"
            if init_path.exists():
                src_path = init_path
        if src_path is None:
            warnings.append(f"{key}: 找不到源码（module={module}），跳过参数审计")
            continue
        src = src_path.read_text(encoding="utf-8")

        # 代码读取的所有参数名（.get 和 [] 两种写法）
        read_keys = set(re.findall(r'self\.params\.get\("(\w+)"', src))
        read_keys |= set(re.findall(r'self\.params\["(\w+)"\]', src))

        defaults_params = defaults.get(key, {})
        if not defaults_params:
            continue
        params_in_defaults = [k for k in defaults_params if k not in ("default_stock_pool",)]
        # 排除 base/引擎通用参数
        unread = [k for k in params_in_defaults if k not in read_keys and k not in BASE_COMMON]
        # 代码读了但 defaults 没有的（可能只是代码默认值，不算断裂，仅提示）
        extra = [k for k in read_keys if k not in params_in_defaults and k not in BASE_COMMON]

        if unread:
            errors.append(
                f"{key}: defaults 有 {len(params_in_defaults)} 参数，代码未读 {len(unread)} 个: {unread}"
            )
            print(f"  ❌ {key}: 断裂 {len(unread)}/{len(params_in_defaults)} 个未读: {unread}")
            if extra:
                print(f"     代码读但 defaults 没有（默认值兜底）: {extra[:6]}")
        else:
            ok += 1
            print(f"  ✅ {key}: 参数闭环完整（{len(params_in_defaults)} 参数）")

    print(f"\n{'='*60}")
    print(f"审计完成: {ok} 闭环, {len(warnings)} 跳过, {len(errors)} 断裂")
    if warnings:
        print("\n⚠️ 跳过（无源码可查）:")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print("\n❌ 参数断裂（调优写回不生效，需对齐参数名）:")
        for e in errors:
            print(f"  - {e}")
        if args.strict:
            sys.exit(1)
    else:
        print("✅ 所有策略参数闭环完整，动态调优链路畅通")


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

    sp = sub.add_parser("param-audit", help="参数闭环审计（defaults↔代码）")
    sp.add_argument("--strict", action="store_true", help="有断裂时退出码 1（CI 用）")

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
    elif args.command == "param-audit":
        cmd_param_audit(manifest, args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
