"""
远端鼠标光标采集。

为什么要单独做:屏幕截图 API(mss / X11 XGetImage / Windows BitBlt)**都不包含
鼠标指针**。不采集光标的话,主控端看不到对方光标的形状变化——是普通箭头、
文本 I 型光标、还是调整大小的双向箭头,全靠猜;悬停反馈也完全丢失。

为什么不把光标画进画面帧:那样光标一动就会让所在区域变成"脏矩形",触发
画面重传。光标是移动最频繁的元素,这会显著增加带宽。单独走一条几十字节的
控制消息,既省流量,又能让客户端以本地帧率平滑地画光标。

传输策略:
- **位置**每次变化都发(几十字节,很便宜);
- **形状图像**只在形状真正变化时才发一次,并带一个 id;客户端按 id 缓存,
  之后同样的形状只需要发 id。系统光标形状总共就那么几种,缓存命中率极高。

平台支持:
- 位置:用 pynput 读取,全平台可用;
- 形状:X11 走 XFixes 扩展(已实测可用);Windows 走 ctypes 调 Win32 API;
  任何一步失败都只降级为"只有位置没有形状",不影响远程桌面主功能。
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import struct

logger = logging.getLogger("host.cursor")

MAX_CURSOR_EDGE = 128  # 超过这个尺寸的光标视为异常,丢弃(防御畸形数据)


class CursorSnapshot:
    """一次光标采样的结果。"""

    __slots__ = ("x", "y", "shape_id", "png_b64", "width", "height", "hot_x", "hot_y")

    def __init__(self, x: int, y: int, shape_id: str | None = None,
                 png_b64: str | None = None, width: int = 0, height: int = 0,
                 hot_x: int = 0, hot_y: int = 0):
        self.x = x
        self.y = y
        self.shape_id = shape_id
        self.png_b64 = png_b64
        self.width = width
        self.height = height
        self.hot_x = hot_x
        self.hot_y = hot_y


class CursorCapture:
    """跨平台光标采集器。与 ScreenCapture 一样有线程亲和性要求,应在
    同一个采集线程里反复调用。"""

    def __init__(self) -> None:
        self._backend = None
        self._backend_ready = False
        self._mouse = None
        self._known_shapes: set[str] = set()
        self._last_shape_id: str | None = None

    # ------------------------------------------------------------------
    # 位置(全平台)
    # ------------------------------------------------------------------

    def _ensure_mouse(self) -> None:
        if self._mouse is not None:
            return
        from pynput.mouse import Controller  # 延迟导入:无显示环境下会失败

        self._mouse = Controller()

    def position(self) -> tuple[int, int] | None:
        try:
            self._ensure_mouse()
            x, y = self._mouse.position
            return int(x), int(y)
        except Exception:  # noqa: BLE001 - 拿不到位置就当作没有光标信息
            return None

    # ------------------------------------------------------------------
    # 形状(平台相关,失败即降级)
    # ------------------------------------------------------------------

    def _ensure_backend(self) -> None:
        if self._backend_ready:
            return
        self._backend_ready = True
        for factory in (_X11CursorBackend.try_create, _WindowsCursorBackend.try_create):
            try:
                backend = factory()
            except Exception as exc:  # noqa: BLE001
                logger.debug("光标形状后端初始化失败: %s", exc)
                continue
            if backend is not None:
                self._backend = backend
                logger.info("光标形状采集已启用(%s)", backend.name)
                return
        logger.info("当前平台无法采集光标形状,仅同步光标位置")

    def sample(self) -> CursorSnapshot | None:
        """采样一次光标。返回 None 表示完全拿不到光标信息。

        只有当形状是客户端还没见过的新形状时,才会带上 png_b64;否则只带
        shape_id,由客户端用缓存。
        """
        pos = self.position()
        if pos is None:
            return None
        self._ensure_backend()

        if self._backend is None:
            return CursorSnapshot(pos[0], pos[1])

        try:
            shape = self._backend.read_shape()
        except Exception as exc:  # noqa: BLE001 - 采集形状失败不影响位置同步
            logger.debug("读取光标形状失败: %s", exc)
            return CursorSnapshot(pos[0], pos[1])
        if shape is None:
            return CursorSnapshot(pos[0], pos[1])

        shape_id, png_bytes, w, h, hx, hy = shape
        if shape_id in self._known_shapes:
            return CursorSnapshot(pos[0], pos[1], shape_id, None, w, h, hx, hy)
        self._known_shapes.add(shape_id)
        return CursorSnapshot(
            pos[0], pos[1], shape_id, base64.b64encode(png_bytes).decode("ascii"), w, h, hx, hy
        )

    def forget_shapes(self) -> None:
        """会话重连后调用:客户端的缓存没了,需要重新下发形状图像。"""
        self._known_shapes.clear()


def _argb_to_png(pixels: list[int], width: int, height: int) -> bytes:
    """把 ARGB32 像素数组转成 PNG 字节。

    X11 XFixes 返回的是**预乘 alpha** 的 ARGB32,需要反预乘才能得到正确的
    RGBA,否则半透明边缘会偏暗。
    """
    from PIL import Image

    raw = bytearray(width * height * 4)
    for i, value in enumerate(pixels):
        a = (value >> 24) & 0xFF
        r = (value >> 16) & 0xFF
        g = (value >> 8) & 0xFF
        b = value & 0xFF
        if 0 < a < 255:  # 反预乘
            r = min(255, r * 255 // a)
            g = min(255, g * 255 // a)
            b = min(255, b * 255 // a)
        off = i * 4
        raw[off] = r
        raw[off + 1] = g
        raw[off + 2] = b
        raw[off + 3] = a
    img = Image.frombytes("RGBA", (width, height), bytes(raw))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


class _X11CursorBackend:
    name = "X11 XFixes"

    def __init__(self, display):
        self._display = display

    @classmethod
    def try_create(cls):
        from Xlib import display as xdisplay

        d = xdisplay.Display()
        if not d.has_extension("XFIXES"):
            d.close()
            return None
        d.xfixes_query_version()
        return cls(d)

    def read_shape(self):
        # 注意:python-xlib 把这个方法注册在 Display 上,但签名要求传一个
        # window 参数(实现里并未使用),所以必须传根窗口进去。
        img = self._display.xfixes_get_cursor_image(self._display.screen().root)
        w, h = int(img.width), int(img.height)
        if w <= 0 or h <= 0 or w > MAX_CURSOR_EDGE or h > MAX_CURSOR_EDGE:
            return None
        pixels = list(img.cursor_image)
        if len(pixels) != w * h:
            return None
        # 用像素内容做指纹:同一形状每次读到的数据一致,可稳定命中客户端缓存
        digest = hashlib.sha1(struct.pack(f"<{len(pixels)}I", *pixels)).hexdigest()[:16]
        return digest, _argb_to_png(pixels, w, h), w, h, int(img.xhot), int(img.yhot)


class _WindowsCursorBackend:
    """Windows 下通过 Win32 API 读取当前光标图标。

    本沙箱无法验证这条路径,因此实现上格外保守:任何一步失败都返回 None,
    退化为"只同步位置"。
    """

    name = "Windows GetCursorInfo"

    def __init__(self):
        import ctypes

        self._ctypes = ctypes
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32

    @classmethod
    def try_create(cls):
        import sys

        if not sys.platform.startswith("win"):
            return None
        return cls()

    def read_shape(self):
        import ctypes
        from ctypes import wintypes

        class CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                        ("hbmColor", wintypes.HBITMAP)]

        info = CURSORINFO()
        info.cbSize = ctypes.sizeof(CURSORINFO)
        if not self._user32.GetCursorInfo(ctypes.byref(info)):
            return None
        if not info.hCursor or not (info.flags & 0x00000001):  # CURSOR_SHOWING
            return None

        icon_info = ICONINFO()
        if not self._user32.GetIconInfo(info.hCursor, ctypes.byref(icon_info)):
            return None
        try:
            from PIL import Image, ImageWin  # noqa: F401 - 仅用于确认 PIL 可用

            # 用 hCursor 句柄值作为形状指纹:Windows 对系统光标复用同一句柄,
            # 因此同一形状的句柄稳定,可用于客户端缓存。
            shape_id = f"win{int(info.hCursor)}"
            width, height = self._icon_size(icon_info)
            if width <= 0 or height <= 0 or width > MAX_CURSOR_EDGE or height > MAX_CURSOR_EDGE:
                return None
            png = self._render_icon(info.hCursor, width, height)
            if png is None:
                return None
            return (shape_id, png, width, height,
                    int(icon_info.xHotspot), int(icon_info.yHotspot))
        finally:
            for handle in (icon_info.hbmMask, icon_info.hbmColor):
                if handle:
                    self._gdi32.DeleteObject(handle)

    def _icon_size(self, icon_info):
        import ctypes
        from ctypes import wintypes

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                        ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                        ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                        ("bmBits", ctypes.c_void_p)]

        bmp = BITMAP()
        handle = icon_info.hbmColor or icon_info.hbmMask
        if not self._gdi32.GetObjectW(handle, ctypes.sizeof(BITMAP), ctypes.byref(bmp)):
            return 0, 0
        height = bmp.bmHeight if icon_info.hbmColor else bmp.bmHeight // 2
        return int(bmp.bmWidth), int(height)

    def _render_icon(self, hcursor, width, height):
        """把光标画到一张离屏位图上再读回像素。"""
        import ctypes

        from PIL import Image

        hdc_screen = self._user32.GetDC(0)
        hdc = self._gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = self._gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        old = self._gdi32.SelectObject(hdc, hbmp)
        try:
            self._user32.DrawIconEx(hdc, 0, 0, hcursor, width, height, 0, None, 0x0003)
            buf = ctypes.create_string_buffer(width * height * 4)

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                            ("biClrImportant", ctypes.c_uint32)]

            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = width
            header.biHeight = -height  # 负数表示自上而下,省一次翻转
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0
            if not self._gdi32.GetDIBits(hdc, hbmp, 0, height, buf, ctypes.byref(header), 0):
                return None
            img = Image.frombuffer("RGBA", (width, height), buf.raw, "raw", "BGRA", 0, 1)
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=False)
            return out.getvalue()
        finally:
            self._gdi32.SelectObject(hdc, old)
            self._gdi32.DeleteObject(hbmp)
            self._gdi32.DeleteDC(hdc)
            self._user32.ReleaseDC(0, hdc_screen)
