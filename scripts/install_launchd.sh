#!/bin/bash
# scripts/install_launchd.sh
# 安装 gugu 交易系统为 macOS launchd 服务
# 用法: bash scripts/install_launchd.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_SRC="$PROJECT_DIR/scripts/com.gugu.trading.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.gugu.trading.plist"

echo "=== gugu launchd 安装脚本 ==="
echo "项目目录: $PROJECT_DIR"

# 检查 plist 文件
if [ ! -f "$PLIST_SRC" ]; then
    echo "❌ 找不到 $PLIST_SRC"
    exit 1
fi

# 检查 Python 环境
PYTHON_PATH="$PROJECT_DIR/.venv/bin/python"
if [ ! -f "$PYTHON_PATH" ]; then
    echo "❌ 找不到 Python: $PYTHON_PATH"
    echo "请先运行: uv sync"
    exit 1
fi

# 更新 plist 中的 Python 路径
echo "📝 更新 plist 配置..."
sed -e "s|/Users/dong/workspace/gugu/.venv/bin/python|$PYTHON_PATH|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# 确保日志目录存在
mkdir -p "$PROJECT_DIR/logs"

# 卸载旧服务（如果存在）
if launchctl list | grep -q "com.gugu.trading"; then
    echo "🔄 卸载旧服务..."
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

# 加载新服务
echo "🚀 加载服务..."
launchctl load "$PLIST_DST"

# 验证
if launchctl list | grep -q "com.gugu.trading"; then
    echo "✅ 服务安装成功！"
    echo ""
    echo "常用命令:"
    echo "  查看状态: launchctl list | grep gugu"
    echo "  停止服务: launchctl unload $PLIST_DST"
    echo "  启动服务: launchctl load $PLIST_DST"
    echo "  查看日志: tail -f $PROJECT_DIR/logs/launchd_out.log"
else
    echo "❌ 服务加载失败，请检查日志"
    exit 1
fi
