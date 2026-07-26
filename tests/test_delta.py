"""host/delta.py(脏矩形检测)的单元测试。

纯 numpy 逻辑,不需要显示器,CI 上必跑。
"""
from __future__ import annotations

import numpy as np
import pytest

from common.protocol import MAX_RECTS_PER_FRAME
from host.delta import DEFAULT_TILE_SIZE, DirtyTracker


def make_frame(h: int, w: int, value: int = 0) -> np.ndarray:
    """构造一张纯色的 (h, w, 3) uint8 图。"""
    return np.full((h, w, 3), value, dtype=np.uint8)


def rect_covers(rects, x: int, y: int, w: int = 1, h: int = 1) -> bool:
    """给定像素区域是否被某个返回矩形完全覆盖。"""
    return any(
        rx <= x and ry <= y and rx + rw >= x + w and ry + rh >= y + h
        for rx, ry, rw, rh in rects
    )


def covered_mask(rects, h: int, w: int) -> np.ndarray:
    """把矩形列表画成布尔覆盖图,便于断言"变化的像素都被覆盖了"。"""
    mask = np.zeros((h, w), dtype=bool)
    for x, y, rw, rh in rects:
        mask[y:y + rh, x:x + rw] = True
    return mask


# ----------------------------------------------------------------------
# 基础行为
# ----------------------------------------------------------------------
def test_first_frame_returns_none():
    tracker = DirtyTracker()
    assert tracker.compute(make_frame(128, 128)) is None


def test_identical_frame_returns_empty_list():
    tracker = DirtyTracker()
    frame = make_frame(128, 128, 10)
    assert tracker.compute(frame) is None  # 首帧
    result = tracker.compute(make_frame(128, 128, 10))
    assert result == []


def test_single_small_change_locates_rect():
    tracker = DirtyTracker()
    base = make_frame(256, 256, 0)
    tracker.compute(base)

    changed = base.copy()
    changed[70:75, 80:90] = 255  # 落在瓦片行1/列1 附近的一小块
    rects = tracker.compute(changed)

    assert rects is not None and len(rects) == 1
    x, y, w, h = rects[0]
    # 返回的矩形必须完整覆盖被改动的像素
    assert rect_covers(rects, 80, 70, 10, 5)
    # 且不应该是整屏(证明确实做了局部检测)
    assert w < 256 and h < 256
    # 瓦片对齐:64 的瓦片下,(80,70) 落在第 1 行第 1 列瓦片
    assert (x, y, w, h) == (64, 64, 64, 64)


def test_two_distant_changes_return_two_rects():
    tracker = DirtyTracker()
    base = make_frame(512, 512, 0)
    tracker.compute(base)

    changed = base.copy()
    changed[10:20, 10:20] = 255      # 左上角
    changed[400:410, 400:410] = 255  # 右下角
    rects = tracker.compute(changed)

    assert rects is not None
    assert len(rects) == 2
    assert rect_covers(rects, 10, 10, 10, 10)
    assert rect_covers(rects, 400, 400, 10, 10)
    # 两个矩形不应该被合并成一个覆盖整屏的大块
    assert all(w <= 64 and h <= 64 for _, _, w, h in rects)


def test_change_of_one_pixel_is_detected():
    tracker = DirtyTracker()
    base = make_frame(128, 128, 7)
    tracker.compute(base)
    changed = base.copy()
    changed[100, 3, 1] = 8  # 只改一个像素的一个通道
    rects = tracker.compute(changed)
    assert rects is not None
    assert rect_covers(rects, 3, 100)


def test_state_updates_after_each_compute():
    """compute 之后内部参考帧必须更新:同样的画面第二次应返回 []。"""
    tracker = DirtyTracker()
    base = make_frame(128, 128, 0)
    tracker.compute(base)
    changed = base.copy()
    changed[0:10, 0:10] = 255
    assert tracker.compute(changed)  # 有变化
    assert tracker.compute(changed.copy()) == []  # 再次同样画面 -> 无变化


def test_tracker_is_immune_to_caller_mutating_the_buffer():
    """调用方复用同一个缓冲区(mss 的 raw buffer 就是这样)也不能影响检测结果。"""
    tracker = DirtyTracker()
    buf = make_frame(128, 128, 0)
    tracker.compute(buf)
    buf[0:10, 0:10] = 255  # 原地修改上一帧传进去的那块内存
    rects = tracker.compute(buf)
    assert rects is not None
    assert rect_covers(rects, 0, 0, 10, 10)


