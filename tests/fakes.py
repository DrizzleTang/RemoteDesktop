"""测试用的假采集器/假连接,供多个测试文件共用。"""
from __future__ import annotations

import asyncio

import numpy as np

from host.capture import CapturedFrame, MonitorInfo
from host.hub import Tick


class FakeCapture:
    """行为完整的假采集器:可脚本化地控制每帧画面内容。

    默认每次 grab_full 返回同一张图(即"画面静止"),调用 mutate() 之后
    的下一帧会有一小块区域变化。
    """

    def __init__(self, width=320, height=240):
        self.width = width
        self.height = height
        self.monitor_index = 1
        self.grab_count = 0
        self.encode_calls = 0
        self.region_calls = 0
        self.last_fmt = None
        self.closed = False
        self._frame = np.zeros((height, width, 3), dtype=np.uint8)

    # --- 画面控制 ---
    def mutate(self, x=32, y=32, size=32, value=None):
        """改动画面上的一小块区域,用于制造"局部变化"。"""
        v = value if value is not None else (self.grab_count * 37 + 11) % 250 + 5
        self._frame[y:y + size, x:x + size] = v

    def mutate_all(self):
        self._frame[:, :] = (self.grab_count * 53 + 7) % 250 + 5

    # --- ScreenCapture 接口 ---
    def grab_full(self) -> CapturedFrame:
        self.grab_count += 1
        return CapturedFrame(rgb=self._frame.copy(), width=self.width,
                             height=self.height, capture_ms=1.0)

    def rescale(self, rgb, scale):
        if scale >= 0.999:
            return rgb, rgb.shape[1], rgb.shape[0]
        h = max(1, round(rgb.shape[0] * scale))
        w = max(1, round(rgb.shape[1] * scale))
        return rgb[:h, :w], w, h  # 简单裁剪代替缩放,尺寸语义一致即可

    def encode_image(self, rgb, quality, fmt=0):
        self.encode_calls += 1
        self.last_fmt = fmt
        return b"FULL:" + bytes([fmt])

    def encode_jpeg(self, rgb, quality):
        return self.encode_image(rgb, quality, 0)

    def encode_region(self, rgb, rect, quality, fmt=0):
        self.region_calls += 1
        self.last_fmt = fmt
        return b"RECT:" + bytes([fmt])

    def list_monitors(self):
        return [
            MonitorInfo(0, 0, 0, self.width, self.height, False, f"全部显示器 ({self.width}x{self.height})"),
            MonitorInfo(1, 0, 0, self.width, self.height, True, f"显示器 1 ({self.width}x{self.height})"),
        ]

    def set_monitor(self, index):
        if index in (0, 1):
            self.monitor_index = index
            return True
        return False

    @property
    def screen_size(self):
        return self.width, self.height

    def close(self):
        self.closed = True


class ScriptedSubscription:
    """按脚本产出 tick 的假订阅,用于单独测试 FrameStreamer 的决策逻辑。

    script 的每一项是 dirty 值:None=需要关键帧,[]=画面没变,[(x,y,w,h)...]=变化区域。
    脚本用尽后重复最后一项。
    """

    def __init__(self, script, width=320, height=240):
        self.script = list(script)
        self.width = width
        self.height = height
        self.desired_fps = 0.0
        self.calls = 0
        self.scaled_calls = 0
        self.closed = False
        self._rgb = np.zeros((height, width, 3), dtype=np.uint8)

    async def next_tick(self) -> Tick:
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        await asyncio.sleep(0)  # 让出一次事件循环,贴近真实异步时序
        return Tick(self.calls, self._rgb, self.script[idx], capture_ms=1.0)

    async def scaled(self, tick, scale):
        self.scaled_calls += 1
        if scale >= 0.999:
            return tick.full_rgb, tick.full_w, tick.full_h
        h = max(1, round(tick.full_h * scale))
        w = max(1, round(tick.full_w * scale))
        return tick.full_rgb[:h, :w], w, h

    def close(self):
        self.closed = True


class FakeWS:
    """最小可用的 websocket 替身:记录出站消息,按脚本喂入入站消息。"""

    def __init__(self):
        self.sent: list = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.remote_address = ("127.0.0.1", 51234)

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return await self.incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        return await self.incoming.get()

    async def close(self):
        self.closed = True

    def binary_messages(self):
        return [m for m in self.sent if isinstance(m, (bytes, bytearray))]

    def text_messages(self):
        return [m for m in self.sent if isinstance(m, str)]
