"""脏矩形(dirty rect)检测。

远程桌面的真实使用场景里,绝大多数时间屏幕只有一小块区域在变化(光标、
正在输入的文本框、滚动中的窗口)。整屏 JPEG 重编码既浪费带宽也浪费 CPU,
在弱网环境下尤其致命。本模块负责回答一个问题:

    "这一帧相比上一帧,哪些区域变了?"

做法是把画面切成固定大小的瓦片(默认 64x64),逐瓦片与上一帧比较得到一张
布尔掩码,再把相邻的脏瓦片合并成较大的矩形——矩形越少,JPEG 编码次数、
协议头部开销和客户端的解码/绘制次数就越少。

本模块是纯 numpy 逻辑,不依赖任何显示环境,便于单元测试与在无显示器的 CI
上运行。真正的采集/编码在 host/capture.py。
"""
from __future__ import annotations

import numpy as np

from common.protocol import MAX_RECTS_PER_FRAME  # 协议规定的单帧矩形数上限(只读引用)

# 默认瓦片边长。64 是带宽与检测精度之间的经验折中:
# 太小 -> 瓦片数量暴涨,合并开销与矩形数上升;太大 -> 一个光标的移动就要重传一大块。
DEFAULT_TILE_SIZE = 64

# 脏瓦片占比超过该阈值时直接改发关键帧:大面积变化时,整帧编码一次
# 通常比编码几十个矩形更划算(JPEG 每次编码都有固定头部/量化表开销,
# 且矩形多了协议开销与客户端解码次数都会显著上升)。
DEFAULT_KEYFRAME_RATIO = 0.6


