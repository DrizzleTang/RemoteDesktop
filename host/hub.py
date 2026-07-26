"""
共享采集管线。

问题:原来每个会话各自持有一份 ScreenCapture + DirtyTracker + 推流循环。
三个观看者 = 同一块屏幕被采集 3 次、差分 3 次,内存也是 3 份(DirtyTracker
要预分配与画面等大的缓冲区,1080p 约 19MB/会话)。CPU 和内存都是 O(N)。

解法:把"采集 + 差分"这一段共享出来,每一"拍"(tick)只做一次:

    采集整幅原始画面  ->  在**原始分辨率**下做差分  ->  得到脏矩形
                                                          |
                    +-------------------------------------+
                    |                    |
              会话A(缩放0.75)      会话B(缩放1.0)
              把矩形映射到自己的坐标系,从缓存的缩放图上裁剪并编码

顺带解决了一个原本就存在的浪费:老实现里 grab(scale) 每帧都要缩放整幅画面,
即使画面根本没变。现在差分在原始分辨率上做,**只有确实要发送时才缩放**,
画面静止时连缩放都省了。缩放结果还会按缩放比缓存,多个同档位的会话共用。

各会话仍保留各自独立的流控状态(等待 ack、关键帧时机、累积的脏区域),
因为它们的网络状况可能完全不同——一个观看者网差不该拖慢其他人。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time

import numpy as np

from host.delta import DirtyTracker

logger = logging.getLogger("host.hub")

IDLE_POLL_MIN_S = 0.05
IDLE_POLL_MAX_S = 0.30
IDLE_BACKOFF_AFTER = 10


class Tick:
    """一次共享采集的结果。缩放后的图像按需生成并缓存。"""

    __slots__ = ("seq", "full_rgb", "full_w", "full_h", "dirty", "capture_ms",
                 "_scaled", "_scaled_locks")

    def __init__(self, seq: int, full_rgb: np.ndarray, dirty, capture_ms: float):
        self.seq = seq
        self.full_rgb = full_rgb
        self.full_h, self.full_w = full_rgb.shape[0], full_rgb.shape[1]
        # dirty 为 None 表示"必须发关键帧";空列表表示"画面完全没变"
        self.dirty = dirty
        self.capture_ms = capture_ms
        self._scaled: dict[float, tuple[np.ndarray, int, int]] = {}
        self._scaled_locks: dict[float, asyncio.Lock] = {}


class SharedCaptureHub:
    """被所有会话共用的采集/差分管线。"""

    def __init__(self, capture, executor):
        self.capture = capture
        self.executor = executor
        self.tracker = DirtyTracker()

        self._loop = asyncio.get_running_loop()
        self._subscribers: set[_Subscription] = set()
        self._tick: Tick | None = None
        self._tick_seq = 0
        self._new_tick = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._idle_streak = 0
        self._closed = False

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------

    def subscribe(self) -> "_Subscription":
        sub = _Subscription(self)
        self._subscribers.add(sub)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="capture-hub")
        return sub

    def unsubscribe(self, sub: "_Subscription") -> None:
        self._subscribers.discard(sub)
        if not self._subscribers and self._task is not None:
            self._task.cancel()
            self._task = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ------------------------------------------------------------------
    # 采集循环
    # ------------------------------------------------------------------

    def _target_interval(self) -> float:
        """按所有订阅者中最高的帧率需求决定采集节奏。"""
        fps_values = [s.desired_fps for s in self._subscribers if s.desired_fps > 0]
        if not fps_values:
            return IDLE_POLL_MIN_S
        return 1.0 / max(fps_values)

    async def _run(self) -> None:
        while not self._closed:
            started = time.monotonic()
            try:
                captured = await self._loop.run_in_executor(
                    self.executor, functools.partial(self.capture.grab_full)
                )
            except Exception:  # noqa: BLE001 - 采集失败不应终止整个管线
                logger.exception("屏幕采集失败,1 秒后重试")
                await asyncio.sleep(1.0)
                continue

            dirty = self.tracker.compute(captured.rgb)
            self._tick_seq += 1
            self._tick = Tick(self._tick_seq, captured.rgb, dirty, captured.capture_ms)
            self._new_tick.set()
            self._new_tick.clear()
            for sub in list(self._subscribers):
                sub._notify(self._tick)

            if dirty is not None and not dirty:
                self._idle_streak += 1
            else:
                self._idle_streak = 0

            await asyncio.sleep(self._sleep_for(started))

    def _sleep_for(self, started: float) -> float:
        if self._idle_streak > IDLE_BACKOFF_AFTER:
            # 画面持续静止时放慢轮询以省 CPU,一旦有变化会立刻恢复
            interval = min(IDLE_POLL_MAX_S,
                           IDLE_POLL_MIN_S + (self._idle_streak - IDLE_BACKOFF_AFTER) * 0.02)
        else:
            interval = max(IDLE_POLL_MIN_S, self._target_interval())
        return max(0.0, interval - (time.monotonic() - started))

    # ------------------------------------------------------------------
    # 缩放结果缓存(多个同档位会话共用一份)
    # ------------------------------------------------------------------

    async def scaled(self, tick: Tick, scale: float) -> tuple[np.ndarray, int, int]:
        key = round(max(0.1, min(1.0, float(scale))), 3)
        cached = tick._scaled.get(key)
        if cached is not None:
            return cached
        lock = tick._scaled_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = tick._scaled.get(key)
            if cached is not None:
                return cached
            result = await self._loop.run_in_executor(
                self.executor, functools.partial(self.capture.rescale, tick.full_rgb, key)
            )
            tick._scaled[key] = result
            return result

    # ------------------------------------------------------------------
    # 显示器切换 / 关闭
    # ------------------------------------------------------------------

    async def set_monitor(self, index: int) -> bool:
        """切换采集的显示器。

        注意这是**全局**的:所有观看者会一起切过去。这符合直觉——大家看的是
        同一场会话;而且只有持有操作权的一方能发起切换。
        """
        ok = await self._loop.run_in_executor(self.executor, self.capture.set_monitor, index)
        if ok:
            self.tracker.reset()
        return ok

    def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.capture.close()


class _Subscription:
    """一个会话对共享管线的订阅。"""

    def __init__(self, hub: SharedCaptureHub):
        self._hub = hub
        self._queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=1)
        self.desired_fps: float = 0.0

    def _notify(self, tick: Tick) -> None:
        # 队列容量为 1:会话跟不上时,新帧直接替换掉还没被取走的旧帧。
        # 旧画面没有补发价值,而会话侧会把跳过期间的脏区域累积起来,
        # 所以丢掉中间帧不会导致画面残缺。
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - 单线程下不会发生
                pass
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:  # pragma: no cover
            pass

    async def next_tick(self) -> Tick:
        return await self._queue.get()

    async def scaled(self, tick: Tick, scale: float):
        return await self._hub.scaled(tick, scale)

    def close(self) -> None:
        self._hub.unsubscribe(self)
