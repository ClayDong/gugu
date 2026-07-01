#!/bin/bash
# gugu watchdog — 监控引擎和 Web 进程，崩溃自动重启
# 用法: nohup bash scripts/watchdog.sh > data/logs/watchdog.log 2>&1 &

PROJECT="/Users/dong/workspace/gugu"
LOG_DIR="$PROJECT/data/logs"
PID_FILE="$PROJECT/data/gugu.pid"
WEB_PID_FILE="$PROJECT/data/web.pid"
MAX_RESTARTS=10
COOLDOWN=5
RESTART_COUNT=0

mkdir -p "$LOG_DIR"

while true; do
    # 检查引擎进程
    ENGINE_PID=""
    if [ -f "$PID_FILE" ]; then
        ENGINE_PID=$(cat "$PID_FILE")
        if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
            ENGINE_PID=""
        fi
    fi

    if [ -z "$ENGINE_PID" ]; then
        RESTART_COUNT=$((RESTART_COUNT + 1))
        if [ $RESTART_COUNT -gt $MAX_RESTARTS ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') ❌ 引擎已达最大重启次数 ($MAX_RESTARTS)，退出"
            exit 1
        fi
        echo "$(date '+%Y-%m-%d %H:%M:%S') 🔄 引擎已退出，第 $RESTART_COUNT 次重启..."
        cd "$PROJECT"
        nohup uv run python -m gugu.engine.scheduler > "$LOG_DIR/engine.log" 2>&1 &
        NEW_PID=$!
        echo "$NEW_PID" > "$PID_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') ✅ 引擎已重启 (PID=$NEW_PID)"
        sleep "$COOLDOWN"
    fi

    # 检查 Web 进程
    WEB_PID=""
    if [ -f "$WEB_PID_FILE" ]; then
        WEB_PID=$(cat "$WEB_PID_FILE")
        if ! kill -0 "$WEB_PID" 2>/dev/null; then
            WEB_PID=""
        fi
    fi

    if [ -z "$WEB_PID" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') 🔄 Web 已退出，重启..."
        cd "$PROJECT"
        nohup uv run uvicorn gugu.web.app:app --host 127.0.0.1 --port 8000 > "$LOG_DIR/web.log" 2>&1 &
        NEW_PID=$!
        echo "$NEW_PID" > "$WEB_PID_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') ✅ Web 已重启 (PID=$NEW_PID)"
        sleep "$COOLDOWN"
    fi

    sleep 30
done