class DirtyTracker:
    """逐帧比较、输出脏矩形列表的状态机。

    典型用法(host 侧采集线程):

        tracker = DirtyTracker()
        rects = tracker.compute(frame.rgb)
        if rects is None:      # 首帧 / 分辨率变化 / 变化面积过大 -> 发关键帧
            send_keyframe(...)
        elif not rects:        # 画面完全静止 -> 一个字节都不用发
            pass
        else:
            send_delta([...])  # 只编码这些矩形

    实例不是线程安全的,应当只在采集线程内使用(与 ScreenCapture 一致)。
    """

    def __init__(self, tile_size: int = DEFAULT_TILE_SIZE,
                 keyframe_ratio: float = DEFAULT_KEYFRAME_RATIO) -> None:
        if tile_size < 1:
            raise ValueError("tile_size 必须 >= 1")
        self.tile_size = int(tile_size)
        self.keyframe_ratio = float(keyframe_ratio)
        # 上一帧的真实尺寸 (h, w),为 None 表示还没有参考帧。
        self._prev_shape: tuple[int, int] | None = None
        # 复用的工作缓冲区,尺寸是"补齐到瓦片整数倍"之后的画面大小。
        # 每帧重新 allocate 这几个大数组(1080p 下每个 6MB)会带来明显的
        # 分配 + 缺页开销,实测占到 compute 总耗时的近一半,所以这里预分配并
        # 在两帧之间轮换(cur/prev 互换),稳态下零分配。
        self._cur_buf: np.ndarray | None = None
        self._prev_buf: np.ndarray | None = None
        self._diff_buf: np.ndarray | None = None
        self._buf_shape: tuple[int, int, int] | None = None

    def reset(self) -> None:
        """丢弃上一帧状态。下一次 compute 必然返回 None(强制关键帧)。

        调用方在以下场景需要 reset:切换显示器、切换输出分辨率/缩放比例、
        客户端重连或主动请求关键帧、连续丢包后需要重新同步画面。
        """
        self._prev_shape = None

    def compute(self, frame: np.ndarray) -> list[tuple[int, int, int, int]] | None:
        """比较 frame 与上一帧,返回脏矩形列表。

        frame: (H, W, 3) uint8 的 RGB 数组,必须是"最终输出分辨率"下的图像
               (即已经按 scale 缩放过),这样返回的坐标可以直接用于协议里的
               矩形头部,客户端无需做任何换算。

        返回:
          None -> 必须发关键帧(首帧 / 尺寸变化 / 变化面积超过 keyframe_ratio /
                  合并后矩形数仍超过 MAX_RECTS_PER_FRAME)
          []   -> 与上一帧逐像素完全相同,无需发送任何数据
          [(x, y, w, h), ...] -> 变化区域,坐标位于 frame 坐标系,保证不越界
        """
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(f"frame 必须是 (H, W, 3) 的数组,实际为 {frame.shape}")
        if frame.dtype != np.uint8:
            # 拷进 uint8 缓冲区会静默截断,与其产生诡异的"检测不到变化",不如直接报错
            raise ValueError(f"frame 必须是 uint8,实际为 {frame.dtype}")
        h, w = int(frame.shape[0]), int(frame.shape[1])
        if h == 0 or w == 0:
            raise ValueError("frame 尺寸不能为 0")

        ts = self.tile_size
        rows = (h + ts - 1) // ts
        cols = (w + ts - 1) // ts

        # 把本帧拷进补齐到瓦片整数倍的工作缓冲区。补齐区域(右侧/下方)始终
        # 保持初始化时的 0 且两个缓冲区都不写它,所以两帧在那里永远相等,
        # 不会产生假的脏瓦片。这次拷贝同时让我们与调用方的缓冲区解耦——mss 的
        # raw buffer 会被下一次 grab 覆盖,不能直接持有。
        had_prev = self._ensure_buffers(h, w, rows * ts, cols * ts)
        self._cur_buf[:h, :w] = frame
        self._prev_shape = (h, w)

        if not had_prev:
            self._swap_buffers()
            return None  # 首帧或分辨率变化:没有可比较的基准,必须发关键帧

        # ---- 向量化的逐瓦片比较 ----
        # 先把逐字节比较结果写进预分配的 bool 缓冲区(out= 避免每帧再分配一个
        # 与画面等大的临时数组),再 reshape 成
        # (瓦片行, 瓦片内 y, 瓦片列, 瓦片内 x*通道) 并沿轴 1、3 归约,
        # 得到 (行, 列) 的脏瓦片掩码。
        # 这里刻意不做 transpose:转置会让内存访问变成跨步访问甚至触发一次整帧
        # 拷贝,而 reshape 只是改视图,归约仍是顺序扫描,快得多。
        np.not_equal(self._cur_buf, self._prev_buf, out=self._diff_buf)
        dirty = self._diff_buf.reshape(rows, ts, cols, ts * 3).any(axis=(1, 3))
        self._swap_buffers()

        dirty_count = int(np.count_nonzero(dirty))
        if dirty_count == 0:
            return []  # 画面完全静止

        total_tiles = rows * cols
        if dirty_count > self.keyframe_ratio * total_tiles:
            return None  # 变化面积过大,整帧编码更划算

        rects = self._merge(dirty, ts, w, h)
        if len(rects) > MAX_RECTS_PER_FRAME:
            # 合并后仍然太碎(例如满屏噪点),协议装不下,退化为关键帧。
            return None
        return rects

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _ensure_buffers(self, h: int, w: int, padded_h: int, padded_w: int) -> bool:
        """保证工作缓冲区可用,返回"上一帧是否可以作为比较基准"。"""
        shape = (padded_h, padded_w, 3)
        if self._buf_shape != shape:
            # 首次调用或补齐后的尺寸变了:重新分配。补齐区域靠 zeros 初始化为 0,
            # 此后永远不写它,于是两帧在补齐区域恒等。
            self._cur_buf = np.zeros(shape, dtype=np.uint8)
            self._prev_buf = np.zeros(shape, dtype=np.uint8)
            self._diff_buf = np.empty(shape, dtype=bool)
            self._buf_shape = shape
            return False
        if self._prev_shape != (h, w):
            # 补齐后尺寸相同但画面尺寸变了(例如 100x100 -> 70x70,瓦片 64 时
            # 都补齐到 128x128):清零,否则上一帧残留在补齐区域的像素会被当成
            # 脏瓦片,进而生成落在画面之外的矩形。
            self._cur_buf[:] = 0
            self._prev_buf[:] = 0
            return False
        return True

    def _swap_buffers(self) -> None:
        """本帧变成下一次比较的参考帧;下一帧写进现在这块 prev,稳态零分配。"""
        self._cur_buf, self._prev_buf = self._prev_buf, self._cur_buf

    @staticmethod
    def _merge(dirty: np.ndarray, tile_size: int, width: int,
               height: int) -> list[tuple[int, int, int, int]]:
        """把 (行, 列) 的脏瓦片掩码合并成尽量少的像素矩形。

        两步策略(不追求最优解,但足以把"整屏变化"从数百个瓦片压成 1 个矩形):
          1. 水平游程合并:同一瓦片行内连续的脏瓦片合成一个横条;
          2. 垂直合并:上下相邻、且左右边界完全相同的横条再纵向合成一个矩形。
        """
        rows, cols = dirty.shape
        rects: list[tuple[int, int, int, int]] = []
        # 尚未收尾的矩形:key 是 (起始列, 结束列),value 是 [起始行, 结束行]
        open_rects: dict[tuple[int, int], list[int]] = {}

        for r in range(rows):
            row_mask = dirty[r]
            spans = _row_spans(row_mask)
            still_open: dict[tuple[int, int], list[int]] = {}
            for span in spans:
                prev = open_rects.pop(span, None)
                if prev is not None:
                    prev[1] = r  # 与上一行边界一致,直接向下延伸
                    still_open[span] = prev
                else:
                    still_open[span] = [r, r]
            # 本行没有接续的横条到此为止,转成最终矩形
            for (c0, c1), (r0, r1) in open_rects.items():
                rects.append(_to_pixel_rect(c0, c1, r0, r1, tile_size, width, height))
            open_rects = still_open

        for (c0, c1), (r0, r1) in open_rects.items():
            rects.append(_to_pixel_rect(c0, c1, r0, r1, tile_size, width, height))

        # 按 (y, x) 排序,保证输出顺序稳定(便于测试与客户端按扫描线顺序绘制)
        rects.sort(key=lambda rect: (rect[1], rect[0]))
        return rects


