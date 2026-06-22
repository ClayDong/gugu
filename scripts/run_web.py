"""Web 仪表盘入口：启动 gugu 交易系统可视化监控。

用法：
    python scripts/run_web.py              # 启动仪表盘（默认 0.0.0.0:8080）
    python scripts/run_web.py --port 9090  # 指定端口
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="gugu Web 仪表盘")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式（自动重载）")
    parser.add_argument("--version", action="version", version="gugu 0.1.0")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("请先安装依赖: uv add fastapi uvicorn")
        sys.exit(1)

    from gugu.web import create_app

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
