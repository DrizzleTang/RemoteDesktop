"""
系统托盘图标(可选功能)。

被控端是一个需要长时间挂在后台的程序。只有一个控制台窗口的话,用户很容易
误关掉它(关掉 = 远程连接直接断开),而且最小化后在任务栏里也不显眼。托盘
图标解决这两个问题:常驻在系统托盘区,鼠标悬停能看到当前连接状态,右键
菜单可以查看连接信息或安全退出。

托盘是**纯可选**的增强:
- Windows 上一般都能正常工作;
- Linux 需要桌面环境提供状态栏协议,服务器/无头环境下没有;
- 任何初始化失败都只记一条日志然后降级为"没有托盘",绝不能影响远程桌面
  主功能——这是本模块所有异常处理的基本原则。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger("host.tray")

_ICON_SIZE = 64


def _build_icon_image():
    """画一个简单的图标:深色圆角背景 + 浅色"屏幕"方框。

    不依赖任何外部图片文件,避免 PyInstaller 打包时还要额外处理资源路径。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([2, 2, _ICON_SIZE - 3, _ICON_SIZE - 3], radius=12, fill=(31, 41, 55, 255))
    draw.rounded_rectangle([12, 16, _ICON_SIZE - 13, _ICON_SIZE - 22], radius=4,
                           outline=(96, 165, 250, 255), width=3)
    draw.rectangle([26, _ICON_SIZE - 20, _ICON_SIZE - 27, _ICON_SIZE - 16], fill=(96, 165, 250, 255))
    return img


class TrayIcon:
    """托盘图标的薄封装。不可用时所有方法都是安全的空操作。"""

    def __init__(self, *, host_name: str, connect_hint: str, password: str,
                 on_quit: Callable[[], None]):
        self._host_name = host_name
        self._connect_hint = connect_hint
        self._password = password
        self._on_quit = on_quit
        self._icon = None
        self._thread: threading.Thread | None = None
        self._status = "等待连接"

    @property
    def available(self) -> bool:
        return self._icon is not None

    def start(self) -> bool:
        """尝试启动托盘图标。成功返回 True,不可用返回 False(不抛异常)。"""
        try:
            import pystray

            image = _build_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem(lambda item: f"状态:{self._status}", None, enabled=False),
                pystray.MenuItem(f"连接方式:{self._connect_hint}", None, enabled=False),
                pystray.MenuItem(f"访问密码:{self._password}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._handle_quit),
            )
            self._icon = pystray.Icon(
                "remotedesktop", image, f"远程桌面被控端 - {self._host_name}", menu
            )
            # pystray 的事件循环是阻塞的,必须放到后台线程,不能占用主线程
            # (主线程要跑 asyncio 事件循环)。daemon=True 保证主程序退出时
            # 不会被这个线程卡住。
            self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
            self._thread.start()
            return True
        except Exception as exc:  # noqa: BLE001 - 托盘不可用是常态,绝不能影响主功能
            logger.info("系统托盘不可用,已跳过(不影响远程桌面功能): %s", exc)
            self._icon = None
            return False

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception as exc:  # noqa: BLE001
            logger.info("托盘图标运行结束: %s", exc)
            self._icon = None

    def _handle_quit(self, icon=None, item=None) -> None:
        try:
            self._on_quit()
        finally:
            self.stop()

    def set_status(self, status: str) -> None:
        """更新托盘悬停提示里的状态文字。"""
        self._status = status
        if self._icon is None:
            return
        try:
            self._icon.title = f"远程桌面被控端 - {self._host_name}({status})"
            self._icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001
            pass
        self._icon = None
