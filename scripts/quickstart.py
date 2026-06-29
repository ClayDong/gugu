"""gugu 一键启动脚本。

用法：
    python scripts/quickstart.py              # 首次配置检查 + 启动
    python scripts/quickstart.py --daemon     # 以守护模式启动
    python scripts/quickstart.py --status     # 检查运行状态
    python scripts/quickstart.py --stop       # 停止运行
    python scripts/quickstart.py --web-only   # 仅启动 Web 仪表盘
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SRC = PROJECT / "src"
PID_FILE = PROJECT / "data" / "gugu.pid"
WEB_PID_FILE = PROJECT / "data" / "web.pid"


def _ensure_env() -> None:
    """检查运行环境。"""
    venv = PROJECT / ".venv"
    if not venv.exists():
        print("❌ .venv 未找到，请先执行: uv sync 或 poetry install")
        sys.exit(1)

    # Python 版本检查
    py_version = sys.version_info
    if py_version.major < 3 or (py_version.major == 3 and py_version.minor < 11):
        print(f"❌ Python 3.11+ 需要，当前 {py_version.major}.{py_version.minor}")
        sys.exit(1)

    print(f"✅ Python {py_version.major}.{py_version.minor}.{py_version.micro}")


def _check_env_file() -> None:
    """检查 .env 配置。"""
    env_file = PROJECT / ".env"
    if not env_file.exists():
        print("❌ .env 未找到")
        example = PROJECT / ".env.example"
        if example.exists():
            print(f"   参考: cp {example} {env_file}")
        sys.exit(1)

    # 检查关键配置（区分"缺失"和"占位符"）
    missing = []
    placeholders = []
    with env_file.open() as f:
        lines = f.readlines()
    env_vars = {line.split("=", 1)[0].strip() for line in lines if "=" in line and not line.startswith("#")}
    for key in ["feishu_app_id", "feishu_app_secret", "feishu_chat_id"]:
        if key not in env_vars:
            missing.append(key)
        else:
            for line in lines:
                if line.startswith(f"{key}=") and "your_" in line:
                    placeholders.append(key)
                    break

    if missing:
        print(f"❌ 飞书配置缺失: {', '.join(missing)}")
        print("   请在 .env 中补充，否则通知功能不可用")
    elif placeholders:
        print(f"⚠️  飞书配置含占位符 (需要: {', '.join(placeholders)})")
        print("   请将 your_xxx 替换为真实值")
    else:
        print("✅ .env 配置完整")


def _check_data_sources() -> None:
    """快速检查数据源可用性。"""
    try:
        from gugu.data.collectors.akshare_collector import AkshareCollector
        from gugu.data.collectors.fallback import SinaCollector

        # 仅检查类导入
        print(f"✅ 数据采集器: {AkshareCollector.source} + {SinaCollector.source}")
    except ImportError as e:
        print(f"❌ 数据源导入失败: {e}")
        sys.exit(1)


def _check_feishu() -> None:
    """检查飞书连接。"""
    try:
        from gugu.config import env as env_getter
        cfg = env_getter()
        if cfg.feishu_app_id and cfg.feishu_app_secret and cfg.feishu_chat_id:
            print(f"✅ 飞书已配置 (chat_id={cfg.feishu_chat_id[:8]}...)")
        else:
            print("⚠️  飞书配置不完整，通知功能将跳过")
    except Exception:
        print("⚠️  飞书配置检查失败")


def _check_llm() -> None:
    """检查 LLM 连接。"""
    try:
        from gugu.config import env as env_getter
        cfg = env_getter()
        if cfg.llm_api_key:
            print(f"✅ LLM 已配置 (model={cfg.llm_model})")
        else:
            print("ℹ️  LLM 未配置，智慧决策使用硬编码规则")
    except Exception:
        print("ℹ️  LLM 配置检查失败")


def _check_watchlist() -> None:
    """检查自选股配置。"""
    try:
        from gugu.config import settings
        wl = settings().get("watchlist", [])
        mode = settings().get("execution", {}).get("mode", "signal_only")
        print(f"✅ 自选股: {len(wl)} 只 | 运行模式: {mode}")
    except Exception:
        print("⚠️  自选股配置检查失败")


def is_running(pid_file: Path) -> int | None:
    """检查进程是否在运行。"""
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)  # 检查进程是否存在
        return pid
    except (ProcessLookupError, ValueError, OSError):
        return None


def start_daemon() -> None:
    """以守护模式启动引擎 + Web 服务器。"""
    existing = is_running(PID_FILE)
    if existing:
        print(f"ℹ️  交易引擎已在运行 (PID={existing})")
    else:
        print("🚀 启动交易引擎守护进程...")
        proc = subprocess.Popen(
            [sys.executable, "-m", "gugu.engine.scheduler"],
            cwd=PROJECT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(proc.pid))
        print(f"   PID: {proc.pid}")

    web_existing = is_running(WEB_PID_FILE)
    if web_existing:
        print(f"ℹ️  Web 仪表盘已在运行 (PID={web_existing})")
    else:
        print("🚀 启动 Web 仪表盘 (端口 8000)...")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "gugu.web.app:app",
             "--host", "127.0.0.1", "--port", "8000"],
            cwd=PROJECT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        WEB_PID_FILE.write_text(str(proc.pid))
        print(f"   PID: {proc.pid}")
        print(f"   访问: http://127.0.0.1:8000")

    print("\n✅ 已启动。执行以下命令检查状态：")
    print(f"   python {sys.argv[0]} --status")
    print(f"   python {sys.argv[0]} --stop")


def stop_daemon() -> None:
    """停止所有守护进程。"""
    for pid_file, name in [(PID_FILE, "交易引擎"), (WEB_PID_FILE, "Web仪表盘")]:
        pid = is_running(pid_file)
        if pid:
            try:
                os.kill(pid, 15)
                time.sleep(0.5)
                print(f"✅ {name} 已停止 (PID={pid})")
            except ProcessLookupError:
                print(f"ℹ️  {name} 已退出")
        else:
            print(f"ℹ️  {name} 未运行")
        if pid_file.exists():
            pid_file.unlink()


def show_status() -> None:
    """显示运行状态。"""
    print("=== gugu 运行状态 ===")
    engine_pid = is_running(PID_FILE)
    web_pid = is_running(WEB_PID_FILE)
    print(f"交易引擎: {'运行中 (PID=' + str(engine_pid) + ')' if engine_pid else '未运行'}")
    print(f"Web仪表盘: {'运行中 (PID=' + str(web_pid) + ')' if web_pid else '未运行'}")
    if web_pid:
        print(f"访问地址: http://127.0.0.1:8000")

    # 显示数据目录状态
    data_dir = PROJECT / "data"
    if data_dir.exists():
        files = list(data_dir.iterdir())
        print(f"数据目录: {len(files)} 个文件")
        hb = data_dir / "heartbeat.json"
        if hb.exists():
            from gugu.utils.log import get_logger
            import json
            try:
                hb_data = json.loads(hb.read_text())
                print(f"最后心跳: {hb_data.get('last_cycle_at', '?')} ({hb_data.get('status', '?')})")
                print(f"总资产: ¥{hb_data.get('total_value', 0):,.0f}")
            except Exception:
                pass


def run_quickstart() -> None:
    """执行首次配置检查。"""
    print("=" * 50)
    print("  gugu 交易系统 — 配置检查")
    print("=" * 50)
    _ensure_env()
    _check_env_file()
    _check_data_sources()
    _check_feishu()
    _check_llm()
    _check_watchlist()
    print("=" * 50)
    print("  配置检查完成。启动命令：")
    print(f"  python {sys.argv[0]} --daemon    # 启动引擎 + Web")
    print(f"  python {sys.argv[0]} --web-only  # 仅启动 Web")
    print("=" * 50)


def run_web_only() -> None:
    """仅启动 Web 仪表盘。"""
    web_existing = is_running(WEB_PID_FILE)
    if web_existing:
        print(f"Web 仪表盘已在运行 (PID={web_existing})")
        print(f"访问: http://127.0.0.1:8000")
        return

    print("启动 Web 仪表盘 (端口 8000)...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "gugu.web.app:app",
         "--host", "127.0.0.1", "--port", "8000"],
        cwd=PROJECT,
    )
    WEB_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    WEB_PID_FILE.write_text(str(proc.pid))
    print(f"PID: {proc.pid}")
    print(f"访问: http://127.0.0.1:8000")


def main() -> None:
    parser = argparse.ArgumentParser(description="gugu 交易系统 - 一键启动")
    parser.add_argument("--daemon", action="store_true", help="以守护模式启动")
    parser.add_argument("--status", action="store_true", help="检查运行状态")
    parser.add_argument("--stop", action="store_true", help="停止运行")
    parser.add_argument("--web-only", action="store_true", help="仅启动 Web 仪表盘")
    parser.add_argument("--quickstart", action="store_true", help="首次配置检查")
    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.stop:
        stop_daemon()
    elif args.daemon:
        _ensure_env()
        start_daemon()
    elif args.web_only:
        run_web_only()
    else:
        run_quickstart()


if __name__ == "__main__":
    main()