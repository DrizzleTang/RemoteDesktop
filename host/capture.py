"""屏幕采集与 JPEG 编码。

设计要点:

1. 线程亲和性:mss 的底层实现要求同一个 mss 实例只能在创建它的线程里使用,
   因此 _ensure_thread_local_sct() 会在每次调用时检查当前线程,必要时重建。
   host/server.py 通过一个专属的单线程 ThreadPoolExecutor 反复调用同一个
   实例,既避免每帧重新初始化 mss 的开销,也天然满足这个要求。

2. 采集与编码解耦:grab() 只负责"截屏 + 缩放",返回 numpy RGB 数组;
   编码由 encode_jpeg()/encode_region() 单独完成。这样 host 侧可以先用
   host/delta.py 做脏矩形检测,画面静止时一次编码都不做,只有变化的小块
   才走 encode_region()——这是弱网下省带宽/省 CPU 的关键。

3. 像素格式转换用 numpy 而不是 mss 的 shot.rgb:后者在 Python 层逐字节
   重排 BGRA->RGB,是老实现里最大的单点开销;numpy 的切片+反转由 C 层
   一次性完成。
"""
from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

from common.protocol import FMT_JPEG, FMT_WEBP

# JPEG 质量的合法区间:低于 5 画面已经不可用,高于 95 体积暴涨而肉眼无差别。
MIN_JPEG_QUALITY = 5
MAX_JPEG_QUALITY = 95
# 缩放比例下限,防止上层传入 0 或负数导致输出尺寸退化为 0。
MIN_SCALE = 0.1


@dataclass
class MonitorInfo:
    """一个可采集的显示器。index 0 是 mss 约定的"所有显示器拼成的虚拟大屏"。"""

    index: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool
    label: str  # 给用户看的中文名字,直接显示在客户端的显示器下拉框里


@dataclass
class CapturedFrame:
    """一次采集的结果。rgb 已经缩放到输出分辨率,可直接喂给 DirtyTracker。"""

    rgb: np.ndarray  # (H, W, 3) uint8
    width: int
    height: int
    capture_ms: float  # 采集+缩放耗时(毫秒),用于 stats 上报与自适应决策


def _make_label(index: int, width: int, height: int) -> str:
    """生成显示器的中文标签。"""
    if index == 0:
        return f"全部显示器 ({width}x{height})"
    return f"显示器 {index} ({width}x{height})"


