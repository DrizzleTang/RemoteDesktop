"""
画面推流器:决定"什么时候发、发整帧还是发变化区域、用什么格式编码"。

采集与差分由共享管线 host/hub.py 负责(多个观看者只做一次);本类只负责
**单个会话**的推流策略与流控,因此各会话之间互不影响——一个观看者网络差
不会拖慢其他人。

推流策略(本项目"弱网下稳定可用"这一目标的核心实现):

1. **一帧一确认的窗口流控**:发出一帧后必须等到对端的 frame_ack(或超时)
   才发下一帧。在途数据量因此始终有界,网络越差自然发得越慢,而不是把
   数据堆在缓冲区里越积越多(bufferbloat)——后者正是"画面越来越延迟、
   鼠标越来越不跟手"的根因。
2. **增量编码**:只编码发生变化的矩形区域。远程桌面的画面绝大多数时间
   只有一小块在变,这一项对带宽和 CPU 的节省通常比降低画质更显著。
3. **跳过的帧要累积脏区域**:等 ack 期间共享管线可能已经推进了好几拍,
   这些变化不能丢——否则那块区域会一直保持旧画面。所以把期间所有脏矩形
   合并起来,下次一起发。
4. **画面完全静止时不发送任何数据**。
5. **周期性关键帧**:纠正任何可能的累积误差(例如某个矩形因拥塞被丢弃)。
6. **编码格式动态选择**:小矩形一律用 WebP(实测比 JPEG 省 80% 以上,因为
   JPEG 每张图有约 600 字节固定头部,小图上头部比数据还大);整幅关键帧则
   看瓶颈——网络吃紧时用 WebP 省带宽,本机 CPU 吃紧时用 JPEG 省编码时间。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time

from common import protocol
from common.adaptive import AdaptiveController
from host.sendqueue import PRIORITY_VIDEO

logger = logging.getLogger("host.streamer")

FRAME_ACK_BASE_TIMEOUT_S = 0.35
FRAME_ACK_MAX_TIMEOUT_S = 2.5
KEYFRAME_INTERVAL_S = 10.0
STATS_WINDOW_S = 5.0
# 关键帧用 WebP 时的额外编码开销大致是 JPEG 的 3-4 倍(实测 1920x1080:
# JPEG 约 7ms,WebP 最快档约 29ms)。只有当"省下来的传输时间"明显超过
# "多花的编码时间"时才值得,所以要求本机编码耗时占比不高。
KEYFRAME_WEBP_MAX_ENCODE_MS = 45.0


def _merge_rects(rects: list[tuple[int, int, int, int]],
                 limit: int) -> list[tuple[int, int, int, int]] | None:
    """把累积的脏矩形去重/合并。矩形数超过 limit 时返回 None(改发关键帧)。

    这里只做"完全重复去重"和"数量兜底",不追求最优合并:共享管线给出的
    矩形本身已经是按瓦片合并过的,跨拍累积主要产生的是重复项。
    """
    if not rects:
        return []
    unique = list(dict.fromkeys(rects))
    if len(unique) > limit:
        return None
    return unique


class FrameStreamer:
    def __init__(self, *, subscription, capture, adaptive: AdaptiveController,
                 executor, send, now_ms, codecs=("jpeg",)):
        """
        subscription: host.hub 的订阅句柄,提供 tick 与缩放后的画面
        capture:      仅用于调用编码方法(采集由共享管线负责)
        adaptive:     自适应画质控制器
        executor:     跑阻塞编码调用的线程池
        send:         回调 send(payload_bytes, priority)
        now_ms:       回调,返回会话相对时间戳(毫秒)
        codecs:       对端声明支持的编码格式名列表,例如 ("webp", "jpeg")
        """
        self.sub = subscription
        self.capture = capture
        self.adaptive = adaptive
        self.executor = executor
        self._send = send
        self._now_ms = now_ms
        self.supports_webp = "webp" in codecs

        self._loop = asyncio.get_running_loop()
        self._seq = 0
        self._pending_seq: int | None = None
        self._sent_at = 0.0
        self._ack_event = asyncio.Event()
        self._force_keyframe = True
        self._last_keyframe_at = 0.0
        self._pending_dirty: list[tuple[int, int, int, int]] = []

        # 统计
        self._recent: list[tuple[float, int]] = []  # (发送时刻, 字节数)
        self.last_capture_ms = 0.0
        self.last_encode_ms = 0.0
        self.last_rect_count = 0
        self.last_codec = "jpeg"
        self.keyframe_count = 0
        self.delta_count = 0
        self.skipped_count = 0

    # ------------------------------------------------------------------
    # 外部事件
    # ------------------------------------------------------------------

    def on_frame_ack(self, seq: int) -> None:
        if seq != self._pending_seq:
            return  # 过期或重复的 ack,忽略
        latency_ms = max(0.0, (time.monotonic() - self._sent_at) * 1000)
        self.adaptive.on_frame_ack(latency_ms)
        self._pending_seq = None
        self._ack_event.set()

    def request_keyframe(self) -> None:
        """要求下一帧强制发送整幅画面(切换显示器/重连后调用)。"""
        self._force_keyframe = True
        self._pending_dirty.clear()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _ack_timeout(self) -> float:
        ewma = self.adaptive.ewma_latency_ms
        if ewma is None:
            return FRAME_ACK_BASE_TIMEOUT_S
        return min(FRAME_ACK_MAX_TIMEOUT_S, max(FRAME_ACK_BASE_TIMEOUT_S, (ewma / 1000) * 3))

    async def _run_blocking(self, fn, *args, **kwargs):
        return await self._loop.run_in_executor(self.executor, functools.partial(fn, *args, **kwargs))

    async def run(self) -> None:
        while True:
            params = self.adaptive.current_params()
            self.sub.desired_fps = max(1.0, params["max_fps"])

            tick = await self.sub.next_tick()
            self.last_capture_ms = tick.capture_ms

            if tick.dirty is None:
                self._force_keyframe = True
                self._pending_dirty.clear()
            else:
                self._pending_dirty.extend(tick.dirty)

            if not self._force_keyframe and not self._pending_dirty:
                self.skipped_count += 1  # 画面完全没变,什么都不发
                continue

            cycle_start = time.monotonic()
            if self._force_keyframe or self._keyframe_due():
                await self._send_keyframe(tick, params)
            else:
                sent = await self._send_delta(tick, params)
                if not sent:
                    continue

            await self._await_ack()
            await self._pace(cycle_start, params)

    def _keyframe_due(self) -> bool:
        return (time.monotonic() - self._last_keyframe_at) > KEYFRAME_INTERVAL_S

    # ------------------------------------------------------------------
    # 编码格式选择
    # ------------------------------------------------------------------

    def _delta_codec(self) -> int:
        # 增量帧的矩形通常很小,WebP 在这里是纯赚:省 80% 以上字节,
        # 编码只多零点几毫秒。
        return protocol.FMT_WEBP if self.supports_webp else protocol.FMT_JPEG

    def _keyframe_codec(self) -> int:
        """整幅关键帧按"瓶颈在哪"选格式。"""
        if not self.supports_webp:
            return protocol.FMT_JPEG
        # 本机编码已经很吃力时(慢机器/高分辨率),再换更慢的 WebP 只会
        # 让帧率进一步掉下去,反而不如快速发一张略大的 JPEG。
        if self.last_encode_ms > KEYFRAME_WEBP_MAX_ENCODE_MS:
            return protocol.FMT_JPEG
        # 带宽已被打满 -> 字节数是瓶颈,WebP 划算;
        # 带宽充裕(或还判断不出来)-> 优先保证低编码延迟,用 JPEG。
        return protocol.FMT_WEBP if self.adaptive.is_bandwidth_saturated() else protocol.FMT_JPEG

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def _send_keyframe(self, tick, params) -> None:
        quality = int(params["jpeg_quality"])
        fmt = self._keyframe_codec()
        rgb, width, height = await self.sub.scaled(tick, params["scale"])

        t0 = time.perf_counter()
        image_bytes = await self._run_blocking(self.capture.encode_image, rgb, quality, fmt)
        self.last_encode_ms = (time.perf_counter() - t0) * 1000
        self.last_rect_count = 1
        self.last_codec = protocol.CODEC_NAMES[fmt]
        self.keyframe_count += 1
        self._force_keyframe = False
        self._last_keyframe_at = time.monotonic()
        self._pending_dirty.clear()

        self._seq += 1
        payload = protocol.encode_video_frame(
            seq=self._seq, ts_ms=self._now_ms(), width=width, height=height,
            quality=quality, fmt=fmt, keyframe=True, image_bytes=image_bytes,
        )
        self._dispatch(payload, len(image_bytes))

    async def _send_delta(self, tick, params) -> bool:
        merged = _merge_rects(self._pending_dirty, protocol.MAX_RECTS_PER_FRAME)
        if merged is None:
            # 变化区域太碎太多,整帧反而更划算
            await self._send_keyframe(tick, params)
            return True
        if not merged:
            return False

        quality = int(params["jpeg_quality"])
        fmt = self._delta_codec()
        rgb, width, height = await self.sub.scaled(tick, params["scale"])
        scale_x = width / tick.full_w
        scale_y = height / tick.full_h

        t0 = time.perf_counter()
        rects = await self._run_blocking(
            self._encode_rects, rgb, merged, quality, fmt, scale_x, scale_y, width, height
        )
        self.last_encode_ms = (time.perf_counter() - t0) * 1000
        if not rects:
            self._pending_dirty.clear()
            return False
        self.last_rect_count = len(rects)
        self.last_codec = protocol.CODEC_NAMES[fmt]
        self.delta_count += 1
        self._pending_dirty.clear()

        self._seq += 1
        payload = protocol.encode_video_delta(
            seq=self._seq, ts_ms=self._now_ms(), width=width, height=height,
            quality=quality, fmt=fmt, rects=rects,
        )
        self._dispatch(payload, sum(len(r[4]) for r in rects))
        return True

    def _encode_rects(self, rgb, dirty, quality, fmt, scale_x, scale_y, width, height):
        """把原始分辨率下的脏矩形映射到输出坐标系并逐个编码。

        映射时左上角向下取整、右下角向上取整,保证缩放后的矩形完整覆盖
        变化区域——否则边缘会留下一条没刷新的缝。
        """
        import math

        out = []
        for (x, y, w, h) in dirty:
            x0 = max(0, min(width, int(math.floor(x * scale_x))))
            y0 = max(0, min(height, int(math.floor(y * scale_y))))
            x1 = max(x0, min(width, int(math.ceil((x + w) * scale_x))))
            y1 = max(y0, min(height, int(math.ceil((y + h) * scale_y))))
            rw, rh = x1 - x0, y1 - y0
            if rw <= 0 or rh <= 0:
                continue
            out.append((x0, y0, rw, rh,
                        self.capture.encode_region(rgb, (x0, y0, rw, rh), quality, fmt)))
        return out

    def _dispatch(self, payload: bytes, image_bytes_len: int) -> None:
        self._pending_seq = self._seq
        self._ack_event.clear()
        self._sent_at = time.monotonic()
        self._send(payload, PRIORITY_VIDEO)

        now = time.monotonic()
        self._recent.append((now, image_bytes_len))
        cutoff = now - STATS_WINDOW_S
        if len(self._recent) > 512 or (self._recent and self._recent[0][0] < cutoff):
            self._recent = [item for item in self._recent if item[0] >= cutoff]

    async def _await_ack(self) -> None:
        seq = self._pending_seq
        timeout = self._ack_timeout()
        try:
            await asyncio.wait_for(self._ack_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if self._pending_seq == seq:
                self._pending_seq = None
                self.adaptive.on_frame_timeout()
                # 拥塞导致丢帧后,下一帧强制整帧:被丢掉的增量矩形无法重传,
                # 若继续发增量,客户端画面会一直带着这块"脏"区域不刷新。
                self._force_keyframe = True
                logger.debug("第 %s 帧确认超时(%.0f ms),降级并强制下一帧为整帧", seq, timeout * 1000)

    async def _pace(self, cycle_start: float, params) -> None:
        target_fps = max(1.0, params["max_fps"])
        min_interval = 1.0 / target_fps
        elapsed = time.monotonic() - cycle_start
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def throughput(self) -> tuple[float, float]:
        now = time.monotonic()
        recent = [item for item in self._recent if item[0] >= now - STATS_WINDOW_S]
        if len(recent) < 2:
            return 0.0, 0.0
        span = max(0.5, now - recent[0][0])
        fps = len(recent) / span
        kbps = sum(size for _, size in recent) * 8 / 1000 / span
        return fps, kbps

    def stats(self) -> dict:
        fps, kbps = self.throughput()
        if kbps > 0:
            # 把实测吞吐喂回自适应控制器,用于区分"带宽打满"与"链路本身慢"
            self.adaptive.on_throughput_sample(kbps)
        return {
            "actual_fps": round(fps, 1),
            "actual_kbps": round(kbps, 1),
            "capture_ms": round(self.last_capture_ms, 1),
            "encode_ms": round(self.last_encode_ms, 1),
            "rects": self.last_rect_count,
            "codec": self.last_codec,
            "keyframes": self.keyframe_count,
            "deltas": self.delta_count,
            "skipped": self.skipped_count,
        }
