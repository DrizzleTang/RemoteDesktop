"""FrameStreamer 的推流决策逻辑测试。

用假的采集器/差分器驱动,不需要真实屏幕,也不碰网络——只验证
"什么时候发关键帧、什么时候发增量帧、什么时候干脆不发"这套策略。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from common import protocol
from common.adaptive import AdaptiveController
from common.protocol import QUALITY_PRESETS
from host.capture import CapturedFrame
from host.streamer import FrameStreamer


class FakeCapture:
    """按脚本产出画面,并记录编码调用次数。"""

    def __init__(self, width=320, height=240):
        self.width = width
        self.height = height
        self.jpeg_calls = 0
        self.region_calls = 0
        self.grab_count = 0

    def grab(self, *, scale):
        self.grab_count += 1
        out_w = max(1, round(self.width * scale))
        out_h = max(1, round(self.height * scale))
        rgb = np.zeros((out_h, out_w, 3), dtype=np.uint8)
        return CapturedFrame(rgb=rgb, width=out_w, height=out_h, capture_ms=1.0)

    def encode_jpeg(self, rgb, quality):
        self.jpeg_calls += 1
        return b"FULLJPEG"

    def encode_region(self, rgb, rect, quality):
        self.region_calls += 1
        return b"RECTJPEG"


class ScriptedTracker:
    """按预设脚本返回脏矩形结果;脚本用尽后一直重复最后一项。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.resets = 0

    def compute(self, frame):
        self.calls += 1
        idx = min(self.calls - 1, len(self.script) - 1)
        return self.script[idx]

    def reset(self):
        self.resets += 1


class Harness:
    """收集 streamer 发出的所有帧,并可选地自动回 ack。"""

    def __init__(self, auto_ack=True):
        self.sent = []
        self.auto_ack = auto_ack
        self.streamer = None

    def send(self, payload, priority):
        kind, decoded = protocol.decode_plaintext(payload)
        self.sent.append((kind, decoded))
        if self.auto_ack and self.streamer is not None:
            # 模拟对端立刻确认(用 call_soon 保证在下一轮事件循环里送达,
            # 更接近真实的异步时序)
            asyncio.get_event_loop().call_soon(self.streamer.on_frame_ack, decoded.seq)

    def kinds(self):
        return [kind for kind, _ in self.sent]


async def run_streamer(streamer, *, until_sent=0, timeout=3.0, harness=None):
    """跑 streamer 直到收集够指定数量的帧(或超时)。"""
    task = asyncio.create_task(streamer.run())
    deadline = asyncio.get_event_loop().time() + timeout
    try:
        while asyncio.get_event_loop().time() < deadline:
            if harness is not None and len(harness.sent) >= until_sent:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def build(script, *, auto_ack=True, mode="hd"):
    harness = Harness(auto_ack=auto_ack)
    capture = FakeCapture()
    adaptive = AdaptiveController()
    # 固定画质档位,避免自适应改变分辨率而干扰"关键帧 vs 增量帧"的判定
    adaptive.set_mode(mode)
    streamer = FrameStreamer(
        capture=capture, tracker=ScriptedTracker(script), adaptive=adaptive,
        executor=ThreadPoolExecutor(max_workers=1), send=harness.send,
        now_ms=lambda: 0,
    )
    harness.streamer = streamer
    return streamer, harness, capture


async def test_first_frame_is_keyframe():
    streamer, harness, capture = build([None])
    await run_streamer(streamer, until_sent=1, harness=harness)
    assert harness.kinds()[0] == protocol.MSG_VIDEO_FRAME
    assert capture.jpeg_calls >= 1


