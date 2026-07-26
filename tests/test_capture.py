"""host/capture.py 的单元测试。

分两类:
  * 纯数组处理(encode_jpeg / encode_region / 参数校验)——不需要显示器,CI 必跑;
  * 真实采集(grab / list_monitors / set_monitor)——没有 DISPLAY 时自动跳过,
    本地可用 `Xvfb :95 -screen 0 1280x800x24 &` + `export DISPLAY=:95` 实测。
"""
from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PIL import Image

from host import capture as capture_mod
from host.capture import CapturedFrame, MonitorInfo, ScreenCapture, _clamp_quality, _make_label

HAS_DISPLAY = bool(os.environ.get("DISPLAY"))
requires_display = pytest.mark.skipif(not HAS_DISPLAY, reason="需要真实/虚拟屏幕(DISPLAY 未设置)")


def noise_image(h: int = 64, w: int = 96, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def jpeg_size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size  # (w, h)


# ----------------------------------------------------------------------
# 不需要显示器的部分
# ----------------------------------------------------------------------
def test_mss_is_imported_lazily():
    """模块顶层不能 import mss:无显示环境下 import 会失败,而其他模块要能安全 import 本模块。"""
    assert not hasattr(capture_mod, "mss")


def test_constructor_does_not_touch_display():
    """构造函数不应初始化 mss(否则无显示环境下连对象都建不出来)。"""
    cap = ScreenCapture(monitor_index=1)
    assert cap.monitor_index == 1
    assert cap._sct is None
    cap.close()  # 从未初始化过也要能安全关闭


def test_encode_jpeg_produces_valid_jpeg():
    cap = ScreenCapture()
    data = cap.encode_jpeg(noise_image(64, 96), 80)
    assert data[:2] == b"\xff\xd8"  # JPEG SOI
    assert jpeg_size(data) == (96, 64)


def test_encode_jpeg_quality_affects_size():
    cap = ScreenCapture()
    img = noise_image(128, 128, seed=7)
    small = cap.encode_jpeg(img, 10)
    large = cap.encode_jpeg(img, 95)
    assert len(small) < len(large)


def test_encode_jpeg_clamps_quality():
    """超出 [5, 95] 的质量参数要被 clamp,而不是把异常抛给调用方。"""
    cap = ScreenCapture()
    img = noise_image(32, 32)
    assert cap.encode_jpeg(img, 100000)[:2] == b"\xff\xd8"
    assert cap.encode_jpeg(img, -20)[:2] == b"\xff\xd8"
    assert _clamp_quality(0) == 5
    assert _clamp_quality(200) == 95
    assert _clamp_quality(50) == 50


def test_encode_jpeg_rejects_bad_shape():
    cap = ScreenCapture()
    with pytest.raises(ValueError):
        cap.encode_jpeg(np.zeros((10, 10), dtype=np.uint8), 60)
    with pytest.raises(ValueError):
        cap.encode_jpeg(np.zeros((10, 10, 4), dtype=np.uint8), 60)


def test_encode_region_crops_correctly():
    cap = ScreenCapture()
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    img[20:40, 50:90] = 255  # 一块白色区域

    data = cap.encode_region(img, (50, 20, 40, 20), 95)
    assert jpeg_size(data) == (40, 20)
    decoded = np.asarray(Image.open(io.BytesIO(data)))
    assert decoded.shape == (20, 40, 3)
    assert decoded.mean() > 200  # 裁到的确实是那块白色区域

    # 裁黑色区域做对照,确认坐标没有搞反 x/y
    dark = np.asarray(Image.open(io.BytesIO(cap.encode_region(img, (0, 0, 40, 20), 95))))
    assert dark.mean() < 50


def test_encode_region_clips_to_bounds():
    """略微越界的矩形要被裁到边界内,而不是崩溃或产生错位画面。"""
    cap = ScreenCapture()
    img = noise_image(50, 60)
    data = cap.encode_region(img, (40, 30, 100, 100), 70)
    assert jpeg_size(data) == (20, 20)  # 被裁成 (40..60, 30..50)


def test_encode_region_rejects_empty_rect():
    cap = ScreenCapture()
    img = noise_image(50, 60)
    with pytest.raises(ValueError):
        cap.encode_region(img, (100, 100, 10, 10), 70)  # 完全在图像外
    with pytest.raises(ValueError):
        cap.encode_region(img, (10, 10, 0, 10), 70)  # 宽度为 0


def test_encode_region_handles_non_contiguous_input():
    """脏矩形来自切片,底层可能不连续;编码前必须自行物化。"""
    cap = ScreenCapture()
    img = noise_image(80, 80)
    view = img[::2, ::2]  # 跨步视图
    data = cap.encode_region(view, (0, 0, 10, 10), 60)
    assert jpeg_size(data) == (10, 10)


def test_make_label():
    assert _make_label(0, 3840, 1080) == "全部显示器 (3840x1080)"
    assert _make_label(1, 1920, 1080) == "显示器 1 (1920x1080)"
    assert _make_label(2, 1280, 800) == "显示器 2 (1280x800)"


def test_dataclasses_have_contract_fields():
    info = MonitorInfo(index=1, left=0, top=0, width=1920, height=1080,
                       is_primary=True, label="显示器 1 (1920x1080)")
    assert (info.index, info.width, info.height, info.is_primary) == (1, 1920, 1080, True)
    frame = CapturedFrame(rgb=np.zeros((2, 2, 3), np.uint8), width=2, height=2, capture_ms=1.5)
    assert frame.rgb.shape == (2, 2, 3) and frame.capture_ms == 1.5


# ----------------------------------------------------------------------
# 需要真实屏幕的部分
# ----------------------------------------------------------------------
@pytest.fixture
def cap():
    c = ScreenCapture(monitor_index=1)
    yield c
    c.close()


@requires_display
def test_list_monitors(cap):
    monitors = cap.list_monitors()
    assert len(monitors) >= 2  # index 0(虚拟大屏) + 至少一个物理显示器
    assert monitors[0].index == 0
    assert monitors[0].label.startswith("全部显示器")
    assert monitors[0].is_primary is False
    assert monitors[1].is_primary is True
    assert monitors[1].label.startswith("显示器 1")
    for m in monitors:
        assert m.width > 0 and m.height > 0
        assert f"{m.width}x{m.height}" in m.label


@requires_display
def test_list_monitors_reuses_thread_local_instance(cap):
    """修正老实现的 bug:list_monitors 不应另外新建一个临时 mss 实例。"""
    cap.list_monitors()
    sct_before = cap._sct
    cap.list_monitors()
    assert cap._sct is sct_before


@requires_display
def test_screen_size_matches_monitor(cap):
    monitors = cap.list_monitors()
    assert cap.screen_size == (monitors[1].width, monitors[1].height)


@requires_display
def test_grab_full_scale(cap):
    w, h = cap.screen_size
    frame = cap.grab(scale=1.0)
    assert isinstance(frame, CapturedFrame)
    assert frame.rgb.shape == (h, w, 3)
    assert frame.rgb.dtype == np.uint8
    assert (frame.width, frame.height) == (w, h)
    assert frame.capture_ms > 0
    assert frame.rgb.flags["C_CONTIGUOUS"]


@requires_display
def test_grab_scaled(cap):
    w, h = cap.screen_size
    frame = cap.grab(scale=0.5)
    assert (frame.width, frame.height) == (round(w * 0.5), round(h * 0.5))
    assert frame.rgb.shape == (frame.height, frame.width, 3)


@requires_display
def test_grab_clamps_scale(cap):
    w, h = cap.screen_size
    assert cap.grab(scale=5.0).width == w  # >1 被 clamp 到 1
    tiny = cap.grab(scale=0.0)  # 0 被 clamp 到下限而不是产生 0 尺寸
    assert tiny.width > 0 and tiny.height > 0
    assert tiny.width < w and tiny.height < h


@requires_display
def test_consecutive_grabs_do_not_share_memory(cap):
    """两帧必须是各自独立的数组,否则 DirtyTracker 会永远"看不到变化"。"""
    a = cap.grab(scale=1.0)
    b = cap.grab(scale=1.0)
    assert not np.shares_memory(a.rgb, b.rgb)


@requires_display
def test_grab_then_encode_roundtrip(cap):
    frame = cap.grab(scale=0.5)
    data = cap.encode_jpeg(frame.rgb, 70)
    assert jpeg_size(data) == (frame.width, frame.height)

    region = cap.encode_region(frame.rgb, (0, 0, 32, 16), 70)
    assert jpeg_size(region) == (32, 16)


@requires_display
def test_set_monitor(cap):
    monitors = cap.list_monitors()
    assert cap.set_monitor(0) is True
    assert cap.monitor_index == 0
    assert cap.screen_size == (monitors[0].width, monitors[0].height)  # 立刻反映新尺寸
    assert cap.grab(scale=1.0).width == monitors[0].width

    assert cap.set_monitor(1) is True
    assert cap.screen_size == (monitors[1].width, monitors[1].height)


@requires_display
def test_set_monitor_invalid_index_returns_false(cap):
    before = cap.monitor_index
    assert cap.set_monitor(99) is False
    assert cap.set_monitor(-1) is False
    assert cap.monitor_index == before  # 失败时保持原样,且不抛异常
    assert cap.grab(scale=1.0).width > 0  # 仍然可用


@requires_display
def test_works_across_threads(cap):
    """线程亲和性:换线程调用时应重建 mss 实例而不是报错。

    (host/server.py 用单线程 executor,但重连/重启 executor 时线程会变。)
    """
    cap.grab(scale=1.0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        frame = pool.submit(lambda: cap.grab(scale=1.0)).result()
        assert frame.width > 0
        pool.submit(cap.close).result()


@requires_display
def test_static_screen_produces_no_dirty_rects(cap):
    """与 DirtyTracker 的联调:静止画面第二帧应当无需发送任何数据。"""
    from host.delta import DirtyTracker

    tracker = DirtyTracker()
    assert tracker.compute(cap.grab(scale=0.5).rgb) is None  # 首帧 -> 关键帧
    assert tracker.compute(cap.grab(scale=0.5).rgb) == []    # 静止 -> 无脏矩形
