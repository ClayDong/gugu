"""check_drift.py 的单元测试：正反例都必须可判定。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_drift.py"
_spec = importlib.util.spec_from_file_location("check_drift_under_test", _SCRIPT)
assert _spec is not None and _spec.loader is not None
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)


def _make_repo(
    tmp_path: Path,
    *,
    init_version: str = "0.9.59",
    declared_strategies: int = 16,
    enabled_status: str | None = None,
    verified: list[str] | None = None,
    mode: str = "paper",
    test_source: str = "",
    with_forward_evidence: bool = False,
    requires_python: str = ">=3.12",
    ci_python: str | None = None,
    dot_python: str | None = None,
) -> Path:
    (tmp_path / "src" / "gugu").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "gugu"\nversion = "0.9.59"\nrequires-python = "{requires_python}"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/gugu/__init__.py").write_text(
        f'__version__ = "{init_version}"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# 变更日志\n\n## 2026-11-04 — v0.9.59 示例\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        f"# gugu\n\n**多策略信号融合** — {declared_strategies} 个注册策略\n", encoding="utf-8"
    )
    settings = {
        "execution": {"mode": mode},
        "strategy": {"verified_strategies": verified or []},
    }
    (tmp_path / "config/settings.yaml").write_text(
        yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8"
    )
    strategies = {f"s{i}": {"validation_gate": {"status": "pending"}} for i in range(16)}
    if enabled_status:
        # 覆盖而非新增，保持策略总数与 README 声明一致
        strategies["s0"]["validation_gate"]["status"] = enabled_status
    (tmp_path / "config/strategy_manifest.yaml").write_text(
        yaml.safe_dump({"signal_strategies": strategies}, allow_unicode=True),
        encoding="utf-8",
    )
    if test_source:
        (tmp_path / "tests/test_sample.py").write_text(test_source, encoding="utf-8")
    if with_forward_evidence:
        # 30 条已实现盈亏、覆盖 10 个交易日，全部写入真实 JSONL 账本
        data_dir = tmp_path / "data"
        if not data_dir.exists():
            data_dir.mkdir()
        rows = [
            json.dumps(
                {
                    "date": f"2026-11-{1 + i % 10:02d}",
                    "symbol": f"sh60000{i % 5}",
                    "realized_pnl": 1.0 if i % 2 else -0.5,
                }
            )
            for i in range(30)
        ]
        (tmp_path / "data/paper_execution_ledger.jsonl").write_text(
            "\n".join(rows), encoding="utf-8"
        )
    if ci_python:
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        for name in ("ci.yml", "quality-gate.yml"):
            (wf_dir / name).write_text(
                f'on:\n  push:\n    branches: [main, "fix/**"]\n'
                f"jobs:\n  t:\n    steps:\n"
                f"      - run: uv run python scripts/check_drift.py\n"
                f"      - uses: actions/setup-python@v5\n"
                f'        with:\n          python-version: "{ci_python}"\n',
                encoding="utf-8",
            )
    if dot_python:
        (tmp_path / ".python-version").write_text(dot_python + "\n", encoding="utf-8")
    return tmp_path


def test_clean_repo_passes(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    assert drift.run_checks(root, tracked={"scripts/check_drift.py"}) == []


def test_version_mismatch_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, init_version="0.6.0")
    errors = drift.run_checks(root, tracked=set())
    assert any("__init__.py 版本" in e for e in errors)


def test_changelog_version_mismatch_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    (root / "CHANGELOG.md").write_text(
        "# 变更日志\n\n## 2026-11-04 — v0.6.0 旧版\n", encoding="utf-8"
    )
    errors = drift.run_checks(root, tracked=set())
    assert any("CHANGELOG.md 版本" in e for e in errors)


def test_readme_strategy_count_mismatch_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, declared_strategies=36)
    errors = drift.run_checks(root, tracked=set())
    assert any("README 声明 36" in e for e in errors)


def test_invalid_execution_mode_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, mode="magic")
    errors = drift.run_checks(root, tracked=set())
    assert any("execution.mode" in e for e in errors)


def test_enabled_strategy_outside_verified_whitelist_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path, enabled_status="enabled")
    errors = drift.run_checks(root, tracked=set())
    assert any("未进入生产白名单" in e for e in errors)


def test_enabled_strategy_inside_whitelist_passes(tmp_path: Path) -> None:
    """enabled + 白名单 + 真实达标账本 → 全流程放行。"""
    root = _make_repo(
        tmp_path,
        enabled_status="enabled",
        verified=["s0"],
        with_forward_evidence=True,
    )
    assert drift.run_checks(root, tracked=set()) == []


def test_enabled_strategy_without_forward_evidence_is_rejected(tmp_path: Path) -> None:
    """P0-5：即便在白名单内，没有真实前向已实现盈亏也不得启用。"""
    root = _make_repo(tmp_path, enabled_status="enabled", verified=["s0"])
    errors = drift.run_checks(root, tracked=set())
    assert any("前向账本未达标" in e for e in errors)


def test_whitelist_only_also_requires_forward_evidence(tmp_path: Path) -> None:
    """只写白名单、不改 manifest status，同样受前向证据门槛约束。"""
    root = _make_repo(tmp_path, verified=["s0"])
    errors = drift.run_checks(root, tracked=set())
    assert any("前向账本未达标" in e for e in errors)


def test_research_phase_without_enabled_strategy_passes(tmp_path: Path) -> None:
    """零 enabled / 零白名单时，不因研究期缺样本把门禁钉死。"""
    root = _make_repo(tmp_path)
    assert not any("前向账本" in e for e in drift.run_checks(root, tracked=set()))


def test_python_version_floor_must_match_ci(tmp_path: Path) -> None:
    """pyproject 下界与 CI python-version 不一致必须报错。"""
    root = _make_repo(tmp_path, requires_python=">=3.11", ci_python="3.12")
    errors = drift.check_python_version(root)
    assert any("CI python-version" in e and "3.11" in e for e in errors)


def test_python_version_consistent_passes(tmp_path: Path) -> None:
    """pyproject / CI / .python-version 三处一致 → 通过。"""
    root = _make_repo(tmp_path, requires_python=">=3.12", ci_python="3.12", dot_python="3.12")
    assert drift.check_python_version(root) == []


def test_python_version_dotfile_mismatch_detected(tmp_path: Path) -> None:
    """.python-version 与 pyproject 下界不一致必须报错。"""
    root = _make_repo(tmp_path, requires_python=">=3.12", dot_python="3.14")
    errors = drift.check_python_version(root)
    assert any(".python-version" in e for e in errors)


def test_python_version_ci_major_minor_normalization(tmp_path: Path) -> None:
    """CI 声明带 patch 版本（如 3.12.4）应按 major.minor 归一化后一致。"""
    root = _make_repo(tmp_path, requires_python=">=3.12", ci_python="3.12.4")
    assert drift.check_python_version(root) == []


def test_untracked_script_module_is_detected(tmp_path: Path) -> None:
    root = _make_repo(
        tmp_path,
        test_source="from scripts.some_helper import thing\n\ndef test_x():\n    assert thing\n",
    )
    errors = drift.run_checks(root, tracked=set())
    assert any("未被 git 跟踪" in e and "scripts/some_helper.py" in e for e in errors)


def test_tracked_script_module_passes(tmp_path: Path) -> None:
    root = _make_repo(
        tmp_path,
        test_source="from scripts.some_helper import thing\n\ndef test_x():\n    assert thing\n",
    )
    assert drift.run_checks(root, tracked={"scripts/some_helper.py"}) == []


def test_string_literal_is_not_treated_as_import() -> None:
    """门禁自身不能被测试夹具里的示例字符串误报（AST 而非正则）。"""
    source = (
        'SAMPLE = "from scripts.ghost_module import thing"\n\ndef test_x():\n    assert SAMPLE\n'
    )
    assert drift.script_modules_in_source(source) == set()


def test_real_import_is_extracted() -> None:
    source = "from scripts.helper import fn\nimport scripts.other\n"
    assert drift.script_modules_in_source(source) == {
        "scripts/helper.py",
        "scripts/other.py",
    }


def _write_workflows(
    root: Path,
    *,
    ci_version: str = "3.12",
    qg_version: str = "3.12",
    ci_drift: bool = True,
    qg_drift: bool = True,
    fix_branch: bool = True,
    bandit_block: bool = True,
) -> None:
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    branches = "[main, master, develop, 'fix/**']" if fix_branch else "[main, master]"
    drift_step = (
        "      - name: Repo drift gate\n        run: uv run python scripts/check_drift.py\n"
    )
    block_step = "      - name: Block on high severity findings\n        run: exit 0\n"
    (wf / "ci.yml").write_text(
        "name: CI\n"
        "on:\n"
        "  push:\n"
        f"    branches: {branches}\n"
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        f'          python-version: "{ci_version}"\n'
        f"{drift_step if ci_drift else ''}",
        encoding="utf-8",
    )
    (wf / "quality-gate.yml").write_text(
        "name: Quality Gate\n"
        "on:\n"
        "  push:\n"
        f"    branches: {branches}\n"
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        f'          python-version: "{qg_version}"\n'
        f"{drift_step if qg_drift else ''}"
        f"{block_step if bandit_block else ''}",
        encoding="utf-8",
    )


def test_workflow_checks_pass(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _write_workflows(root)
    assert drift.check_workflows(root) == []


def test_workflow_dir_absent_is_skipped(tmp_path: Path) -> None:
    assert drift.check_workflows(_make_repo(tmp_path)) == []


def test_workflow_python_version_mismatch_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _write_workflows(root, ci_version="3.11")
    assert any("Python 版本不一致" in e for e in drift.check_workflows(root))


def test_workflow_missing_drift_gate_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _write_workflows(root, qg_drift=False)
    assert any("未接入 Repo drift gate" in e for e in drift.check_workflows(root))


def test_workflow_missing_fix_branch_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _write_workflows(root, fix_branch=False)
    assert any("fix/**" in e for e in drift.check_workflows(root))


def test_workflow_missing_bandit_block_is_detected(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    _write_workflows(root, bandit_block=False)
    assert any("bandit 高危阻断" in e for e in drift.check_workflows(root))


def test_real_repo_passes_all_checks() -> None:
    root = Path(__file__).resolve().parents[2]
    assert drift.run_checks(root, tracked=drift.git_tracked(root)) == []
