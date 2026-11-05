#!/usr/bin/env python3
"""仓库漂移与完整性门禁（CI 可调）。

检查项（全部为可机器判定的事实，不依赖任何文档自述）：
1. 版本一致性：pyproject.toml / src/gugu/__init__.py / CHANGELOG.md 首个版本号
2. 治理约束：execution.mode 合法；manifest 中 status=enabled 的策略必须
   在 settings.verified_strategies 白名单内
3. README 声明的策略数量与 config/strategy_manifest.yaml 实测一致
4. 仓库完整性：tests/ 中 import 的 scripts.* 模块必须已被 git 跟踪
   （防止「本地能跑、干净 clone 必挂」，2026-11-04 评审 P0-1）
5. 前向证据门槛：一旦有策略被标为 enabled 或写入生产白名单，
   必须存在达标（30+ 已实现盈亏 / 10+ 交易日）的真实账本
   （2026-11-04 评审 P0-5；零策略时不因研究期缺样本把 CI 钉死）
6. Python 版本基准一致：pyproject requires-python 下界 == CI python-version
   == .python-version（2026-11-05 P1-C：本地 3.14 绿、CI 3.12 红这类漂移可拦截）

用法：python scripts/check_drift.py [--root PATH]
退出码：0 = 全部通过；1 = 存在漂移。
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ALLOWED_EXECUTION_MODES = {"signal_only", "track_live", "paper", "live"}
_ALLOWED_SCRIPT_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")


def script_modules_in_source(text: str) -> set[str]:
    """用 AST 提取真实的 scripts.* import，避免把字符串常量误判为依赖。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "scripts":
                found.update(f"scripts/{alias.name}.py" for alias in node.names)
            elif node.module and node.module.startswith("scripts."):
                head = node.module.split(".")[1]
                if _ALLOWED_SCRIPT_MODULE_RE.match(head):
                    found.add(f"scripts/{head}.py")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("scripts."):
                    head = alias.name.split(".")[1]
                    if _ALLOWED_SCRIPT_MODULE_RE.match(head):
                        found.add(f"scripts/{head}.py")
    return found


def check_versions(root: Path) -> list[str]:
    """三处版本号必须一致。"""
    errors: list[str] = []
    try:
        pyproject_version = tomllib.loads((root / "pyproject.toml").read_text())["project"][
            "version"
        ]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        return [f"无法读取 pyproject.toml 版本: {exc}"]

    init_text = (root / "src/gugu/__init__.py").read_text(encoding="utf-8")
    init_match = re.search(r'__version__\s*=\s*["\']([^"\']+)', init_text)
    if not init_match:
        errors.append("src/gugu/__init__.py 缺少 __version__")
    elif init_match.group(1) != pyproject_version:
        errors.append(f"__init__.py 版本 {init_match.group(1)} != pyproject {pyproject_version}")

    changelog_head = (root / "CHANGELOG.md").read_text(encoding="utf-8")[:500]
    changelog_match = _VERSION_RE.search(changelog_head)
    if not changelog_match:
        errors.append("CHANGELOG.md 开头未找到版本号")
    elif changelog_match.group(1) != pyproject_version:
        errors.append(
            f"CHANGELOG.md 版本 {changelog_match.group(1)} != pyproject {pyproject_version}"
        )
    return errors


