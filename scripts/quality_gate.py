# gugu 项目代码质量门禁脚本
# 在 CI 和 pre-commit 中运行，自动检测常见反模式

import argparse
import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src" / "gugu"
TESTS_DIR = PROJECT_ROOT / "tests"

# === 默认配置 ===
DEFAULT_MAX_FILE_LINES = 500
DEFAULT_MAX_FUNC_LINES = 50
DEFAULT_MAX_CLASS_LINES = 300


def check_file_size(file_path: Path, max_lines: int = DEFAULT_MAX_FILE_LINES) -> list[str]:
    """检查文件大小"""
    issues = []
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return issues
    if len(lines) > max_lines:
        issues.append(
            f"  {file_path.relative_to(PROJECT_ROOT)}: {len(lines)} 行, " f"超过 {max_lines} 行限制"
        )
    return issues


def check_function_size(file_path: Path, max_lines: int = DEFAULT_MAX_FUNC_LINES) -> list[str]:
    """检查函数大小"""
    issues = []
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_lines = (node.end_lineno or node.lineno) - node.lineno
            if func_lines > max_lines:
                issues.append(
                    f"  {file_path.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                    f"{node.name} ({func_lines} 行, 超过 {max_lines} 行限制)"
                )
    return issues


def check_test_quality(test_file: Path) -> list[str]:
    """检测测试质量问题"""
    issues = []
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        # 检测 assert True
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                issues.append(
                    f"  {test_file.relative_to(PROJECT_ROOT)}:{node.lineno}: assert True (无意义断言)"
                )

        # 检测空测试函数
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            body = [n for n in node.body if not isinstance(n, ast.Expr)]
            if not body or all(isinstance(n, ast.Pass) for n in body):
                issues.append(
                    f"  {test_file.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                    f"{node.name} (空测试函数)"
                )

            # 检测没有 assert 的测试函数
            has_assert = any(isinstance(n, ast.Assert) for n in body if body)
            if not has_assert and body:
                issues.append(
                    f"  {test_file.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                    f"{node.name} (无断言的测试函数)"
                )

            # 检测 asyncio.sleep(N) 长睡眠（>10秒）
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    if hasattr(sub.func, "attr") and sub.func.attr == "sleep":
                        if sub.args and isinstance(sub.args[0], ast.Constant):
                            sleep_val = sub.args[0].value
                            if isinstance(sleep_val, (int, float)) and sleep_val > 10:
                                issues.append(
                                    f"  {test_file.relative_to(PROJECT_ROOT)}:{sub.lineno}: "
                                    f"asyncio.sleep({sleep_val}) 长睡眠, 可能导致测试超时"
                                )

    return issues


def check_production_code(code_file: Path) -> list[str]:
    """检测生产代码反模式"""
    issues = []
    try:
        content = code_file.read_text(encoding="utf-8")
        tree = ast.parse(content)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        # 检测可变默认参数
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + node.args.kw_defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    issues.append(
                        f"  {code_file.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                        f"{node.name} (可变默认参数)"
                    )

        # 检测裸 except: pass
        if isinstance(node, ast.ExceptHandler):
            if node.body and isinstance(node.body[0], ast.Pass) and len(node.body) == 1:
                issues.append(
                    f"  {code_file.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                    f"bare except: pass (静默吞错误)"
                )

        # 检测硬编码密钥/密码
        if isinstance(node, ast.Call):
            if hasattr(node.func, "id") and node.func.id == "os.environ":
                if node.args and isinstance(node.args[0], ast.Constant):
                    key = node.args[0].value
                    if isinstance(key, str):
                        key_upper = key.upper()
                        if any(s in key_upper for s in ["KEY", "SECRET", "PASSWORD", "TOKEN"]):
                            issues.append(
                                f"  {code_file.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                                f"硬编码密钥 {key}"
                            )

    return issues


def check_1e8_double_conversion(code_file: Path) -> list[str]:
    """检测可能的 1e8 双重转换"""
    issues = []
    # 跳过 collector 模块（它们合法地转换）
    if "collector" in str(code_file.relative_to(PROJECT_ROOT)):
        return issues

    try:
        content = code_file.read_text(encoding="utf-8")
        tree = ast.parse(content)
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # 检测 / 1e8 或 / 100_000_000
            if isinstance(node.right, ast.Constant):
                val = node.right.value
                if val == 1e8 or val == 100_000_000:
                    issues.append(
                        f"  {code_file.relative_to(PROJECT_ROOT)}:{node.lineno}: "
                        f"除以 {val:.0e} (collector 外的双重转换风险)"
                    )

    return issues


def main():
    parser = argparse.ArgumentParser(description="gugu 代码质量门禁检查")
    parser.add_argument(
        "--mode",
        choices=["strict", "warning", "test-only", "code-only"],
        default="warning",
        help="strict: 发现问题返回非零退出码; warning: 仅报告; test-only/code-only: 仅检查测试/代码",
    )
    parser.add_argument("--max-file-lines", type=int, default=DEFAULT_MAX_FILE_LINES)
    parser.add_argument("--max-func-lines", type=int, default=DEFAULT_MAX_FUNC_LINES)
    args = parser.parse_args()

    all_issues = []

    print("=" * 60)
    print("gugu 代码质量门禁检查")
    print(f"模式: {args.mode}")
    print("=" * 60)

    # 检查生产代码
    if SRC_DIR.exists() and args.mode not in ("test-only"):
        for py_file in SRC_DIR.rglob("*.py"):
            if py_file.is_file() and py_file.name != "__init__.py":
                all_issues.extend(check_file_size(py_file, args.max_file_lines))
                all_issues.extend(check_function_size(py_file, args.max_func_lines))
                all_issues.extend(check_production_code(py_file))
                all_issues.extend(check_1e8_double_conversion(py_file))

    # 检查测试代码
    if TESTS_DIR.exists() and args.mode not in ("code-only"):
        for py_file in TESTS_DIR.rglob("test_*.py"):
            if py_file.is_file():
                all_issues.extend(check_test_quality(py_file))

    # 输出结果
    if all_issues:
        print(f"\n发现 {len(all_issues)} 个问题:\n")
        for issue in all_issues:
            print(issue)
        print(f"\n总计: {len(all_issues)} 个问题")
        if args.mode == "strict":
            sys.exit(1)
        else:
            sys.exit(0)
    else:
        print("\n✅ 未发现问题，代码质量合格")
        sys.exit(0)


if __name__ == "__main__":
    main()