class ScreenCapture:
    """屏幕采集器。同一实例只应在单一线程内反复调用(见模块头部说明)。"""

    def __init__(self, monitor_index: int = 1) -> None:
        self._monitor_index = int(monitor_index)
        self._sct = None
        self._owner_thread: threading.Thread | None = None
        self._monitor: dict | None = None

    # ------------------------------------------------------------------
    # mss 实例管理
    # ------------------------------------------------------------------
    def _ensure_thread_local_sct(self) -> None:
        """保证 self._sct 属于当前线程;线程变了就重建。"""
        current = threading.current_thread()
        if self._sct is not None and self._owner_thread is current:
            return
        # 延迟导入:无显示环境下 import mss 可能失败,而其他模块(包括测试)
        # 需要能安全地 import 本模块。
        import mss

        if self._sct is not None:
            # 换线程了:旧实例属于别的线程,尽力关闭,失败也不影响后续采集。
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                pass
        # mss >= 10.2 用 mss.MSS,旧版本(requirements 允许 >=9.0)只有工厂函数
        # mss.mss();后者在新版里会打 DeprecationWarning,所以优先取前者。
        factory = getattr(mss, "MSS", None) or mss.mss
        self._sct = factory()
        self._owner_thread = current
        self._resolve_monitor()

    def _resolve_monitor(self) -> None:
        """把 self._monitor_index 解析成 mss 的 monitor 字典;越界时回落到 0(全屏)。"""
        monitors = self._sct.monitors
        idx = self._monitor_index if 0 <= self._monitor_index < len(monitors) else 0
        self._monitor_index = idx
        self._monitor = monitors[idx]

    # ------------------------------------------------------------------
    # 显示器信息与切换
    # ------------------------------------------------------------------
    def list_monitors(self) -> list[MonitorInfo]:
        """列出所有可用显示器。

        复用线程本地的 mss 实例(老实现在这里额外 `with mss.mss()` 开了一个
        临时实例,既浪费又可能在非采集线程上初始化 X 连接,已修正)。
        """
        self._ensure_thread_local_sct()
        infos: list[MonitorInfo] = []
        for i, m in enumerate(self._sct.monitors):
            # mss 约定:monitors[0] 是虚拟大屏,monitors[1] 是主显示器。
            infos.append(MonitorInfo(
                index=i, left=m["left"], top=m["top"],
                width=m["width"], height=m["height"],
                is_primary=(i == 1),
                label=_make_label(i, m["width"], m["height"]),
            ))
        return infos

    def set_monitor(self, index: int) -> bool:
        """运行时切换采集的显示器。索引非法时返回 False 且保持原样,不抛异常。

        注意:切换后画面尺寸通常会变,调用方必须让 DirtyTracker.reset() 并
        重发一帧关键帧。
        """
        self._ensure_thread_local_sct()
        monitors = self._sct.monitors
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if not (0 <= index < len(monitors)):
            return False
        self._monitor_index = index
        self._monitor = monitors[index]
        return True

    @property
    def monitor_index(self) -> int:
        return self._monitor_index

    @property
    def screen_size(self) -> tuple[int, int]:
        """当前显示器的真实像素尺寸(未缩放),用于把归一化鼠标坐标还原成绝对坐标。"""
        self._ensure_thread_local_sct()
        return self._monitor["width"], self._monitor["height"]

    # ------------------------------------------------------------------
    # 采集与编码
    # ------------------------------------------------------------------
    def grab(self, *, scale: float) -> CapturedFrame:
        """采集一帧并按 scale (0<scale<=1) 缩放到输出分辨率。阻塞调用。"""
        t0 = time.perf_counter()
        self._ensure_thread_local_sct()
        shot = self._sct.grab(self._monitor)

        # mss 的 raw 是 BGRA 字节流;用 numpy 视图取前 3 通道再反转即得 RGB,
        # 全程只有一次真正的拷贝(下面的 ascontiguousarray / resize)。
        raw = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        rgb_view = raw[:, :, :3][:, :, ::-1]

        full_h, full_w = rgb_view.shape[0], rgb_view.shape[1]
        scale = max(MIN_SCALE, min(1.0, float(scale)))
        if scale < 0.999:
            out_w = max(1, round(full_w * scale))
            out_h = max(1, round(full_h * scale))
            img = Image.fromarray(np.ascontiguousarray(rgb_view))
            rgb = np.asarray(img.resize((out_w, out_h), Image.BILINEAR))
        else:
            out_w, out_h = full_w, full_h
            # ascontiguousarray 会把负步长的视图物化成独立的连续数组——这一步
            # 不能省:shot.raw 的缓冲区会被下一次 grab 覆盖,而 DirtyTracker
            # 需要跨帧持有数据。
            rgb = np.ascontiguousarray(rgb_view)

        capture_ms = (time.perf_counter() - t0) * 1000.0
        return CapturedFrame(rgb=rgb, width=out_w, height=out_h, capture_ms=capture_ms)

    def grab_full(self) -> CapturedFrame:
        """采集一帧原始分辨率画面(不缩放)。

        共享采集管线用这个:差分在原始分辨率上做,缩放推迟到"确实要发送"
        的时候再按需进行,画面静止时可以完全省掉缩放开销。
        """
        return self.grab(scale=1.0)

    def rescale(self, rgb: np.ndarray, scale: float) -> tuple[np.ndarray, int, int]:
        """把原始画面缩放到目标比例,返回 (数组, 宽, 高)。scale>=1 时原样返回。"""
        full_h, full_w = rgb.shape[0], rgb.shape[1]
        scale = max(MIN_SCALE, min(1.0, float(scale)))
        if scale >= 0.999:
            return rgb, full_w, full_h
        out_w = max(1, round(full_w * scale))
        out_h = max(1, round(full_h * scale))
        img = Image.fromarray(np.ascontiguousarray(rgb)).resize((out_w, out_h), Image.BILINEAR)
        return np.asarray(img), out_w, out_h

    def encode_image(self, rgb: np.ndarray, quality: int, fmt: int = FMT_JPEG) -> bytes:
        """把 (H, W, 3) 的 RGB 数组编码成 JPEG 或 WebP 字节。

        WebP 一律用 method=0(最快档)。默认档的压缩率只好一点点,编码耗时
        却是 4 倍以上(实测整屏 1920x1080:默认档 135ms vs 最快档 29ms),
        对实时推流完全不可接受。
        """
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError(f"rgb 必须是 (H, W, 3) 的数组,实际为 {rgb.shape}")
        img = Image.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8))
        buf = io.BytesIO()
        if fmt == FMT_WEBP:
            img.save(buf, format="WEBP", quality=_clamp_quality(quality), method=0)
        else:
            img.save(buf, format="JPEG", quality=_clamp_quality(quality))
        return buf.getvalue()

    def encode_jpeg(self, rgb: np.ndarray, quality: int) -> bytes:
        """[兼容别名] 等价于 encode_image(..., fmt=FMT_JPEG)。"""
        return self.encode_image(rgb, quality, FMT_JPEG)

    def encode_region(self, rgb: np.ndarray, rect: tuple[int, int, int, int],
                      quality: int, fmt: int = FMT_JPEG) -> bytes:
        """裁剪出 rect=(x, y, w, h) 区域并编码。

        rect 会被裁剪到图像边界内(容错:即使上游给了略微越界的矩形也不会崩),
        但裁剪后为空则视为调用方的 bug,抛 ValueError。
        """
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError(f"rgb 必须是 (H, W, 3) 的数组,实际为 {rgb.shape}")
        x, y, w, h = (int(v) for v in rect)
        img_h, img_w = rgb.shape[0], rgb.shape[1]
        x0 = max(0, min(x, img_w))
        y0 = max(0, min(y, img_h))
        x1 = max(x0, min(x + w, img_w))
        y1 = max(y0, min(y + h, img_h))
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"矩形 {rect} 与图像 {img_w}x{img_h} 无交集")
        return self.encode_image(rgb[y0:y1, x0:x1], quality, fmt)

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001 - 关闭阶段的异常无需向上抛
                pass
            self._sct = None
            self._owner_thread = None


def _clamp_quality(quality: int) -> int:
    return max(MIN_JPEG_QUALITY, min(MAX_JPEG_QUALITY, int(quality)))
