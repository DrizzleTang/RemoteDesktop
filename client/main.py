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
import traceback
import webbrowser


def _safe_print(*args, **kwargs) -> None:
    """尽量打印,但绝不能因为打印本身崩溃退出。

    打包成 --noconsole 的 exe(主控端就是)后,没有控制台窗口时 stdout/stderr
    在某些 Windows 环境下会是 None 或者一个写入即报错的坏文件描述符——这会导致
    程序在还没显示任何东西之前就直接崩溃退出,表现就是"双击完全没反应"。
    """
    try:
        print(*args, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def _crash_log_path() -> str:
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
    return os.path.join(base, "RemoteDesktop-Client-错误日志.txt")


def _report_fatal_error(exc: BaseException) -> None:
    """没有控制台窗口时用户看不到任何报错,这里把详情写进 exe 旁边的文件,
    并在 Windows 下额外弹一个消息框,保证"启动失败"不会表现成"毫无反应"。"""
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        with open(_crash_log_path(), "w", encoding="utf-8") as f:
            f.write(detail)
    except Exception:  # noqa: BLE001
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes

            port_hint = ""
            if isinstance(exc, OSError):
                port_hint = "\n\n常见原因:端口已被占用(比如主控端已经在运行了,或被其他程序占用)。"
            ctypes.windll.user32.MessageBoxW(
                None,
                f"启动失败: {exc}{port_hint}\n\n详细信息已写入:\n{_crash_log_path()}",
                "远程桌面 - 主控端",
                0x10,  # MB_ICONERROR
            )
        except Exception:  # noqa: BLE001
            pass


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
        _safe_print(f"错误: 找不到静态资源目录 {static_dir}")
        sys.exit(1)

    def handler_factory(*a, **kw):
        return _QuietHandler(*a, directory=static_dir, **kw)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), handler_factory) as httpd:
        url = f"http://127.0.0.1:{args.port}/"
        _safe_print("=" * 56)
        _safe_print(" 远程桌面 - 主控端 已启动")
        _safe_print(f" 请在浏览器中打开: {url}")
        _safe_print(" 按 Ctrl+C 退出")
        _safe_print("=" * 56)
        if not args.no_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            _safe_print("\n已退出。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _safe_print("\n已退出。")
    except Exception as exc:  # noqa: BLE001 - 顶层入口:任何异常都要能被用户看到,不能悄无声息地退出
        _safe_print(f"启动失败: {exc}")
        _report_fatal_error(exc)
        sys.exit(1)
