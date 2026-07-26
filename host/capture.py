"""
屏幕采集与编码。

每个 ScreenCapture 实例绑定到调用它的线程(mss 的底层实现要求同一个
mss 实例只能在创建它的线程里使用),因此本模块被设计为在 host/server.py
里通过一个专属的单线程 executor 反复调用同一个实例,既避免了每帧重新
初始化 mss 的开销,也天然满足了这个线程亲和性要求。
"""
from __future__ import annotations

import io
import threading
from dataclasses import dataclass

from PIL import Image


@dataclass
class MonitorInfo:
    index: int
    left: int
    top: int
    width: int
    height: int


@dataclass
class EncodedFrame:
    width: int
    height: int
    jpeg_bytes: bytes


class ScreenCapture:
    """屏幕采集器。同一实例只应在单一线程内反复调用 grab_and_encode。"""

    def __init__(self, monitor_index: int = 1):
        self._monitor_index = monitor_index
        self._sct = None
        self._owner_thread = None
        self._monitor = None

    def _ensure_thread_local_sct(self):
        current = threading.current_thread()
        if self._sct is not None and self._owner_thread is current:
            return
        import mss  # 延迟导入:无显示环境下也能安全 import 本模块的其他内容

        self._sct = mss.mss()
        self._owner_thread = current
        monitors = self._sct.monitors
        idx = self._monitor_index if 0 <= self._monitor_index < len(monitors) else 0
        self._monitor = monitors[idx]

    def list_monitors(self) -> list[MonitorInfo]:
        self._ensure_thread_local_sct()
        import mss

        with mss.mss() as sct:
            return [
                MonitorInfo(index=i, left=m["left"], top=m["top"], width=m["width"], height=m["height"])
                for i, m in enumerate(sct.monitors)
            ]

    @property
    def screen_size(self) -> tuple[int, int]:
        self._ensure_thread_local_sct()
        return self._monitor["width"], self._monitor["height"]

    def grab_and_encode(self, *, scale: float, jpeg_quality: int) -> EncodedFrame:
        """截取当前屏幕,按 scale 缩放后编码为 JPEG。这是一个阻塞调用,
        调用方(host/server.py)应通过线程池 executor 调用,避免阻塞事件循环。"""
        self._ensure_thread_local_sct()
        shot = self._sct.grab(self._monitor)
        img = Image.frombytes("RGB", shot.size, shot.rgb)

        full_w, full_h = img.size
        scale = max(0.1, min(1.0, scale))
        if scale < 0.999:
            out_w = max(1, round(full_w * scale))
            out_h = max(1, round(full_h * scale))
            img = img.resize((out_w, out_h), Image.BILINEAR)
        else:
            out_w, out_h = full_w, full_h

        buf = io.BytesIO()
        quality = max(5, min(95, int(jpeg_quality)))
        img.save(buf, format="JPEG", quality=quality)
        return EncodedFrame(width=out_w, height=out_h, jpeg_bytes=buf.getvalue())

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001 - 关闭阶段的异常无需向上抛
                pass
            self._sct = None