# ----------------------------------------------------------------------
# 关键帧触发条件
# ----------------------------------------------------------------------
def test_full_screen_change_triggers_keyframe():
    tracker = DirtyTracker()
    tracker.compute(make_frame(256, 256, 0))
    assert tracker.compute(make_frame(256, 256, 255)) is None


def test_ratio_threshold_is_configurable():
    """把阈值调到很高时,整屏变化就不再触发关键帧,而是合并成 1 个大矩形。"""
    tracker = DirtyTracker(keyframe_ratio=1.5)
    tracker.compute(make_frame(256, 256, 0))
    rects = tracker.compute(make_frame(256, 256, 255))
    assert rects == [(0, 0, 256, 256)]  # 全屏合并成单个矩形,而不是 16 个瓦片


def test_size_change_triggers_keyframe():
    tracker = DirtyTracker()
    tracker.compute(make_frame(256, 256, 0))
    assert tracker.compute(make_frame(128, 256, 0)) is None
    # 尺寸变化后新的尺寸成为基准
    assert tracker.compute(make_frame(128, 256, 0)) == []


def test_size_change_within_same_padded_size():
    """100x100 -> 70x70(瓦片 64 时都补齐到 128x128):不能被上一帧残留在
    补齐区域的像素污染,否则会生成落在画面之外的矩形。"""
    tracker = DirtyTracker()
    assert tracker.compute(make_frame(100, 100, 200)) is None
    assert tracker.compute(make_frame(70, 70, 5)) is None  # 尺寸变了 -> 关键帧
    rects = tracker.compute(make_frame(70, 70, 5))
    assert rects == []  # 残留像素不得产生假脏瓦片

    changed = make_frame(70, 70, 5)
    changed[65:70, 65:70] = 9
    rects = tracker.compute(changed)
    assert rects is not None
    for x, y, w, h in rects:
        assert w > 0 and h > 0 and x + w <= 70 and y + h <= 70


def test_reset_forces_keyframe():
    tracker = DirtyTracker()
    frame = make_frame(128, 128, 3)
    tracker.compute(frame)
    assert tracker.compute(frame.copy()) == []
    tracker.reset()
    assert tracker.compute(frame.copy()) is None
    assert tracker.compute(frame.copy()) == []


def test_too_many_rects_triggers_keyframe():
    """棋盘式散点变化:合并后矩形数超过协议上限 -> 退化为关键帧。"""
    ts = 8
    # 用 8px 瓦片构造一张足够大的图,让"隔一个瓦片改一个"的散点数超过 512,
    # 同时保持脏瓦片占比约 25%(低于默认 0.6,确保是被矩形数上限拦下的)。
    n = 48  # 48x48 = 2304 个瓦片,其中约 576 个脏瓦片
    size = n * ts
    tracker = DirtyTracker(tile_size=ts)
    base = make_frame(size, size, 0)
    tracker.compute(base)

    changed = base.copy()
    for r in range(0, n, 2):
        for c in range(0, n, 2):
            changed[r * ts, c * ts] = 255
    dirty_tiles = len(range(0, n, 2)) ** 2
    assert dirty_tiles / (n * n) < 0.6  # 确认没被占比阈值拦下
    assert dirty_tiles > MAX_RECTS_PER_FRAME
    assert tracker.compute(changed) is None


# ----------------------------------------------------------------------
# 边界与合并效果
# ----------------------------------------------------------------------
def test_non_divisible_size_rects_stay_in_bounds():
    """100x70 的图配 64 的瓦片:最后一行/列瓦片更小,矩形不得越界。"""
    h, w = 70, 100
    tracker = DirtyTracker(tile_size=64, keyframe_ratio=1.5)
    base = make_frame(h, w, 0)
    tracker.compute(base)

    changed = base.copy()
    changed[65:70, 95:100] = 255  # 改右下角(处于"半个瓦片"里)
    rects = tracker.compute(changed)

    assert rects is not None and rects
    for x, y, rw, rh in rects:
        assert x >= 0 and y >= 0
        assert rw > 0 and rh > 0
        assert x + rw <= w, f"矩形 {(x, y, rw, rh)} 越过右边界 {w}"
        assert y + rh <= h, f"矩形 {(x, y, rw, rh)} 越过下边界 {h}"
    assert rect_covers(rects, 95, 65, 5, 5)


