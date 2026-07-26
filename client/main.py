"""
主控端(client)启动入口。

真正的远程桌面客户端逻辑(WebSocket 连接、加解密、画面渲染、输入采集)
完全运行在浏览器里(见 client/static/),这里只是一个"双击即用"的便捷
启动器:在本机启动一个仅监听 127.0.0.1 的静态文件服务器,并自动打开
默认浏览器。之所以不直接用 file:// 打开 index.html,是因为浏览器的
SubtleCrypto / Clipboard 等 API 通常要求"安全上下文"(HTTPS 或
127.0.0.1/localhost),用本地 HTTP 服务器可以自然满足这个条件。
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import sys
import threading
import webbrowser


def _static_dir() -> str:
    """定位 client/static 目录。

    被 PyInstaller 打包成 onefile exe 运行时,普通的相对路径找不到打包
    进 exe 里的资源,必须通过 sys._MEIPASS(运行时解压的临时目录)定位;
    未打包时按源码目录结构定位。
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "client", "static")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="远程桌面 - 主控端网页启动器")
    parser.add_argument("--port", type=int, default=8080, help="本地静态服务器端口(默认 8080)")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - 与父类签名保持一致
        pass  # 静态资源请求日志较吵,默认静默;需要调试时可临时改回 print


def main() -> None:
    args = build_arg_parser().parse_args()
    static_dir = _static_dir()
    if not os.path.isdir(static_dir):
        print(f"错误: 找不到静态资源目录 {static_dir}")
        sys.exit(1)

    def handler_factory(*a, **kw):
        return _QuietHandler(*a, directory=static_dir, **kw)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), handler_factory) as httpd:
        url = f"http://127.0.0.1:{args.port}/"
        print("=" * 56)
        print(" 远程桌面 - 主控端 已启动")
        print(f" 请在浏览器中打开: {url}")
        print(" 按 Ctrl+C 退出")
        print("=" * 56)
        if not args.no_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出。")


if __name__ == "__main__":
    main()