def check_governance(root: Path) -> list[str]:
    """执行模式与生产白名单约束。"""
    errors: list[str] = []
    settings = yaml.safe_load((root / "config/settings.yaml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load((root / "config/strategy_manifest.yaml").read_text(encoding="utf-8"))

    mode = (settings.get("execution") or {}).get("mode")
    if mode not in ALLOWED_EXECUTION_MODES:
        errors.append(f"execution.mode={mode!r} 不在允许集合 {sorted(ALLOWED_EXECUTION_MODES)}")

    verified = set((settings.get("strategy") or {}).get("verified_strategies") or [])
    enabled = {
        name
        for name, spec in (manifest.get("signal_strategies") or {}).items()
        if (spec.get("validation_gate") or {}).get("status") == "enabled"
    }
    if not enabled.issubset(verified):
        errors.append(f"manifest 中 enabled 策略未进入生产白名单: {sorted(enabled - verified)}")
    return errors


def check_readme_strategy_count(root: Path) -> list[str]:
    """README 里"N 个注册策略"必须与 manifest 实测一致。"""
    readme = (root / "README.md").read_text(encoding="utf-8")
    match = re.search(r"(\d+)\s*个注册策略", readme)
    if not match:
        return []
    manifest = yaml.safe_load((root / "config/strategy_manifest.yaml").read_text(encoding="utf-8"))
    actual = len(manifest.get("signal_strategies") or {})
    declared = int(match.group(1))
    if declared != actual:
        return [f"README 声明 {declared} 个注册策略，manifest 实测 {actual} 个"]
    return []


def check_repo_modules(root: Path, tracked: set[str]) -> list[str]:
    """tests/ 引用的 scripts.* 模块必须已被 git 跟踪。"""
    missing: set[str] = set()
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    for path in tests_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for rel in script_modules_in_source(text):
            if rel not in tracked:
                missing.add(rel)
    return (
        [f"测试引用了未被 git 跟踪的模块（干净 clone 会失败）: {sorted(missing)}"]
        if missing
        else []
    )


def git_tracked(root: Path) -> set[str]:
    """返回 git ls-files 结果；非 git 仓库时返回空集。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return set(out.stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        return set()


def check_forward_evidence_gate(root: Path) -> list[str]:
    """有生产策略被启用时，前向已实现盈亏账本必须真实达标。

    只检查真实账本，绝不生成或填充模拟样本。零 enabled / 零白名单时放行，
    避免把「研究期本来就没有前向样本」变成永久红灯；但任何人想打开生产
    准入，就必须先积累真实成交证据。
    """
    try:
        from scripts.check_forward_evidence import evaluate_ledger
    except ModuleNotFoundError:  # 直接以 `python scripts/check_drift.py` 运行
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from check_forward_evidence import evaluate_ledger

    settings = yaml.safe_load((root / "config/settings.yaml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load((root / "config/strategy_manifest.yaml").read_text(encoding="utf-8"))
    verified = set((settings.get("strategy") or {}).get("verified_strategies") or [])
    enabled = {
        name
        for name, spec in (manifest.get("signal_strategies") or {}).items()
        if (spec.get("validation_gate") or {}).get("status") == "enabled"
    }
    if not verified and not enabled:
        return []
    report = evaluate_ledger(root / "data" / "paper_execution_ledger.jsonl")
    if report["passed"]:
        return []
    reasons = "; ".join(report["reasons"])
    return [
        f"存在已启用/白名单策略 {sorted(verified | enabled)}，但前向账本未达标："
        f"{reasons}（禁止用回测或手写样本冒充前向验证）"
    ]


def _major_minor(version: str) -> str:
    """取版本号的 major.minor 段（"3.14.3" → "3.14"，"3.12" → "3.12"）。"""
    parts = version.strip().split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version.strip()


def _ci_python_versions(root: Path) -> set[str]:
    """收集所有 workflow 声明的 python-version（major.minor）。"""
    wf_dir = root / ".github" / "workflows"
    versions: set[str] = set()
    if not wf_dir.is_dir():
        return versions
    for path in wf_dir.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for raw in re.findall(r'python-version:\s*["\']?([\d.]+)', text):
            versions.add(_major_minor(raw))
    return versions


def check_python_version(root: Path) -> list[str]:
    """Python 版本基准三处一致：pyproject 下界 == CI == .python-version。

    目标不是强制本地装某个版本，而是把「本地与 CI 用不同 Python」从隐式
    风险变成可判定、可拦截的门禁项（2026-11-05 P1-C）。本地解释器版本
    只需满足 pyproject requires-python；真正要一致的是声明的基准版本。
    """
    errors: list[str] = []
    try:
        requires = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ].get("requires-python", "")
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        return [f"无法读取 pyproject.toml requires-python: {exc}"]
    m = re.search(r">=\s*(\d+\.\d+)", requires)
    if not m:
        errors.append(f"pyproject requires-python={requires!r} 未声明 >=x.y 下界")
        return errors
    floor = m.group(1)

    ci_versions = _ci_python_versions(root)
    if ci_versions and floor not in ci_versions:
        errors.append(
            f"pyproject requires-python 下界 {floor} 与 CI python-version "
            f"{sorted(ci_versions)} 不一致"
        )

    pv_file = root / ".python-version"
    if pv_file.exists():
        dot = _major_minor(pv_file.read_text(encoding="utf-8"))
        if dot != floor:
            errors.append(f".python-version={dot} 与 pyproject requires-python 下界 {floor} 不一致")
    return errors


def check_workflows(root: Path) -> list[str]:
    """两个 CI 管道必须同 Python 版本、同触发范围、都挂漂移门禁。

    目录不存在时跳过（允许在裁剪过的临时仓库里只测其他检查项）。
    PyYAML 1.1 会把裸键 ``on`` 解析成布尔 True，需兼容两种键名。
    """
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    errors: list[str] = []
    expected = {"ci.yml", "quality-gate.yml"}
    present = {p.name for p in wf_dir.glob("*.yml")}
    for name in sorted(expected - present):
        errors.append(f"缺少 CI 工作流文件: .github/workflows/{name}")

    all_versions: set[str] = set()
    for name in sorted(expected & present):
        text = (wf_dir / name).read_text(encoding="utf-8")
        versions = set(re.findall(r'python-version:\s*["\']?([\d.]+)', text))
        if not versions:
            errors.append(f"{name} 未声明 python-version")
        all_versions |= versions
        if "scripts/check_drift.py" not in text:
            errors.append(f"{name} 未接入 Repo drift gate")
        data = yaml.safe_load(text) or {}
        trigger = data.get("on") or data.get(True) or {}
        push = trigger.get("push") or {}
        branches = push.get("branches") or []
        if "fix/**" not in branches:
            errors.append(f"{name} 的 push 触发分支不含 fix/**，当前开发分支将不跑 CI")

    if len(all_versions) > 1:
        errors.append(f"双 CI 管道 Python 版本不一致: {sorted(all_versions)}")

    qg = wf_dir / "quality-gate.yml"
    if qg.exists() and "Block on high severity findings" not in qg.read_text(encoding="utf-8"):
        errors.append("quality-gate.yml 缺少 bandit 高危阻断步骤")
    return errors


def run_checks(root: Path, tracked: set[str] | None = None) -> list[str]:
    errors = check_versions(root)
    errors += check_governance(root)
    errors += check_readme_strategy_count(root)
    errors += check_repo_modules(root, tracked if tracked is not None else git_tracked(root))
    errors += check_workflows(root)
    errors += check_forward_evidence_gate(root)
    errors += check_python_version(root)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gugu 仓库漂移与完整性门禁")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors = run_checks(args.root)
    if errors:
        for item in errors:
            print(f"ERROR: {item}")
        print(f"漂移检查失败：{len(errors)} 项")
        return 1
    print(
        "漂移检查通过：版本一致 / 治理约束成立 / README 与 manifest 一致 / "
        "测试模块已入库 / 前向证据门槛成立 / Python 版本基准一致"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
