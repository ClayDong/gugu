"""Web 仪表盘入口：启动 gugu 交易系统可视化监控。

用法：
    python scripts/run_web.py              # 启动仪表盘（默认 127.0.0.1:8080，仅本机访问）
    python scripts/run_web.py --port 9090  # 指定端口
    python scripts/run_web.py --host 0.0.0.0  # 显式开放局域网访问（需配置 GUGU_ADMIN_API_KEY）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gugu import __version__  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="gugu Web 仪表盘")
    # 安全默认：只绑定本机回环。局域网/远程访问必须显式 --host 0.0.0.0 并配置管理 API Key。
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式（自动重载）")
    parser.add_argument("--version", action="version", version=f"gugu {__version__}")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        os.environ["GUGU_WEB_REMOTE"] = "1"
        os.environ.pop("GUGU_WEB_LOCAL", None)
    else:
        os.environ["GUGU_WEB_LOCAL"] = "1"
        os.environ.pop("GUGU_WEB_REMOTE", None)

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
