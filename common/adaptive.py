"""
自适应画质控制器(弱网稳定性的核心)。

设计原则 —— "快降慢升"(借鉴 TCP Vegas / BBR 等拥塞控制思想):
- 一旦检测到网络变差(帧确认延迟升高、或帧确认超时/丢帧),立刻大幅降级,
  优先保证鼠标/键盘操作的响应性,而不是画面清晰度。
- 网络恢复后,只有连续多次采样都表现良好,才逐级、缓慢地尝试升级画质,
  避免网络抖动导致画质来回跳变("闪烁")影响体验。
- 帧确认超时(说明上一帧还没到对方就已经严重滞后)比延迟升高更严重,
  会触发更激进的降级,并且短时间内(冷却期)禁止升级,防止刚降级又立刻
  升回去导致的振荡。

本模块不做任何网络 I/O,只是纯逻辑状态机,便于单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.protocol import QUALITY_AUTO, QUALITY_CUSTOM, QUALITY_PRESETS


@dataclass(frozen=True)
class QualityLevel:
    key: str
    label: str  # 中文展示名
    scale: float  # 编码前分辨率缩放比例 (0~1]
    jpeg_quality: int  # JPEG 质量 1-100
    max_fps: float


# 由低到高排列,索引即"档位"。极端弱网下宁可低画质也要保持能操作。
LEVELS: tuple[QualityLevel, ...] = (
    QualityLevel("extreme", "极速", 0.35, 28, 8),
    QualityLevel("smooth", "流畅", 0.5, 42, 12),
    QualityLevel("balanced", "均衡", 0.75, 58, 18),
    QualityLevel("clear", "清晰", 1.0, 75, 24),
    QualityLevel("hd", "高清", 1.0, 90, 30),
)
assert tuple(level.key for level in LEVELS) == QUALITY_PRESETS

# 依据"帧确认延迟"(ms)判断当前网络状况能撑起哪个目标档位。
# (上界, 目标档位索引),从严格到宽松排列。
_LATENCY_BUCKETS: tuple[tuple[float, int], ...] = (
    (80, 4),
    (180, 3),
    (350, 2),
    (700, 1),
    (float("inf"), 0),
)

EWMA_ALPHA = 0.3
UPGRADE_STREAK_REQUIRED = 5  # 连续多少次"良好"采样才允许升一档
COOLDOWN_AFTER_TIMEOUT = 8  # 超时后,至少再等这么多次采样才允许升级
DOWNGRADE_STREAK_FOR_EXTRA_DROP = 3  # 连续多少次都指向"更低两档以上"时额外多降一档
SATURATION_RATIO = 0.75  # 当前吞吐达到近期峰值的这个比例即视为"带宽已打满"
PEAK_DECAY = 0.97  # 吞吐峰值每次采样的衰减系数,让峰值估计能跟随网络变化


def _latency_to_level(latency_ms: float) -> int:
    for upper, level_idx in _LATENCY_BUCKETS:
        if latency_ms <= upper:
            return level_idx
    return 0  # pragma: no cover - 理论不可达,_LATENCY_BUCKETS 以 inf 兜底


@dataclass
class AdaptiveController:
    mode: str = QUALITY_AUTO  # "auto" | 手动档位 key(见 QUALITY_PRESETS) | "custom"
    custom_scale: float = 0.75
    custom_jpeg_quality: int = 60
    custom_max_fps: float = 20.0

    _level_idx: int = field(default=2, init=False)  # 智能模式起始档位:均衡
    _ewma_latency_ms: float | None = field(default=None, init=False)
    _upgrade_streak: int = field(default=0, init=False)
    _cooldown: int = field(default=0, init=False)
    _low_target_streak: int = field(default=0, init=False)
    last_rtt_ms: float | None = field(default=None, init=False)
    consecutive_timeouts: int = field(default=0, init=False)
    # 带宽观测:用于区分"链路本身就慢"和"带宽被自己打满"这两种延迟升高
    _recent_kbps: float | None = field(default=None, init=False)
    _peak_kbps: float | None = field(default=None, init=False)

    def set_mode(self, mode: str, *, custom_scale: float | None = None,
                 custom_jpeg_quality: int | None = None, custom_max_fps: float | None = None) -> None:
        if mode != QUALITY_AUTO and mode != QUALITY_CUSTOM and mode not in QUALITY_PRESETS:
            raise ValueError(f"unknown quality mode: {mode}")
        self.mode = mode
        if mode == QUALITY_CUSTOM:
            if custom_scale is not None:
                self.custom_scale = max(0.1, min(1.0, custom_scale))
            if custom_jpeg_quality is not None:
                self.custom_jpeg_quality = max(5, min(95, int(custom_jpeg_quality)))
            if custom_max_fps is not None:
                self.custom_max_fps = max(1.0, min(60.0, custom_max_fps))

    def on_frame_ack(self, ack_latency_ms: float) -> None:
        """收到某一帧的 ack,记录其"发送到确认"的往返延迟,驱动智能模式调档。"""
        self.consecutive_timeouts = 0
        if self._ewma_latency_ms is None:
            self._ewma_latency_ms = ack_latency_ms
        else:
            self._ewma_latency_ms = (
                EWMA_ALPHA * ack_latency_ms + (1 - EWMA_ALPHA) * self._ewma_latency_ms
            )
        if self.mode != QUALITY_AUTO:
            return
        target = _latency_to_level(self._ewma_latency_ms)
        if target < self._level_idx:
            # 变差:立刻降级(每次最多降到 target,若连续多次都指向"低两档以上"则额外再多降一档)
            if self._level_idx - target >= 2:
                self._low_target_streak += 1
            else:
                self._low_target_streak = 0
            drop_extra = 1 if self._low_target_streak >= DOWNGRADE_STREAK_FOR_EXTRA_DROP else 0
            new_idx = max(0, target - drop_extra)
            if self.is_bandwidth_saturated() is False:
                # 带宽还很充裕,延迟高是链路本身的问题(跨国/卫星/移动网络)。
                # 这种情况下狂降画质对延迟毫无帮助,只会让画面白白变糊,
                # 因此每次最多降一档,慢慢试探。
                new_idx = max(new_idx, self._level_idx - 1)
            self._level_idx = new_idx
            self._upgrade_streak = 0
            self._cooldown = max(self._cooldown, 2)
        elif target > self._level_idx:
            self._low_target_streak = 0
            if self._cooldown > 0:
                self._cooldown -= 1
                self._upgrade_streak = 0
            else:
                self._upgrade_streak += 1
                if self._upgrade_streak >= UPGRADE_STREAK_REQUIRED:
                    self._level_idx = min(len(LEVELS) - 1, self._level_idx + 1)
                    self._upgrade_streak = 0
        else:
            self._low_target_streak = 0
            if self._cooldown > 0:
                self._cooldown -= 1
            self._upgrade_streak = 0

    def on_frame_timeout(self) -> None:
        """一帧长时间未收到 ack(严重拥塞/丢包信号),比普通延迟升高更激进地降级。"""
        self.consecutive_timeouts += 1
        if self.mode != QUALITY_AUTO:
            return
        drop = 1 if self.consecutive_timeouts == 1 else 2
        self._level_idx = max(0, self._level_idx - drop)
        self._upgrade_streak = 0
        self._low_target_streak = 0
        self._cooldown = COOLDOWN_AFTER_TIMEOUT

    def on_throughput_sample(self, kbps: float) -> None:
        """记录一次实际吞吐观测(由推流器每秒上报一次)。

        为什么需要这个:只看延迟无法区分两种截然不同的情况——
        (a) **带宽被打满**:我们自己发得太多,数据在链路缓冲里排队,延迟升高。
            此时降低画质能直接减少字节数,延迟会立刻改善。
        (b) **链路本身延迟高**(跨国、卫星、移动网络),但带宽其实很充裕。
            此时降低画质对延迟几乎没有帮助,只是白白牺牲清晰度。
        判据是把当前吞吐和"近期观测到的吞吐峰值"比较:接近峰值说明已经
        撑满了链路(情况 a),远低于峰值说明瓶颈不在带宽(情况 b)。
        """
        if kbps <= 0:
            return
        self._recent_kbps = kbps
        if self._peak_kbps is None:
            self._peak_kbps = kbps
        else:
            # 峰值缓慢衰减,避免历史上某次突发把峰值永久抬高,导致之后
            # 永远判定为"未饱和"
            self._peak_kbps = max(kbps, self._peak_kbps * PEAK_DECAY)

    def is_bandwidth_saturated(self) -> bool | None:
        """True=带宽已打满,False=链路本身慢,None=数据不足无法判断。"""
        if self._recent_kbps is None or self._peak_kbps is None or self._peak_kbps <= 0:
            return None
        return (self._recent_kbps / self._peak_kbps) >= SATURATION_RATIO

    def on_ping_rtt(self, rtt_ms: float) -> None:
        """独立心跳 RTT,仅用于展示与统计,不直接驱动调档(避免与 frame_ack 信号叠加振荡)。"""
        self.last_rtt_ms = rtt_ms

    @property
    def ewma_latency_ms(self) -> float | None:
        return self._ewma_latency_ms

    def current_level(self) -> QualityLevel:
        return LEVELS[self._level_idx]

    def current_params(self) -> dict:
        if self.mode == QUALITY_CUSTOM:
            return {
                "mode": QUALITY_CUSTOM, "level_label": "自定义",
                "scale": self.custom_scale, "jpeg_quality": self.custom_jpeg_quality,
                "max_fps": self.custom_max_fps,
            }
        if self.mode != QUALITY_AUTO:
            level = next(l for l in LEVELS if l.key == self.mode)
            return {
                "mode": self.mode, "level_label": level.label,
                "scale": level.scale, "jpeg_quality": level.jpeg_quality,
                "max_fps": level.max_fps,
            }
        level = self.current_level()
        return {
            "mode": QUALITY_AUTO, "level_label": f"智能({level.label})",
            "scale": level.scale, "jpeg_quality": level.jpeg_quality,
            "max_fps": level.max_fps,
        }

    def stats_snapshot(self) -> dict:
        params = self.current_params()
        saturated = self.is_bandwidth_saturated()
        params.update({
            "ewma_ack_latency_ms": round(self._ewma_latency_ms, 1) if self._ewma_latency_ms else None,
            "rtt_ms": round(self.last_rtt_ms, 1) if self.last_rtt_ms is not None else None,
            "peak_kbps": round(self._peak_kbps, 1) if self._peak_kbps is not None else None,
            "bw_saturated": saturated,
        })
        return params
