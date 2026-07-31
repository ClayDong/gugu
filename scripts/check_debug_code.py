#!/usr/bin/env python3
"""检查代码中的调试残留：breakpoint、pdb、ipdb、print(调试)等"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEBUG_PATTERNS = [
    "breakpoint(",
    "pdb.set_trace(",
    "ipdb.set_trace(",
    "import pdb",
    "import ipdb",
    "import pudb",
    "print('DEBUG",
    "print('TODO",
    "print('FIXME",
    'print("DEBUG',
    'print("TODO',
    'print("FIXME',
    "# DEBUG:",
    "# TODO: remove",
    "# FIXME: remove",
    "console.log(",
    "debugger;",
]

# 排除的文件
EXCLUDE_DIRS = {
    "tests/",
    "scripts/",
    "_refs/",
    "quality_gate.py",
    "check_debug_code.py",
}


def check_debug_code(file_path: Path) -> list[str]:
    """检查调试代码"""
    issues = []
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return issues

    for pattern in EXCLUDE_DIRS:
        if pattern in str(file_path.relative_to(PROJECT_ROOT)):
            return issues

    for i, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        # 跳过注释行
        if stripped.startswith("#"):
            continue
        # 跳过空行
        if not stripped:
            continue
        for pattern in DEBUG_PATTERNS:
            if pattern in line:
                issues.append(f"  {file_path.relative_to(PROJECT_ROOT)}:{i}: {stripped[:70]}")
                break  # 每行只报告一次

    return issues


def main():
    src_dir = PROJECT_ROOT / "src" / "gugu"
    all_issues = []

    print("检查调试代码...")

    for py_file in src_dir.rglob("*.py"):
        if py_file.is_file():
            all_issues.extend(check_debug_code(py_file))

    if all_issues:
        print(f"\n发现 {len(all_issues)} 处调试代码残留:\n")
        for issue in all_issues:
            print(issue)
        sys.exit(1)
    else:
        print("✅ 未发现调试代码残留")
        sys.exit(0)


if __name__ == "__main__":
    main()
