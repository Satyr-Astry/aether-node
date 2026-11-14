"""AetherNode 启动入口：先起 HTTP 服务，再开应用窗口。

窗口优先级：pywebview（若已安装）→ Edge/Chrome 的 --app 模式（无地址栏的应用窗口）→ 系统浏览器。
--app 模式依赖已安装的 WebView2/Chromium 内核浏览器，零额外依赖，观感接近原生桌面程序。

用法：
    python run.py               交互使用
    python run.py --no-window   只跑服务（服务器/调试）
    python run.py --browser     强制用系统浏览器
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_ready(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(host, port):
            return True
        time.sleep(0.3)
    return False


def find_chromium_browser() -> Path | None:
    bases = [
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
        os.environ.get("LOCALAPPDATA"),
    ]
    relatives = [
        "Microsoft/Edge/Application/msedge.exe",
        "Microsoft/Edge Beta/Application/msedge.exe",
        "Google/Chrome/Application/chrome.exe",
        "Microsoft/EdgeWebView/Application/msedgewebview2.exe",
    ]
    for base in bases:
        if not base:
            continue
        for rel in relatives:
            candidate = Path(base) / rel
            if candidate.exists():
                return candidate
    return None


def open_app_window(url: str, home: Path) -> bool:
    """用 --app 模式开一个无地址栏窗口；进程退出即返回 True。"""
    exe = find_chromium_browser()
    if not exe:
        return False
    profile = home / "browser-profile"
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        proc = subprocess.Popen([
            str(exe),
            f"--app={url}",
            "--window-size=1320,880",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
        ])
    except Exception as exc:
        print(f"  应用窗口启动失败：{exc}")
        return False
    print("  已打开应用窗口，关闭窗口即退出")
    proc.wait()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="AetherNode — P2P AI 服务网络节点")
    parser.add_argument("--no-window", action="store_true", help="只启动服务，不打开窗口")
    parser.add_argument("--browser", action="store_true", help="强制用系统浏览器打开")
    parser.add_argument("--host", default=None, help="覆盖监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口")
    args = parser.parse_args()

    from aether import APP_NAME, __version__
    from aether.config import get_settings, load_settings, set_runtime_host, set_runtime_port

    load_settings()
    settings = get_settings()
    host = args.host or settings.effective_host()
    port = args.port or settings.api_port
    # 命令行可能覆盖地址与端口，内置 relay 地址、对外端点与可达性判定都要跟着走
    set_runtime_port(port)
    set_runtime_host(host)
    url = f"http://127.0.0.1:{port}"

    print(f"  {APP_NAME} {__version__}")
    print(f"  数据目录: {settings.home}")
    print(f"  监听: {host}:{port}")

    server = None
    if port_open("127.0.0.1", port):
        print(f"  端口 {port} 已在使用，直接连接已有实例")
    else:
        import uvicorn

        config = uvicorn.Config(
            "aether.api.app:app",
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, name="uvicorn", daemon=True).start()
        if not wait_ready("127.0.0.1", port, 60):
            print("  [错误] 服务启动超时，请检查端口占用与依赖安装")
            return 1
        print(f"  服务已就绪: {url}")

    def shutdown() -> None:
        if server:
            server.should_exit = True

    if args.no_window:
        print("  仅服务模式运行中，Ctrl+C 退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        shutdown()
        return 0

    if args.browser:
        webbrowser.open(url)
        print("  已用浏览器打开，Ctrl+C 退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        shutdown()
        return 0

    # 1) pywebview（装了就用原生窗口）
    try:
        import webview

        webview.create_window(APP_NAME, url, width=1320, height=880, min_size=(1000, 660))
        webview.start()
        shutdown()
        return 0
    except Exception:
        pass

    # 2) Chromium --app 模式
    if open_app_window(url, settings.home):
        shutdown()
        return 0

    # 3) 系统浏览器兜底
    print("  未找到可用的应用窗口内核，改用系统浏览器")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
