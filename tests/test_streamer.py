"""FrameStreamer 的推流决策逻辑测试。

用假的订阅源驱动,不需要真实屏幕,也不碰网络——只验证"什么时候发关键帧、
什么时候发增量帧、什么时候干脆不发、用什么格式编码"这套策略。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import protocol
from common.adaptive import AdaptiveController
from common.protocol import QUALITY_PRESETS
from host.streamer import FrameStreamer
from tests.fakes import FakeCapture, ScriptedSubscription


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
            asyncio.get_running_loop().call_soon(self.streamer.on_frame_ack, decoded.seq)

    def kinds(self):
        return [kind for kind, _ in self.sent]

    def frames(self):
        return [msg for _, msg in self.sent]


async def run_streamer(streamer, *, until_sent=0, timeout=3.0, harness=None):
    task = asyncio.create_task(streamer.run())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while asyncio.get_running_loop().time() < deadline:
            if harness is not None and len(harness.sent) >= until_sent:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def build(script, *, auto_ack=True, mode="hd", codecs=("jpeg",)):
    harness = Harness(auto_ack=auto_ack)
    capture = FakeCapture()
    adaptive = AdaptiveController()
    # 固定画质档位,避免自适应改变分辨率而干扰"关键帧 vs 增量帧"的判定
    adaptive.set_mode(mode)
    sub = ScriptedSubscription(script)
    streamer = FrameStreamer(
        subscription=sub, capture=capture, adaptive=adaptive,
        executor=ThreadPoolExecutor(max_workers=1), send=harness.send,
        now_ms=lambda: 0, codecs=codecs,
    )
    harness.streamer = streamer
    return streamer, harness, capture


async def test_first_frame_is_keyframe():
    streamer, harness, capture = build([None])
    await run_streamer(streamer, until_sent=1, harness=harness)
    assert harness.kinds()[0] == protocol.MSG_VIDEO_FRAME
    assert capture.encode_calls >= 1


async def test_small_change_sends_delta_not_keyframe():
    """核心用例:首帧关键帧之后,小范围变化必须走增量帧。"""
    streamer, harness, capture = build([None, [(10, 10, 32, 32)]])
    await run_streamer(streamer, until_sent=4, harness=harness)

    kinds = harness.kinds()
    assert kinds[0] == protocol.MSG_VIDEO_FRAME, "首帧应为关键帧"
    assert protocol.MSG_VIDEO_DELTA in kinds[1:], f"后续帧应出现增量帧,实际为 {kinds}"
    assert capture.region_calls >= 1, "增量帧必须走 encode_region 而不是整屏编码"


async def test_static_screen_sends_nothing():
    streamer, harness, _ = build([None, []])
    await run_streamer(streamer, until_sent=99, timeout=1.0, harness=harness)
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
    assert got == rects  # scale=1.0 时坐标应原样保留


async def test_dirty_rects_accumulate_across_skipped_ticks():
    """等 ack 期间共享管线推进的脏区域必须累积,不能丢——否则那块区域
    会一直停留在旧画面上。"""
    streamer, harness, _ = build([None, [(0, 0, 16, 16)], [(200, 100, 16, 16)]], auto_ack=True)
    # 手动累积:模拟"还在等 ack 时又来了新的脏区域"
    streamer._pending_dirty.extend([(0, 0, 16, 16), (200, 100, 16, 16)])
    merged = sorted(set(streamer._pending_dirty))
    assert (0, 0, 16, 16) in merged and (200, 100, 16, 16) in merged


async def test_request_keyframe_forces_full_frame():
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]])
    await run_streamer(streamer, until_sent=3, harness=harness)

    streamer.request_keyframe()
    assert streamer._force_keyframe is True
    assert streamer._pending_dirty == []

    harness.sent.clear()
    await run_streamer(streamer, until_sent=1, harness=harness)
    assert harness.kinds()[0] == protocol.MSG_VIDEO_FRAME


async def test_ack_timeout_triggers_downgrade_and_keyframe():
    """不回 ack 时应触发超时降级,并且后续帧强制整帧
    (被丢掉的增量矩形无法重传,继续发增量会让画面一直残留脏区域)。"""
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]], auto_ack=False, mode="auto")
    await run_streamer(streamer, until_sent=3, timeout=3.0, harness=harness)

    assert streamer.adaptive.consecutive_timeouts > 0, "未收到 ack 应触发超时降级"
    kinds = harness.kinds()
    assert len(kinds) >= 2, f"应持续尝试发帧,实际只发了 {len(kinds)} 帧"
    assert all(k == protocol.MSG_VIDEO_FRAME for k in kinds), \
        f"持续超时的情况下不应发送增量帧,实际为 {kinds}"


# ---------------------------------------------------------------------------
# 编码格式选择
# ---------------------------------------------------------------------------

async def test_delta_uses_webp_when_client_supports_it():
    """增量帧的矩形很小,WebP 省 80% 以上字节而编码只多零点几毫秒,应无条件启用。"""
    streamer, harness, capture = build([None, [(1, 1, 8, 8)]], codecs=("webp", "jpeg"))
    await run_streamer(streamer, until_sent=3, harness=harness)
    deltas = [msg for kind, msg in harness.sent if kind == protocol.MSG_VIDEO_DELTA]
    assert deltas, "应有增量帧"
    assert deltas[0].fmt == protocol.FMT_WEBP


async def test_delta_falls_back_to_jpeg_for_old_browsers():
    streamer, harness, _ = build([None, [(1, 1, 8, 8)]], codecs=("jpeg",))
    await run_streamer(streamer, until_sent=3, harness=harness)
    deltas = [msg for kind, msg in harness.sent if kind == protocol.MSG_VIDEO_DELTA]
    assert deltas and deltas[0].fmt == protocol.FMT_JPEG


async def test_keyframe_prefers_jpeg_when_bandwidth_is_not_the_bottleneck():
    """带宽充裕时应优先保证低编码延迟(JPEG),而不是花 4 倍时间去省字节。"""
    streamer, harness, _ = build([None], codecs=("webp", "jpeg"))
    streamer.adaptive.on_throughput_sample(1000)
    streamer.adaptive.on_throughput_sample(100)  # 当前远低于峰值 -> 未饱和
    assert streamer.adaptive.is_bandwidth_saturated() is False
    assert streamer._keyframe_codec() == protocol.FMT_JPEG


async def test_keyframe_uses_webp_when_bandwidth_is_saturated():
    streamer, harness, _ = build([None], codecs=("webp", "jpeg"))
    streamer.adaptive.on_throughput_sample(500)
    streamer.adaptive.on_throughput_sample(500)  # 贴着峰值 -> 已饱和
    assert streamer.adaptive.is_bandwidth_saturated() is True
    assert streamer._keyframe_codec() == protocol.FMT_WEBP


async def test_keyframe_avoids_webp_when_cpu_is_the_bottleneck():
    """本机编码已经很吃力时,再换更慢的 WebP 只会让帧率进一步掉下去。"""
    streamer, harness, _ = build([None], codecs=("webp", "jpeg"))
    streamer.adaptive.on_throughput_sample(500)
    streamer.adaptive.on_throughput_sample(500)
    streamer.last_encode_ms = 200.0
    assert streamer._keyframe_codec() == protocol.FMT_JPEG


async def test_stats_reports_capture_encode_and_codec():
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]], codecs=("webp",))
    await run_streamer(streamer, until_sent=3, harness=harness)
    stats = streamer.stats()
    for key in ("actual_fps", "actual_kbps", "capture_ms", "encode_ms", "rects",
                "codec", "keyframes", "deltas", "skipped"):
        assert key in stats, f"统计信息缺少字段 {key}"
    assert stats["capture_ms"] > 0


async def test_stats_feed_throughput_back_into_adaptive():
    """吞吐观测必须回流给自适应控制器,否则带宽感知没有输入。"""
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]])
    await run_streamer(streamer, until_sent=4, harness=harness)
    streamer.stats()
    assert streamer.adaptive._peak_kbps is not None


@pytest.mark.parametrize("preset", QUALITY_PRESETS)
async def test_all_quality_presets_produce_frames(preset):
    streamer, harness, _ = build([None, [(1, 1, 4, 4)]], mode=preset)
    await run_streamer(streamer, until_sent=2, timeout=2.5, harness=harness)
    assert harness.sent, f"档位 {preset} 下应能正常出帧"


async def test_rects_are_mapped_into_scaled_coordinates():
    """脏矩形来自原始分辨率,必须映射到当前缩放后的输出坐标系,
    且映射时要向外取整,避免缩放后边缘留下没刷新的缝。"""
    streamer, harness, _ = build([None, [(100, 80, 40, 20)]], mode="smooth")  # scale=0.5
    await run_streamer(streamer, until_sent=3, harness=harness)
    deltas = [msg for kind, msg in harness.sent if kind == protocol.MSG_VIDEO_DELTA]
    assert deltas, "应有增量帧"
    x, y, w, h = deltas[0].rects[0][:4]
    assert x == 50 and y == 40, f"左上角应向下取整到 (50,40),实际 ({x},{y})"
    assert w >= 20 and h >= 10, f"宽高应向上取整以完整覆盖,实际 {w}x{h}"
