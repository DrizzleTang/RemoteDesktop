"""
画面推流器:把"采集 -> 差分 -> 编码 -> 发送 -> 等确认"这一整套流程从
Session 里抽出来,单独成类。

这样拆分的好处:
- Session 只负责连接生命周期与消息分发,推流的策略逻辑集中在这里;
- 推流逻辑可以脱离 WebSocket 单独测试(构造函数注入 capture / 发送回调,
  测试里传假对象即可)。

推流策略(本项目"弱网下稳定可用"这一目标的核心实现):

1. **一帧一确认的窗口流控**:发出一帧后必须等到对端的 frame_ack(或超时)
   才发下一帧。在途数据量因此始终有界,网络越差自然发得越慢,而不是把
   数据堆在缓冲区里越积越多(bufferbloat)——后者正是"画面越来越延迟、
   鼠标越来越不跟手"的根因。
2. **增量编码**:每帧先与上一帧做分块差分,只编码发生变化的矩形区域。
   远程桌面的画面绝大多数时间只有一小块在变(光标、正在输入的输入框),
   这一项对带宽和 CPU 的节省通常比降低画质更显著。
3. **画面完全静止时不发送任何数据**,并逐步放慢采集轮询频率以省 CPU;
   一旦检测到变化立刻恢复。
4. **周期性关键帧**:即使一直是增量更新,也每隔一段时间强制发一个整帧,
   用于纠正任何可能的累积误差(例如某个矩形因为拥塞被丢弃)。
5. 大面积变化时由 DirtyTracker 判定改发关键帧(整帧编码一次通常比编码
   几十个矩形更划算)。
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
IDLE_POLL_MIN_S = 0.05  # 画面静止时最快的轮询间隔
IDLE_POLL_MAX_S = 0.30  # 持续静止一段时间后放慢到的轮询间隔
IDLE_BACKOFF_AFTER = 10  # 连续多少次"无变化"后开始放慢轮询
STATS_WINDOW_S = 5.0


class FrameStreamer:
    def __init__(self, *, capture, tracker, adaptive: AdaptiveController,
                 executor, send, now_ms):
        """
        capture:  host.capture.ScreenCapture 实例
        tracker:  host.delta.DirtyTracker 实例
        adaptive: 自适应画质控制器
        executor: 用于跑阻塞的采集/编码调用的线程池(必须是单线程,以满足
                  mss 的线程亲和性要求)
        send:     回调 send(payload_bytes, priority) -> None,把待发送数据
                  丢进发送队列(本类不直接接触 WebSocket)
        now_ms:   回调,返回会话相对时间戳(毫秒)
        """
        self.capture = capture
        self.tracker = tracker
        self.adaptive = adaptive
        self.executor = executor
        self._send = send
        self._now_ms = now_ms

        self._loop = asyncio.get_running_loop()
        self._seq = 0
        self._pending_seq: int | None = None
        self._sent_at = 0.0
        self._ack_event = asyncio.Event()
        self._force_keyframe = True
        self._last_keyframe_at = 0.0
        self._idle_streak = 0

        # 统计
        self._recent: list[tuple[float, int]] = []  # (发送时刻, 字节数)
        self.last_capture_ms = 0.0
        self.last_encode_ms = 0.0
        self.last_rect_count = 0
        self.keyframe_count = 0
        self.delta_count = 0
        self.skipped_count = 0

    # ------------------------------------------------------------------
    # 外部事件
    # ------------------------------------------------------------------

    def on_frame_ack(self, seq: int) -> None:
        """收到对端对某一帧的确认。"""
        if seq != self._pending_seq:
            return  # 过期或重复的 ack,忽略
        latency_ms = max(0.0, (time.monotonic() - self._sent_at) * 1000)
        self.adaptive.on_frame_ack(latency_ms)
        self._pending_seq = None
        self._ack_event.set()

    def request_keyframe(self) -> None:
        """要求下一帧强制发送整幅画面(切换显示器/画质突变后调用)。"""
        self._force_keyframe = True
        self.tracker.reset()

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
            cycle_start = time.monotonic()
            params = self.adaptive.current_params()

            try:
                captured = await self._run_blocking(self.capture.grab, scale=params["scale"])
            except Exception:  # noqa: BLE001 - 采集失败不应终止会话
                logger.exception("屏幕采集失败,1 秒后重试")
                await asyncio.sleep(1.0)
                continue

            self.last_capture_ms = captured.capture_ms
            dirty = self.tracker.compute(captured.rgb)

            if dirty is None:
                # 首帧 / 尺寸变化 / 大面积变化 -> 必须整帧
                await self._send_keyframe(captured, params)
            elif not dirty:
                # 画面完全没变:什么都不发,并逐步放慢轮询以省 CPU
                self.skipped_count += 1
                self._idle_streak += 1
                await asyncio.sleep(self._idle_sleep())
                continue
            elif self._should_send_keyframe():
                await self._send_keyframe(captured, params)
            else:
                await self._send_delta(captured, params, dirty)

            self._idle_streak = 0
            await self._await_ack()
            await self._pace(cycle_start, params)

    def _idle_sleep(self) -> float:
        if self._idle_streak <= IDLE_BACKOFF_AFTER:
            return IDLE_POLL_MIN_S
        # 静止越久,轮询越慢(线性退避到上限),一旦有变化会立刻恢复
        extra = (self._idle_streak - IDLE_BACKOFF_AFTER) * 0.02
        return min(IDLE_POLL_MAX_S, IDLE_POLL_MIN_S + extra)

    def _should_send_keyframe(self) -> bool:
        if self._force_keyframe:
            return True
        return (time.monotonic() - self._last_keyframe_at) > KEYFRAME_INTERVAL_S

    async def _send_keyframe(self, captured, params) -> None:
        quality = int(params["jpeg_quality"])
        t0 = time.perf_counter()
        image_bytes = await self._run_blocking(self.capture.encode_jpeg, captured.rgb, quality)
        self.last_encode_ms = (time.perf_counter() - t0) * 1000
        self.last_rect_count = 1
        self.keyframe_count += 1
        self._force_keyframe = False
        self._last_keyframe_at = time.monotonic()

        self._seq += 1
        payload = protocol.encode_video_frame(
            seq=self._seq, ts_ms=self._now_ms(), width=captured.width, height=captured.height,
            quality=quality, fmt=protocol.FMT_JPEG, keyframe=True, image_bytes=image_bytes,
        )
        self._dispatch(payload, len(image_bytes))

    async def _send_delta(self, captured, params, dirty) -> None:
        quality = int(params["jpeg_quality"])
        t0 = time.perf_counter()
        rects = await self._run_blocking(self._encode_rects, captured.rgb, dirty, quality)
        self.last_encode_ms = (time.perf_counter() - t0) * 1000
        self.last_rect_count = len(rects)
        self.delta_count += 1

        self._seq += 1
        payload = protocol.encode_video_delta(
            seq=self._seq, ts_ms=self._now_ms(), width=captured.width, height=captured.height,
            quality=quality, fmt=protocol.FMT_JPEG, rects=rects,
        )
        self._dispatch(payload, sum(len(r[4]) for r in rects))

    def _encode_rects(self, rgb, dirty, quality):
        """在线程池里批量编码所有脏矩形(一次 run_in_executor 调用完成,
        避免为每个矩形都付一次线程调度开销)。"""
        out = []
        for (x, y, w, h) in dirty:
            out.append((x, y, w, h, self.capture.encode_region(rgb, (x, y, w, h), quality)))
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
                # 这条日志是排查弱网问题的关键线索:大量超时说明对端确认跟不上,
                # 会持续触发降级与整帧重传。
                logger.debug("第 %d 帧确认超时(%.0f ms),降级并强制下一帧为整帧", seq, timeout * 1000)

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
        return {
            "actual_fps": round(fps, 1),
            "actual_kbps": round(kbps, 1),
            "capture_ms": round(self.last_capture_ms, 1),
            "encode_ms": round(self.last_encode_ms, 1),
            "rects": self.last_rect_count,
            "keyframes": self.keyframe_count,
            "deltas": self.delta_count,
            "skipped": self.skipped_count,
        }