def test_non_divisible_size_edge_only_change():
    """只改最后一列的一个像素也要能检出,且矩形贴边不越界。"""
    h, w = 70, 100
    tracker = DirtyTracker(tile_size=64, keyframe_ratio=1.5)
    base = make_frame(h, w, 0)
    tracker.compute(base)
    changed = base.copy()
    changed[69, 99] = 128
    rects = tracker.compute(changed)
    assert rects is not None
    assert rect_covers(rects, 99, 69)
    for x, y, rw, rh in rects:
        assert x + rw <= w and y + rh <= h


def test_horizontal_run_is_merged_into_few_rects():
    """一整行瓦片连续变化应合并成 1 个横条,而不是每瓦片一个矩形。"""
    tracker = DirtyTracker()
    h, w = 256, 640  # 4 行 x 10 列瓦片
    base = make_frame(h, w, 0)
    tracker.compute(base)

    changed = base.copy()
    changed[70:80, :] = 255  # 横跨整幅宽度的一条
    rects = tracker.compute(changed)

    assert rects == [(0, 64, 640, 64)]  # 10 个脏瓦片合并成 1 个矩形


def test_vertical_runs_are_merged():
    """上下相邻且左右边界一致的横条应纵向合并成一个矩形。"""
    tracker = DirtyTracker()
    h, w = 512, 512
    base = make_frame(h, w, 0)
    tracker.compute(base)

    changed = base.copy()
    changed[64:192, 64:128] = 255  # 覆盖 2 行 x 1 列瓦片
    rects = tracker.compute(changed)

    assert rects == [(64, 64, 64, 128)]


def test_l_shape_is_not_over_merged():
    """L 形变化无法合成一个矩形,但也不应退化成逐瓦片:期望 2 个矩形。"""
    tracker = DirtyTracker()
    h, w = 256, 256
    base = make_frame(h, w, 0)
    tracker.compute(base)

    changed = base.copy()
    changed[0:64, 0:256] = 255   # 第 0 行整行(4 个瓦片)
    changed[64:128, 0:64] = 255  # 第 1 行第 0 列(1 个瓦片)
    rects = tracker.compute(changed)

    assert rects is not None
    assert len(rects) == 2
    covered = covered_mask(rects, h, w)
    changed_mask = np.any(changed != base, axis=2)
    assert covered[changed_mask].all()  # 所有变化像素都被覆盖


def test_all_changed_pixels_are_covered_random_case():
    """随机散布若干小块,断言返回矩形覆盖了全部变化像素。"""
    rng = np.random.default_rng(1234)
    h, w = 480, 640
    tracker = DirtyTracker()
    base = make_frame(h, w, 0)
    tracker.compute(base)

    changed = base.copy()
    for _ in range(8):
        y = int(rng.integers(0, h - 20))
        x = int(rng.integers(0, w - 20))
        changed[y:y + 20, x:x + 20] = 255
    rects = tracker.compute(changed)

    assert rects is not None
    covered = covered_mask(rects, h, w)
    changed_mask = np.any(changed != base, axis=2)
    assert covered[changed_mask].all()
    assert len(rects) <= 8 * 4  # 每块最多跨 2x2 个瓦片


def test_custom_tile_size_gives_tighter_rects():
    tracker = DirtyTracker(tile_size=16)
    base = make_frame(128, 128, 0)
    tracker.compute(base)
    changed = base.copy()
    changed[20:24, 20:24] = 255
    rects = tracker.compute(changed)
    assert rects == [(16, 16, 16, 16)]


def test_rects_are_sorted_by_scanline_order():
    tracker = DirtyTracker()
    base = make_frame(256, 256, 0)
    tracker.compute(base)
    changed = base.copy()
    changed[200, 10] = 255
    changed[10, 200] = 255
    rects = tracker.compute(changed)
    assert rects is not None and len(rects) == 2
    assert rects == sorted(rects, key=lambda r: (r[1], r[0]))


# ----------------------------------------------------------------------
# 参数校验
# ----------------------------------------------------------------------
def test_invalid_tile_size_rejected():
    with pytest.raises(ValueError):
        DirtyTracker(tile_size=0)


def test_invalid_frame_shape_rejected():
    tracker = DirtyTracker()
    with pytest.raises(ValueError):
        tracker.compute(np.zeros((10, 10), dtype=np.uint8))
    with pytest.raises(ValueError):
        tracker.compute(np.zeros((10, 10, 4), dtype=np.uint8))
    with pytest.raises(ValueError):
        tracker.compute(np.zeros((10, 10, 3), dtype=np.float32))


def test_default_tile_size_constant():
    assert DEFAULT_TILE_SIZE == 64
    assert DirtyTracker().tile_size == 64
