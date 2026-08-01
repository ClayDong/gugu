#!/usr/bin/env python3
"""静态检查 v2：按类名精确匹配策略代码参数引用 vs strategy_defaults.yaml 定义。

零风险：不导入项目模块，纯文件解析。
用法: python3 scripts/check_strategy_params.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_DIR = ROOT / "src" / "gugu" / "strategies"
DEFAULTS_FILE = ROOT / "config" / "strategy_defaults.yaml"


def parse_defaults(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([a-zA-Z0-9_]+):\s*(#.*)?$", line)
        if m and not line.startswith(" "):
            current = m.group(1)
            result[current] = set()
            continue
        if current is not None:
            m2 = re.match(r"^\s{2}([a-zA-Z0-9_]+):", line)
            if m2:
                result[current].add(m2.group(1))
    return result


def extract_class_param_refs(code: str) -> dict[str, dict[str, Any]]:
    """按类提取参数引用和策略名: {class_name: {"direct": set, "get": set, "name": str}}"""
    classes: dict[str, dict[str, Any]] = {}
    current_cls: str | None = None
    for line in code.splitlines():
        cm = re.match(r"^class\s+(\w+)\s*\([^)]*\)\s*:", line)
        if cm:
            current_cls = cm.group(1)
            classes[current_cls] = {"direct": set(), "get": set(), "name": None}
            continue
        if current_cls is None:
            continue
        # 类内策略名赋值（name = "xxx"），取第一个
        nm = re.match(r'\s+name\s*=\s*["\']([a-zA-Z0-9_]+)["\']', line)
        if nm and classes[current_cls]["name"] is None:
            classes[current_cls]["name"] = nm.group(1)
        for m in re.finditer(r'self\.params\[["\']([a-zA-Z0-9_]+)["\']\]', line):
            classes[current_cls]["direct"].add(m.group(1))
        for m in re.finditer(
            r'self\.params\.get\(\s*["\']([a-zA-Z0-9_]+)["\']', line
        ):
            classes[current_cls]["get"].add(m.group(1))
    return classes


def main() -> None:
    defaults = parse_defaults(DEFAULTS_FILE)
    issues: list[str] = []
    checked = 0
    for py in sorted(STRATEGIES_DIR.rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        code = py.read_text(encoding="utf-8")
        if "self.params" not in code:
            continue
        classes = extract_class_param_refs(code)
        if not classes:
            continue
        for cls_name, refs in classes.items():
            checked += 1
            sname = refs.get("name") or cls_name
            defined = defaults.get(sname, set())
            if sname not in defaults:
                issues.append(
                    f"⚠️ {py.name}::{cls_name} (策略 {sname}): strategy_defaults.yaml 无此配置块（若未启用可忽略）"
                )
                continue
            direct_refs: set[str] = refs["direct"]
            get_refs: set[str] = refs["get"]
            missing_direct = sorted(direct_refs - defined)
            if missing_direct:
                issues.append(
                    f"❌ {py.name}::{cls_name} (策略 {sname}): 直接索引但未配置: {missing_direct}"
                )
            missing_get = sorted(get_refs - defined)
            if missing_get:
                issues.append(
                    f"🔶 {py.name}::{cls_name} (策略 {sname}): .get 缺失(有默认值可容忍): {missing_get}"
                )

    print(f"检查 {checked} 个策略类\n")
    if not issues:
        print("✅ 所有策略参数引用均有配置")
        return
    for i in issues:
        print(i)
    errs = [i for i in issues if i.startswith("❌")]
    if errs:
        print(f"\n❌ {len(errs)} 个存在 KeyError 风险（必须修复）")
        sys.exit(1)


if __name__ == "__main__":
    main()