def _row_spans(row_mask: np.ndarray) -> list[tuple[int, int]]:
    """求一行掩码里所有连续 True 的区间,返回 [(起始列, 结束列(含)), ...]。

    用 diff + flatnonzero 向量化实现;瓦片行数很少(1080p/64 只有 17 行),
    所以外层按行的 Python 循环不构成瓶颈。
    """
    if not row_mask.any():
        return []
    # 首尾各垫一个 False,这样每段的起点对应 +1、终点对应 -1
    padded = np.empty(row_mask.size + 2, dtype=np.int8)
    padded[0] = 0
    padded[-1] = 0
    padded[1:-1] = row_mask
    d = np.diff(padded)
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))


def _to_pixel_rect(c0: int, c1: int, r0: int, r1: int, tile_size: int,
                   width: int, height: int) -> tuple[int, int, int, int]:
    """瓦片坐标 -> 像素矩形 (x, y, w, h),并裁剪到图像边界内。

    尺寸不能被 tile_size 整除时,最后一行/列的瓦片实际更小,这里靠 min(...)
    截断,确保返回的矩形永远不越界(越界会让 encode_region 裁出错误的区域)。
    """
    x = c0 * tile_size
    y = r0 * tile_size
    x_end = min((c1 + 1) * tile_size, width)
    y_end = min((r1 + 1) * tile_size, height)
    return (x, y, x_end - x, y_end - y)
