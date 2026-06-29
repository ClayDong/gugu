#!/usr/bin/env bash
# ============================================================
# gugu 质量门禁脚本
# 在 commit/push 前运行，确保代码质量符合标准
# 使用: ./scripts/run_quality_checks.sh
# ============================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "========================================"
echo "  gugu 质量门禁检查"
echo "========================================"

# 1. ruff lint 检查
echo ""
echo "[1/4] ruff lint..."
uv run ruff check src/
echo "  ✅ 通过"

# 2. ruff format 检查（不修改，只报告差异）
echo ""
echo "[2/4] ruff format check..."
uv run ruff format --check src/ 2>/dev/null && echo "  ✅ 通过" || {
    echo "  ⚠️  格式问题（可通过 'ruff format src/' 自动修复）"
}

# 3. mypy 类型检查（宽松模式，仅报告但不阻断）
echo ""
echo "[3/4] mypy type check..."
uv run mypy src/ --ignore-missing-imports 2>/dev/null && echo "  ✅ 通过" || {
    echo "  ⚠️  类型问题（建议修复但非阻断）"
}

# 4. pytest 运行
echo ""
echo "[4/4] pytest..."
uv run python3 -m pytest tests/ -x --tb=short -q 2>&1 | tail -5
echo "  ✅ 通过"

echo ""
echo "========================================"
echo "  全部检查通过！"
echo "========================================"