async def test_small_change_sends_delta_not_keyframe():
    """核心用例:首帧关键帧之后,小范围变化必须走增量帧。"""
    streamer, harness, capture = build([None, [(10, 10, 32, 32)]])
    await run_streamer(streamer, until_sent=4, harness=harness)

    kinds = harness.kinds()
    assert kinds[0] == protocol.MSG_VIDEO_FRAME, "首帧应为关键帧"
    assert len(kinds) >= 2, f"应该持续发帧,实际只发了 {len(kinds)} 帧"
    assert protocol.MSG_VIDEO_DELTA in kinds[1:], f"后续帧应出现增量帧,实际为 {kinds}"
    assert capture.region_calls >= 1, "增量帧必须走 encode_region 而不是整屏编码"


async def test_static_screen_sends_nothing():
    streamer, harness, _ = build([None, []])
    await run_streamer(streamer, until_sent=99, timeout=1.0, harness=harness)
    # 只有首帧那一个关键帧,之后画面静止就不再发送任何数据
    assert len(harness.sent) == 1, f"静止画面不应继续发送,实际发了 {len(harness.sent)} 帧"
    assert streamer.skipped_count > 0


async def test_tracker_none_forces_keyframe():
    streamer, harness, _ = build([None, None, None])
    await run_streamer(streamer, until_sent=3, harness=harness)
    assert set(harness.kinds()) == {protocol.MSG_VIDEO_FRAME}


async def test_delta_rect_payload_roundtrips():
    rects = [(4, 8, 16, 32), (100, 50, 8, 8)]
    streamer, harness, _ = build([None, rects])
    await run_streamer(streamer, until_sent=3, harness=harness)

    deltas = [msg for kind, msg in harness.sent if kind == protocol.MSG_VIDEO_DELTA]
    assert deltas, "应至少有一个增量帧"
    got = [(r[0], r[1], r[2], r[3]) for r in deltas[0].rects]
    assert got == rects
    assert all(r[4] == b"RECTJPEG" for r in deltas[0].rects)


async def test_request_keyframe_resets_tracker_and_forces_full_frame():
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]])
    await run_streamer(streamer, until_sent=3, harness=harness)
    tracker = streamer.tracker

    before = tracker.resets
    streamer.request_keyframe()
    assert tracker.resets == before + 1
    assert streamer._force_keyframe is True

    harness.sent.clear()
    await run_streamer(streamer, until_sent=1, harness=harness)
    assert harness.kinds()[0] == protocol.MSG_VIDEO_FRAME


async def test_ack_timeout_triggers_downgrade_and_keyframe():
    """不回 ack 时应触发超时降级,并且下一帧强制整帧
    (被丢掉的增量矩形无法重传,继续发增量会让画面一直残留脏区域)。"""
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]], auto_ack=False, mode="auto")
    await run_streamer(streamer, until_sent=3, timeout=3.0, harness=harness)

    assert streamer.adaptive.consecutive_timeouts > 0, "未收到 ack 应触发超时降级"
    # 即使差分器一直报告"只有一小块变化",由于每帧都超时(增量矩形已丢失且
    # 无法重传),后续帧必须全部是关键帧,而不能继续发增量。
    kinds = harness.kinds()
    assert len(kinds) >= 2, f"应持续尝试发帧,实际只发了 {len(kinds)} 帧"
    assert all(k == protocol.MSG_VIDEO_FRAME for k in kinds), \
        f"持续超时的情况下不应发送增量帧,实际为 {kinds}"


async def test_stats_reports_capture_and_encode_time():
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]])
    await run_streamer(streamer, until_sent=3, harness=harness)
    stats = streamer.stats()
    for key in ("actual_fps", "actual_kbps", "capture_ms", "encode_ms", "rects",
                "keyframes", "deltas", "skipped"):
        assert key in stats, f"统计信息缺少字段 {key}"
    assert stats["capture_ms"] > 0


@pytest.mark.parametrize("preset", QUALITY_PRESETS)
async def test_all_quality_presets_produce_frames(preset):
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]], mode=preset)
    await run_streamer(streamer, until_sent=2, timeout=2.5, harness=harness)
    assert harness.sent, f"档位 {preset} 下应能正常出帧"
